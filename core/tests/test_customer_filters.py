import csv
import io
import uuid
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from core.billing import issue_invoice, record_payment, reverse_payment
from core.customer_import import confirm_import, preview_import
from core.forms import CustomerForm
from core.models import AuditEvent, Customer, Invoice, Membership, MonitoringDevice, MonitoringPoll, NetworkJob, Payment
from .auth_helpers import verified_login
from .test_customer_import import source
from .test_workflow import fixtures
import time

TODAY = date(2026, 11, 14)


@override_settings(SIMULATION=True)
class CustomerFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        cls.nas = MonitoringDevice.objects.create(tenant=cls.tenants[0], name='NAS pusat', enabled=False)
        cls.other_nas = MonitoringDevice.objects.create(tenant=cls.tenants[1], name='NAS rahasia ISP lain')
        cls.second_nas = MonitoringDevice.objects.create(tenant=cls.tenants[0], name='NAS cabang')
        cls.cases = {}
        for index, (name, offset, paid) in enumerate([
            ('overdue', -1, 0), ('today', 0, 0), ('tomorrow', 1, 0), ('day7', 7, 0),
            ('day8', 8, 0), ('paid', -2, 'full'), ('partial', -3, 100000), ('reversed', -4, 'reverse'),
        ]):
            customer = cls.add_customer(index, nas=cls.nas if index % 2 == 0 else cls.second_nas)
            invoice, _ = issue_invoice(cls.tenants[0], customer.pk, date(2026, 11, 1), cls.owners[0])
            Invoice.objects.filter(pk=invoice.pk).update(due_date=TODAY+timedelta(days=offset))
            if paid:
                payment, _ = record_payment(cls.tenants[0], invoice.pk,
                    invoice.total if paid in {'full', 'reverse'} else paid, uuid.uuid4(), cls.owners[0])
                if paid == 'reverse':
                    reverse_payment(cls.tenants[0], payment.pk, 'Koreksi pembayaran fiktif', cls.owners[0])
            cls.cases[name] = customer

    @classmethod
    def add_customer(cls, index, **changes):
        return Customer.objects.create(tenant=cls.tenants[0], code=f'FILTER-{index}', name=f'Pelanggan Filter {index:03}',
            phone=f'0812777{index:05}', username=f'filter-{index}', address='Alamat fiktif', area='Dago',
            plan=cls.customers[0].plan, **changes)

    def setUp(self):
        verified_login(self.client, self.owners[0])
        clock = patch('django.utils.timezone.now', return_value=datetime(2026, 11, 14, 5, tzinfo=datetime_timezone.utc))
        clock.start(); self.addCleanup(clock.stop)

    def ids(self, **params):
        params = {'size': 50, **params}
        response = self.client.get('/customers/', params)
        self.assertEqual(response.status_code, 200)
        return {item.pk for item in response.context['page']}

    def expected(self, *names):
        return {self.cases[name].pk for name in names}

    def data(self, customer, **changes):
        return {**{key: getattr(customer, key) for key in ['name', 'email', 'phone', 'username', 'address', 'area']},
            'plan': customer.plan_id, 'nas': customer.nas_id or '', **changes}

    def test_overdue_uses_unpaid_balance_and_reversal(self):
        self.assertEqual(self.ids(due='overdue'), self.expected('overdue', 'partial', 'reversed'))

    def test_today_is_not_overdue(self):
        self.assertEqual(self.ids(due='today'), self.expected('today'))

    def test_next_seven_days_excludes_today_and_day_eight(self):
        self.assertEqual(self.ids(due='next7'), self.expected('tomorrow', 'day7'))

    def test_clear_includes_no_invoice_and_fully_paid_customers(self):
        self.assertEqual(self.ids(due='clear'), {self.customers[0].pk, self.cases['paid'].pk})

    def test_any_unpaid_includes_all_outstanding_dates(self):
        self.assertEqual(self.ids(due='unpaid'), self.expected('overdue', 'today', 'tomorrow', 'day7', 'day8', 'partial', 'reversed'))

    def test_financial_summary_matches_ledger_for_all_rows(self):
        response = self.client.get('/customers/', {'size': 50})
        for customer in response.context['page']:
            invoices = list(Invoice.objects.filter(tenant=self.tenants[0], customer=customer))
            pending = [invoice for invoice in invoices if invoice.balance > 0]
            self.assertEqual(customer.receivable_amount, sum(invoice.balance for invoice in pending))
            self.assertEqual(customer.oldest_due, min((invoice.due_date for invoice in pending), default=None))
        partial = next(c for c in response.context['page'] if c.pk == self.cases['partial'].pk)
        self.assertEqual(partial.receivable_amount, 149750)

    def test_multiple_invoices_and_payments_are_not_multiplied_by_joins(self):
        customer = self.cases['overdue']
        first = Invoice.objects.get(customer=customer)
        second, _ = issue_invoice(self.tenants[0], customer.pk, date(2026, 12, 1), self.owners[0])
        for amount in [10000, 20000]:
            record_payment(self.tenants[0], first.pk, amount, uuid.uuid4(), self.owners[0])
        record_payment(self.tenants[0], second.pk, 40000, uuid.uuid4(), self.owners[0])
        response = self.client.get('/customers/', {'q': customer.code})
        row = response.context['page'][0]
        self.assertEqual(row.receivable_amount, first.total+second.total-70000)
        self.assertEqual(row.oldest_due, TODAY-timedelta(days=1))
        self.assertEqual(self.ids(q=customer.code, due='overdue'), {customer.pk})

    def test_pay_and_reverse_are_reflected_on_the_next_request(self):
        invoice = Invoice.objects.get(customer=self.cases['overdue'])
        payment, _ = record_payment(self.tenants[0], invoice.pk, invoice.total, uuid.uuid4(), self.owners[0])
        self.assertNotIn(invoice.customer_id, self.ids(due='overdue'))
        reverse_payment(self.tenants[0], payment.pk, 'Koreksi fiktif', self.owners[0])
        self.assertIn(invoice.customer_id, self.ids(due='overdue'))

    def test_wib_date_changes_at_1700_utc(self):
        with patch('django.utils.timezone.now', return_value=datetime(2026, 11, 13, 16, 59, tzinfo=datetime_timezone.utc)):
            self.assertEqual(self.ids(due='today'), self.expected('overdue'))
        with patch('django.utils.timezone.now', return_value=datetime(2026, 11, 13, 17, 0, tzinfo=datetime_timezone.utc)):
            self.assertEqual(self.ids(due='today'), self.expected('today'))

    def test_nas_filter_includes_disabled_devices_and_has_no_online_claim(self):
        expected = set(Customer.objects.filter(tenant=self.tenants[0], nas=self.nas).values_list('pk', flat=True))
        self.assertEqual(self.ids(nas=str(self.nas.pk)), expected)
        response = self.client.get('/customers/', {'nas': self.nas.pk})
        self.assertContains(response, 'NAS pusat')
        self.assertContains(response, 'nonaktif')
        self.assertContains(response, 'tidak menunjukkan sesi pelanggan sedang online')

    def test_unassigned_filter_includes_legacy_customers(self):
        self.assertEqual(self.ids(nas='unassigned'), {self.customers[0].pk})

    def test_nas_and_due_combine_with_existing_search_area_plan_state_sort(self):
        Customer.objects.filter(pk=self.cases['partial'].pk).update(service_state='isolated')
        self.assertEqual(self.ids(nas=str(self.nas.pk), due='overdue', area='Dago', state='isolated',
            plan=str(self.customers[0].plan_id), q='Filter', sort='-name'), self.expected('partial'))

    def test_other_tenant_and_unknown_nas_return_no_rows_or_names(self):
        self.assertEqual(self.ids(nas=str(self.other_nas.pk)), set())
        self.assertNotContains(self.client.get('/customers/'), self.other_nas.name)
        self.assertEqual(self.ids(nas='999999999'), set())

    def test_invalid_filters_do_not_expand_to_all_customers(self):
        for nas in ['bad', '-1', '0', '１２', '9'*5000, str(2**63)]:
            with self.subTest(nas=nas[:30]): self.assertEqual(self.ids(nas=nas), set())
        self.assertEqual(self.ids(due='bad'), set())

    def test_invalid_plan_id_does_not_raise_or_broaden_list_or_export(self):
        for plan in ['bad', '-1', '0', '１２', '9'*5000, str(2**63)]:
            with self.subTest(plan=plan[:30]):
                self.assertEqual(self.ids(plan=plan), set())
                response = self.client.get('/customers/export/', {'plan': plan})
                self.assertEqual(len(list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))), 1)

    def test_pagination_preserves_nas_due_sort_and_size_in_links(self):
        for index in range(100, 112): self.add_customer(index, nas=self.nas)
        response = self.client.get('/customers/', {'nas': self.nas.pk, 'due': 'clear', 'sort': '-name', 'size': 10})
        self.assertEqual(response.context['page'].paginator.count, 12)
        self.assertEqual(len(response.context['page']), 10)
        self.assertContains(response, f'nas={self.nas.pk}&amp;due=clear&amp;sort=-name&amp;size=10&amp;page=2')
        second = self.client.get('/customers/', {'nas': self.nas.pk, 'due': 'clear', 'sort': '-name', 'size': 10, 'page': 2})
        self.assertEqual(len(second.context['page']), 2)

    def test_export_uses_same_filters_without_page_limit(self):
        params = {'nas': self.nas.pk, 'due': 'overdue', 'sort': '-name', 'size': 10, 'page': 2}
        response = self.client.get('/customers/export/', params)
        rows = list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))
        self.assertEqual({row[0] for row in rows[1:]}, {self.cases[n].code for n in ['overdue', 'partial']})
        self.assertEqual(len(rows[0]), 6)  # Existing export columns remain compatible.

    def test_summary_has_bounded_queries_as_page_size_grows(self):
        for index in range(100, 126): self.add_customer(index, nas=self.nas)
        with CaptureQueriesContext(connection) as small: self.client.get('/customers/', {'size': 10})
        with CaptureQueriesContext(connection) as large: self.client.get('/customers/', {'size': 50})
        self.assertEqual(len(small), len(large))

    def test_mapping_form_scopes_choices_and_keeps_optional_selection(self):
        form = CustomerForm(tenant=self.tenants[0])
        self.assertEqual(set(form.fields['nas'].queryset), {self.nas, self.second_nas})
        form = CustomerForm(self.data(self.customers[0]), tenant=self.tenants[0], instance=self.customers[0])
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data['nas'])

    def test_mapping_post_preserves_finances_and_audits_before_after(self):
        customer = self.cases['overdue']
        before = list(Invoice.objects.filter(customer=customer).values())
        payment_count, jobs = Payment.objects.count(), NetworkJob.objects.count()
        response = self.client.post(f'/customers/{customer.pk}/edit/', self.data(customer, nas=self.second_nas.pk))
        self.assertRedirects(response, f'/customers/{customer.pk}/')
        customer.refresh_from_db(); self.assertEqual(customer.nas_id, self.second_nas.pk)
        event = AuditEvent.objects.filter(action='customer.updated').get()
        self.assertIn(f'NAS: {self.nas.pk} → {self.second_nas.pk}', event.detail)
        self.assertEqual(list(Invoice.objects.filter(customer=customer).values()), before)
        self.assertEqual((Payment.objects.count(), NetworkJob.objects.count()), (payment_count, jobs))
        self.assertFalse(MonitoringPoll.objects.exists())

    def test_cross_tenant_nas_post_is_rejected_without_leaking_name(self):
        customer = self.customers[0]
        response = self.client.post(f'/customers/{customer.pk}/edit/', self.data(customer, nas=self.other_nas.pk))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors.get('nas'))
        self.assertNotContains(response, self.other_nas.name)
        customer.refresh_from_db(); self.assertIsNone(customer.nas_id)

    def test_legacy_invalid_mapping_does_not_disclose_foreign_device_name(self):
        customer = self.customers[0]
        Customer.objects.filter(pk=customer.pk).update(nas=self.other_nas)
        self.assertNotContains(self.client.get('/customers/'), self.other_nas.name)
        self.assertNotContains(self.client.get(f'/customers/{customer.pk}/'), self.other_nas.name)

    def test_model_validation_rejects_cross_tenant_mapping_and_fk_is_protected(self):
        customer = self.customers[0]; customer.nas = self.other_nas
        with self.assertRaises(ValidationError): customer.full_clean()
        with self.assertRaises(ProtectedError): self.nas.delete()

    def test_clear_mapping_and_customer_detail_are_plaintext(self):
        customer = self.cases['overdue']
        self.nas.name = '<script>unsafe</script>'; self.nas.save()
        self.assertContains(self.client.get(f'/customers/{customer.pk}/'), '&lt;script&gt;unsafe&lt;/script&gt;')
        self.assertNotContains(self.client.get(f'/customers/{customer.pk}/'), '<script>unsafe</script>')
        self.client.post(f'/customers/{customer.pk}/edit/', self.data(customer, nas=''))
        customer.refresh_from_db(); self.assertIsNone(customer.nas_id)

    def test_read_only_finance_and_denied_presets_cannot_write_mapping(self):
        for role in ['finance', 'cashier', 'noc', 'technician', 'customer']:
            user = get_user_model().objects.create_user('filter-'+role)
            Membership.objects.create(user=user, tenant=self.tenants[0], role=role,
                customer=self.customers[0] if role == 'customer' else None)
            client = Client(); verified_login(client, user)
            self.assertEqual(client.get('/customers/', {'nas': self.nas.pk, 'due': 'overdue'}).status_code,
                200 if role == 'finance' else 403)
            self.assertEqual(client.post(f'/customers/{self.customers[0].pk}/edit/',
                self.data(self.customers[0], nas=self.nas.pk)).status_code, 403)

    def test_mapping_requires_csrf(self):
        client = Client(enforce_csrf_checks=True); verified_login(client, self.owners[0])
        self.assertEqual(client.post(f'/customers/{self.customers[0].pk}/edit/',
            self.data(self.customers[0], nas=self.nas.pk)).status_code, 403)

    def test_gets_do_not_map_collect_or_create_financial_records(self):
        before = (Customer.objects.filter(nas__isnull=False).count(), Invoice.objects.count(), NetworkJob.objects.count(),
            AuditEvent.objects.count(), MonitoringPoll.objects.count())
        self.client.get('/customers/', {'nas': self.nas.pk, 'due': 'overdue'})
        self.client.get(f'/customers/{self.customers[0].pk}/edit/')
        after = (Customer.objects.filter(nas__isnull=False).count(), Invoice.objects.count(), NetworkJob.objects.count(),
            AuditEvent.objects.count(), MonitoringPoll.objects.count())
        self.assertEqual(before, after)

    def test_csv_import_keeps_strict_schema_and_new_customers_unassigned(self):
        batch = preview_import(self.owners[0], source(self.tenants[0]), uuid.uuid4())
        batch = confirm_import(self.owners[0], batch.pk, time.time(), 1)
        self.assertEqual(batch.status, 'completed')
        self.assertIsNone(batch.rows.get().customer.nas_id)
