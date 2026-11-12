import json
from datetime import date, timedelta
from io import StringIO
from unittest.mock import patch
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from core.billing import issue_invoice
from core.gateway import apply_status, reserve_order
from core.midtrans import GatewayUnavailable
from core.models import GatewayConfig, GatewayEvent, GatewayOrder, NetworkJob, Payment
from .test_payment_gateway import KEY, account_keys, status_payload
from .test_workflow import fixtures


@override_settings(SIMULATION=True)
class GatewayReconciliationCLITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        GatewayConfig.objects.create(tenant=cls.tenants[0], merchant_id="G-TEST", enabled=True)

    def setUp(self):
        self.keys = override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=account_keys(self.tenants[0]))
        self.keys.enable()
        self.addCleanup(self.keys.disable)

    def order(self, month=1):
        invoice, _ = issue_invoice(self.tenants[0], self.customers[0].pk, date(2026, month, 1), self.owners[0])
        return reserve_order(self.owners[0], invoice.pk)

    def run_command(self, failed=False, **options):
        out = StringIO()
        if failed:
            with self.assertRaises(CommandError):
                call_command("reconcile_gateway_payments", stdout=out, **options)
        else:
            call_command("reconcile_gateway_payments", stdout=out, **options)
        return out.getvalue()

    def report(self, failed=False, **options):
        return json.loads(self.run_command(failed=failed, as_json=True, **options))

    def test_dry_run_never_contacts_provider_or_writes_tables(self):
        self.order()
        with patch("core.midtrans.SandboxClient.request") as request, CaptureQueriesContext(connection) as queries:
            report = self.report(dry_run=True)
        request.assert_not_called()
        self.assertEqual(report["outcome"], "planned")
        self.assertEqual((report["selected"], report["attempted"], report["verified"], report["unavailable"]), (1, 0, 0, 0))
        self.assertEqual(report["results"][0]["outcome"], "planned")
        self.assertFalse(any(q["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"} for q in queries))

    def test_dry_run_uses_existing_scope_limit_and_fair_order_without_mutation(self):
        first, second, third = [self.order(month) for month in [1, 2, 3]]
        before = timezone.now()-timedelta(hours=1)
        GatewayOrder.objects.filter(pk=first.pk).update(last_checked_at=before)
        report = self.report(dry_run=True, tenant=self.tenants[0].pk, limit=2)
        self.assertEqual([row["order_id"] for row in report["results"]], [second.order_id, third.order_id])
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.last_checked_at, before)
        self.assertIsNone(second.last_checked_at)

    def test_dry_run_without_mode_credentials_or_checkout_activation(self):
        self.order()
        GatewayConfig.objects.update(enabled=False)
        with override_settings(SIMULATION=False, MIDTRANS_SANDBOX_ACCOUNT_KEYS={}):
            self.assertEqual(self.report(dry_run=True)["selected"], 1)

    def test_existing_empty_scope_is_distinct_from_unknown_tenant(self):
        empty = self.report(tenant=self.tenants[1].pk)
        self.assertEqual((empty["outcome"], empty["selected"], empty["attempted"]), ("empty", 0, 0))
        missing = self.report(failed=True, tenant=999999)
        self.assertEqual(missing["outcome"], "unknown_tenant")
        self.assertIsNone(missing["selected"])
        self.assertEqual(missing["results"], [])

    def test_empty_all_tenants_is_not_reported_as_verification(self):
        with patch("core.midtrans.SandboxClient.request") as request:
            report = self.report()
        request.assert_not_called()
        self.assertIsNone(report["tenant_id"])
        self.assertEqual((report["selected"], report["attempted"], report["verified"]), (0, 0, 0))
        self.assertEqual(report["outcome"], "empty")

    def test_tenant_scope_does_not_attempt_another_isp(self):
        self.order()
        with patch("core.gateway.SandboxClient.status") as status:
            report = self.report(tenant=self.tenants[1].pk)
        status.assert_not_called()
        self.assertEqual(report["selected"], 0)

    def test_settled_requires_opt_in_and_terminal_orders_are_excluded(self):
        settled, ended = self.order(), self.order(2)
        apply_status(settled.pk, status_payload(settled))
        apply_status(ended.pk, status_payload(ended, "expire"))
        self.assertEqual(self.report(dry_run=True)["selected"], 0)
        report = self.report(dry_run=True, include_settled=True)
        self.assertEqual([row["order_id"] for row in report["results"]], [settled.order_id])

    def test_all_unavailable_gives_failure_and_redacts_exception(self):
        self.order()
        with patch("core.gateway.SandboxClient.status", side_effect=GatewayUnavailable("PRIVATE KEY DSN")):
            report = self.report(failed=True)
        self.assertEqual((report["outcome"], report["attempted"], report["unavailable"]), ("failed", 1, 1))
        self.assertIsNone(report["results"][0]["local_status"])
        self.assertNotIn("PRIVATE", json.dumps(report))
        self.assertFalse(Payment.objects.exists())

    def test_partial_failure_continues_batch_and_keeps_verified_payment(self):
        failed, successful = self.order(), self.order(2)
        def status(order):
            if order.pk == failed.pk:
                raise GatewayUnavailable
            return status_payload(order)
        with patch("core.gateway.SandboxClient.status", side_effect=status), patch("core.gateway.SandboxClient.create") as create:
            report = self.report(failed=True)
        create.assert_not_called()
        self.assertEqual((report["outcome"], report["selected"], report["attempted"], report["verified"], report["unavailable"]), ("partial", 2, 2, 1, 1))
        self.assertEqual([row["outcome"] for row in report["results"]], ["unavailable", "verified"])
        successful.refresh_from_db()
        self.assertIsNotNone(successful.payment_id)
        self.assertEqual((Payment.objects.count(), NetworkJob.objects.count()), (1, 1))

    def test_successful_pending_verification_does_not_mean_paid(self):
        self.order()
        with patch("core.gateway.SandboxClient.status", side_effect=lambda order: status_payload(order, "pending")):
            report = self.report()
        self.assertEqual((report["outcome"], report["verified"]), ("completed", 1))
        self.assertEqual(report["results"][0]["local_status"], "pending")
        self.assertFalse(Payment.objects.exists())

    def test_verified_replay_with_settled_opt_in_does_not_duplicate_ledger(self):
        self.order()
        with patch("core.gateway.SandboxClient.status", side_effect=status_payload):
            self.report()
            self.assertEqual(self.report(include_settled=True)["verified"], 1)
        self.assertEqual((Payment.objects.count(), NetworkJob.objects.count(), GatewayEvent.objects.count()), (1, 1, 1))

    def test_wrong_status_identity_is_unavailable_without_credit(self):
        order = self.order()
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(order, merchant_id="PRIVATE WRONG ACCOUNT")):
            output = self.run_command(failed=True, as_json=True)
        self.assertNotIn("PRIVATE", output)
        self.assertEqual(json.loads(output)["unavailable"], 1)
        self.assertFalse(Payment.objects.exists())

    def test_selection_database_failure_is_redacted_unknown_work(self):
        with patch.object(GatewayOrder.objects, "filter", side_effect=OperationalError("PRIVATE DB PASSWORD")), patch("core.midtrans.SandboxClient.request") as request:
            report = self.report(failed=True, dry_run=True)
        request.assert_not_called()
        self.assertEqual(report["outcome"], "database_unavailable")
        self.assertIsNone(report["selected"])
        self.assertEqual((report["attempted"], report["results"]), (0, []))
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_failure_to_record_attempt_still_reports_failure_and_continues(self):
        first = self.order()
        self.order(2)
        def status(order):
            if order.pk == first.pk:
                raise GatewayUnavailable("PRIVATE TRANSPORT")
            return status_payload(order, "pending")
        with patch("core.gateway.SandboxClient.status", side_effect=status), patch("core.gateway.mark_unavailable", side_effect=OperationalError("PRIVATE DATABASE")):
            report = self.report(failed=True)
        self.assertEqual((report["outcome"], report["verified"], report["unavailable"]), ("partial", 1, 1))
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_selection_is_frozen_before_first_status_request(self):
        first = self.order()
        def status(order):
            self.order(2)
            return status_payload(order, "pending")
        with patch("core.gateway.SandboxClient.status", side_effect=status) as status_request:
            report = self.report()
        status_request.assert_called_once()
        self.assertEqual(report["selected"], 1)
        self.assertEqual(report["results"][0]["order_id"], first.order_id)
        self.assertEqual(self.report(dry_run=True)["selected"], 2)

    def test_report_is_bounded_minimal_and_uses_wib(self):
        order = self.order()
        report = self.report(dry_run=True)
        self.assertEqual(report["schema_version"], 1)
        self.assertTrue(report["generated_at"].endswith("+07:00"))
        self.assertEqual(set(report["results"][0]), {"order_id", "tenant_id", "outcome", "local_status"})
        for private in [KEY, "G-TEST", self.customers[0].name]:
            self.assertNotIn(private, json.dumps(report))

    def test_invalid_limit_or_tenant_is_rejected_before_database_or_network(self):
        for options in [{"limit": x} for x in [0, 501, True, None, "1"]]+[{"tenant": x} for x in [0, -1, True, "1"]]:
            with self.subTest(options=options), patch.object(GatewayOrder.objects, "filter") as query, patch("core.midtrans.SandboxClient.request") as request:
                with self.assertRaises(CommandError):
                    self.run_command(as_json=True, **options)
                query.assert_not_called()
                request.assert_not_called()

    def test_human_output_keeps_counts_but_never_claims_dry_run_verified(self):
        self.order()
        text = self.run_command(dry_run=True)
        self.assertIn("dry-run", text)
        self.assertIn("Tidak ada request Midtrans", text)
        with patch("core.gateway.SandboxClient.status", side_effect=GatewayUnavailable):
            text = self.run_command(failed=True)
        self.assertIn("verified=0; unavailable=1", text)
