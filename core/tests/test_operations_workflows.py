import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone
from core.access import save_member, save_role
from core.models import Customer, Installation, Invoice, Membership, NetworkJob, Plan, Tenant, Ticket, WorkEvent
from core.network import run_job
from core.operations import (activate_installation, activation_quote, add_comment, assign_installation, create_installation,
    create_ticket, manage_ticket, transition_installation, transition_ticket)
from .auth_helpers import verified_login
from .test_workflow import fixtures

PERIOD = date(2026, 11, 1)

def technician(tenant, username="teknisi"):
    user = get_user_model().objects.create_user(username=username, password="Local-technician-2026!")
    return Membership.objects.create(user=user, tenant=tenant, role="technician")


def prospect_data(tenant, index=1):
    return {"name": f"Calon Pelanggan {index}", "email": "calon@example.invalid", "phone": f"0812998{index:05d}",
        "username": f"psb-{index}", "address": "Jl. Fiktif No. 10", "area": "Dago", "plan_id": tenant.plan_set.first().pk}


def installed_order(owner, tech, index=1):
    data = prospect_data(tech.tenant, index)
    data["assigned_id"] = tech.pk
    order = create_installation(owner, data)
    for state in ["survey", "scheduled", "installing", "installed"]:
        order = transition_installation(owner, order.pk, state, "Catatan pemasangan fiktif untuk uji",
            timezone.now() + timedelta(days=1) if state == "scheduled" else None, order.version, uuid.uuid4())
    return order


class TicketWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        cls.tech = technician(cls.tenants[0])
        cls.other_tech = technician(cls.tenants[0], "teknisi-lain")

    def setUp(self):
        verified_login(self.client, self.owners[0])

    def ticket(self, **kwargs):
        data = {"customer_id": self.customers[0].pk, "title": "Koneksi terputus", "description": "Koneksi tidak tersedia sejak pagi.",
            "category": "connection", "assigned_id": self.tech.pk}
        data.update(kwargs)
        return create_ticket(self.owners[0], **data)

    def portal(self):
        user = get_user_model().objects.create_user(username="portal-uji", password="Local-customer-2026!")
        Membership.objects.create(user=user, tenant=self.tenants[0], role="customer", customer=self.customers[0])
        client = Client()
        verified_login(client, user)
        return client, user

    def test_ticket_creation_retry_is_unique_and_has_no_financial_side_effect(self):
        key = uuid.uuid4()
        first, second = self.ticket(request_key=key), self.ticket(request_key=key)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(WorkEvent.objects.count(), 1)
        self.assertFalse(Invoice.objects.exists())
        with self.assertRaises(ValidationError):
            self.ticket(request_key=key, title="Judul berbeda")

    def test_customer_and_assignee_must_be_same_tenant(self):
        with self.assertRaises(Customer.DoesNotExist):
            self.ticket(customer_id=self.customers[1].pk)
        foreign_tech = technician(self.tenants[1], "foreign-tech")
        with self.assertRaises(ValidationError):
            self.ticket(assigned_id=foreign_tech.pk)

    def test_portal_creation_ignores_spoofed_customer_priority_and_assignee(self):
        client, user = self.portal()
        response = client.post("/operations/tickets/new/", {"customer": self.customers[1].pk, "priority": "urgent",
            "assigned_to": self.tech.pk, "title": "Laporan portal", "description": "Tidak dapat terhubung hari ini", "category": "connection", "request_key": uuid.uuid4()})
        self.assertEqual(response.status_code, 302)
        ticket = Ticket.objects.get()
        self.assertEqual(ticket.customer_id, self.customers[0].pk)
        self.assertEqual(ticket.priority, "normal")
        self.assertIsNone(ticket.assigned_to_id)

    def test_private_comments_never_reach_customer_html_or_pagination(self):
        ticket = self.ticket()
        add_comment(self.owners[0], ticket.pk, "Catatan rahasia internal", True, uuid.uuid4())
        add_comment(self.owners[0], ticket.pk, "Tim sedang menangani gangguan", False, uuid.uuid4())
        client, _ = self.portal()
        response = client.get(f"/operations/tickets/{ticket.pk}/")
        self.assertContains(response, "Tim sedang menangani gangguan")
        self.assertNotContains(response, "Catatan rahasia internal")
        self.assertEqual(response.context["page"].paginator.count, 2)  # public create + comment
        self.assertNotContains(response, self.owners[0].username)

    def test_customer_cannot_write_internal_notes_or_change_ticket_state(self):
        ticket = self.ticket()
        client, user = self.portal()
        self.assertEqual(client.post(f"/operations/tickets/{ticket.pk}/comment/", {"body": "Privat palsu", "internal": "on", "request_key": uuid.uuid4()}).status_code, 403)
        self.assertEqual(client.post(f"/operations/tickets/{ticket.pk}/status/", {}).status_code, 403)
        with self.assertRaises(PermissionDenied):
            add_comment(user, ticket.pk, "Tidak boleh privat", True, uuid.uuid4())

    def test_customer_same_tenant_other_customer_and_cross_tenant_ticket_are_hidden(self):
        peer = Customer.objects.create(tenant=self.tenants[0], code="PEER", plan=self.customers[0].plan, name="Pelanggan Lain",
            phone="08123333333", username="peer", address="Lain", area="Dago")
        same_tenant = self.ticket(customer_id=peer.pk)
        foreign = create_ticket(self.owners[1], self.customers[1].pk, "Tiket ISP lain", "Data privat ISP berbeda", "connection")
        client, _ = self.portal()
        for ticket in [same_tenant, foreign]:
            self.assertEqual(client.get(f"/operations/tickets/{ticket.pk}/").status_code, 404)
            self.assertEqual(client.post(f"/operations/tickets/{ticket.pk}/comment/", {}).status_code, 404)
        self.assertNotContains(client.get("/operations/tickets/?q=Pelanggan"), "Pelanggan Lain")

    def test_technician_reads_and_updates_only_assigned_objects(self):
        assigned = self.ticket()
        unassigned = self.ticket(title="Belum ditugaskan", assigned_id=None)
        other = self.ticket(title="Tugas teknisi lain", assigned_id=self.other_tech.pk)
        client = Client()
        verified_login(client, self.tech.user)
        response = client.get("/operations/tickets/")
        self.assertContains(response, assigned.title)
        self.assertNotContains(response, unassigned.title)
        self.assertNotContains(response, other.title)
        for ticket in [unassigned, other]:
            self.assertEqual(client.get(f"/operations/tickets/{ticket.pk}/").status_code, 404)
            self.assertEqual(client.post(f"/operations/tickets/{ticket.pk}/status/", {}).status_code, 404)
        self.assertEqual(client.get("/customers/").status_code, 403)
        self.assertEqual(client.get("/invoices/1/pdf/").status_code, 403)
        self.assertEqual(client.get(f"/operations/tickets/{assigned.pk}/manage/").status_code, 403)
        transition_ticket(self.tech.user, assigned.pk, "in_progress", "Teknisi mulai memeriksa gangguan", assigned.version, uuid.uuid4())

    def test_reassignment_revokes_old_technician_object_access(self):
        ticket = self.ticket()
        client = Client()
        verified_login(client, self.tech.user)
        manage_ticket(self.owners[0], ticket.pk, "normal", "connection", self.other_tech.pk, ticket.version, uuid.uuid4())
        self.assertEqual(client.get(f"/operations/tickets/{ticket.pk}/").status_code, 404)
        # If assignment changes between the HTTP lookup and domain mutation, still fail closed as 404.
        with patch("core.operation_views.transition_ticket", side_effect=Ticket.DoesNotExist):
            self.assertEqual(self.client.post(f"/operations/tickets/{ticket.pk}/status/", {"status": "in_progress",
                "note": "Penugasan berubah saat diproses", "version": ticket.version, "request_key": uuid.uuid4()}).status_code, 404)

    def test_inactive_technician_cannot_be_assigned(self):
        get_user_model().objects.filter(pk=self.tech.user_id).update(is_active=False)
        with self.assertRaises(ValidationError):
            self.ticket()

    def test_sla_snapshot_does_not_change_when_tenant_policy_changes(self):
        ticket = self.ticket()
        self.assertEqual(ticket.sla_due_at, ticket.created_at + timedelta(hours=24))
        Tenant.objects.filter(pk=self.tenants[0].pk).update(sla_normal_hours=48, sla_high_hours=1)
        ticket = manage_ticket(self.owners[0], ticket.pk, "high", "connection", self.tech.pk, ticket.version, uuid.uuid4())
        self.assertEqual(ticket.sla_due_at, ticket.created_at + timedelta(hours=8))
        newer = self.ticket()
        self.assertEqual(newer.sla_due_at, newer.created_at + timedelta(hours=48))

    def test_waiting_and_reopening_do_not_reset_sla_clock(self):
        ticket = self.ticket()
        due = ticket.sla_due_at
        ticket = transition_ticket(self.owners[0], ticket.pk, "in_progress", "Penanganan telah dimulai oleh tim", ticket.version, uuid.uuid4())
        ticket = transition_ticket(self.owners[0], ticket.pk, "waiting_customer", "Menunggu konfirmasi kondisi perangkat", ticket.version, uuid.uuid4())
        with patch("core.operations.timezone.now", return_value=due + timedelta(hours=1)):
            ticket = transition_ticket(self.owners[0], ticket.pk, "resolved", "Pelanggan mengonfirmasi layanan pulih", ticket.version, uuid.uuid4())
            self.assertEqual(ticket.sla_label, "Selesai melewati SLA")
        ticket = transition_ticket(self.owners[0], ticket.pk, "in_progress", "Gangguan muncul kembali dan ditinjau", ticket.version, uuid.uuid4())
        self.assertEqual(ticket.sla_due_at, due)
        self.assertIsNone(ticket.resolved_at)

    def test_state_machine_version_and_replay_protect_history(self):
        ticket = self.ticket()
        with self.assertRaises(ValidationError):
            transition_ticket(self.owners[0], ticket.pk, "closed", "Tidak boleh langsung menutup tiket", 1, uuid.uuid4())
        key = uuid.uuid4()
        ticket = transition_ticket(self.owners[0], ticket.pk, "resolved", "Koneksi telah diuji dan normal", 1, key)
        replay = transition_ticket(self.owners[0], ticket.pk, "resolved", "Koneksi telah diuji dan normal", 1, key)
        self.assertEqual(replay.version, ticket.version)
        with self.assertRaises(ValidationError):
            transition_ticket(self.owners[0], ticket.pk, "closed", "Menutup dengan versi form lama", 1, uuid.uuid4())
        with self.assertRaises(ValidationError):
            transition_ticket(self.tech.user, ticket.pk, "closed", "Teknisi tidak berwenang menutup", ticket.version, uuid.uuid4())
        ticket = transition_ticket(self.owners[0], ticket.pk, "closed", "Pelanggan menyetujui penutupan tiket", ticket.version, uuid.uuid4())
        with self.assertRaises(ValidationError):
            manage_ticket(self.owners[0], ticket.pk, "high", "connection", None, ticket.version, uuid.uuid4())

    def test_comment_retry_and_plain_text_encoding(self):
        ticket = self.ticket()
        body, key = '<img src=x onerror="safe-test">', uuid.uuid4()
        add_comment(self.owners[0], ticket.pk, body, True, key)
        add_comment(self.owners[0], ticket.pk, body, True, key)
        self.assertEqual(ticket.events.filter(kind="comment").count(), 1)
        response = self.client.get(f"/operations/tickets/{ticket.pk}/")
        self.assertContains(response, "&lt;img")
        self.assertNotContains(response, body)

    def test_ticket_sla_settings_validation_and_permissions(self):
        data = {"sla_low_hours": 72, "sla_normal_hours": 24, "sla_high_hours": 8, "sla_urgent_hours": 0}
        self.client.post("/operations/sla/", data)
        self.assertEqual(Tenant.objects.get(pk=self.tenants[0].pk).sla_urgent_hours, 2)
        Membership.objects.filter(user=self.owners[0]).update(role="ops")
        self.assertEqual(self.client.post("/operations/sla/", data).status_code, 403)

    def test_ticket_filters_pagination_and_csrf(self):
        for index in range(12):
            self.ticket(title=f"Gangguan {index}", priority="high")
        response = self.client.get("/operations/tickets/?q=Gangguan&priority=high&size=10&page=2")
        self.assertEqual(len(response.context["page"]), 2)
        self.assertEqual(response.context["page"].start_index(), 11)
        client = Client(enforce_csrf_checks=True)
        verified_login(client, self.owners[0])
        self.assertEqual(client.post("/operations/tickets/new/", {}).status_code, 403)


class InstallationWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        cls.tech = technician(cls.tenants[0])

    def setUp(self):
        verified_login(self.client, self.owners[0])

    def installed(self):
        return installed_order(self.owners[0], self.tech)

    def activate(self, order, fingerprint=None):
        fingerprint = fingerprint or activation_quote(order, PERIOD)[1]
        return activate_installation(self.owners[0], order.pk, PERIOD, fingerprint, time.time(), 1)

    def test_prospect_retry_and_spoofed_status_do_not_create_customer_or_invoice(self):
        data, key = prospect_data(self.tenants[0]), uuid.uuid4()
        first = create_installation(self.owners[0], data, key)
        self.assertEqual(create_installation(self.owners[0], data, key).pk, first.pk)
        self.assertEqual(Customer.objects.count(), 2)
        self.assertFalse(Invoice.objects.exists())
        with self.assertRaises(ValidationError):
            create_installation(self.owners[0], {**data, "status": "activated"})

    def test_normalized_duplicate_customer_phone_is_rejected(self):
        data = prospect_data(self.tenants[0])
        data["phone"] = "+62" + self.customers[0].phone[1:]
        with self.assertRaises(ValidationError):
            create_installation(self.owners[0], data)

    def test_psb_requires_same_tenant_plan_and_technician(self):
        foreign = technician(self.tenants[1], "tech-other-isp")
        data = prospect_data(self.tenants[0])
        with self.assertRaises(Plan.DoesNotExist):
            create_installation(self.owners[0], {**data, "plan_id": self.customers[1].plan_id})
        with self.assertRaises(ValidationError):
            create_installation(self.owners[0], {**data, "assigned_id": foreign.pk})

    def test_schedule_requires_active_assignment_and_future_aware_time(self):
        order = create_installation(self.owners[0], prospect_data(self.tenants[0]))
        order = transition_installation(self.owners[0], order.pk, "survey", "Survei awal dimulai di lokasi", None, order.version, uuid.uuid4())
        with self.assertRaises(ValidationError):
            transition_installation(self.owners[0], order.pk, "scheduled", "Jadwal tanpa teknisi harus ditolak", timezone.now()+timedelta(days=1), order.version, uuid.uuid4())
        order = assign_installation(self.owners[0], order.pk, self.tech.pk, order.version, uuid.uuid4())
        with self.assertRaises(ValidationError):
            transition_installation(self.owners[0], order.pk, "scheduled", "Jadwal lampau tidak dapat dipilih", timezone.now()-timedelta(hours=1), order.version, uuid.uuid4())
        with self.assertRaises(ValidationError):
            transition_installation(self.owners[0], order.pk, "installed", "Tidak boleh melompati tahapan", None, order.version, uuid.uuid4())

    def test_technician_scope_and_activation_permission(self):
        order = self.installed()
        other = create_installation(self.owners[0], prospect_data(self.tenants[0], 2))
        client = Client()
        verified_login(client, self.tech.user)
        self.assertEqual(client.get(f"/operations/installations/{order.pk}/").status_code, 200)
        self.assertEqual(client.get(f"/operations/installations/{other.pk}/").status_code, 404)
        self.assertEqual(client.get(f"/operations/installations/{order.pk}/activate/").status_code, 403)
        self.assertEqual(client.get(f"/operations/installations/{order.pk}/assign/").status_code, 403)
        with self.assertRaises(PermissionDenied):
            activate_installation(self.tech.user, order.pk, PERIOD, "fake", time.time(), 1)

    def test_cancel_needs_reason_and_has_no_financial_side_effect(self):
        order = create_installation(self.owners[0], prospect_data(self.tenants[0]))
        with self.assertRaises(ValidationError):
            transition_installation(self.owners[0], order.pk, "cancelled", "", None, 1, uuid.uuid4())
        order = transition_installation(self.owners[0], order.pk, "cancelled", "Calon pelanggan membatalkan pengajuan", None, 1, uuid.uuid4())
        with self.assertRaises(ValidationError):
            self.activate(order)
        self.assertEqual(Customer.objects.count(), 2)
        self.assertFalse(Invoice.objects.exists())

    def test_activation_creates_one_customer_invoice_and_pending_job(self):
        order = self.activate(self.installed())
        self.assertEqual(order.status, "activated")
        self.assertEqual(Customer.objects.count(), 3)
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(order.customer.service_state, "pending")
        self.assertEqual(order.invoice.balance, order.invoice.total)
        self.assertEqual(order.invoice.total, 249750)
        self.assertEqual(NetworkJob.objects.filter(customer=order.customer).count(), 1)
        self.assertEqual(order.invoice.customer_id, order.customer_id)

    def test_activation_retry_keeps_same_records_and_rejects_other_period(self):
        order = self.installed()
        fingerprint = activation_quote(order, PERIOD)[1]
        first = self.activate(order, fingerprint)
        second = self.activate(order, fingerprint)
        self.assertEqual(first.customer_id, second.customer_id)
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(NetworkJob.objects.count(), 1)
        with self.assertRaises(ValidationError):
            activate_installation(self.owners[0], order.pk, date(2026, 12, 1), fingerprint, time.time(), 1)

    def test_activation_rejects_stale_price_settings_or_psb_version(self):
        order = self.installed()
        for model, pk, changes in [(Plan, order.plan_id, {"price": 999999}), (Tenant, order.tenant_id, {"tax_bp": 0}),
                                   (Installation, order.pk, {"version": 99}), (Installation, order.pk, {"phone": "08122299999"})]:
            quote = activation_quote(order, PERIOD)[1]
            original = model.objects.filter(pk=pk).values(*changes).get()
            model.objects.filter(pk=pk).update(**changes)
            with self.assertRaises(ValidationError):
                self.activate(order, quote)
            model.objects.filter(pk=pk).update(**original)
        self.assertFalse(Invoice.objects.exists())

    def test_duplicate_customer_created_after_psb_blocks_activation(self):
        order = self.installed()
        Customer.objects.create(tenant=self.tenants[0], plan=order.plan, code="DUP", name=order.name, phone=order.phone,
            username="another-name", address=order.address, area=order.area)
        with self.assertRaises(ValidationError):
            self.activate(order)
        self.assertFalse(Invoice.objects.exists())
        order.refresh_from_db()
        self.assertEqual(order.status, "installed")

    def test_activation_rollback_removes_new_customer_if_invoice_fails(self):
        order = self.installed()
        with patch("core.operations.write_snapshot_invoice", side_effect=ValidationError("Uji kegagalan invoice")):
            with self.assertRaises(ValidationError):
                self.activate(order)
        self.assertEqual(Customer.objects.count(), 2)
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(NetworkJob.objects.exists())
        self.assertFalse(order.events.filter(kind="activated").exists())

    def test_failed_network_job_does_not_fake_active_or_rollback_activation(self):
        order = self.activate(self.installed())
        Customer.objects.filter(pk=order.customer_id).update(router_available=False)
        job = NetworkJob.objects.get(customer=order.customer)
        self.assertEqual(run_job(job.pk).status, "failed")
        order.customer.refresh_from_db()
        self.assertEqual(order.customer.service_state, "pending")
        self.assertTrue(Invoice.objects.filter(pk=order.invoice_id).exists())

    def test_activation_requires_fresh_mfa_and_live_access_version(self):
        order = self.installed()
        quote = activation_quote(order, PERIOD)[1]
        for verified_at, version in [(time.time()-301, 1), (time.time()+30, 1), (time.time(), 999)]:
            with self.assertRaises(PermissionDenied):
                activate_installation(self.owners[0], order.pk, PERIOD, quote, verified_at, version)

    def test_signed_quote_http_review_tamper_stale_and_replay(self):
        order = self.installed()
        url = f"/operations/installations/{order.pk}/activate/?period=2026-11"
        token = self.client.get(url).context["form"].initial["quote"]
        self.client.post(url, {"quote": token})
        self.assertFalse(Invoice.objects.exists())
        response = self.client.post(url, {"quote": token + "tamper", "reviewed": "on"})
        self.assertContains(response, "tidak valid atau kedaluwarsa")
        payload = signing.loads(token, salt="tagihnet.psb.activation.v1")
        wrong = signing.dumps({**payload, "actor": self.owners[1].pk}, salt="tagihnet.psb.activation.v1")
        self.assertEqual(self.client.post(url, {"quote": wrong, "reviewed": "on"}).status_code, 403)
        for _ in range(2):
            self.assertEqual(self.client.post(url, {"quote": token, "reviewed": "on", "total": 1, "status": "paid"}).status_code, 302)
        self.assertEqual(Invoice.objects.get().total, 249750)
        self.assertEqual(Invoice.objects.get().paid, 0)
        self.assertEqual(Customer.objects.count(), 3)

    def test_quote_expiry_is_rejected_and_csrf_required(self):
        order = self.installed()
        url = f"/operations/installations/{order.pk}/activate/"
        with patch("django.core.signing.time.time", return_value=time.time()-901):
            token = self.client.get(url).context["form"].initial["quote"]
        self.assertContains(self.client.post(url, {"quote": token, "reviewed": "on"}), "tidak valid atau kedaluwarsa")
        client = Client(enforce_csrf_checks=True)
        verified_login(client, self.owners[0])
        self.assertEqual(client.post(url, {"quote": token, "reviewed": "on"}).status_code, 403)
        self.assertFalse(Invoice.objects.exists())

    def test_cross_tenant_psb_and_metadata_not_visible(self):
        order = self.installed()
        client = Client()
        verified_login(client, self.owners[1])
        for tail in ["", "activate/", "assign/"]:
            self.assertEqual(client.get(f"/operations/installations/{order.pk}/{tail}").status_code, 404)
        self.assertEqual(client.post(f"/operations/installations/{order.pk}/status/", {}).status_code, 404)
        self.assertNotContains(client.get("/operations/installations/"), order.name)

    def test_technician_cannot_get_global_scope_through_custom_role(self):
        role = save_role(self.owners[0], "All tickets", ["tickets"])
        with self.assertRaises(ValidationError):
            save_member(self.owners[0], self.tech.pk, "technician", role.pk, None, True)


class OperationConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.tenants, self.owners, self.customers = fixtures()
        self.tech = technician(self.tenants[0])

    def parallel(self, function):
        def wrapped(index):
            close_old_connections()
            try:
                return function(index)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(wrapped, range(2)))

    def test_concurrent_ticket_status_has_no_lost_update(self):
        ticket = create_ticket(self.owners[0], self.customers[0].pk, "Gangguan", "Koneksi tidak tersedia", "connection")
        def update(index):
            try:
                transition_ticket(self.owners[0], ticket.pk, ["in_progress", "resolved"][index], "Pembaruan status secara bersamaan", 1, uuid.uuid4())
                return True
            except ValidationError:
                return False
        self.assertEqual(sorted(self.parallel(update)), [False, True])
        self.assertEqual(ticket.events.filter(kind="status").count(), 1)

    def test_concurrent_activation_creates_customer_invoice_and_job_once(self):
        order = installed_order(self.owners[0], self.tech)
        fingerprint = activation_quote(order, PERIOD)[1]
        results = self.parallel(lambda _: activate_installation(self.owners[0], order.pk, PERIOD, fingerprint, time.time(), 1).customer_id)
        self.assertEqual(results[0], results[1])
        self.assertEqual(Customer.objects.count(), 3)
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(NetworkJob.objects.count(), 1)
        self.assertEqual(order.events.filter(kind="activated").count(), 1)
