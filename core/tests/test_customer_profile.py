import uuid
from datetime import date, timedelta
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from core.access import save_role
from core.billing import audit, issue_invoice, record_payment, reverse_payment
from core.customer_profile import TABS
from core.models import AccessRole, AuditEvent, Customer, Invoice, MFADevice, Membership, NetworkJob, Payment, PaymentReversal, Ticket, WorkEvent
from core.operations import add_comment, create_ticket
from .auth_helpers import verified_login
from .test_workflow import fixtures


@override_settings(SIMULATION=True)
class CustomerProfileTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        cls.customer, cls.actor = cls.customers[0], cls.owners[0]
        cls.invoices = [issue_invoice(cls.tenants[0], cls.customer.pk, date(2026, month, 1), cls.actor)[0]
            for month in [2, 3, 4]]
        cls.first_payment, _ = record_payment(cls.tenants[0], cls.invoices[0].pk, 100000, uuid.uuid4(), cls.actor, 'Pembayaran profil pertama')
        cls.reversed_payment, _ = record_payment(cls.tenants[0], cls.invoices[0].pk, 50000, uuid.uuid4(), cls.actor, 'Pembayaran yang dikoreksi')
        reverse_payment(cls.tenants[0], cls.reversed_payment.pk, 'Koreksi profil pelanggan fiktif', cls.actor)
        record_payment(cls.tenants[0], cls.invoices[1].pk, cls.invoices[1].total, uuid.uuid4(), cls.actor)
        cls.second_customer = cls.add_customer(1)
        cls.ticket = create_ticket(cls.actor, cls.customer.pk, 'Gangguan profil pelanggan', 'Koneksi terputus di lokasi fiktif', 'connection')
        create_ticket(cls.actor, cls.second_customer.pk, 'Gangguan pelanggan lain', 'Deskripsi pelanggan lain', 'connection')
        audit(cls.tenants[0], cls.actor, 'customer.created', cls.customer.pk, 'Identitas pelanggan dibuat')

    @classmethod
    def add_customer(cls, index):
        return Customer.objects.create(tenant=cls.tenants[0], plan=cls.customer.plan, name=f'Profil tambahan {index}',
            code=f'PROFILE-{index}', username=f'profile-{index}', phone=f'0812888{index:05}', address='Alamat fiktif', area='Dago')

    def setUp(self):
        verified_login(self.client, self.actor)
        self.url = f'/customers/{self.customer.pk}/'

    def get(self, tab='summary', **params):
        return self.client.get(self.url, {'tab': tab, **params})

    def custom_client(self, rights, name='profile-role'):
        role = save_role(self.actor, name, rights)
        user = get_user_model().objects.create_user(name)
        Membership.objects.create(user=user, tenant=self.tenants[0], role='ops', access_role=role)
        client = Client(); verified_login(client, user)
        return client, role

    def test_default_summary_has_live_ledger_balance_without_loading_all_invoice_rows(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['tab'], 'summary')
        self.assertEqual(response.context['balance'], 399500)
        self.assertContains(response, 'Rp399.500')
        self.assertNotIn('page', response.context)
        self.assertNotContains(response, self.invoices[0].number)

    def test_owner_has_seven_accessible_navigation_links(self):
        response = self.get()
        self.assertEqual([tab for tab, _ in response.context['tabs']], [tab for tab, _, _ in TABS])
        self.assertContains(response, 'aria-label="Tab pelanggan"')
        self.assertContains(response, 'aria-current="page"', count=1)
        self.assertNotContains(response, 'role="tab"')  # Links remain native server navigation.

    def test_every_owner_tab_renders_its_actual_content(self):
        headings = {'summary': 'Ringkasan pelanggan', 'service': 'Riwayat rekonsiliasi jaringan',
            'invoices': 'Faktur pelanggan', 'payments': 'Pembayaran pelanggan', 'tickets': self.ticket.title,
            'location': 'Lokasi pelanggan', 'history': 'Identitas pelanggan dibuat'}
        for tab, heading in headings.items():
            with self.subTest(tab=tab): self.assertContains(self.get(tab), heading)

    def test_unknown_or_path_like_tab_is_404(self):
        for value in ['unknown', '../location', '../../etc/passwd', 'payments.html', 'SUMMARY']:
            self.assertEqual(self.get(value).status_code, 404)

    def test_tabs_are_get_only(self):
        self.assertEqual(self.client.post(self.url, {'tab': 'payments'}).status_code, 405)

    def test_anonymous_and_denied_presets_cannot_read_any_tab(self):
        self.assertEqual(Client().get(self.url+'?tab=payments').status_code, 302)
        for role in ['cashier', 'noc', 'customer', 'technician']:
            user = get_user_model().objects.create_user('profile-'+role)
            Membership.objects.create(user=user, tenant=self.tenants[0], role=role,
                customer=self.customer if role == 'customer' else None)
            client = Client(); verified_login(client, user)
            for tab, _, _ in TABS:
                self.assertEqual(client.get(self.url, {'tab': tab}).status_code, 403)

    def test_cross_tenant_customer_is_404_in_every_tab(self):
        for tab, _, _ in TABS:
            self.assertEqual(self.client.get(f'/customers/{self.customers[1].pk}/', {'tab': tab}).status_code, 404)
        self.assertEqual(self.client.get('/customers/999999/').status_code, 404)

    def test_contacts_only_custom_role_cannot_read_restricted_tabs(self):
        client, _ = self.custom_client({'customers'})
        response = client.get(self.url)
        self.assertEqual([tab for tab, _ in response.context['tabs']], ['summary', 'service', 'location'])
        # The existing customers capability still permits summary receivables.
        self.assertEqual(response.context['balance'], 399500)
        for tab in ['invoices', 'payments', 'tickets', 'history']:
            self.assertEqual(client.get(self.url, {'tab': tab}).status_code, 403)
        for tab in ['summary', 'service', 'location']:
            self.assertEqual(client.get(self.url, {'tab': tab}).status_code, 200)

    def test_finance_can_read_financial_tabs_but_not_tickets_or_audit(self):
        user = get_user_model().objects.create_user('profile-finance')
        Membership.objects.create(user=user, tenant=self.tenants[0], role='finance')
        client = Client(); verified_login(client, user)
        for tab in ['invoices', 'payments']: self.assertEqual(client.get(self.url, {'tab': tab}).status_code, 200)
        for tab in ['tickets', 'history']: self.assertEqual(client.get(self.url, {'tab': tab}).status_code, 403)

    def test_revoked_custom_tab_permission_is_checked_on_next_request(self):
        client, role = self.custom_client({'customers', 'invoices'})
        self.assertEqual(client.get(self.url, {'tab': 'payments'}).status_code, 200)
        AccessRole.objects.filter(pk=role.pk).update(capabilities=['customers'])
        self.assertEqual(client.get(self.url, {'tab': 'payments'}).status_code, 403)

    def test_invoice_rows_match_partial_paid_and_reversed_ledger(self):
        response = self.get('invoices')
        rows = {row['invoice'].pk: row for row in response.context['invoice_rows']}
        self.assertEqual(len(rows), 3)
        self.assertEqual((rows[self.invoices[0].pk]['paid'], rows[self.invoices[0].pk]['balance']), (100000, 149750))
        self.assertEqual(rows[self.invoices[1].pk]['status'], 'paid')
        self.assertEqual(rows[self.invoices[2].pk]['balance'], 249750)
        for invoice in self.invoices:
            self.assertEqual(rows[invoice.pk]['balance'], Invoice.objects.get(pk=invoice.pk).balance)

    def test_future_partial_and_unpaid_statuses_are_distinct(self):
        future = timezone.localdate()+timedelta(days=7)
        Invoice.objects.filter(pk__in=[self.invoices[0].pk, self.invoices[2].pk]).update(due_date=future)
        rows = {row['invoice'].pk: row for row in self.get('invoices').context['invoice_rows']}
        self.assertEqual(rows[self.invoices[0].pk]['status'], 'partial')
        self.assertEqual(rows[self.invoices[2].pk]['status'], 'issued')

    def test_plan_price_changes_do_not_recalculate_issued_invoice_totals(self):
        plan = self.customer.plan; plan.price = 900000; plan.save()
        self.assertContains(self.get('service'), 'Rp900.000')
        for row in self.get('invoices').context['invoice_rows']:
            self.assertEqual(row['invoice'].total, 249750)

    def test_payment_summary_includes_reversals_without_deleting_original_entries(self):
        response = self.get('payments')
        self.assertEqual(response.context['payment_totals'], {'gross': 399750, 'reversed': 50000, 'valid': 349750})
        self.assertEqual(response.context['page'].paginator.count, 3)
        self.assertContains(response, 'Koreksi profil pelanggan fiktif')
        self.assertContains(response, 'Pembayaran yang dikoreksi')

    def test_other_customer_and_foreign_tenant_payments_are_excluded(self):
        for customer, tenant, owner in [(self.second_customer, self.tenants[0], self.actor),
                                      (self.customers[1], self.tenants[1], self.owners[1])]:
            invoice, _ = issue_invoice(tenant, customer.pk, date(2026, 2, 1), owner)
            record_payment(tenant, invoice.pk, 1234, uuid.uuid4(), owner, 'PRIVATE OTHER CUSTOMER')
        response = self.get('payments')
        self.assertEqual(response.context['page'].paginator.count, 3)
        self.assertNotContains(response, 'PRIVATE OTHER CUSTOMER')
        self.assertEqual(self.get('invoices').context['page'].paginator.count, 3)

    def test_tickets_are_scoped_and_do_not_embed_private_comments(self):
        add_comment(self.actor, self.ticket.pk, 'Komentar internal tidak ditampilkan di daftar profil', True, uuid.uuid4())
        response = self.get('tickets')
        self.assertEqual(response.context['page'].paginator.count, 1)
        self.assertContains(response, self.ticket.title)
        self.assertNotContains(response, 'Gangguan pelanggan lain')
        self.assertNotContains(response, 'Komentar internal tidak ditampilkan')

    def test_history_filters_action_and_target_to_prevent_numeric_id_collisions(self):
        audit(self.tenants[0], self.actor, 'invoice.issued', self.customer.pk, 'PRIVATE INVOICE EVENT')
        audit(self.tenants[0], self.actor, 'customer.updated', self.second_customer.pk, 'PRIVATE CUSTOMER EVENT')
        audit(self.tenants[1], self.owners[1], 'customer.updated', self.customer.pk, 'PRIVATE TENANT EVENT')
        response = self.get('history')
        self.assertEqual(response.context['page'].paginator.count, 1)
        self.assertContains(response, 'Identitas pelanggan dibuat')
        for text in ['PRIVATE INVOICE EVENT', 'PRIVATE CUSTOMER EVENT', 'PRIVATE TENANT EVENT']:
            self.assertNotContains(response, text)

    def test_payment_note_and_reversal_reason_are_plaintext(self):
        Payment.objects.filter(pk=self.reversed_payment.pk).update(note='<script>note()</script>')
        PaymentReversal.objects.filter(payment=self.reversed_payment).update(reason='<script>reason()</script>')
        response = self.get('payments')
        self.assertContains(response, '&lt;script&gt;note()&lt;/script&gt;')
        self.assertContains(response, '&lt;script&gt;reason()&lt;/script&gt;')
        self.assertNotContains(response, '<script>note()</script>')

    def test_location_encodes_external_search_and_does_not_invent_coordinates(self):
        Customer.objects.filter(pk=self.customer.pk).update(address='Jl. Fiktif & <img src=x>', area='<script>area</script>')
        response = self.get('location')
        self.assertContains(response, 'Belum diverifikasi')
        self.assertContains(response, '&lt;img src=x&gt;')
        self.assertContains(response, 'rel="noopener noreferrer"')
        self.assertNotContains(response, '<script>area</script>')

    def test_empty_customer_has_useful_empty_states_and_zero_financial_totals(self):
        url = f'/customers/{self.second_customer.pk}/'
        # The second fixture customer has a ticket, but no financial or network records.
        for tab, text in [('invoices', 'Belum ada faktur'), ('payments', 'Belum ada pembayaran'),
                          ('service', 'Belum ada pekerjaan jaringan'), ('history', 'Belum ada perubahan pelanggan')]:
            self.assertContains(self.client.get(url, {'tab': tab}), text)
        response = self.client.get(url, {'tab': 'payments'})
        self.assertEqual(response.context['payment_totals'], {'gross': 0, 'reversed': 0, 'valid': 0})

    def test_invoice_pagination_and_links_keep_selected_tab_and_size(self):
        for year in [2023, 2024]:
            for month in range(1, 13): issue_invoice(self.tenants[0], self.customer.pk, date(year, month, 1), self.actor)
        response = self.get('invoices', size=10, page=2)
        self.assertEqual(response.context['page'].paginator.count, 27)
        self.assertEqual(response.context['page'].start_index(), 11)
        self.assertEqual(len(response.context['invoice_rows']), 10)
        self.assertContains(response, 'tab=invoices&amp;size=10&amp;page=3')
        self.assertEqual(self.get('invoices', size=999).context['profile_size'], '10')

    def test_payment_totals_are_all_pages_and_query_count_does_not_grow_per_row(self):
        for _ in range(23): record_payment(self.tenants[0], self.invoices[2].pk, 1, uuid.uuid4(), self.actor)
        with CaptureQueriesContext(connection) as small: response = self.get('payments', size=10)
        with CaptureQueriesContext(connection) as large: full = self.get('payments', size=50)
        self.assertEqual(len(small), len(large))
        self.assertEqual(response.context['payment_totals'], full.context['payment_totals'])
        self.assertEqual(response.context['payment_totals']['valid'], 349773)
        self.assertEqual(response.context['page'].paginator.count, 26)

    def test_invoice_query_count_does_not_grow_per_row(self):
        for year in [2023, 2024]:
            for month in range(1, 13): issue_invoice(self.tenants[0], self.customer.pk, date(year, month, 1), self.actor)
        with CaptureQueriesContext(connection) as small: self.get('invoices', size=10)
        with CaptureQueriesContext(connection) as large: self.get('invoices', size=50)
        self.assertEqual(len(small), len(large))

    def test_network_and_history_pagination_do_not_drop_old_records(self):
        for _ in range(21):
            NetworkJob.objects.create(tenant=self.tenants[0], customer=self.customer)
            audit(self.tenants[0], self.actor, 'customer.updated', self.customer.pk, 'Perubahan fiktif')
        service, history = self.get('service', page=2), self.get('history', page=2)
        self.assertEqual(service.context['page'].paginator.count, 25)
        self.assertEqual(history.context['page'].paginator.count, 22)
        self.assertEqual(service.context['page'].start_index(), 11)
        self.assertEqual(history.context['page'].start_index(), 11)

    def test_all_tabs_are_read_only_for_business_records(self):
        models = [Customer, Invoice, Payment, PaymentReversal, NetworkJob, Ticket, WorkEvent, AuditEvent, MFADevice]
        before = [model.objects.count() for model in models]
        for tab, _, _ in TABS: self.get(tab)
        self.assertEqual([model.objects.count() for model in models], before)

    def test_summary_and_payment_tabs_refresh_after_a_new_reversal(self):
        before = self.get().context['balance']
        reverse_payment(self.tenants[0], self.first_payment.pk, 'Pembatalan pembayaran lanjutan', self.actor)
        self.assertEqual(self.get().context['balance'], before+100000)
        self.assertEqual(self.get('payments').context['payment_totals']['valid'], 249750)
