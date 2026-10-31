import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from io import StringIO
from unittest.mock import patch
import pyotp
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone
from core.billing import issue_invoice, write_snapshot_invoice
from core.bulk_billing import cancel_batch, confirm_batch, create_preview, process_batch
from core.models import AuditEvent, BillingBatch, BillingBatchItem, Customer, Invoice, Membership, Plan, Tenant
from .auth_helpers import TEST_TOTP_SECRET, verified_login
from .test_workflow import fixtures

PERIOD = date(2026, 2, 1)
FILTERS = {"state": "active", "plan": None, "area": ""}


class BulkBillingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()

    def setUp(self):
        verified_login(self.client, self.owners[0])

    def preview(self, filters=None, discount=0):
        return create_preview(self.owners[0], PERIOD, filters or FILTERS, discount)

    def confirm(self, batch):
        version = Membership.objects.get(user=self.owners[0]).session_version
        return confirm_batch(self.owners[0], batch.pk, time.time(), version)

    def add_customer(self, index):
        return Customer.objects.create(tenant=self.tenants[0], plan=self.customers[0].plan, code=f"B-{index}",
            username=f"bulk-{index}", name=f"Contoh Batch {index}", phone=f"081299{index:06d}",
            address="Alamat fiktif", area="Dago")

    def test_preview_has_no_invoice_side_effect_and_selects_only_actor_tenant(self):
        batch = self.preview(discount=25000)
        self.assertFalse(Invoice.objects.exists())
        self.assertEqual(batch.items.count(), 1)
        item = batch.items.get()
        self.assertEqual(item.customer_id, self.customers[0].pk)
        self.assertEqual(item.snapshot["invoice"]["total"], 222000)
        self.assertEqual(item.snapshot["invoice"]["due_date"], "2026-02-28")

    def test_active_is_default_and_area_plan_state_filters_are_real(self):
        extra = self.add_customer(1)
        extra.service_state = "isolated"
        extra.save()
        self.assertEqual(self.preview().items.count(), 1)
        batch = self.preview({"state": "isolated", "plan": extra.plan_id, "area": "Dago"})
        self.assertEqual(list(batch.items.values_list("customer_id", flat=True)), [extra.pk])
        self.assertEqual(self.preview({"state": "all", "plan": None, "area": ""}).items.count(), 2)

    def test_existing_invoice_is_shown_with_original_amount_and_discount_untouched(self):
        invoice, _ = issue_invoice(self.tenants[0], self.customers[0].pk, PERIOD, self.owners[0], discount=10000)
        # A large requested discount is harmless for an already-issued row: it will be skipped.
        batch = self.preview(discount=250000)
        item = batch.items.get()
        self.assertEqual(item.status, "skipped")
        self.assertEqual(item.snapshot["invoice"]["discount"], 10000)
        self.assertEqual(item.snapshot["invoice"]["total"], invoice.total)
        self.confirm(batch)
        process_batch(batch.pk)
        invoice.refresh_from_db()
        self.assertEqual(invoice.discount, 10000)
        self.assertEqual(Invoice.objects.count(), 1)
        for discount in [-1, 1.5, 100000001]:
            with self.assertRaises(ValidationError):
                self.preview(discount=discount)

    def test_empty_invalid_and_cross_tenant_plan_filters_are_rejected(self):
        for filters in [{"state": "unknown"}, {"state": "active", "area": "Missing"},
                        {"state": "active", "plan": self.customers[1].plan_id}, {"tenant_id": self.tenants[1].pk}]:
            with self.subTest(filters=filters), self.assertRaises(ValidationError):
                self.preview(filters)
        self.assertFalse(BillingBatch.objects.exists())

    def test_preview_rejects_invalid_discount_on_new_rows(self):
        with self.assertRaises(ValidationError):
            self.preview(discount=225000)
        self.assertFalse(BillingBatch.objects.exists())

    def test_preview_is_bounded_to_500_customers_without_silent_truncation(self):
        Customer.objects.bulk_create([Customer(tenant=self.tenants[0], plan=self.customers[0].plan, code=f"M-{index}",
            username=f"many-{index}", name="Fiktif", phone=f"081233{index:06d}", address="Contoh", area="Contoh") for index in range(500)])
        with self.assertRaisesMessage(ValidationError, "500"):
            self.preview()
        self.assertFalse(BillingBatch.objects.exists())

    def test_confirmation_is_queued_idempotently_without_issuing_in_http_request(self):
        batch = self.preview()
        self.confirm(batch)
        self.assertEqual(self.confirm(batch).status, "queued")
        self.assertFalse(Invoice.objects.exists())
        self.assertEqual(AuditEvent.objects.filter(action="billing.queued").count(), 1)
        process_batch(batch.pk)
        self.assertEqual(Invoice.objects.count(), 1)

    def test_confirmation_requires_mfa_and_current_access_version(self):
        batch = self.preview()
        for verified, version in [(time.time() - 301, 1), (time.time() + 30, 1), (time.time(), 999)]:
            with self.assertRaises(PermissionDenied):
                confirm_batch(self.owners[0], batch.pk, verified, version)
        batch.refresh_from_db()
        self.assertEqual(batch.status, "preview")

    def test_expired_preview_never_queues_or_issues(self):
        batch = self.preview()
        BillingBatch.objects.filter(pk=batch.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.confirm(batch).status, "expired")
        self.assertEqual(process_batch(batch.pk), 0)
        self.assertFalse(Invoice.objects.exists())

    def test_price_customer_settings_and_selection_changes_make_preview_stale(self):
        changes = [(Plan, self.customers[0].plan_id, {"price": 225001}),
                   (Customer, self.customers[0].pk, {"name": "Nama baru"}),
                   (Tenant, self.tenants[0].pk, {"tax_bp": 0}),
                   (Tenant, self.tenants[0].pk, {"due_day": 15}),
                   (Tenant, self.tenants[0].pk, {"invoice_footer": "Footer baru"}),
                   (Customer, self.customers[0].pk, {"service_state": "isolated"})]
        for model, pk, values in changes:
            with self.subTest(values=values):
                batch = self.preview()
                before = model.objects.filter(pk=pk).values(*values).get()
                model.objects.filter(pk=pk).update(**values)
                self.assertEqual(self.confirm(batch).status, "stale")
                model.objects.filter(pk=pk).update(**before)
        batch = self.preview()
        self.add_customer(100)
        self.assertEqual(self.confirm(batch).status, "stale")
        self.assertFalse(Invoice.objects.exists())

    def test_invoice_created_before_confirmation_requires_new_preview(self):
        batch = self.preview()
        issue_invoice(self.tenants[0], self.customers[0].pk, PERIOD, self.owners[0])
        self.assertEqual(self.confirm(batch).status, "stale")

    def test_approved_snapshot_survives_later_price_tax_and_identity_change(self):
        batch = self.preview(discount=25000)
        self.confirm(batch)
        Plan.objects.filter(pk=self.customers[0].plan_id).update(price=900000)
        Tenant.objects.filter(pk=self.tenants[0].pk).update(tax_bp=0, due_day=1, invoice_footer="Baru")
        Customer.objects.filter(pk=self.customers[0].pk).update(name="Nama berikutnya")
        process_batch(batch.pk)
        invoice = Invoice.objects.get()
        self.assertEqual(invoice.total, 222000)
        self.assertEqual(invoice.tax_bp, 1100)
        self.assertEqual(invoice.customer_name, "Pelanggan 0")
        self.assertEqual(invoice.due_date, date(2026, 2, 28))
        self.assertNotEqual(invoice.footer, "Baru")

    def test_single_invoice_issued_after_confirmation_is_skipped_without_overwrite(self):
        batch = self.preview(discount=25000)
        self.confirm(batch)
        invoice, _ = issue_invoice(self.tenants[0], self.customers[0].pk, PERIOD, self.owners[0])
        process_batch(batch.pk)
        self.assertEqual(batch.items.get().status, "skipped")
        self.assertEqual(Invoice.objects.count(), 1)
        invoice.refresh_from_db()
        self.assertEqual(invoice.discount, 0)

    def test_progress_commits_per_row_and_resumes_after_worker_restart(self):
        self.add_customer(1)
        self.add_customer(2)
        batch = self.preview()
        self.confirm(batch)
        self.assertEqual(process_batch(batch.pk, limit=1), 1)
        batch.refresh_from_db()
        self.assertEqual(batch.status, "processing")
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(process_batch(batch.pk), 2)
        self.assertEqual(process_batch(batch.pk), 0)
        self.assertEqual(Invoice.objects.count(), 3)
        self.assertEqual(AuditEvent.objects.filter(action="billing.completed").count(), 1)

    def test_cancel_before_and_during_processing_preserves_issued_invoices(self):
        self.add_customer(1)
        first = self.preview()
        self.confirm(first)
        cancel_batch(self.owners[0], first.pk)
        self.assertEqual(process_batch(first.pk), 0)
        self.assertFalse(Invoice.objects.exists())
        second = self.preview()
        self.confirm(second)
        process_batch(second.pk, limit=1)
        cancel_batch(self.owners[0], second.pk)
        self.assertEqual(process_batch(second.pk), 0)
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(second.items.filter(status="pending").count(), 1)

    def test_worker_stops_remaining_rows_when_creator_access_changes(self):
        self.add_customer(1)
        batch = self.preview()
        self.confirm(batch)
        process_batch(batch.pk, limit=1)
        Membership.objects.filter(user=self.owners[0]).update(session_version=2)
        self.assertEqual(process_batch(batch.pk), 0)
        batch.refresh_from_db()
        self.assertEqual(batch.status, "blocked")
        self.assertEqual(Invoice.objects.count(), 1)

    def test_deactivated_creator_cannot_be_processed(self):
        batch = self.preview()
        self.confirm(batch)
        get_user_model().objects.filter(pk=self.owners[0].pk).update(is_active=False)
        process_batch(batch.pk)
        batch.refresh_from_db()
        self.assertEqual(batch.status, "blocked")
        self.assertFalse(Invoice.objects.exists())

    def test_invalid_snapshot_rolls_back_row_invoice_and_its_audit(self):
        batch = self.preview()
        self.confirm(batch)
        def fail_after_write(*args, **kwargs):
            write_snapshot_invoice(*args, **kwargs)
            raise ValidationError("Uji kegagalan setelah penulisan")
        with patch("core.bulk_billing.write_snapshot_invoice", side_effect=fail_after_write):
            process_batch(batch.pk)
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action="invoice.issued").exists())
        self.assertEqual(batch.items.get().status, "pending")
        batch.refresh_from_db()
        self.assertEqual(batch.status, "blocked")

    def test_worker_rejects_cross_tenant_customer_corruption(self):
        batch = self.preview()
        self.confirm(batch)
        batch.items.update(customer=self.customers[1])
        process_batch(batch.pk)
        batch.refresh_from_db()
        self.assertEqual(batch.status, "blocked")
        self.assertFalse(Invoice.objects.exists())

    def test_http_confirmation_requires_review_and_ignores_browser_money(self):
        batch = self.preview()
        url = f"/invoices/batches/{batch.pk}/confirm/"
        self.assertEqual(self.client.post(url, {}).status_code, 400)
        self.assertEqual(self.client.post(url, {"reviewed": "on", "total": 1, "tenant_id": self.tenants[1].pk}).status_code, 302)
        process_batch(batch.pk)
        self.assertEqual(Invoice.objects.get().total, 249750)

    def test_unsafe_methods_and_missing_csrf_do_not_confirm_or_cancel(self):
        batch = self.preview()
        client = Client(enforce_csrf_checks=True)
        verified_login(client, self.owners[0])
        for action in ["confirm", "cancel"]:
            url = f"/invoices/batches/{batch.pk}/{action}/"
            self.assertEqual(client.get(url).status_code, 405)
            self.assertEqual(client.post(url, {"reviewed": "on"}).status_code, 403)
        batch.refresh_from_db()
        self.assertEqual(batch.status, "preview")

    def test_cross_tenant_and_other_creator_batch_objects_are_not_readable(self):
        batch = self.preview()
        for user in [self.owners[1], get_user_model().objects.create_user(username="other-owner")]:
            if user.username == "other-owner":
                Membership.objects.create(user=user, tenant=self.tenants[0], role="owner")
            client = Client()
            verified_login(client, user)
            for route in [f"/invoices/batches/{batch.pk}/", f"/invoices/batches/{batch.pk}/confirm/", f"/invoices/batches/{batch.pk}/cancel/"]:
                response = client.get(route) if route.endswith(f"{batch.pk}/") else client.post(route, {"reviewed": "on"})
                self.assertEqual(response.status_code, 404)
            self.assertNotContains(client.get("/invoices/batches/"), str(batch.pk))

    def test_single_issue_permission_does_not_imply_mass_issue(self):
        Membership.objects.filter(user=self.owners[0]).update(role="ops")
        self.assertEqual(self.client.get("/invoices/new/").status_code, 200)
        self.assertEqual(self.client.get("/invoices/batches/").status_code, 403)
        with self.assertRaises(PermissionDenied):
            self.preview()

    def test_stepup_returns_to_same_preview_without_saving_early(self):
        batch = self.preview()
        session = self.client.session
        session["mfa_verified_at"] = time.time() - 301
        session.save()
        response = self.client.post(f"/invoices/batches/{batch.pk}/confirm/", {"reviewed": "on"})
        self.assertRedirects(response, "/account/security/")
        batch.refresh_from_db()
        self.assertEqual(batch.status, "preview")
        response = self.client.post("/account/security/", {"password": "Local-test-secret-2026!", "code": pyotp.TOTP(TEST_TOTP_SECRET).now()})
        self.assertRedirects(response, f"/invoices/batches/{batch.pk}/")
        self.assertNotIn("stepup_return", self.client.session)

    def test_worker_command_does_not_process_unconfirmed_preview(self):
        batch = self.preview()
        call_command("run_billing_worker", once=True, stdout=StringIO())
        self.assertFalse(Invoice.objects.exists())
        self.confirm(batch)
        call_command("run_billing_worker", once=True, stdout=StringIO())
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(BillingBatch.objects.get(pk=batch.pk).status, "completed")

    def test_list_and_detail_pagination_preserve_summary(self):
        for index in range(27):
            self.add_customer(index)
        batch = self.preview()
        response = self.client.get(f"/invoices/batches/{batch.pk}/?size=10&page=2")
        self.assertEqual(len(response.context["page"]), 10)
        self.assertEqual(response.context["summary"]["selected"], 28)
        self.assertEqual(response.context["page"].start_index(), 11)
        self.assertEqual(self.client.get("/invoices/batches/").status_code, 200)


class BulkConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.tenants, self.owners, self.customers = fixtures()

    def parallel(self, functions):
        def wrapped(function):
            close_old_connections()
            try:
                return function()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(wrapped, functions))

    def queue(self):
        batch = create_preview(self.owners[0], PERIOD, FILTERS)
        confirm_batch(self.owners[0], batch.pk, time.time(), 1)
        return batch

    def test_two_workers_same_batch_issue_once(self):
        batch = self.queue()
        self.parallel([lambda: process_batch(batch.pk), lambda: process_batch(batch.pk)])
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(batch.items.filter(status="created").count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="billing.completed").count(), 1)

    def test_overlapping_confirmed_batches_do_not_duplicate_invoices(self):
        first, second = self.queue(), self.queue()
        self.parallel([lambda: process_batch(first.pk), lambda: process_batch(second.pk)])
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(BillingBatchItem.objects.filter(status="created").count(), 1)
        self.assertEqual(BillingBatchItem.objects.filter(status="skipped").count(), 1)

    def test_bulk_worker_racing_single_issue_preserves_one_invoice(self):
        batch = self.queue()
        self.parallel([lambda: process_batch(batch.pk), lambda: issue_invoice(self.tenants[0], self.customers[0].pk, PERIOD, self.owners[0])])
        self.assertEqual(Invoice.objects.count(), 1)
