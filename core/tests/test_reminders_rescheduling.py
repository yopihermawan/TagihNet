import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from core.billing import issue_invoice, record_payment
from core.access import save_role
from core.models import Customer, Installation, Invoice, Membership, Plan, Reminder, ReminderPolicy, Tenant, Ticket
from core.operations import (create_installation, create_ticket, edit_installation, manage_ticket,
    reschedule_installation, transition_installation, transition_ticket)
from core.reminders import plan_reminders, process_reminder, save_policy
from .auth_helpers import verified_login
from .test_operations_workflows import prospect_data, technician
from .test_workflow import fixtures

NOW = datetime(2026, 10, 31, 10, 0, tzinfo=ZoneInfo("Asia/Jakarta"))
VALUES = {"billing_enabled": True, "sla_enabled": True, "lead_days": 3, "billing_hour": 9, "scenario": "normal"}


class ReschedulingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        cls.tech = technician(cls.tenants[0])

    def setUp(self):
        verified_login(self.client, self.owners[0])

    def order(self, scheduled=False):
        data = prospect_data(self.tenants[0])
        data["assigned_id"] = self.tech.pk
        order = create_installation(self.owners[0], data)
        if scheduled:
            for status in ["survey", "scheduled"]:
                order = transition_installation(self.owners[0], order.pk, status, "Survei dan penjadwalan untuk uji",
                    timezone.now()+timedelta(days=1) if status == "scheduled" else None, order.version, uuid.uuid4())
        return order

    def values(self, order, **changes):
        values = {key: getattr(order, key) for key in ["name", "email", "phone", "username", "address", "area", "plan_id"]}
        return {**values, **changes}

    def test_edit_normalizes_data_and_retains_before_after_history(self):
        order = self.order()
        values = self.values(order, name="Nama baru", phone="+62 812-9999999")
        edited = edit_installation(self.owners[0], order.pk, values, "Koreksi informasi dari calon pelanggan", order.version, uuid.uuid4())
        self.assertEqual(edited.phone, "08129999999")
        self.assertEqual(edited.name, "Nama baru")
        body = edited.events.get(kind="edited").body
        self.assertIn(order.name, body)
        self.assertIn("Nama baru", body)
        self.assertEqual(Customer.objects.count(), 2)
        self.assertFalse(Invoice.objects.exists())

    def test_edit_scheduled_order_clears_appointment_and_requires_survey_again(self):
        order = self.order(scheduled=True)
        edited = edit_installation(self.owners[0], order.pk, self.values(order, address="Alamat yang dikoreksi"),
            "Lokasi pemasangan berubah dan perlu survei", order.version, uuid.uuid4())
        self.assertEqual(edited.status, "survey")
        self.assertIsNone(edited.scheduled_for)
        self.assertIn("dibatalkan", edited.events.get(kind="edited").body)

    def test_edit_replay_is_idempotent_and_stale_forms_are_rejected(self):
        order = self.order()
        values, key = self.values(order, name="Koreksi nama"), uuid.uuid4()
        first = edit_installation(self.owners[0], order.pk, values, "Nama diperbaiki sesuai pengajuan", order.version, key)
        second = edit_installation(self.owners[0], order.pk, values, "Nama diperbaiki sesuai pengajuan", order.version, key)
        self.assertEqual(first.version, second.version)
        self.assertEqual(second.events.filter(kind="edited").count(), 1)
        with self.assertRaises(ValidationError):
            edit_installation(self.owners[0], order.pk, self.values(order, name="Form lama"), "Perubahan dari formulir kedaluwarsa", order.version, uuid.uuid4())

    def test_identity_edit_is_locked_when_field_work_has_started(self):
        order = self.order(scheduled=True)
        order = transition_installation(self.owners[0], order.pk, "installing", "Teknisi sudah mulai bekerja", None, order.version, uuid.uuid4())
        with self.assertRaises(ValidationError):
            edit_installation(self.owners[0], order.pk, self.values(order, name="Tidak boleh"), "Identitas setelah pengerjaan dimulai", order.version, uuid.uuid4())

    def test_cross_tenant_plan_and_duplicate_customer_data_are_rejected(self):
        order = self.order()
        with self.assertRaises(Plan.DoesNotExist):
            edit_installation(self.owners[0], order.pk, self.values(order, plan_id=self.customers[1].plan_id), "Paket ISP berbeda tidak diizinkan", order.version, uuid.uuid4())
        with self.assertRaises(ValidationError):
            edit_installation(self.owners[0], order.pk, self.values(order, phone=self.customers[0].phone), "Nomor sudah digunakan pelanggan", order.version, uuid.uuid4())

    def test_reschedule_tracks_both_dates_without_financial_side_effects(self):
        order = self.order(scheduled=True)
        new_date = order.scheduled_for + timedelta(days=1)
        key = uuid.uuid4()
        first = reschedule_installation(self.owners[0], order.pk, new_date, "Pelanggan meminta jadwal baru", order.version, key)
        second = reschedule_installation(self.owners[0], order.pk, new_date, "Pelanggan meminta jadwal baru", order.version, key)
        self.assertEqual(first.version, second.version)
        self.assertEqual(first.scheduled_for, new_date)
        self.assertEqual(first.events.filter(kind="rescheduled").count(), 1)
        self.assertIn("→", first.events.get(kind="rescheduled").body)
        self.assertFalse(Invoice.objects.exists())

    def test_reschedule_requires_future_time_active_technician_and_scheduled_state(self):
        order = self.order()
        with self.assertRaises(ValidationError):
            reschedule_installation(self.owners[0], order.pk, timezone.now()+timedelta(days=1), "Belum pada tahap Terjadwal", order.version, uuid.uuid4())
        order = transition_installation(self.owners[0], order.pk, "survey", "Survei awal sudah dilaksanakan", None, order.version, uuid.uuid4())
        order = transition_installation(self.owners[0], order.pk, "scheduled", "Jadwal awal telah disepakati", timezone.now()+timedelta(days=1), order.version, uuid.uuid4())
        with self.assertRaises(ValidationError):
            reschedule_installation(self.owners[0], order.pk, timezone.now()-timedelta(hours=1), "Waktu lampau tidak berlaku", order.version, uuid.uuid4())
        get_user_model().objects.filter(pk=self.tech.user_id).update(is_active=False)
        with self.assertRaises(ValidationError):
            reschedule_installation(self.owners[0], order.pk, timezone.now()+timedelta(days=2), "Teknisi sudah tidak aktif", order.version, uuid.uuid4())

    def test_technician_cannot_edit_or_reschedule_and_other_isp_is_hidden(self):
        order = self.order(scheduled=True)
        for user, status in [(self.tech.user, 403), (self.owners[1], 404)]:
            client = Client()
            verified_login(client, user)
            for action in ["edit", "reschedule"]:
                self.assertEqual(client.get(f"/operations/installations/{order.pk}/{action}/").status_code, status)
                self.assertEqual(client.post(f"/operations/installations/{order.pk}/{action}/", {}).status_code, status)


@override_settings(SIMULATION=True)
class ReminderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()

    def setUp(self):
        verified_login(self.client, self.owners[0])
        self.clock = patch("core.reminders.timezone.now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def policy(self, **changes):
        existing = ReminderPolicy.objects.filter(tenant=self.tenants[0]).first()
        return save_policy(self.owners[0], {**VALUES, **changes}, existing.version if existing else 1, time.time(), 1)

    def invoice(self, due=None, period=date(2026, 10, 1)):
        invoice, _ = issue_invoice(self.tenants[0], self.customers[0].pk, period, self.owners[0])
        Invoice.objects.filter(pk=invoice.pk).update(due_date=due or NOW.date())
        invoice.refresh_from_db()
        return invoice

    def ticket(self):
        with patch("core.operations.timezone.now", return_value=NOW-timedelta(hours=25)):
            return create_ticket(self.owners[0], self.customers[0].pk, "Tiket melewati SLA", "Koneksi belum pulih hingga sekarang", "connection")

    def test_reminders_are_disabled_by_default_and_get_has_no_side_effect(self):
        self.invoice()
        self.client.get("/operations/reminders/")
        self.client.get("/operations/reminders/settings/")
        self.assertFalse(ReminderPolicy.objects.exists())
        call_command("run_reminder_worker", once=True, stdout=StringIO())
        self.assertFalse(Reminder.objects.exists())

    def test_planner_is_idempotent_for_billing_day_and_sla_deadline(self):
        invoice, ticket = self.invoice(), self.ticket()
        policy = self.policy()
        self.assertEqual(plan_reminders(policy.pk), 2)
        self.assertEqual(plan_reminders(policy.pk), 0)
        self.assertEqual(Reminder.objects.filter(invoice=invoice).count(), 1)
        self.assertEqual(Reminder.objects.filter(ticket=ticket).count(), 1)

    def test_billing_hour_uses_wib_and_sla_is_not_delayed_until_billing_hour(self):
        self.invoice()
        self.ticket()
        policy = self.policy(billing_hour=11)
        self.assertEqual(plan_reminders(policy.pk), 1)
        self.assertEqual(Reminder.objects.get().kind, "sla")
        with patch("core.reminders.timezone.now", return_value=NOW+timedelta(hours=1)):
            self.assertEqual(plan_reminders(policy.pk), 1)

    def test_lead_days_and_paid_invoices_filter_candidates_on_server(self):
        future = self.invoice(due=NOW.date()+timedelta(days=4))
        policy = self.policy(lead_days=3)
        self.assertEqual(plan_reminders(policy.pk), 0)
        Invoice.objects.filter(pk=future.pk).update(due_date=NOW.date()+timedelta(days=3))
        record_payment(self.tenants[0], future.pk, future.total, uuid.uuid4(), self.owners[0])
        self.assertEqual(plan_reminders(policy.pk), 0)

    def test_partial_payment_message_uses_latest_balance_not_preview_amount(self):
        invoice = self.invoice()
        policy = self.policy()
        plan_reminders(policy.pk)
        record_payment(self.tenants[0], invoice.pk, 100000, uuid.uuid4(), self.owners[0])
        reminder = process_reminder(Reminder.objects.get().pk)
        self.assertEqual(reminder.status, "simulated")
        self.assertIn("Rp149.750", reminder.body)
        self.assertNotIn("Rp249.750", reminder.body)

    def test_payment_before_processing_skips_reminder_and_does_not_call_adapter(self):
        invoice = self.invoice()
        policy = self.policy()
        plan_reminders(policy.pk)
        record_payment(self.tenants[0], invoice.pk, invoice.total, uuid.uuid4(), self.owners[0])
        with patch("core.reminders.SandboxNotifier.record") as adapter:
            reminder = process_reminder(Reminder.objects.get().pk)
        self.assertEqual(reminder.status, "skipped")
        self.assertEqual(reminder.body, "")
        adapter.assert_not_called()

    def test_ticket_resolved_before_processing_skips_private_alert(self):
        ticket = self.ticket()
        policy = self.policy()
        plan_reminders(policy.pk)
        transition_ticket(self.owners[0], ticket.pk, "resolved", "Gangguan telah diselesaikan oleh tim", ticket.version, uuid.uuid4())
        self.assertEqual(process_reminder(Reminder.objects.get().pk).status, "skipped")

    def test_reopen_creates_new_sla_cycle_but_comments_do_not_duplicate(self):
        ticket = self.ticket()
        policy = self.policy()
        plan_reminders(policy.pk)
        first = Reminder.objects.get()
        process_reminder(first.pk)
        ticket = transition_ticket(self.owners[0], ticket.pk, "resolved", "Layanan kembali normal sementara", ticket.version, uuid.uuid4())
        ticket = transition_ticket(self.owners[0], ticket.pk, "in_progress", "Gangguan muncul lagi dan dibuka ulang", ticket.version, uuid.uuid4())
        self.assertEqual(ticket.sla_cycle, 2)
        self.assertEqual(plan_reminders(policy.pk), 1)
        self.assertEqual(plan_reminders(policy.pk), 0)
        self.assertEqual(Reminder.objects.filter(ticket=ticket).count(), 2)

    def test_reprioritization_invalidates_pending_deadline_and_plans_new_one(self):
        ticket = self.ticket()
        policy = self.policy()
        plan_reminders(policy.pk)
        first = Reminder.objects.get()
        ticket = manage_ticket(self.owners[0], ticket.pk, "urgent", "connection", None, ticket.version, uuid.uuid4())
        self.assertEqual(process_reminder(first.pk).status, "skipped")
        self.assertEqual(plan_reminders(policy.pk), 1)

    def test_next_business_day_allows_one_new_billing_reminder_and_skips_old_pending(self):
        self.invoice()
        policy = self.policy()
        plan_reminders(policy.pk)
        first = Reminder.objects.get()
        with patch("core.reminders.timezone.now", return_value=NOW+timedelta(days=1)):
            self.assertEqual(process_reminder(first.pk).status, "skipped")
            self.assertEqual(plan_reminders(policy.pk), 1)
        self.assertEqual(Reminder.objects.count(), 2)

    def test_permission_change_blocks_queue_until_policy_is_reapproved(self):
        self.invoice()
        policy = self.policy()
        plan_reminders(policy.pk)
        Membership.objects.filter(user=self.owners[0]).update(session_version=2)
        row = process_reminder(Reminder.objects.get().pk)
        self.assertEqual(row.status, "blocked")
        self.assertEqual(plan_reminders(policy.pk), 0)
        policy = save_policy(self.owners[0], VALUES, policy.version, time.time(), 2)
        self.assertEqual(process_reminder(row.pk).status, "simulated")

    def test_disabling_policy_skips_already_queued_work(self):
        self.invoice()
        policy = self.policy()
        plan_reminders(policy.pk)
        self.policy(billing_enabled=False, sla_enabled=False)
        self.assertEqual(process_reminder(Reminder.objects.get().pk).status, "skipped")

    def test_adapter_failure_backoff_and_maximum_three_attempts(self):
        self.invoice()
        policy = self.policy(scenario="unavailable")
        plan_reminders(policy.pk)
        row = process_reminder(Reminder.objects.get().pk)
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.attempts, 1)
        self.assertEqual(process_reminder(row.pk).attempts, 1)
        for expected in [2, 3]:
            Reminder.objects.filter(pk=row.pk).update(next_attempt_at=NOW-timedelta(seconds=1))
            row = process_reminder(row.pk)
            self.assertEqual(row.attempts, expected)
        self.policy(scenario="normal")
        self.assertEqual(process_reminder(row.pk).status, "failed")
        self.assertEqual(process_reminder(row.pk).attempts, 3)

    def test_retry_after_transient_failure_rechecks_and_does_not_repeat_success(self):
        self.invoice()
        policy = self.policy(scenario="unavailable")
        plan_reminders(policy.pk)
        row = process_reminder(Reminder.objects.get().pk)
        self.policy(scenario="normal")
        Reminder.objects.filter(pk=row.pk).update(next_attempt_at=NOW-timedelta(seconds=1))
        self.assertEqual(process_reminder(row.pk).status, "simulated")
        self.assertEqual(process_reminder(row.pk).attempts, 2)

    @override_settings(SIMULATION=False)
    def test_real_dispatch_is_fail_closed(self):
        with self.assertRaises(ValidationError):
            self.policy()
        with self.assertRaises(CommandError):
            call_command("run_reminder_worker", once=True, stdout=StringIO())
        self.assertFalse(Reminder.objects.exists())

    def test_policy_requires_mfa_current_version_and_valid_bounded_fields(self):
        for proof, access in [(time.time()-301, 1), (time.time()+30, 1), (time.time(), 99)]:
            with self.assertRaises(PermissionDenied):
                save_policy(self.owners[0], VALUES, 1, proof, access)
        for values in [{**VALUES, "lead_days": 15}, {**VALUES, "billing_hour": 24}, {**VALUES, "scenario": "unknown"}]:
            with self.assertRaises(ValidationError):
                save_policy(self.owners[0], values, 1, time.time(), 1)

    def test_other_roles_and_tenants_cannot_read_or_modify_outbox(self):
        self.invoice()
        policy = self.policy()
        plan_reminders(policy.pk)
        row = Reminder.objects.get()
        other = Client()
        verified_login(other, self.owners[1])
        self.assertEqual(other.get(f"/operations/reminders/{row.pk}/").status_code, 404)
        self.assertEqual(other.get("/operations/reminders/").context["page"].paginator.count, 0)
        Membership.objects.filter(user=self.owners[0]).update(role="ops")
        self.assertEqual(self.client.get("/operations/reminders/").status_code, 403)
        self.assertEqual(self.client.post("/operations/reminders/settings/", {}).status_code, 403)

    def test_custom_reminder_role_requires_all_dependencies_and_worker_rechecks_revocation(self):
        with self.assertRaises(ValidationError):
            save_role(self.owners[0], "Pengingat tidak lengkap", {"reminders", "tickets"})
        role = save_role(self.owners[0], "Petugas pengingat", {"reminders", "tickets", "invoices", "customers"})
        user = get_user_model().objects.create_user(username="reminder-operator")
        member = Membership.objects.create(user=user, tenant=self.tenants[0], role="ops", access_role=role)
        verified_login(self.client, user)
        self.assertEqual(self.client.get("/operations/reminders/").status_code, 200)
        policy = save_policy(user, VALUES, 1, time.time(), member.session_version)
        self.invoice()
        plan_reminders(policy.pk)
        save_role(self.owners[0], role.name, {"tickets", "invoices", "customers"}, role_id=role.pk)
        self.assertEqual(process_reminder(Reminder.objects.get().pk).status, "blocked")
        with self.assertRaises(PermissionDenied):
            save_policy(user, VALUES, policy.version, time.time(), member.session_version)

    def test_policy_http_requires_review_csrf_and_stale_version_is_rejected(self):
        client = Client(enforce_csrf_checks=True)
        verified_login(client, self.owners[0])
        self.assertEqual(client.post("/operations/reminders/settings/", {}).status_code, 403)
        data = {**VALUES, "version": 1}
        self.client.post("/operations/reminders/settings/", data)
        self.assertFalse(ReminderPolicy.objects.exists())
        data["reviewed"] = "on"
        self.assertEqual(self.client.post("/operations/reminders/settings/", data).status_code, 302)
        self.assertContains(self.client.post("/operations/reminders/settings/", data), "Data sudah berubah")

    def test_receipt_is_plain_text_and_not_exposed_in_customer_portal(self):
        ticket = self.ticket()
        Ticket.objects.filter(pk=ticket.pk).update(title='<script>safe-marker</script>')
        policy = self.policy()
        plan_reminders(policy.pk)
        row = process_reminder(Reminder.objects.get().pk)
        response = self.client.get(f"/operations/reminders/{row.pk}/")
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, '<script>safe-marker</script>')
        self.assertIn("Tidak ada pesan nyata dikirim", row.body)

    def test_command_failure_exit_and_bounded_planning_are_honest(self):
        self.invoice()
        policy = self.policy(scenario="unavailable")
        with self.assertRaises(CommandError):
            call_command("run_reminder_worker", once=True, limit=1, stdout=StringIO(), stderr=StringIO())
        self.assertEqual(Reminder.objects.get().status, "failed")
        for limit in [0, 501]:
            with self.assertRaises(CommandError):
                call_command("run_reminder_worker", once=True, limit=limit, stdout=StringIO())

    def test_changed_billing_hour_defers_pending_row_without_consuming_attempt(self):
        self.invoice()
        policy = self.policy()
        plan_reminders(policy.pk)
        self.policy(billing_hour=11)
        row = process_reminder(Reminder.objects.get().pk)
        self.assertEqual((row.status, row.attempts), ("pending", 0))
        self.assertEqual(row.next_attempt_at, NOW.replace(hour=11))
        with patch("core.reminders.timezone.now", return_value=NOW.replace(hour=11)):
            self.assertEqual(process_reminder(row.pk).status, "simulated")

    def test_worker_rotates_past_ten_policies_even_when_approval_is_invalid(self):
        for index in range(11):
            tenant = Tenant.objects.create(name=f"ISP antrean {index}")
            ReminderPolicy.objects.create(tenant=tenant, sla_enabled=True)
        call_command("run_reminder_worker", once=True, stdout=StringIO())
        self.assertEqual(ReminderPolicy.objects.filter(last_checked_at__isnull=True).count(), 1)
        with patch("core.reminders.timezone.now", return_value=NOW+timedelta(seconds=5)):
            call_command("run_reminder_worker", once=True, stdout=StringIO())
        self.assertFalse(ReminderPolicy.objects.filter(last_checked_at__isnull=True).exists())


class ReminderConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.tenants, self.owners, self.customers = fixtures()

    def parallel(self, functions):
        def wrapped(function):
            close_old_connections()
            try:
                return function()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(wrapped, functions))

    def test_two_planners_and_workers_create_one_simulated_receipt(self):
        with patch("core.reminders.timezone.now", return_value=NOW):
            invoice, _ = issue_invoice(self.tenants[0], self.customers[0].pk, date(2026, 10, 1), self.owners[0])
            policy = save_policy(self.owners[0], VALUES, 1, time.time(), 1)
            self.parallel([lambda: plan_reminders(policy.pk), lambda: plan_reminders(policy.pk)])
            self.assertEqual(Reminder.objects.count(), 1)
            row_id = Reminder.objects.get().pk
            self.parallel([lambda: process_reminder(row_id), lambda: process_reminder(row_id)])
            row = Reminder.objects.get()
            self.assertEqual(row.status, "simulated")
            self.assertEqual(row.attempts, 1)

    def test_two_reschedules_do_not_lose_updates(self):
        tech = technician(self.tenants[0])
        data = {**prospect_data(self.tenants[0]), "assigned_id": tech.pk}
        order = create_installation(self.owners[0], data)
        order = transition_installation(self.owners[0], order.pk, "survey", "Survei awal selesai dilaksanakan", None, order.version, uuid.uuid4())
        order = transition_installation(self.owners[0], order.pk, "scheduled", "Jadwal awal telah disepakati", timezone.now()+timedelta(days=1), order.version, uuid.uuid4())
        def reschedule(offset):
            try:
                reschedule_installation(self.owners[0], order.pk, order.scheduled_for+timedelta(days=offset),
                    "Perubahan jadwal secara bersamaan", order.version, uuid.uuid4())
                return True
            except ValidationError:
                return False
        results = self.parallel([lambda: reschedule(1), lambda: reschedule(2)])
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(order.events.filter(kind="rescheduled").count(), 1)
