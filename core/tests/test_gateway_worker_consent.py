import time
from datetime import date, timedelta
from io import StringIO
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from core.billing import issue_invoice
from core.gateway import reserve_order
from core.gateway_worker import claim_policy, process_policy, run_cycle, save_policy
from core.models import AuditEvent, GatewayConfig, GatewayReconciliationPolicy, Membership, Payment
from .test_payment_gateway import KEY, status_payload
from .test_workflow import fixtures


@override_settings(SIMULATION=True)
class WorkerConsentReportingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        cls.invoices = []
        for index in range(2):
            invoice, _ = issue_invoice(cls.tenants[index], cls.customers[index].pk, date(2026, 8, 1), cls.owners[index])
            cls.invoices.append(invoice)
            GatewayConfig.objects.create(tenant=cls.tenants[index], merchant_id="G-TEST" if index == 0 else "G-SECOND", enabled=True)

    def setUp(self):
        self.keys = override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS={str(tenant.pk): {
            "merchant_id": "G-TEST" if index == 0 else "G-SECOND", "server_key": KEY}
            for index, tenant in enumerate(self.tenants)})
        self.keys.enable()
        self.addCleanup(self.keys.disable)

    def policy(self, index=0):
        return save_policy(self.owners[index], True, False, 60, 0, time.time(), 1)

    def revoke(self, index=0):
        Membership.objects.filter(user=self.owners[index]).update(session_version=2)

    def once(self, failed=False):
        out, err = StringIO(), StringIO()
        if failed:
            with self.assertRaises(CommandError):
                call_command("run_gateway_worker", once=True, stdout=out, stderr=err)
        else:
            call_command("run_gateway_worker", once=True, stdout=out, stderr=err)
        return out.getvalue()

    def test_once_fails_when_due_policy_is_blocked_before_claim(self):
        policy = self.policy()
        self.revoke()
        with patch("core.gateway.SandboxClient.status") as status:
            output = self.once(failed=True)
        status.assert_not_called()
        self.assertIn(f"policy={policy.pk} status=blocked", output)
        self.assertNotIn("0 kebijakan diproses", output)

    def test_claim_returns_newly_blocked_policy_without_a_lease(self):
        policy = self.policy()
        self.revoke()
        blocked = claim_policy(policy.pk)
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.last_status, "blocked")
        self.assertFalse(blocked.enabled)
        self.assertIsNone(blocked.lease_token)
        self.assertIsNone(blocked.lease_until)

    def test_invalid_owner_account_role_version_tenant_or_missing_approver_is_reported(self):
        policy = self.policy()
        variants = [{"active": False}, {"role": "finance"}, {"session_version": 2},
            {"tenant_id": self.tenants[1].pk}, {"approved_by_id": None}]
        for variant in variants:
            with self.subTest(variant=variant):
                get_user_model().objects.filter(pk=self.owners[0].pk).update(is_active=variant.get("active", True))
                Membership.objects.filter(user=self.owners[0]).update(role=variant.get("role", "owner"),
                    session_version=variant.get("session_version", 1), tenant_id=variant.get("tenant_id", self.tenants[0].pk))
                GatewayReconciliationPolicy.objects.filter(pk=policy.pk).update(enabled=True, last_status="never",
                    approved_by_id=variant.get("approved_by_id", self.owners[0].pk))
                with patch("core.gateway.SandboxClient.status") as status:
                    results = run_cycle()
                status.assert_not_called()
                self.assertEqual([result.last_status for result in results], ["blocked"])

    def test_previously_blocked_or_future_policies_are_skipped_without_duplicate_audit(self):
        policy = self.policy()
        GatewayReconciliationPolicy.objects.filter(pk=policy.pk).update(next_run_at=timezone.now()+timedelta(minutes=1))
        self.revoke()
        self.assertEqual(run_cycle(), [])
        GatewayReconciliationPolicy.objects.filter(pk=policy.pk).update(next_run_at=timezone.now()-timedelta(seconds=1))
        self.once(failed=True)
        self.assertEqual(run_cycle(), [])
        self.assertIn("0 kebijakan diproses", self.once())
        self.assertEqual(AuditEvent.objects.filter(action="gateway.polling_blocked").count(), 1)

    def test_historical_counts_and_success_are_preserved_but_not_reported_as_new_work(self):
        order = reserve_order(self.owners[0], self.invoices[0].pk)
        policy = self.policy()
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(order, "pending")):
            previous = process_policy(policy.pk)
        GatewayReconciliationPolicy.objects.filter(pk=policy.pk).update(next_run_at=timezone.now()-timedelta(seconds=1))
        self.revoke()
        output = self.once(failed=True)
        self.assertNotIn("verified=", output)
        self.assertNotIn("unavailable=", output)
        policy.refresh_from_db()
        self.assertEqual((policy.last_run_at, policy.last_success_at, policy.last_verified),
            (previous.last_run_at, previous.last_success_at, 1))

    def test_blocked_tenant_does_not_prevent_another_due_tenant_recording_payment(self):
        first, second = self.policy(), self.policy(1)
        order = reserve_order(self.owners[1], self.invoices[1].pk)
        self.revoke()
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(order, merchant_id="G-SECOND")) as status:
            output = self.once(failed=True)
        self.assertEqual(status.call_count, 1)
        self.assertEqual(status.call_args.args[0].tenant_id, self.tenants[1].pk)
        self.assertIn(f"policy={first.pk} status=blocked", output)
        self.assertIn(f"policy={second.pk} status=completed verified=1", output)
        self.assertEqual(Payment.objects.get().tenant_id, self.tenants[1].pk)

    def test_daemon_reports_block_then_continues_without_repeating_transition(self):
        self.policy()
        self.revoke()
        output = StringIO()
        with patch("core.management.commands.run_gateway_worker.time.sleep", side_effect=[None, KeyboardInterrupt]) as sleep, patch("core.gateway.SandboxClient.status") as status:
            call_command("run_gateway_worker", stdout=output, stderr=StringIO())
        status.assert_not_called()
        self.assertEqual(sleep.call_count, 2)
        self.assertIn("status=blocked", output.getvalue())
        self.assertEqual(AuditEvent.objects.filter(action="gateway.polling_blocked").count(), 1)

    def test_fresh_owner_approval_is_required_and_restores_processing(self):
        policy = self.policy()
        self.revoke()
        self.once(failed=True)
        for version, verified in [(1, time.time()), (2, time.time()-301)]:
            with self.assertRaises(PermissionDenied):
                save_policy(self.owners[0], True, False, 60, policy.version, verified, version)
        save_policy(self.owners[0], True, False, 60, policy.version, time.time(), 2)
        order = reserve_order(self.owners[0], self.invoices[0].pk)
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(order)):
            output = self.once()
        self.assertIn("status=completed verified=1", output)
        self.assertEqual(Payment.objects.count(), 1)

    def test_revocation_during_request_keeps_payment_and_reports_block_without_old_counts(self):
        order = reserve_order(self.owners[0], self.invoices[0].pk)
        self.policy()
        def status(current):
            self.revoke()
            return status_payload(current)
        with patch("core.gateway.SandboxClient.status", side_effect=status) as remote:
            output = self.once(failed=True)
        remote.assert_called_once()
        self.assertIn("status=blocked", output)
        self.assertNotIn("verified=", output)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(order.invoice.balance, 0)
