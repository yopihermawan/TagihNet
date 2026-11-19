"""Historical issuer identity is financial evidence, captured only on successful new writes."""
import uuid
from datetime import date
from io import BytesIO
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from pypdf import PdfReader

from core.billing import issue_invoice, invoice_snapshot, record_payment, reverse_payment, write_snapshot_invoice
from core.document_identity import LEGACY_NOTICE
from core.models import (AuditEvent, Invoice, InvoiceIssuerSnapshot, InvoiceLine, NetworkJob,
                         Payment, PaymentIssuerSnapshot, Tenant)
from .auth_helpers import verified_login
from .test_workflow import fixtures


class DocumentIssuerTests(TestCase):
    def setUp(self):
        self.tenants, self.owners, self.customers = fixtures()
        self.tenant, self.owner, self.customer = self.tenants[0], self.owners[0], self.customers[0]
        self.invoice, _ = issue_invoice(self.tenant, self.customer.pk, date(2026, 2, 1), self.owner)
        self.key = uuid.uuid4()
        self.payment, _ = record_payment(self.tenant, self.invoice.pk, 100000, self.key, self.owner)
        verified_login(self.client, self.owner)

    def rename(self, name, address='Alamat penerbit baru'):
        Tenant.objects.filter(pk=self.tenant.pk).update(name=name, address=address)

    def paths(self):
        return (reverse('invoice_detail', args=[self.invoice.pk]),
                reverse('invoice_pdf', args=[self.invoice.pk]),
                reverse('payment_receipt', args=[self.invoice.pk, self.payment.pk]),
                reverse('payment_receipt_pdf', args=[self.invoice.pk, self.payment.pk]))

    def pdf_text(self, response):
        return ' '.join(' '.join(p.extract_text().split()) for p in PdfReader(BytesIO(response.content)).pages)

    def test_invoice_and_payment_capture_their_own_creation_identity_and_time(self):
        self.rename('Penerbit saat pembayaran', 'Alamat saat pembayaran')
        payment, _ = record_payment(self.tenant, self.invoice.pk, 10000, uuid.uuid4(), self.owner)
        self.rename('Profil setelah semua dokumen')
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        payment.refresh_from_db()
        self.assertEqual((invoice.issuer_snapshot.name, invoice.issuer_snapshot.address), ('ISP 0', 'Indonesia'))
        self.assertEqual((payment.issuer_snapshot.name, payment.issuer_snapshot.address),
                         ('Penerbit saat pembayaran', 'Alamat saat pembayaran'))
        self.assertEqual(invoice.issuer_snapshot.captured_at, invoice.issued_at)
        self.assertEqual(payment.issuer_snapshot.captured_at, payment.created_at)

    def test_html_and_pdf_reprints_keep_identity_after_profile_change(self):
        self.rename('Profil terkini tidak historis')
        for path in self.paths():
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                if response['Content-Type'] == 'application/pdf':
                    text = self.pdf_text(response)
                    self.assertIn('ISP 0', text)
                    self.assertNotIn('Profil terkini tidak historis', text)
                else:
                    self.assertEqual(response.context['issuer'].name, 'ISP 0')
                    self.assertTrue(response.context['issuer'].historical)
                self.assertNotIn(LEGACY_NOTICE.encode(), response.content)

    def test_legacy_reprints_label_fallback_without_backfill_or_business_write(self):
        InvoiceIssuerSnapshot.objects.all().delete()
        PaymentIssuerSnapshot.objects.all().delete()
        self.rename('Profil terkini dokumen lama')
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as captured:
            for path in self.paths():
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                if response['Content-Type'] == 'application/pdf':
                    text = self.pdf_text(response)
                    self.assertIn(LEGACY_NOTICE, text)
                    self.assertIn('Profil terkini dokumen lama', text)
                else:
                    self.assertContains(response, LEGACY_NOTICE)
                    self.assertFalse(response.context['issuer'].historical)
        writes = [q['sql'] for q in captured if q['sql'].lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ')) and 'django_session' not in q['sql']]
        self.assertEqual(writes, [])
        self.assertFalse(InvoiceIssuerSnapshot.objects.exists())
        self.assertFalse(PaymentIssuerSnapshot.objects.exists())

    def test_replay_preserves_existing_snapshot_and_legacy_absence(self):
        before = (list(InvoiceIssuerSnapshot.objects.values()), list(PaymentIssuerSnapshot.objects.values()))
        self.rename('Nanti')
        self.assertFalse(issue_invoice(self.tenant, self.customer.pk, self.invoice.period, self.owner)[1])
        self.assertFalse(record_payment(self.tenant, self.invoice.pk, self.payment.amount, self.key, self.owner)[1])
        self.assertEqual(before, (list(InvoiceIssuerSnapshot.objects.values()), list(PaymentIssuerSnapshot.objects.values())))
        InvoiceIssuerSnapshot.objects.all().delete()
        PaymentIssuerSnapshot.objects.all().delete()
        self.assertFalse(issue_invoice(self.tenant, self.customer.pk, self.invoice.period, self.owner)[1])
        self.assertFalse(record_payment(self.tenant, self.invoice.pk, self.payment.amount, self.key, self.owner)[1])
        self.assertFalse(InvoiceIssuerSnapshot.objects.exists())
        self.assertFalse(PaymentIssuerSnapshot.objects.exists())

    def test_approved_money_snapshot_and_stale_tenant_use_issuer_at_issuance(self):
        period = date(2026, 3, 1)
        snapshot = invoice_snapshot(self.tenant, self.customer, period)
        self.rename('Penerbit saat penerbitan', 'Alamat saat penerbitan')
        Tenant.objects.filter(pk=self.tenant.pk).update(tax_bp=0, invoice_footer='Footer baru')
        with transaction.atomic():
            invoice = write_snapshot_invoice(self.tenant, self.customer, period, snapshot, self.owner)
        self.assertEqual(invoice.total, snapshot['invoice']['total'])
        self.assertEqual(invoice.footer, snapshot['invoice']['footer'])
        self.assertEqual(invoice.issuer_snapshot.name, 'Penerbit saat penerbitan')
        self.assertEqual(invoice.issuer_snapshot.address, 'Alamat saat penerbitan')

    def counts(self):
        return tuple(m.objects.count() for m in (Invoice, InvoiceLine, Payment, NetworkJob, AuditEvent,
                                                 InvoiceIssuerSnapshot, PaymentIssuerSnapshot))

    def test_invoice_audit_failure_rolls_back_document_and_identity(self):
        before = self.counts()
        with patch('core.billing.audit', side_effect=RuntimeError('fixture failure')):
            with self.assertRaises(RuntimeError):
                issue_invoice(self.tenant, self.customer.pk, date(2026, 3, 1), self.owner)
        self.assertEqual(self.counts(), before)

    def test_payment_audit_failure_rolls_back_document_and_identity(self):
        before = self.counts()
        with patch('core.billing.audit', side_effect=RuntimeError('fixture failure')):
            with self.assertRaises(RuntimeError):
                record_payment(self.tenant, self.invoice.pk, 10000, uuid.uuid4(), self.owner)
        self.assertEqual(self.counts(), before)

    def test_snapshot_insert_failure_rolls_back_invoice_and_payment(self):
        before = self.counts()
        for target in ('InvoiceIssuerSnapshot', 'PaymentIssuerSnapshot'):
            with self.subTest(target=target), patch('core.models.'+target+'.objects.create', side_effect=RuntimeError('fixture failure')):
                with self.assertRaises(RuntimeError):
                    if target == 'InvoiceIssuerSnapshot':
                        issue_invoice(self.tenant, self.customer.pk, date(2026, 3, 1), self.owner)
                    else:
                        record_payment(self.tenant, self.invoice.pk, 10000, uuid.uuid4(), self.owner)
                self.assertEqual(self.counts(), before)

    def test_reversal_keeps_original_payment_identity(self):
        before = list(PaymentIssuerSnapshot.objects.values())
        self.rename('Profil saat koreksi')
        reverse_payment(self.tenant, self.payment.pk, 'Koreksi simulasi untuk pengujian', self.owner)
        self.assertEqual(list(PaymentIssuerSnapshot.objects.values()), before)
        self.assertIn('ISP 0', self.pdf_text(self.client.get(self.paths()[3])))

    def test_tenant_scope_still_gates_all_documents(self):
        verified_login(self.client, self.owners[1])
        for path in self.paths():
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_snapshot_markup_is_text_in_html_and_pdf(self):
        name = '<img src="https://issuer.invalid/x">'
        address = 'Jl. Contoh & <b>TagihNet</b>'
        self.rename(name, address)
        invoice, _ = issue_invoice(self.tenant, self.customer.pk, date(2026, 3, 1), self.owner)
        payment, _ = record_payment(self.tenant, invoice.pk, 10000, uuid.uuid4(), self.owner)
        for path in (reverse('invoice_detail', args=[invoice.pk]),
                     reverse('payment_receipt', args=[invoice.pk, payment.pk])):
            response = self.client.get(path)
            self.assertContains(response, '&lt;img')
            self.assertNotContains(response, name)
        for path in (reverse('invoice_pdf', args=[invoice.pk]),
                     reverse('payment_receipt_pdf', args=[invoice.pk, payment.pk])):
            response = self.client.get(path)
            self.assertIn(name, self.pdf_text(response))
            self.assertTrue(all(not p.get('/Annots') for p in PdfReader(BytesIO(response.content)).pages))
