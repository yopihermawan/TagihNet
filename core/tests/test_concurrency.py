import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from django.db import close_old_connections
from django.test import TransactionTestCase, override_settings
from core.billing import issue_invoice, record_payment
from core.models import Invoice, Payment
from .test_workflow import fixtures

@override_settings(SIMULATION=True)
class ConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.tenants, self.owners, self.customers = fixtures()

    def run_parallel(self, function):
        def wrapped(_):
            close_old_connections()
            try:
                return function()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(wrapped, range(2)))

    def test_concurrent_monthly_invoice_is_unique(self):
        results = self.run_parallel(lambda: issue_invoice(self.tenants[0], self.customers[0].pk,
            date(2026, 2, 1), self.owners[0])[0].pk)
        self.assertEqual(results[0], results[1])
        self.assertEqual(Invoice.objects.count(), 1)

    def test_concurrent_payment_replay_affects_ledger_once(self):
        invoice, _ = issue_invoice(self.tenants[0], self.customers[0].pk, date(2026, 2, 1), self.owners[0])
        key = uuid.uuid4()
        results = self.run_parallel(lambda: record_payment(self.tenants[0], invoice.pk, invoice.total,
            key, self.owners[0])[0].pk)
        self.assertEqual(results[0], results[1])
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(invoice.balance, 0)
