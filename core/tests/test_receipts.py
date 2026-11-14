"""Payment receipts preserve transaction evidence and inherit invoice scope."""
import uuid
from datetime import date, datetime, timezone as dt_timezone
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from pypdf import PdfReader

from core.billing import issue_invoice, record_payment, reverse_payment
from core.models import AccessRole, AuditEvent, Customer, Invoice, Membership, NetworkJob, Payment, PaymentReversal
from core.receipts import receipt_context, render_receipt, terbilang
from .auth_helpers import verified_login
from .test_workflow import fixtures


class RupiahWordsTests(SimpleTestCase):
    def test_units_and_teens(self):
        for number, words in ((1, "satu"), (10, "sepuluh"), (11, "sebelas"), (12, "dua belas"), (19, "sembilan belas")):
            with self.subTest(number=number):
                self.assertEqual(terbilang(number), words+" rupiah")

    def test_tens_and_hundreds(self):
        for number, words in ((20, "dua puluh"), (99, "sembilan puluh sembilan"), (100, "seratus"),
                              (101, "seratus satu"), (111, "seratus sebelas"), (200, "dua ratus"),
                              (999, "sembilan ratus sembilan puluh sembilan")):
            with self.subTest(number=number):
                self.assertEqual(terbilang(number), words+" rupiah")

    def test_thousands_and_payment_limits(self):
        for number, words in ((1000, "seribu"), (1001, "seribu satu"), (1100, "seribu seratus"),
                              (2000, "dua ribu"), (1000000, "satu juta"), (200000000, "dua ratus juta"),
                              (249750, "dua ratus empat puluh sembilan ribu tujuh ratus lima puluh")):
            with self.subTest(number=number):
                self.assertEqual(terbilang(number), words+" rupiah")

    def test_bigint_range_and_scales(self):
        for power, name in ((9, "miliar"), (12, "triliun"), (15, "kuadriliun"), (18, "kuintiliun")):
            self.assertEqual(terbilang(10**power+1), "satu "+name+" satu rupiah")
        result = terbilang(9223372036854775807)
        self.assertTrue(result.startswith("sembilan kuintiliun"))
        self.assertTrue(result.endswith("delapan ratus tujuh rupiah"))
        self.assertNotIn("  ", result)

    def test_reject_noninteger_or_outside_database_range(self):
        for value in (0, -1, True, 1.5, "1", None, 9223372036854775808):
            with self.subTest(value=value), self.assertRaises(ValueError):
                terbilang(value)


@override_settings(SIMULATION=True)
class ReceiptTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        cls.invoice, _ = issue_invoice(cls.tenants[0], cls.customers[0].pk, date(2026, 2, 1), cls.owners[0])
        cls.other_invoice, _ = issue_invoice(cls.tenants[1], cls.customers[1].pk, date(2026, 2, 1), cls.owners[1])
        cls.payment, _ = record_payment(cls.tenants[0], cls.invoice.pk, 100000, uuid.uuid4(), cls.owners[0])
        cls.other_payment, _ = record_payment(cls.tenants[1], cls.other_invoice.pk, 50000, uuid.uuid4(), cls.owners[1])

    def setUp(self):
        verified_login(self.client, self.owners[0])

    def url(self, pdf=False, invoice=None, payment=None):
        return reverse("payment_receipt_pdf" if pdf else "payment_receipt",
                       args=[(invoice or self.invoice).pk, (payment or self.payment).pk])

    def login_role(self, role, customer=None, access_role=None):
        user = get_user_model().objects.create_user(username=f"receipt-{role}")
        Membership.objects.create(user=user, tenant=self.tenants[0], role=role,
                                  customer=customer, access_role=access_role)
        verified_login(self.client, user)
        return user

    def pdf_text(self, response):
        self.assertEqual(response.status_code, 200)
        return "\n".join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages)

    def test_anonymous_requires_login_on_both_routes(self):
        for pdf in (False, True):
            self.assertRedirects(Client().get(self.url(pdf)), "/login/?next="+self.url(pdf), fetch_redirect_response=False)

    def test_invoice_roles_can_read_html_and_pdf(self):
        for role in ("owner", "ops", "finance", "cashier"):
            with self.subTest(role=role):
                self.login_role(role)
                self.assertContains(self.client.get(self.url()), "KUITANSI PEMBAYARAN")
                self.assertIn("Rp100.000", self.pdf_text(self.client.get(self.url(True))))

    def test_noc_and_technician_denied(self):
        for role in ("noc", "technician"):
            self.login_role(role)
            for pdf in (False, True):
                self.assertEqual(self.client.get(self.url(pdf)).status_code, 403)

    def test_custom_role_requires_invoice_capability(self):
        custom = AccessRole.objects.create(tenant=self.tenants[0], name="Penerima", capabilities=["customers"])
        self.login_role("ops", access_role=custom)
        for pdf in (False, True):
            self.assertEqual(self.client.get(self.url(pdf)).status_code, 403)
        custom.capabilities = ["invoices"]
        custom.save()
        for pdf in (False, True):
            self.assertEqual(self.client.get(self.url(pdf)).status_code, 200)

    def test_foreign_tenant_and_payment_substitution_hidden(self):
        for pdf in (False, True):
            self.assertEqual(self.client.get(self.url(pdf, invoice=self.other_invoice, payment=self.other_payment)).status_code, 404)
            self.assertEqual(self.client.get(self.url(pdf, payment=self.other_payment)).status_code, 404)

    def test_same_tenant_wrong_invoice_hidden(self):
        second, _ = issue_invoice(self.tenants[0], self.customers[0].pk, date(2026, 3, 1), self.owners[0])
        for pdf in (False, True):
            self.assertEqual(self.client.get(self.url(pdf, invoice=second)).status_code, 404)

    def test_portal_reads_own_receipts_only(self):
        self.login_role("customer", customer=self.customers[0])
        self.assertContains(self.client.get(self.url()), "Seratus ribu rupiah", html=False)
        self.assertIn("Rp100.000", self.pdf_text(self.client.get(self.url(True))))
        for pdf in (False, True):
            self.assertEqual(self.client.get(self.url(pdf, invoice=self.other_invoice, payment=self.other_payment)).status_code, 404)

    def test_portal_same_isp_other_customer_hidden(self):
        customer = self.customers[0]
        other = Customer.objects.create(tenant=customer.tenant, plan=customer.plan, code="RECEIPT-2",
            username="receipt-2", name="Rahasia pelanggan lain", phone="08123456789", address="Rahasia")
        invoice, _ = issue_invoice(self.tenants[0], other.pk, date(2026, 2, 1), self.owners[0])
        payment, _ = record_payment(self.tenants[0], invoice.pk, 12345, uuid.uuid4(), self.owners[0])
        self.login_role("customer", customer=customer)
        for pdf in (False, True):
            response = self.client.get(self.url(pdf, invoice=invoice, payment=payment))
            self.assertEqual(response.status_code, 404)
            self.assertNotContains(response, "Rahasia pelanggan lain", status_code=404)

    def test_revoked_session_cannot_read(self):
        member = Membership.objects.get(user=self.owners[0])
        member.session_version += 1
        member.save()
        for pdf in (False, True):
            self.assertEqual(self.client.get(self.url(pdf)).status_code, 302)

    def test_inactive_user_cannot_read(self):
        self.owners[0].is_active = False
        self.owners[0].save()
        for pdf in (False, True):
            self.assertEqual(self.client.get(self.url(pdf)).status_code, 302)

    def test_get_only(self):
        for pdf in (False, True):
            for method in ("post", "put", "patch", "delete", "head"):
                self.assertEqual(getattr(self.client, method)(self.url(pdf)).status_code, 405)

    def test_reading_receipts_does_not_mutate_ledger_or_audit(self):
        models = (Invoice, Payment, PaymentReversal, Customer, AuditEvent, NetworkJob)
        before = [list(model.objects.order_by("pk").values()) for model in models]
        for _ in range(2):
            for pdf in (False, True):
                self.assertEqual(self.client.get(self.url(pdf)).status_code, 200)
        self.assertEqual(before, [list(model.objects.order_by("pk").values()) for model in models])

    def test_partial_payment_is_one_transaction_without_paid_claim(self):
        response = self.client.get(self.url())
        self.assertContains(response, "Rp100.000")
        self.assertContains(response, "tidak menyatakan faktur telah lunas")
        self.assertNotContains(response, "Rp249.750")
        self.assertEqual(self.invoice.balance, 149750)

    def test_each_payment_has_distinct_receipt_number_and_amount(self):
        second, _ = record_payment(self.tenants[0], self.invoice.pk, 20000, uuid.uuid4(), self.owners[0])
        first = self.client.get(self.url()).context
        response = self.client.get(self.url(payment=second))
        self.assertNotEqual(first["receipt_number"], response.context["receipt_number"])
        self.assertContains(response, "Rp20.000")
        self.assertNotContains(response, "Rp100.000")

    def test_number_uses_wib_transaction_date_and_stays_stable(self):
        Payment.objects.filter(pk=self.payment.pk).update(created_at=datetime(2026, 1, 31, 18, 2, 3, tzinfo=dt_timezone.utc))
        response = self.client.get(self.url())
        number = f"TN-KW-20260201-{self.payment.pk:06d}"
        self.assertEqual(response.context["receipt_number"], number)
        self.assertContains(response, "01 Feb 2026 01:02:03")
        payment = Payment.objects.get(pk=self.payment.pk)
        self.assertEqual(receipt_context(payment, datetime(2027, 1, 1, tzinfo=dt_timezone.utc))["receipt_number"], number)

    def test_customer_snapshot_is_preserved_issuer_is_current(self):
        original = self.invoice.customer_name
        Customer.objects.filter(pk=self.customers[0].pk).update(name="Nama berubah", address="Alamat berubah")
        self.tenants[0].name = "ISP nama terkini"
        self.tenants[0].save()
        response = self.client.get(self.url())
        self.assertContains(response, original)
        self.assertContains(response, "Jl. Contoh")
        self.assertNotContains(response, "Nama berubah")
        self.assertNotContains(response, "Alamat berubah")
        self.assertContains(response, "ISP nama terkini")
        self.assertContains(response, "mengikuti data saat dicetak")

    def test_reversal_updates_existing_receipt_without_losing_original(self):
        before = self.client.get(self.url()).context["receipt_number"]
        reverse_payment(self.tenants[0], self.payment.pk, "Koreksi transfer simulasi yang keliru", self.owners[0])
        response = self.client.get(self.url())
        self.assertEqual(before, response.context["receipt_number"])
        self.assertContains(response, "Direversal · transaksi tidak berlaku")
        self.assertContains(response, "Koreksi transfer simulasi yang keliru")
        self.assertContains(response, "Rp100.000")
        text = self.pdf_text(self.client.get(self.url(True)))
        self.assertIn("DIREVERSAL", text)
        self.assertIn("Rp100.000", text)
        self.assertIn("Koreksi transfer simulasi yang keliru", text)
        self.assertEqual(Payment.objects.get(pk=self.payment.pk).amount, 100000)

    def test_simulation_notice_cannot_disappear_after_config_change(self):
        with override_settings(SIMULATION=False):
            self.assertContains(self.client.get(self.url()), "SIMULASI - BUKAN BUKTI PEMBAYARAN NYATA")
            self.assertIn("SIMULASI - BUKAN BUKTI PEMBAYARAN NYATA", self.pdf_text(self.client.get(self.url(True))))

    def test_html_escapes_customer_notes_and_reversal(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(customer_name='<script>alert("customer")</script>')
        Payment.objects.filter(pk=self.payment.pk).update(note='<img src=x onerror="alert(1)">')
        reverse_payment(self.tenants[0], self.payment.pk, '<script>alert("reason")</script>', self.owners[0])
        response = self.client.get(self.url())
        self.assertContains(response, "&lt;script&gt;")
        self.assertContains(response, "&lt;img")
        self.assertNotContains(response, '<img src=x')
        self.assertNotContains(response, '<script>alert(')

    def test_html_and_pdf_are_private_no_store(self):
        for pdf in (False, True):
            response = self.client.get(self.url(pdf))
            self.assertIn("no-store", response["Cache-Control"])
            self.assertIn("private", response["Cache-Control"])
            self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_pdf_headers_title_page_size_and_text(self):
        response = self.client.get(self.url(True))
        self.assertEqual(response["Content-Type"], "application/pdf")
        number = self.client.get(self.url()).context["receipt_number"]
        self.assertEqual(response["Content-Disposition"], f'inline; filename="{number}.pdf"')
        pdf = PdfReader(BytesIO(response.content))
        self.assertEqual(pdf.metadata.title, number)
        self.assertEqual(len(pdf.pages), 1)
        for page in pdf.pages:
            self.assertAlmostEqual(float(page.mediabox.width), 595.276, delta=1)
            self.assertAlmostEqual(float(page.mediabox.height), 841.89, delta=1)
        text = pdf.pages[0].extract_text()
        for value in (number, self.invoice.number, "seratus ribu rupiah", "Tanpa catatan", "Dicetak:", "Dicatat oleh: owner0", "SIMULASI"):
            self.assertIn(value, text)

    def test_pdf_treats_markup_as_text_without_fetching_or_links(self):
        marker = '<img src="https://example.invalid/no-fetch"/>'
        Payment.objects.filter(pk=self.payment.pk).update(note=marker)
        with patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected URL fetch")):
            response = self.client.get(self.url(True))
        pdf = PdfReader(BytesIO(response.content))
        self.assertIn(marker, "\n".join(page.extract_text() for page in pdf.pages))
        self.assertTrue(all(not page.get("/Annots") for page in pdf.pages))

    def test_pdf_embeds_local_fonts_and_preserves_latin_accents(self):
        Payment.objects.filter(pk=self.payment.pk).update(note="José, jalané – transfer simulasi")
        response = self.client.get(self.url(True))
        self.assertIn("José, jalané – transfer simulasi", self.pdf_text(response))
        pdf = PdfReader(BytesIO(response.content))
        embedded = [font.get_object().get("/FontDescriptor") for font in pdf.pages[0]["/Resources"]["/Font"].values()]
        self.assertTrue(any(descriptor and descriptor.get_object().get("/FontFile2") for descriptor in embedded))

    def test_long_text_wraps_without_layout_failure_or_truncation(self):
        self.invoice.customer_name = "P"*100
        self.invoice.customer_address = "A"*250
        self.invoice.tenant.name = "I"*100
        self.invoice.tenant.address = "B"*250
        self.payment.invoice = self.invoice
        self.payment.note = "C"*250
        self.payment.save(update_fields=["note"])
        PaymentReversal.objects.create(payment=self.payment, recorded_by=self.owners[0], reason="D"*250)
        self.payment = Payment.objects.select_related("reversal__recorded_by", "recorded_by").get(pk=self.payment.pk)
        self.payment.invoice = self.invoice
        pdf = PdfReader(BytesIO(render_receipt(receipt_context(self.payment, self.payment.created_at))))
        text = "".join(page.extract_text() for page in pdf.pages).replace("\n", "")
        for char, size in (("P",100), ("A",250), ("I",100), ("B",250), ("C",250), ("D",250)):
            self.assertIn(char*size, text)
        self.assertIn("mengikutidatasaatdicetak", "".join(text.split()))

    def test_invoice_links_include_reversed_payment(self):
        reverse_payment(self.tenants[0], self.payment.pk, "Koreksi pengujian tautan kuitansi", self.owners[0])
        self.assertContains(self.client.get(reverse("invoice_detail", args=[self.invoice.pk])), f'href="{self.url()}"')

    def test_customer_payment_tab_links_to_receipt(self):
        url = reverse("customer_detail", args=[self.customers[0].pk])+"?tab=payments"
        self.assertContains(self.client.get(url), f'href="{self.url()}"')
