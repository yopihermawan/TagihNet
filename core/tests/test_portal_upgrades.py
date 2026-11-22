import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import OperationalError, connections
from django.test import Client, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse

from core.models import AuditEvent, Customer, Invoice, Membership, NetworkJob, Plan, Tenant, Ticket
from core.portal_upgrades import REVIEW_SECONDS, submit_request
from .auth_helpers import verified_login


class PortalUpgradeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(name="ISP A")
        other = Tenant.objects.create(name="ISP B")
        cls.current = Plan.objects.create(tenant=cls.tenant, name="Dasar", speed_mbps=15, price=150000)
        cls.faster = Plan.objects.create(tenant=cls.tenant, name="Lebih Cepat", speed_mbps=50, price=250000)
        cls.equal = Plan.objects.create(tenant=cls.tenant, name="Setara", speed_mbps=15, price=200000)
        cls.archived = Plan.objects.create(tenant=cls.tenant, name="Arsip", speed_mbps=100, price=300000, is_active=False)
        cls.foreign = Plan.objects.create(tenant=other, name="Katalog Luar", speed_mbps=150, price=450000)
        cls.customer = Customer.objects.create(tenant=cls.tenant, plan=cls.current, code="TN-A", name="Pelanggan A",
                                             username="pelanggan-a", phone="081200000001")
        unrelated = Customer.objects.create(tenant=other, plan=cls.foreign, code="TN-B", name="Pelanggan B",
                                           username="pelanggan-b", phone="081200000002")
        users = get_user_model().objects
        cls.user = users.create_user(username="portal-a")
        cls.peer = users.create_user(username="portal-a-kedua")
        cls.unrelated = users.create_user(username="portal-b")
        cls.operator = users.create_user(username="operator-a")
        Membership.objects.create(user=cls.user, tenant=cls.tenant, customer=cls.customer, role="customer")
        Membership.objects.create(user=cls.peer, tenant=cls.tenant, customer=cls.customer, role="customer")
        Membership.objects.create(user=cls.unrelated, tenant=other, customer=unrelated, role="customer")
        Membership.objects.create(user=cls.operator, tenant=cls.tenant, role="ops")

    def setUp(self):
        verified_login(self.client, self.user)

    def review(self, plan=None):
        return self.client.get(reverse("portal_upgrade_review", args=[(plan or self.faster).pk]))

    def confirm(self, token, client=None):
        return (client or self.client).post(reverse("portal_upgrade_confirm"), {"quote": token})

    def test_portal_link_lists_only_faster_active_packages_from_the_same_isp(self):
        self.assertContains(self.client.get(reverse("portal")), reverse("portal_upgrade"))
        options = self.client.get(reverse("portal_upgrade"))
        self.assertContains(options, "Lebih Cepat")
        for hidden in ("Setara", "Arsip", "Katalog Luar"):
            self.assertNotContains(options, hidden)
        for plan in (self.current, self.equal, self.archived, self.foreign):
            with self.subTest(plan=plan.name):
                self.assertEqual(self.review(plan).status_code, 404)

    def test_empty_catalogue_and_an_archived_current_plan(self):
        Plan.objects.filter(pk=self.current.pk).update(is_active=False)
        self.assertContains(self.client.get(reverse("portal_upgrade")), "Lebih Cepat")
        Plan.objects.filter(pk=self.faster.pk).update(is_active=False)
        self.assertContains(self.client.get(reverse("portal_upgrade")), "belum ada paket yang lebih cepat")

    def test_review_writes_nothing_and_confirmation_creates_one_scoped_billing_ticket(self):
        review = self.review()
        self.assertContains(review, "150.000")
        self.assertContains(review, "250.000")
        self.assertFalse(Ticket.objects.exists())
        response = self.confirm(review.context["token"])
        ticket = Ticket.objects.get()
        self.assertRedirects(response, reverse("ticket_detail", args=[ticket.pk]))
        self.assertEqual((ticket.customer_id, ticket.created_by_id, ticket.category, ticket.status),
                         (self.customer.pk, self.user.pk, "billing", "open"))
        self.assertIn("Lebih Cepat · 50 Mbps · Rp250.000", ticket.description)
        self.assertEqual(ticket.events.count(), 1)
        self.assertTrue(AuditEvent.objects.filter(tenant=self.tenant, actor=self.user, action="ticket.created").exists())
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.plan_id, self.current.pk)
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(NetworkJob.objects.exists())
        operator = Client()
        verified_login(operator, self.operator)
        self.assertContains(operator.get(reverse("ticket_list")), "Pengajuan upgrade paket")
        outsider = Client()
        verified_login(outsider, self.unrelated)
        self.assertEqual(outsider.get(reverse("ticket_detail", args=[ticket.pk])).status_code, 404)

    def test_replaying_a_confirmation_preserves_the_original_ticket_after_repricing(self):
        token = self.review().context["token"]
        first = self.confirm(token)
        Plan.objects.filter(pk=self.faster.pk).update(price=260000)
        replay = self.confirm(token)
        self.assertEqual(first.headers["Location"], replay.headers["Location"])
        self.assertIn("Rp250.000", Ticket.objects.get().description)
        self.assertEqual(Ticket.objects.get().events.count(), 1)

    def test_quoted_terms_and_availability_must_still_match_at_submission(self):
        for name, value in (("price", 275000), ("version", 2), ("is_active", False)):
            with self.subTest(changed=name):
                token = self.review().context["token"]
                Plan.objects.filter(pk=self.faster.pk).update(**{name: value})
                self.assertContains(self.confirm(token), "Paket atau harga telah berubah", status_code=400)
                Plan.objects.filter(pk=self.faster.pk).update(**{name: getattr(self.faster, name)})
        token = self.review().context["token"]
        Customer.objects.filter(pk=self.customer.pk).update(plan=self.equal)
        self.assertEqual(self.confirm(token).status_code, 400)
        self.assertFalse(Ticket.objects.exists())

    def test_quote_rejects_another_account_even_for_the_same_customer(self):
        token = self.review().context["token"]
        for account in (self.peer, self.unrelated):
            with self.subTest(account=account.username):
                other = Client()
                verified_login(other, account)
                self.assertEqual(self.confirm(token, other).status_code, 403)
        Membership.objects.filter(user=self.user).update(session_version=2)
        verified_login(self.client, self.user)
        self.assertEqual(self.confirm(token).status_code, 403)
        self.assertFalse(Ticket.objects.exists())

    def test_missing_tampered_and_expired_quotes_do_not_create_tickets(self):
        token = self.review().context["token"]
        expired = signing.b62_encode(int(time.time()) - REVIEW_SECONDS - 10)
        with patch.object(signing.TimestampSigner, "timestamp", return_value=expired):
            expired_token = self.review().context["token"]
        for value in ("", token + "modified", expired_token):
            self.assertContains(self.confirm(value), "Tinjauan tidak valid atau sudah kedaluwarsa", status_code=400)
        self.assertFalse(Ticket.objects.exists())

    def test_confirmation_rejects_get_missing_csrf_and_staff(self):
        token = self.review().context["token"]
        self.assertEqual(self.client.get(reverse("portal_upgrade_confirm")).status_code, 405)
        csrf = Client(enforce_csrf_checks=True)
        verified_login(csrf, self.user)
        self.assertEqual(self.confirm(token, csrf).status_code, 403)
        operator = Client()
        verified_login(operator, self.operator)
        self.assertEqual(operator.get(reverse("portal_upgrade")).status_code, 403)
        self.assertEqual(self.confirm(token, operator).status_code, 403)
        self.assertFalse(Ticket.objects.exists())

    def test_transient_failure_preserves_the_same_confirmation_for_retry(self):
        token = self.review().context["token"]
        with patch("core.portal_upgrades.submit_request", side_effect=OperationalError):
            response = self.confirm(token)
        self.assertContains(response, "Coba kirim ulang", status_code=503)
        self.assertEqual(response.context["token"], token)
        self.assertEqual(self.confirm(token).status_code, 302)
        self.assertEqual(Ticket.objects.count(), 1)


class PortalUpgradeConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_confirmations_create_one_ticket_and_one_event(self):
        tenant = Tenant.objects.create(name="ISP uji serentak")
        current = Plan.objects.create(tenant=tenant, name="Dasar", speed_mbps=15, price=150000)
        faster = Plan.objects.create(tenant=tenant, name="Lebih Cepat", speed_mbps=50, price=250000)
        customer = Customer.objects.create(tenant=tenant, plan=current, code="TN-RACE", name="Pelanggan Uji",
                                           username="pelanggan-uji", phone="081200000003")
        actor = get_user_model().objects.create_user(username="portal-serentak")
        Membership.objects.create(user=actor, tenant=tenant, customer=customer, role="customer")
        verified_login(self.client, actor)
        review = self.client.get(reverse("portal_upgrade_review", args=[faster.pk]))
        token = review.context["token"]
        ready = Barrier(2, timeout=10)

        def send():
            try:
                ready.wait()
                return submit_request(actor, token).pk
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = [executor.submit(send) for _ in range(2)]
            self.assertEqual(first.result(timeout=15), second.result(timeout=15))
        self.assertEqual(Ticket.objects.get().events.count(), 1)
