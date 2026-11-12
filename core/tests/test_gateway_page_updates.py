import json
import time
import uuid
from datetime import date, timedelta
from unittest.mock import patch
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from core.billing import issue_invoice
from core.gateway import apply_status, reserve_order
from core.gateway_page_updates import page_snapshot
from core.models import GatewayConfig, GatewayOrder, Membership, MFADevice
from .auth_helpers import verified_login
from .test_payment_gateway import KEY, URL, account_keys, status_payload
from .test_workflow import fixtures


@override_settings(SIMULATION=True)
class GatewayPageUpdateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        cls.invoice, _ = issue_invoice(cls.tenants[0], cls.customers[0].pk, date(2026, 11, 1), cls.owners[0])
        GatewayConfig.objects.create(tenant=cls.tenants[0], merchant_id="G-TEST", enabled=True)

    def setUp(self):
        self.keys = override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=account_keys(self.tenants[0]))
        self.keys.enable()
        self.addCleanup(self.keys.disable)
        self.order = reserve_order(self.owners[0], self.invoice.pk)
        self.url = reverse("gateway_order_snapshot", args=[self.order.pk])
        verified_login(self.client, self.owners[0])

    def test_private_minimal_snapshot_and_cache_headers(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(set(data), {"revision", "status", "label", "watch"})
        self.assertRegex(data["revision"], r"^[a-f0-9]{64}$")
        self.assertEqual(data["status"], "creating")
        self.assertTrue(data["watch"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        for value in [KEY, "G-TEST", self.invoice.customer_name, str(self.order.pk)]:
            self.assertNotIn(value, response.content.decode())

    def test_unauthed_known_and_unknown_order_are_identical_and_uncached(self):
        client = Client()
        responses = [client.get(self.url), client.get(reverse("gateway_order_snapshot", args=[uuid.uuid4()]))]
        for response in responses:
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"error": "authentication required"})
            self.assertIn("no-store", response.headers["Cache-Control"])

    def test_foreign_tenant_and_missing_order_are_not_found(self):
        verified_login(self.client, self.owners[1])
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.get(reverse("gateway_order_snapshot", args=[uuid.uuid4()])).status_code, 404)

    def test_customer_scope_is_rechecked(self):
        user = get_user_model().objects.create_user(username="snapshot-portal")
        member = Membership.objects.create(user=user, tenant=self.tenants[0], role="customer", customer=self.customers[0])
        verified_login(self.client, user)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        # Another customer in this ISP must not gain access through a public UUID.
        from core.models import Customer
        sibling = Customer.objects.create(tenant=self.tenants[0], code="SNAP-SIB", name="Sibling", phone="08000000000",
            username="snapshot-sibling", address="Lab", area="Lab", plan=self.customers[0].plan)
        Membership.objects.filter(pk=member.pk).update(customer=sibling)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        Membership.objects.filter(pk=member.pk).update(customer=self.customers[1])
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_invoice_reader_can_read_without_payment_permission(self):
        Membership.objects.filter(user=self.owners[0]).update(role="ops")
        self.assertEqual(self.client.get(self.url).status_code, 200)
        response = self.client.get(reverse("gateway_order", args=[self.order.pk]))
        self.assertContains(response, "Muat ulang halaman untuk melihat rincian terbaru")
        self.assertNotContains(response, "Gunakan Periksa status pembayaran")
        Membership.objects.filter(user=self.owners[0]).update(role="noc")
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_only_get_is_supported(self):
        for method in ["post", "put", "delete", "head"]:
            self.assertEqual(getattr(self.client, method)(self.url).status_code, 405)

    def test_snapshot_does_not_call_provider_or_write_any_table(self):
        with patch("core.midtrans.SandboxClient.request") as remote, CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url, {"status": "settled", "tenant_id": self.tenants[1].pk})
        self.assertEqual(response.status_code, 200)
        remote.assert_not_called()
        writes = [q["sql"] for q in queries if q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"}]
        self.assertEqual(writes, [])
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "creating")

    def test_background_reads_do_not_renew_session_activity(self):
        session = self.client.session
        last_seen, started = time.time()-100, time.time()-500
        session.update({"last_seen": last_seen, "started_at": started})
        session.save()
        for _ in range(2):
            self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(self.client.session["last_seen"], last_seen)
        self.assertEqual(self.client.session["started_at"], started)
        self.client.get(reverse("gateway_order", args=[self.order.pk]))
        self.assertGreater(self.client.session["last_seen"], last_seen)

    def test_idle_or_absolute_expiry_is_still_enforced(self):
        for field, age in [("last_seen", 1801), ("started_at", 28801)]:
            verified_login(self.client, self.owners[0])
            session = self.client.session
            session[field] = time.time()-age
            session.save()
            self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_access_and_mfa_revocation_are_still_enforced(self):
        Membership.objects.filter(user=self.owners[0]).update(session_version=2)
        self.assertEqual(self.client.get(self.url).status_code, 401)
        verified_login(self.client, self.owners[0])
        MFADevice.objects.filter(user=self.owners[0]).update(revision=uuid.uuid4())
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_no_merchant_keys_opt_in_or_recent_step_up_needed_to_read(self):
        GatewayConfig.objects.update(enabled=False)
        session = self.client.session
        session["mfa_verified_at"] = time.time()-301
        session.save()
        with override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS={}, SIMULATION=False):
            self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_revision_stable_then_changes_after_verified_status(self):
        a = self.client.get(self.url).json()
        self.assertEqual(a, self.client.get(self.url).json())
        apply_status(self.order.pk, status_payload(self.order, "pending"))
        b = self.client.get(self.url).json()
        self.assertNotEqual(a["revision"], b["revision"])
        self.assertTrue(b["watch"])
        apply_status(self.order.pk, status_payload(self.order))
        c = self.client.get(self.url).json()
        self.assertNotEqual(b["revision"], c["revision"])
        self.assertEqual(c["label"], "Pembayaran tercatat")
        self.assertFalse(c["watch"])

    def test_checkout_availability_and_lease_expiry_change_revision_without_exposing_values(self):
        before = page_snapshot(self.order)
        self.order.checkout_url = URL
        after = page_snapshot(self.order)
        self.assertNotEqual(before["revision"], after["revision"])
        self.assertNotIn(URL, json.dumps(after))
        self.order.checkout_lease_token = uuid.uuid4()
        self.order.checkout_lease_until = timezone.now()+timedelta(seconds=60)
        active = page_snapshot(self.order)
        self.order.checkout_lease_until = timezone.now()-timedelta(seconds=1)
        self.assertNotEqual(active["revision"], page_snapshot(self.order)["revision"])
        self.assertNotIn(str(self.order.checkout_lease_token), json.dumps(active))

    def test_all_terminal_and_review_states_stop_watch(self):
        for state in ["settled", "deny", "cancel", "expire", "review", "refunded"]:
            self.order.status = state
            self.assertFalse(page_snapshot(self.order)["watch"])

    def test_page_baseline_matches_snapshot_and_watcher_is_absent_after_settlement(self):
        response = self.client.get(reverse("gateway_order", args=[self.order.pk]))
        self.assertContains(response, 'data-revision="'+self.client.get(self.url).json()["revision"]+'"')
        self.assertContains(response, "gateway-watch.js")
        self.assertContains(response, "tidak memeriksa status ke Midtrans")
        apply_status(self.order.pk, status_payload(self.order))
        response = self.client.get(reverse("gateway_order", args=[self.order.pk]))
        self.assertNotContains(response, "gateway-watch.js")
        self.assertNotContains(response, 'id="gateway-watch"')
