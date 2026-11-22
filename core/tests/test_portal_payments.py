import uuid
from datetime import date, datetime, timedelta, timezone

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.models import Customer, Invoice, Membership, Payment, PaymentReversal, Plan, Tenant
from .auth_helpers import verified_login


class PortalPaymentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(name="ISP portal pembayaran")
        outside = Tenant.objects.create(name="ISP lain")
        users = get_user_model().objects
        cls.portal_user = users.create_user(username="pelanggan-portal")
        cls.operator = users.create_user(username="operator-internal")
        Membership.objects.create(user=cls.operator, tenant=cls.tenant, role="owner")
        cls.invoice = cls.invoice_for(cls.tenant, "sendiri", "081200002001")
        cls.same_isp = cls.invoice_for(cls.tenant, "pelanggan-lain", "081200002002")
        cls.foreign = cls.invoice_for(outside, "isp-lain", "081200002003")
        Membership.objects.create(user=cls.portal_user, tenant=cls.tenant, role="customer", customer=cls.invoice.customer)
        cls.timestamp = datetime(2026, 11, 22, 17, 10, tzinfo=timezone.utc)

    @staticmethod
    def invoice_for(tenant, code, phone):
        plan = Plan.objects.create(tenant=tenant, name="Paket uji", speed_mbps=25, price=150000)
        customer = Customer.objects.create(tenant=tenant, plan=plan, name=code, code=code, username=code, phone=phone)
        return Invoice.objects.create(tenant=tenant, customer=customer, period=date(2026, 11, 1),
            due_date=date(2026, 11, 25), customer_name=code, customer_address="Alamat fiktif", subtotal=150000, total=150000)

    def setUp(self):
        verified_login(self.client, self.portal_user)
        self.url = reverse("portal_payments")

    def payment(self, amount, *, invoice=None, tenant=None, reversed_at=None):
        invoice = invoice or self.invoice
        payment = Payment.objects.create(tenant=tenant or invoice.tenant, invoice=invoice, recorded_by=self.operator,
            event_key=uuid.uuid4(), amount=amount, created_at=self.timestamp, note="Catatan uji di kuitansi")
        if reversed_at:
            PaymentReversal.objects.create(payment=payment, recorded_by=self.operator, created_at=reversed_at,
                                           reason="Koreksi transaksi fiktif")
        return payment

    def receipt(self, payment, *, pdf=False):
        return reverse("payment_receipt_pdf" if pdf else "payment_receipt", args=[payment.invoice_id, payment.pk])

    def test_portal_link_and_scope_keep_unrelated_payments_out_of_rows_and_totals(self):
        own = self.payment(30000)
        outside_customer = self.payment(44000, invoice=self.same_isp)
        outside_isp = self.payment(35000, invoice=self.foreign)
        malformed = self.payment(5000, invoice=self.foreign, tenant=self.tenant)
        self.assertContains(self.client.get(reverse("portal")), f'href="{self.url}"')

        response = self.client.get(self.url, {"customer": self.same_isp.customer_id, "tenant": self.foreign.tenant_id})
        self.assertEqual([row.pk for row in response.context["page"]], [own.pk])
        self.assertEqual(response.context["payment_totals"], {"gross": 30000, "reversed": 0, "valid": 30000})
        self.assertContains(response, self.receipt(own))
        for unrelated in (outside_customer, outside_isp, malformed):
            self.assertNotContains(response, self.receipt(unrelated))
            self.assertEqual(self.client.get(self.receipt(unrelated)).status_code, 404)
        self.assertNotContains(response, "operator-internal")
        self.assertNotContains(response, "Catatan uji di kuitansi")

    def test_status_filter_displays_wib_and_keeps_existing_receipts_and_all_time_totals(self):
        valid = self.payment(45000)
        reversed_payment = self.payment(20000, reversed_at=self.timestamp + timedelta(minutes=30))
        for state, expected in (("recorded", valid), ("reversed", reversed_payment)):
            with self.subTest(state=state):
                response = self.client.get(self.url, {"state": state})
                self.assertEqual([payment.pk for payment in response.context["page"]], [expected.pk])
                self.assertEqual(response.context["payment_totals"], {"gross": 65000, "reversed": 20000, "valid": 45000})
                self.assertContains(response, "23 Nov 2026 00:10 WIB")
                self.assertContains(response, self.receipt(expected))
        self.assertContains(response, "23 Nov 2026 00:40 WIB")
        self.assertContains(self.client.get(self.receipt(reversed_payment)), "Direversal · transaksi tidak berlaku")
        pdf = self.client.get(self.receipt(reversed_payment, pdf=True))
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.content.startswith(b"%PDF"))

    def test_pagination_has_stable_order_for_identical_timestamps_and_retains_filter(self):
        payments = [self.payment(100) for _ in range(31)]
        response = self.client.get(self.url, {"state": "recorded", "size": 10, "page": 2})
        page = response.context["page"]
        self.assertEqual([payment.pk for payment in page], [item.pk for item in reversed(payments)][10:20])
        self.assertEqual(page.paginator.count, 31)
        self.assertContains(response, 'state=recorded&amp;size=10&amp;page=3')
        self.assertEqual(response.context["payment_totals"]["valid"], 3100)

    def test_empty_filtered_list_and_invalid_parameters_show_clear_fallbacks(self):
        self.assertContains(self.client.get(self.url), "Belum ada pembayaran tercatat.")
        self.payment(40000)
        filtered = self.client.get(self.url, {"state": "reversed", "size": 25, "page": 5})
        self.assertContains(filtered, "Belum ada pembayaran dengan status ini.")
        self.assertContains(filtered, f'href="{self.url}?size=25"')
        fallback = self.client.get(self.url, {"state": "unknown", "size": 1000, "page": "invalid"})
        self.assertEqual(fallback.context["payment_state"], "")
        self.assertEqual(fallback.context["page"].paginator.per_page, 10)
        self.assertEqual(fallback.context["page"].paginator.count, 1)

    def test_query_count_does_not_increase_with_rendered_payments(self):
        for index in range(3):
            self.payment(100, reversed_at=self.timestamp if index % 2 else None)
        with CaptureQueriesContext(connection) as short:
            first = self.client.get(self.url)
        for index in range(57):
            self.payment(100, reversed_at=self.timestamp if index % 2 else None)
        with CaptureQueriesContext(connection) as long:
            later = self.client.get(self.url, {"size": 50})
        self.assertEqual((len(first.context["page"]), len(later.context["page"])), (3, 50))
        self.assertEqual(len(short), len(long))

    def test_history_requires_a_customer_session_and_accepts_only_get(self):
        self.assertRedirects(Client().get(self.url), f'{reverse("login")}?next={self.url}', fetch_redirect_response=False)
        self.assertEqual(self.client.post(self.url).status_code, 405)
        operator = Client()
        verified_login(operator, self.operator)
        self.assertEqual(operator.get(self.url).status_code, 403)
