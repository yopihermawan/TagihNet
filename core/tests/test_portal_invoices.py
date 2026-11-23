import uuid
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.billing import record_payment, reverse_payment
from core.models import Customer, Invoice, Membership, Plan, Tenant
from .auth_helpers import verified_login


class PortalInvoiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(name="ISP portal")
        foreign = Tenant.objects.create(name="ISP pembanding")
        cls.plan = Plan.objects.create(tenant=cls.tenant, name="Paket portal", speed_mbps=25, price=150000)
        foreign_plan = Plan.objects.create(tenant=foreign, name="Paket pembanding", speed_mbps=50, price=250000)
        cls.customer = cls.create_customer(cls.tenant, cls.plan, "pemilik", "081200001001")
        cls.same_isp = cls.create_customer(cls.tenant, cls.plan, "tetangga", "081200001002")
        cls.foreign = cls.create_customer(foreign, foreign_plan, "asing", "081200001003")
        users = get_user_model().objects
        cls.user = users.create_user(username="pemilik-portal")
        cls.operator = users.create_user(username="operator-portal")
        Membership.objects.create(user=cls.user, tenant=cls.tenant, customer=cls.customer, role="customer")
        Membership.objects.create(user=cls.operator, tenant=cls.tenant, role="owner")

    @staticmethod
    def create_customer(tenant, plan, code, phone):
        return Customer.objects.create(tenant=tenant, plan=plan, code=code, name=code, username=code, phone=phone)

    def setUp(self):
        verified_login(self.client, self.user)
        self.url = reverse("portal")
        self.today = timezone.localdate()

    def add_invoices(self, start, end, *, customer=None, due=None):
        customer = customer or self.customer
        return Invoice.objects.bulk_create([
            Invoice(tenant=customer.tenant, customer=customer, period=date(2020 + index // 12, index % 12 + 1, 1),
                    due_date=due or self.today, customer_name=customer.name, customer_address="Alamat fiktif",
                    subtotal=150000, total=150000)
            for index in range(start, end)
        ])

    def test_paginates_scoped_invoices_and_preserves_status_and_size_in_links(self):
        self.add_invoices(0, 61)
        other_invoice, = self.add_invoices(0, 1, customer=self.same_isp)
        foreign_invoice, = self.add_invoices(0, 1, customer=self.foreign)
        response = self.client.get(self.url, {"state": "issued", "size": 25, "page": 2,
                                             "customer": self.same_isp.pk, "tenant": self.foreign.tenant_id})
        page = response.context["page"]
        self.assertEqual((page.paginator.count, page.start_index(), page.end_index()), (61, 26, 50))
        self.assertTrue(all(row["invoice"].customer_id == self.customer.pk for row in response.context["invoice_rows"]))
        self.assertNotContains(response, other_invoice.number)
        self.assertNotContains(response, foreign_invoice.number)
        self.assertContains(response, 'state=issued&amp;size=25&amp;page=3')
        self.assertContains(response, "Detail " + response.context["invoice_rows"][0]["invoice"].number)

    def test_filters_share_wib_due_boundary_and_ignore_reversed_payments(self):
        issued, partial = self.add_invoices(0, 2)
        overdue, paid = self.add_invoices(2, 4, due=self.today - timedelta(days=1))
        record_payment(self.tenant, partial.pk, 40000, uuid.uuid4(), self.operator)
        record_payment(self.tenant, overdue.pk, 20000, uuid.uuid4(), self.operator)
        record_payment(self.tenant, paid.pk, 150000, uuid.uuid4(), self.operator)
        reversed_payment, _ = record_payment(self.tenant, issued.pk, 150000, uuid.uuid4(), self.operator)
        reverse_payment(self.tenant, reversed_payment.pk, "Koreksi transaksi uji portal", self.operator)

        for state, invoice, balance, label in (
            ("issued", issued, 150000, "Terbit"), ("partial", partial, 110000, "Dibayar sebagian"),
            ("overdue", overdue, 130000, "Menunggak"), ("paid", paid, 0, "Lunas"),
        ):
            with self.subTest(state=state):
                response = self.client.get(self.url, {"state": state})
                row, = response.context["invoice_rows"]
                self.assertEqual((row["invoice"].pk, row["balance"], row["status"], row["label"]),
                                 (invoice.pk, balance, state, label))
                self.assertContains(response, f'<option value="{state}" selected>{label}</option>', html=True)
                self.assertEqual(response.context["billing_summary"], {
                    "total": 390000, "overdue": 130000, "next_due": self.today, "next_invoice_id": issued.pk,
                })

    def test_summary_scopes_all_open_invoices_independently_of_the_filter_and_page(self):
        overdue = self.add_invoices(0, 12, due=self.today - timedelta(days=1))
        upcoming, paid = self.add_invoices(12, 14)
        self.add_invoices(0, 1, customer=self.same_isp)
        self.add_invoices(0, 1, customer=self.foreign)
        record_payment(self.tenant, overdue[0].pk, 50000, uuid.uuid4(), self.operator)
        record_payment(self.tenant, paid.pk, 150000, uuid.uuid4(), self.operator)

        response = self.client.get(self.url, {"state": "paid", "page": 2})
        self.assertEqual([row["invoice"].pk for row in response.context["invoice_rows"]], [paid.pk])
        self.assertEqual(response.context["billing_summary"], {
            "total": 1900000, "overdue": 1750000, "next_due": self.today, "next_invoice_id": upcoming.pk,
        })
        self.assertContains(response, f'href="{reverse("invoice_detail", args=[upcoming.pk])}">Lihat faktur berikutnya')

    def test_next_invoice_ignores_overdue_and_paid_items_and_breaks_due_date_ties(self):
        self.add_invoices(0, 1, due=self.today - timedelta(days=1))
        paid, first, second = self.add_invoices(1, 4)
        later, = self.add_invoices(4, 5, due=self.today + timedelta(days=1))
        record_payment(self.tenant, paid.pk, 150000, uuid.uuid4(), self.operator)

        summary = self.client.get(self.url).context["billing_summary"]
        self.assertEqual((summary["next_due"], summary["next_invoice_id"]), (self.today, first.pk))
        for invoice in (first, second, later):
            record_payment(self.tenant, invoice.pk, 150000, uuid.uuid4(), self.operator)
        remaining = self.client.get(self.url)
        self.assertEqual(remaining.context["billing_summary"], {
            "total": 150000, "overdue": 150000, "next_due": None, "next_invoice_id": None,
        })
        self.assertNotContains(remaining, "Lihat faktur berikutnya")

    def test_summary_is_zero_for_empty_and_settled_histories(self):
        empty = {"total": 0, "overdue": 0, "next_due": None, "next_invoice_id": None}
        self.assertEqual(self.client.get(self.url).context["billing_summary"], empty)
        invoice, = self.add_invoices(0, 1)
        record_payment(self.tenant, invoice.pk, 150000, uuid.uuid4(), self.operator)
        response = self.client.get(self.url)
        self.assertEqual(response.context["billing_summary"], empty)
        self.assertContains(response, "Belum ada jatuh tempo berikutnya.")

    def test_empty_filter_and_invalid_parameters_have_safe_fallbacks(self):
        self.assertContains(self.client.get(self.url), "Belum ada tagihan.")
        self.add_invoices(0, 2)
        filtered = self.client.get(self.url, {"state": "paid", "size": 25, "page": 8})
        self.assertContains(filtered, "Belum ada tagihan dengan status ini.")
        self.assertContains(filtered, f'href="{self.url}?size=25"')
        fallback = self.client.get(self.url, {"state": "unknown", "size": 200, "page": "invalid"})
        self.assertEqual(fallback.context["invoice_state"], "")
        self.assertEqual(fallback.context["page"].paginator.per_page, 10)
        self.assertEqual(len(fallback.context["invoice_rows"]), 2)

    def test_queries_do_not_grow_with_the_invoice_count_or_page_size(self):
        self.add_invoices(0, 3)
        with CaptureQueriesContext(connection) as small:
            first_page = self.client.get(self.url)
        self.add_invoices(3, 60)
        with CaptureQueriesContext(connection) as large:
            full_page = self.client.get(self.url, {"size": 50})
        self.assertEqual((len(first_page.context["invoice_rows"]), len(full_page.context["invoice_rows"])), (3, 50))
        self.assertEqual(len(small), len(large))
