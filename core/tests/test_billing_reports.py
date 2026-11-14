import csv
import io
import uuid
from datetime import date, datetime
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.test import Client, TransactionTestCase, override_settings
from core.access import save_role
from core.billing import issue_invoice, record_payment, reverse_payment
from core.billing_reports import BillingReport, WIB, report_snapshot, validate_range
from core.models import AccessRole, AuditEvent, Customer, Invoice, Membership, NetworkJob, Payment, PaymentReversal
from .auth_helpers import verified_login
from .test_workflow import fixtures

START, END = date(2026, 10, 1), date(2026, 10, 31)


def stamp(day, hour=12, minute=0, second=0):
    return datetime.combine(date.fromisoformat(day), datetime.min.time(), WIB).replace(hour=hour, minute=minute, second=second)


@override_settings(SIMULATION=True)
class BillingReportTests(TransactionTestCase):
    def setUp(self):
        clock = patch('django.utils.timezone.now', return_value=stamp('2026-11-14'))
        clock.start(); self.addCleanup(clock.stop)
        self.tenants, self.owners, self.customers = fixtures()
        self.actor, self.tenant, self.customer = self.owners[0], self.tenants[0], self.customers[0]
        self.old, _ = issue_invoice(self.tenant, self.customer.pk, date(2026, 9, 1), self.actor)
        self.new, _ = issue_invoice(self.tenant, self.customer.pk, date(2026, 10, 1), self.actor)
        Invoice.objects.filter(pk=self.old.pk).update(issued_at=stamp('2026-09-30'))
        Invoice.objects.filter(pk=self.new.pk).update(issued_at=stamp('2026-10-15'))
        self.past, _ = record_payment(self.tenant, self.old.pk, 100000, uuid.uuid4(), self.actor, 'Penerimaan September')
        self.first, _ = record_payment(self.tenant, self.old.pk, 50000, uuid.uuid4(), self.actor, 'Batas awal Oktober')
        self.last, _ = record_payment(self.tenant, self.new.pk, 200000, uuid.uuid4(), self.actor, 'Batas akhir Oktober')
        Payment.objects.filter(pk=self.past.pk).update(created_at=stamp('2026-09-30', 13))
        Payment.objects.filter(pk=self.first.pk).update(created_at=stamp('2026-10-01', 0))
        Payment.objects.filter(pk=self.last.pk).update(created_at=stamp('2026-10-31', 23, 59, 59))
        reversal = reverse_payment(self.tenant, self.past.pk, 'Koreksi penerimaan bulan sebelumnya', self.actor)
        PaymentReversal.objects.filter(pk=reversal.pk).update(created_at=stamp('2026-11-01', 0))
        verified_login(self.client, self.actor)

    def report(self, start=START, end=END):
        return BillingReport(self.actor, start, end)

    def get(self, mode='cash', **values):
        return self.client.get('/reports/billing/', {'start': START.isoformat(), 'end': END.isoformat(), 'mode': mode, **values})

    def export(self, mode='cash', **values):
        return self.client.get('/reports/billing/export.csv', {'start': START.isoformat(), 'end': END.isoformat(), 'mode': mode, **values})

    def test_roll_forward_matches_independent_invoice_balances(self):
        summary = self.report().summary()
        self.assertEqual(summary, {'opening': 149750, 'issued': 249750, 'received': 250000, 'reversed': 0,
            'net': 250000, 'closing': 149500, 'actual': 149500, 'overdue': 99750, 'reconciles': True})

    def test_later_reversal_does_not_rewrite_previous_period(self):
        september = self.report(date(2026, 9, 1), date(2026, 9, 30)).summary()
        self.assertEqual((september['received'], september['reversed'], september['closing']), (100000, 0, 149750))
        november = self.report(date(2026, 11, 1), date(2026, 11, 14)).summary()
        self.assertEqual((november['received'], november['reversed'], november['net']), (0, 100000, -100000))
        self.assertEqual((november['opening'], november['closing']), (149500, 249500))

    def test_cutoffs_include_start_and_last_second_exclude_next_midnight(self):
        entries = list(self.report().rows('cash'))
        self.assertEqual(len(entries), 2)
        self.assertEqual(sum(row['signed_amount'] for row in entries), 250000)
        self.assertEqual({row['entry_id'] for row in entries}, {self.first.pk, self.last.pk})
        nov = list(self.report(date(2026, 11, 1), date(2026, 11, 1)).rows('cash'))
        self.assertEqual([(row['entry_type'], row['signed_amount']) for row in nov], [('reversal', -100000)])

    def test_report_timezone_is_wib_even_under_utc_override(self):
        from django.utils import timezone
        with timezone.override('UTC'):
            report = self.report()
            self.assertEqual(report.lower.utcoffset().total_seconds(), 25200)
            self.assertEqual(report.summary()['received'], 250000)

    def test_as_of_receivables_include_prior_invoices_and_ignore_later_reversal(self):
        rows = {row.pk: row for row in self.report().rows('receivables')}
        self.assertEqual(set(rows), {self.old.pk, self.new.pk})
        self.assertEqual((rows[self.old.pk].paid_at_end, rows[self.old.pk].remaining), (150000, 99750))
        self.assertEqual(rows[self.new.pk].remaining, 49750)

    def test_invoice_list_uses_issue_time_instead_of_service_period(self):
        Invoice.objects.filter(pk=self.new.pk).update(period=date(2027, 1, 1))
        self.assertEqual([invoice.pk for invoice in self.report().rows('invoices')], [self.new.pk])

    def test_future_issued_invoice_is_excluded_from_historical_balance(self):
        future, _ = issue_invoice(self.tenant, self.customer.pk, date(2026, 12, 1), self.actor)
        Invoice.objects.filter(pk=future.pk).update(issued_at=stamp('2026-11-02'))
        self.assertEqual(self.report().summary()['closing'], 149500)
        self.assertEqual(self.report().rows('invoices').count(), 1)

    def test_fully_paid_invoice_disappears_only_after_payment_cutoff(self):
        payment, _ = record_payment(self.tenant, self.new.pk, 49750, uuid.uuid4(), self.actor)
        Payment.objects.filter(pk=payment.pk).update(created_at=stamp('2026-11-02'))
        self.assertIn(self.new.pk, [row.pk for row in self.report().rows('receivables')])
        november = self.report(date(2026, 11, 1), date(2026, 11, 14))
        self.assertNotIn(self.new.pk, [row.pk for row in november.rows('receivables')])

    def test_price_and_customer_edits_do_not_replace_issued_snapshots(self):
        Customer.objects.filter(pk=self.customer.pk).update(name='Nama baru')
        plan = self.customer.plan; plan.price = 999999; plan.save()
        row = self.report().rows('invoices').get()
        self.assertEqual((row.customer_name, row.total), ('Pelanggan 0', 249750))
        self.assertTrue(all(row['customer_label'] == 'Pelanggan 0' for row in self.report().rows('cash')))

    def test_same_period_payment_and_reversal_both_appear_with_signed_amounts(self):
        PaymentReversal.objects.filter(payment=self.past).update(created_at=stamp('2026-10-31', 23))
        report = self.report()
        self.assertEqual(report.summary()['reversed'], 100000)
        self.assertEqual(sum(row['signed_amount'] for row in report.rows('cash')), 150000)
        self.assertEqual(report.rows('cash').count(), 3)

    def test_foreign_tenant_entries_cannot_change_summary_or_rows(self):
        invoice, _ = issue_invoice(self.tenants[1], self.customers[1].pk, date(2026, 10, 1), self.owners[1])
        Invoice.objects.filter(pk=invoice.pk).update(issued_at=stamp('2026-10-02'), customer_name='PRIVATE FOREIGN ISP')
        payment, _ = record_payment(self.tenants[1], invoice.pk, 1234, uuid.uuid4(), self.owners[1], 'PRIVATE FOREIGN ISP')
        Payment.objects.filter(pk=payment.pk).update(created_at=stamp('2026-10-03'))
        self.assertEqual(self.report().summary()['received'], 250000)
        for mode in ['cash', 'invoices', 'receivables']:
            self.assertNotContains(self.get(mode, tenant=self.tenants[1].pk), 'PRIVATE FOREIGN ISP')

    def test_invalid_date_ranges_fail_closed_in_view_and_export(self):
        for values in [{'start': 'x'}, {'start': '2026-10-32'}, {'start': '2026-11-02'},
                       {'end': '2026-11-15'}, {'start': '1999-01-01'}, {'start': '2024-01-01'}, {'end': ''}]:
            with self.subTest(values=values):
                self.assertEqual(self.get(**values).status_code, 400)
                self.assertEqual(self.export(**values).status_code, 400)

    def test_leap_year_366_day_limit_is_inclusive(self):
        validate_range(date(2024, 1, 1), date(2024, 12, 31), date(2026, 11, 14))
        with self.assertRaises(ValidationError): validate_range(date(2024, 1, 1), date(2025, 1, 1))
        with self.assertRaises(ValidationError): validate_range(datetime(2026, 10, 1), END)

    def test_empty_default_month_has_useful_state_and_zero_period_receipts(self):
        response = self.client.get('/reports/billing/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['report'].start, date(2026, 11, 1))
        self.assertEqual(response.context['summary']['received'], 0)
        self.assertEqual(self.get(start='2026-11-03', end='2026-11-04').context['page'].paginator.count, 0)

    def test_unknown_mode_and_mutating_methods_are_rejected(self):
        self.assertEqual(self.get(mode='../../secret').status_code, 404)
        self.assertEqual(self.export(mode='bad').status_code, 404)
        self.assertEqual(self.client.post('/reports/billing/').status_code, 405)
        self.assertEqual(self.client.post('/reports/billing/export.csv').status_code, 405)

    def test_anonymous_and_default_presets_without_report_right_are_denied(self):
        self.assertEqual(Client().get('/reports/billing/').status_code, 302)
        for role in ['ops', 'cashier', 'noc', 'customer', 'technician']:
            user = get_user_model().objects.create_user('report-'+role)
            Membership.objects.create(user=user, tenant=self.tenant, role=role,
                customer=self.customer if role == 'customer' else None)
            client = Client(); verified_login(client, user)
            self.assertEqual(client.get('/reports/billing/').status_code, 403)
            self.assertEqual(client.get('/reports/billing/export.csv').status_code, 403)

    def test_finance_preset_can_read_and_export(self):
        user = get_user_model().objects.create_user('report-finance')
        Membership.objects.create(user=user, tenant=self.tenant, role='finance')
        client = Client(); verified_login(client, user)
        self.assertEqual(client.get('/reports/billing/').status_code, 200)
        self.assertEqual(client.get('/reports/billing/export.csv').status_code, 200)

    def test_custom_role_dependencies_and_separate_export_permission(self):
        for rights in [{'billing_reports'}, {'invoices', 'export_billing_reports'}]:
            with self.assertRaises(ValidationError): save_role(self.actor, 'Invalid reports', rights)
        role = save_role(self.actor, 'Read reports', {'invoices', 'billing_reports'})
        user = get_user_model().objects.create_user('report-reader')
        Membership.objects.create(user=user, tenant=self.tenant, role='ops', access_role=role)
        client = Client(); verified_login(client, user)
        response = client.get('/reports/billing/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Ekspor CSV')
        self.assertEqual(client.get('/reports/billing/export.csv').status_code, 403)
        AccessRole.objects.filter(pk=role.pk).update(capabilities=['invoices'])
        self.assertEqual(client.get('/reports/billing/').status_code, 403)

    def test_domain_checks_inactive_actor_and_malformed_custom_permissions(self):
        get_user_model().objects.filter(pk=self.actor.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied): self.report()
        role = AccessRole.objects.create(tenant=self.tenant, name='Malformed reports', capabilities=['billing_reports'])
        user = get_user_model().objects.create_user('malformed-report-role')
        Membership.objects.create(user=user, tenant=self.tenant, role='ops', access_role=role)
        with self.assertRaises(PermissionDenied): BillingReport(user, START, END)

    def test_csv_escapes_text_formulas_and_preserves_negative_numeric_rupiah(self):
        PaymentReversal.objects.filter(payment=self.past).update(reason='=1+1')
        response = self.export(start='2026-11-01', end='2026-11-14')
        rows = list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))
        self.assertEqual((rows[1][5], rows[1][6]), ('-100000', "'=1+1"))
        self.assertTrue(rows[1][0].endswith('+07:00'))
        self.assertEqual(len(rows), 2)

    def test_csv_invoice_balance_uses_historical_cutoff(self):
        rows = list(csv.reader(io.StringIO(self.export('receivables').content.decode('utf-8-sig'))))
        self.assertEqual({int(row[7]) for row in rows[1:]}, {99750, 49750})

    def test_export_logs_scope_and_does_not_silently_truncate(self):
        response = self.export()
        self.assertEqual(response.status_code, 200)
        event = AuditEvent.objects.get(action='report.exported')
        self.assertEqual((event.tenant_id, event.actor_id, event.target), (self.tenant.pk, self.actor.pk, 'cash'))
        with patch('core.report_views.MAX_EXPORT_ROWS', 1):
            self.assertEqual(self.export().status_code, 400)
        self.assertEqual(AuditEvent.objects.filter(action='report.exported').count(), 1)

    def test_pagination_keeps_range_mode_and_totals(self):
        for _ in range(12):
            payment, _ = record_payment(self.tenant, self.old.pk, 1, uuid.uuid4(), self.actor)
            Payment.objects.filter(pk=payment.pk).update(created_at=stamp('2026-10-12'))
        response = self.get(size=10)
        self.assertEqual(response.context['page'].paginator.count, 14)
        self.assertContains(response, 'start=2026-10-01&amp;end=2026-10-31&amp;mode=cash&amp;size=10&amp;page=2')
        second = self.get(size=10, page=2)
        self.assertEqual(len(second.context['page']), 4)
        self.assertEqual(second.context['summary'], response.context['summary'])
        exported = list(csv.reader(io.StringIO(self.export(page=2, size=10).content.decode('utf-8-sig'))))
        self.assertEqual(len(exported), 15)

    def test_web_note_is_plaintext_and_report_get_has_no_business_effects(self):
        Payment.objects.filter(pk=self.first.pk).update(note='<script>note()</script>')
        models = [Invoice, Payment, PaymentReversal, Customer, NetworkJob, AuditEvent]
        before = [model.objects.count() for model in models]
        for mode in ['cash', 'invoices', 'receivables']:
            response = self.get(mode)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, '<script>note()</script>')
        self.assertContains(self.get(), '&lt;script&gt;note()&lt;/script&gt;')
        self.assertEqual([model.objects.count() for model in models], before)

    def test_reconciliation_flags_inconsistent_host_inserted_timestamps(self):
        Invoice.objects.filter(pk=self.old.pk).update(issued_at=stamp('2026-11-02'))
        self.assertFalse(self.report().summary()['reconciles'])
        self.assertContains(self.get(), 'Saldo perlu diperiksa')

    def test_snapshot_scope_is_explicit_and_does_not_leak_read_only_mode(self):
        with report_snapshot():
            self.assertTrue(connection.in_atomic_block)
            self.assertEqual(self.report().summary()['closing'], 149500)
            if connection.vendor == 'postgresql':
                with connection.cursor() as cursor:
                    cursor.execute('SHOW transaction_isolation'); self.assertEqual(cursor.fetchone()[0], 'repeatable read')
                    cursor.execute('SHOW transaction_read_only'); self.assertEqual(cursor.fetchone()[0], 'on')
        self.assertTrue(connection.get_autocommit())
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SHOW transaction_read_only'); self.assertEqual(cursor.fetchone()[0], 'off')
        with transaction.atomic():
            with self.assertRaises(ValidationError):
                with report_snapshot(): pass
