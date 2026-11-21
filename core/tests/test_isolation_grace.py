from datetime import date, datetime
import time
import uuid
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from core import billing_scheduler
from core.billing import issue_invoice, record_payment, reverse_payment
from core.billing_settings import SettingsConflict, confirm_change, form_token, review_change, values_for
from core.bulk_billing import confirm_batch, create_preview, process_batch
from core.isolation import GRACE_FIELD, invoice_grace_days
from core.models import Invoice, InvoiceIsolationTerms, NetworkJob, Tenant, TenantIsolationPolicy
from core.network import run_job
from core.operations import activate_installation, activation_quote

from .auth_helpers import verified_login
from .test_operations_workflows import installed_order, technician
from .test_workflow import fixtures


@override_settings(SIMULATION=True)
class IsolationGraceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()

    def setUp(self):
        self.tenant, self.actor, self.customer = self.tenants[0], self.owners[0], self.customers[0]
        verified_login(self.client, self.actor)

    def proposal(self, days):
        tenant = Tenant.objects.get(pk=self.tenant.pk)
        return review_change(self.actor, form_token(self.actor), {**values_for(tenant), GRACE_FIELD: days})[0]

    def save_grace(self, days):
        return confirm_change(self.actor, self.proposal(days), time.time(), 1)

    def issue(self, month):
        return issue_invoice(self.tenant, self.customer.pk, date(2024, month, 1), self.actor)[0]

    def reconcile_on(self, day, job=None):
        if job is None:
            job = NetworkJob.objects.create(tenant=self.tenant, customer=self.customer)
        with patch('core.network.timezone.localdate', return_value=day):
            self.assertEqual(run_job(job.pk).status, 'confirmed')
        self.customer.refresh_from_db()
        return self.customer.service_state

    def test_grace_boundary_leap_month_partial_payment_and_reversal(self):
        self.save_grace(3)
        invoice = self.issue(2)
        self.assertEqual(invoice.due_date, date(2024, 2, 29))
        self.assertFalse(NetworkJob.objects.exists())
        with patch('core.network.timezone.localdate', return_value=date(2024, 3, 3)):
            self.assertEqual(invoice.status, 'overdue')
        self.assertEqual(self.reconcile_on(date(2024, 3, 3)), 'active')
        partial, _ = record_payment(self.tenant, invoice.pk, 10000, uuid.uuid4(), self.actor)
        self.assertEqual(self.reconcile_on(date(2024, 3, 4), NetworkJob.objects.latest('pk')), 'isolated')
        queued = NetworkJob.objects.create(tenant=self.tenant, customer=self.customer)
        record_payment(self.tenant, invoice.pk, invoice.total - partial.amount, uuid.uuid4(), self.actor)
        self.assertEqual(self.reconcile_on(date(2024, 3, 4), queued), 'active')
        reverse_payment(self.tenant, partial.pk, 'Koreksi pembayaran uji tenggang', self.actor)
        self.assertEqual(self.reconcile_on(date(2024, 3, 4), NetworkJob.objects.latest('pk')), 'isolated')

    def test_existing_invoice_and_replay_do_not_inherit_new_grace(self):
        legacy = self.issue(1)
        self.save_grace(30)
        invoice = self.issue(2)
        self.assertEqual(invoice_grace_days(invoice), 30)
        self.assertEqual(self.issue(1).pk, legacy.pk)
        self.assertFalse(InvoiceIsolationTerms.objects.filter(invoice=legacy).exists())
        self.assertEqual(self.reconcile_on(date(2024, 2, 1)), 'isolated')
        self.save_grace(0)
        self.assertEqual(invoice_grace_days(self.issue(2)), 30)
        self.assertFalse(InvoiceIsolationTerms.objects.filter(invoice=self.issue(3)).exists())

    def test_approved_batches_freeze_terms_while_changed_preview_is_stale(self):
        filters = {'state': 'active', 'plan': None, 'area': ''}
        def preview(month):
            return create_preview(self.actor, date(2024, month, 1), filters)
        def confirm(batch):
            return confirm_batch(self.actor, batch.pk, time.time(), 1)

        old = confirm(preview(1))
        self.save_grace(3)
        approved = confirm(preview(2))
        pending = preview(3)
        self.save_grace(10)
        self.assertEqual(confirm(pending).status, 'stale')
        self.assertEqual(process_batch(old.pk), 1)
        self.assertEqual(process_batch(approved.pk), 1)
        self.assertEqual(invoice_grace_days(Invoice.objects.get(period=date(2024, 1, 1))), 0)
        self.assertEqual(invoice_grace_days(Invoice.objects.get(period=date(2024, 2, 1))), 3)

    def test_scheduled_batch_freezes_grace_at_queue_time(self):
        token, _ = billing_scheduler.review_change(self.actor, {'enabled': True, 'day': 1,
            'state': 'active', 'plan': None, 'area': ''}, 0)
        schedule = billing_scheduler.confirm_change(self.actor, token, time.time(), 1)
        self.save_grace(5)
        run = billing_scheduler.run_schedule(schedule.pk, datetime(2027, 2, 1, 10, tzinfo=billing_scheduler.WIB))
        self.save_grace(1)
        self.assertEqual(process_batch(run.batch_id), 1)
        self.assertEqual(invoice_grace_days(Invoice.objects.get(period=run.period)), 5)

    def test_prorated_psb_quote_rechecks_grace_before_issuing(self):
        order = installed_order(self.actor, technician(self.tenant))
        period, start = date(2024, 2, 1), date(2024, 2, 12)
        quote = activation_quote(order, period, service_start=start)[1]
        self.save_grace(3)
        with self.assertRaises(ValidationError):
            activate_installation(self.actor, order.pk, period, quote, time.time(), 1, service_start=start)
        self.assertFalse(Invoice.objects.exists())
        order.refresh_from_db()
        quote = activation_quote(order, period, service_start=start)[1]
        activated = activate_installation(self.actor, order.pk, period, quote, time.time(), 1, service_start=start)
        self.assertEqual(invoice_grace_days(activated.invoice), 3)
        self.assertIn('Prorata', activated.invoice.lines.get().description)

    def test_settings_grace_is_reviewed_bounded_and_rolled_back_with_audit(self):
        for invalid in (-1, 31, True, '3'):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                self.proposal(invalid)
        token = self.proposal(3)
        with patch('core.billing_settings.audit', side_effect=RuntimeError), self.assertRaises(RuntimeError):
            confirm_change(self.actor, token, time.time(), 1)
        self.assertFalse(TenantIsolationPolicy.objects.exists())
        self.assertTrue(confirm_change(self.actor, token, time.time(), 1))
        with self.assertRaises(SettingsConflict):
            confirm_change(self.actor, token, time.time(), 1)
        self.assertFalse(self.save_grace(3))
        self.assertFalse(TenantIsolationPolicy.objects.filter(tenant=self.tenants[1]).exists())

    def test_invoice_and_grace_snapshot_roll_back_on_late_failure(self):
        self.save_grace(3)
        with patch('core.billing.audit', side_effect=RuntimeError), self.assertRaises(RuntimeError):
            self.issue(2)
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(InvoiceIsolationTerms.objects.exists())
