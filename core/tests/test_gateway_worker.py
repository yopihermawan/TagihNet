import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from io import StringIO
from threading import Event
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from core.billing import issue_invoice
from core.gateway import apply_status, reserve_order
from core.gateway_worker import claim_policy, finish_policy, process_policy, renew_policy, run_cycle, save_policy
from core.midtrans import GatewayUnavailable
from core.models import AuditEvent, GatewayConfig, GatewayOrder, GatewayReconciliationPolicy, Membership, NetworkJob, Payment
from .auth_helpers import verified_login
from .test_payment_gateway import KEY, account_keys, status_payload
from .test_workflow import fixtures


@override_settings(SIMULATION=True)
class GatewayWorkerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        cls.invoice, _ = issue_invoice(cls.tenants[0], cls.customers[0].pk, date(2026, 8, 1), cls.owners[0])
        cls.other_invoice, _ = issue_invoice(cls.tenants[1], cls.customers[1].pk, date(2026, 8, 1), cls.owners[1])
        GatewayConfig.objects.create(tenant=cls.tenants[0], merchant_id="G-TEST", enabled=True)

    def setUp(self):
        self.keys = override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=account_keys(self.tenants[0]))
        self.keys.enable()
        self.addCleanup(self.keys.disable)
        verified_login(self.client, self.owners[0])

    def policy(self, include_settled=False):
        return save_policy(self.owners[0], True, include_settled, 60, 0, time.time(), 1)

    def order(self):
        return reserve_order(self.owners[0], self.invoice.pk)

    def due(self, policy):
        GatewayReconciliationPolicy.objects.filter(pk=policy.pk).update(next_run_at=timezone.now()-timedelta(seconds=1))

    def test_default_off_and_get_settings_do_not_schedule_or_contact_gateway(self):
        with patch("core.gateway.SandboxClient.status") as status:
            self.assertEqual(run_cycle(), [])
            response = self.client.get("/settings/payment-gateway/")
        status.assert_not_called()
        self.assertFalse(response.context["policy_form"].initial["enabled"])
        self.assertIsNone(response.context["policy"])
        self.assertFalse(GatewayReconciliationPolicy.objects.exists())

    def test_policy_requires_owner_recent_mfa_and_valid_interval(self):
        for interval in [0, 59, 61, 3600, True]:
            with self.subTest(interval=interval), self.assertRaises(ValidationError):
                save_policy(self.owners[0], True, False, interval, 0, time.time(), 1)
        with self.assertRaises(PermissionDenied):
            save_policy(self.owners[0], True, False, 60, 0, time.time()-301, 1)
        Membership.objects.filter(user=self.owners[0]).update(role="finance")
        with self.assertRaises(PermissionDenied):
            self.policy()
        self.assertFalse(GatewayReconciliationPolicy.objects.exists())

    def test_version_check_and_disable_invalidate_active_lease(self):
        policy = self.policy()
        leased = claim_policy(policy.pk)
        with self.assertRaises(ValidationError):
            self.policy()
        saved = save_policy(self.owners[0], False, False, 300, policy.version, time.time(), 1)
        self.assertFalse(saved.enabled)
        self.assertIsNone(saved.lease_token)
        self.assertFalse(renew_policy(policy.pk, leased.lease_token, leased.version))

    def test_credentials_are_required_for_enable_but_not_disable(self):
        with override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS={}):
            with self.assertRaises(ValidationError):
                self.policy()
            policy = save_policy(self.owners[0], False, False, 60, 0, time.time(), 1)
        self.assertFalse(policy.enabled)

    def test_merchant_cannot_switch_away_from_existing_orders(self):
        self.order()
        with override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS={str(self.tenants[0].pk): {"merchant_id":"G-OTHER","server_key":KEY}}), self.assertRaises(ValidationError):
            self.policy()

    def test_worker_records_only_own_tenant_and_never_creates_checkout_or_expiry(self):
        order = self.order()
        other = GatewayOrder.objects.create(tenant=self.tenants[1], invoice=self.other_invoice,
            merchant_id="G-OTHER", created_by=self.owners[1], amount=self.other_invoice.total)
        policy = self.policy()
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(order)) as status, patch("core.gateway.SandboxClient.create") as create, patch("core.gateway_expiry.SandboxClient.expire") as expire:
            result = process_policy(policy.pk)
        self.assertEqual(status.call_count, 1)
        self.assertEqual(status.call_args.args[0].pk, order.pk)
        create.assert_not_called()
        expire.assert_not_called()
        self.assertEqual(result.last_status, "completed")
        self.assertEqual(result.last_verified, 1)
        self.assertEqual(Payment.objects.get().tenant, self.tenants[0])
        self.assertEqual(self.invoice.balance, 0)
        other.refresh_from_db()
        self.assertEqual(other.status, "creating")
        self.assertEqual(self.other_invoice.balance, self.other_invoice.total)

    def test_success_schedules_next_run_and_early_repeat_has_no_effect(self):
        order, policy = self.order(), self.policy()
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(order,"pending")) as status:
            result = process_policy(policy.pk)
            self.assertIsNone(process_policy(policy.pk))
            self.assertEqual(run_cycle(), [])
        self.assertEqual(status.call_count, 1)
        self.assertEqual(result.failure_streak, 0)
        self.assertEqual(result.next_run_at-result.last_run_at, timedelta(seconds=60))
        self.assertIsNone(result.lease_token)

    def test_all_failures_back_off_with_cap_and_success_resets(self):
        order, policy = self.order(), self.policy()
        for delay in [60, 120, 240, 480, 960, 1920, 3600, 3600]:
            self.due(policy)
            with patch("core.gateway.SandboxClient.status", side_effect=GatewayUnavailable):
                result = process_policy(policy.pk)
            self.assertEqual(result.next_run_at-result.last_run_at, timedelta(seconds=delay))
            self.assertEqual(result.last_status, "failed")
            self.assertEqual(result.last_unavailable, 1)
            self.assertIsNone(result.last_success_at)
        self.due(policy)
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(order,"pending")):
            result = process_policy(policy.pk)
        self.assertEqual(result.failure_streak, 0)
        self.assertEqual(result.next_run_at-result.last_run_at, timedelta(seconds=60))
        self.assertIsNotNone(result.last_success_at)

    def test_failed_order_does_not_starve_later_order(self):
        first = self.order()
        invoice, _ = issue_invoice(self.tenants[0], self.customers[0].pk, date(2026,9,1), self.owners[0])
        second = reserve_order(self.owners[0], invoice.pk)
        policy = self.policy()
        with patch("core.gateway.SandboxClient.status", side_effect=GatewayUnavailable):
            process_policy(policy.pk, limit=1)
        self.due(policy)
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(second)) as status:
            process_policy(policy.pk, limit=1)
        self.assertEqual(status.call_args.args[0].pk, second.pk)
        self.assertEqual(invoice.balance, 0)
        self.assertEqual(GatewayOrder.objects.get(pk=first.pk).status, "creating")

    def test_include_settled_finds_missed_refund_without_ledger_reversal(self):
        order = self.order()
        apply_status(order.pk, status_payload(order))
        policy = self.policy()
        with patch("core.gateway.SandboxClient.status") as status:
            self.assertEqual(process_policy(policy.pk).last_status, "idle")
        status.assert_not_called()
        policy = save_policy(self.owners[0], True, True, 60, policy.version, time.time(), 1)
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(order,"refund")):
            process_policy(policy.pk)
        self.assertEqual(GatewayOrder.objects.get(pk=order.pk).status, "review")
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(NetworkJob.objects.count(), 1)
        self.assertEqual(self.invoice.balance, 0)

    def test_revoked_approval_blocks_and_disables_policy_before_network(self):
        self.order()
        policy = self.policy()
        Membership.objects.filter(user=self.owners[0]).update(session_version=2)
        with patch("core.gateway.SandboxClient.status") as status:
            self.assertIsNone(process_policy(policy.pk))
        status.assert_not_called()
        policy.refresh_from_db()
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.last_status, "blocked")
        self.assertIsNone(policy.lease_token)

    def test_revocation_mid_batch_stops_before_next_order(self):
        first = self.order()
        invoice, _ = issue_invoice(self.tenants[0], self.customers[0].pk, date(2026,9,1), self.owners[0])
        reserve_order(self.owners[0], invoice.pk)
        policy = self.policy()
        def remote(order):
            Membership.objects.filter(user=self.owners[0]).update(session_version=2)
            return status_payload(order)
        with patch("core.gateway.SandboxClient.status", side_effect=remote) as status:
            result = process_policy(policy.pk)
        self.assertEqual(status.call_count, 1)
        self.assertEqual(status.call_args.args[0].pk, first.pk)
        self.assertFalse(result.enabled)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(invoice.balance, invoice.total)

    def test_disabled_policy_and_live_mode_do_not_poll(self):
        self.order()
        policy = self.policy()
        policy.enabled = False
        policy.save()
        with patch("core.gateway.SandboxClient.status") as status:
            self.assertIsNone(process_policy(policy.pk))
            with override_settings(SIMULATION=False), self.assertRaises(ValidationError):
                run_cycle()
        status.assert_not_called()

    def test_lost_lease_and_old_finish_cannot_overwrite_new_run(self):
        order, policy = self.order(), self.policy()
        old = claim_policy(policy.pk)
        self.assertIsNone(claim_policy(policy.pk))
        GatewayReconciliationPolicy.objects.filter(pk=policy.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(order)):
            new = process_policy(policy.pk)
        self.assertFalse(renew_policy(policy.pk, old.lease_token, old.version))
        result = finish_policy(policy.pk, old.lease_token, old.version, 0, 9)
        self.assertEqual(result.last_status, new.last_status)
        self.assertEqual(result.last_verified, 1)
        self.assertEqual(result.last_unavailable, 0)

    def test_disabling_policy_midflight_keeps_verified_payment_but_stops_batch(self):
        self.order()
        invoice, _ = issue_invoice(self.tenants[0], self.customers[0].pk, date(2026,9,1), self.owners[0])
        reserve_order(self.owners[0], invoice.pk)
        policy = self.policy()
        def remote(order):
            save_policy(self.owners[0], False, False, 60, policy.version, time.time(), 1)
            return status_payload(order)
        with patch("core.gateway.SandboxClient.status", side_effect=remote) as status:
            result = process_policy(policy.pk)
        self.assertEqual(status.call_count, 1)
        self.assertFalse(result.enabled)
        self.assertEqual(result.last_status, "never")
        self.assertEqual(Payment.objects.count(), 1)

    def test_audit_retry_does_not_repeat_network_or_payment(self):
        from core.billing import audit as write_audit
        order, policy = self.order(), self.policy()
        failed = False
        def audit(tenant, actor, action, *args, **kwargs):
            nonlocal failed
            if action=="gateway.polling_run" and not failed:
                failed=True
                raise OperationalError("database is locked")
            return write_audit(tenant, actor, action, *args, **kwargs)
        with patch("core.gateway_worker.audit", side_effect=audit), patch("core.gateway.SandboxClient.status", return_value=status_payload(order)) as status:
            result = process_policy(policy.pk)
        self.assertTrue(failed)
        self.assertEqual(status.call_count, 1)
        self.assertEqual(result.last_verified, 1)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="gateway.polling_run").count(), 1)

    def test_http_policy_review_csrf_scope_and_secret_redaction(self):
        url = "/settings/payment-gateway/reconciliation/"
        data = {"enabled":"on","include_settled":"on","interval":300,"version":0,"reviewed":"on","tenant_id":self.tenants[1].pk}
        secure = Client(enforce_csrf_checks=True)
        verified_login(secure, self.owners[0])
        self.assertEqual(secure.post(url,data).status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url,{**data,"reviewed":""}).status_code, 400)
        self.assertFalse(GatewayReconciliationPolicy.objects.exists())
        self.assertRedirects(self.client.post(url,data), "/settings/payment-gateway/")
        policy = GatewayReconciliationPolicy.objects.get()
        self.assertEqual(policy.tenant, self.tenants[0])
        self.assertEqual(policy.approved_by, self.owners[0])
        response = self.client.get("/settings/payment-gateway/")
        self.assertNotContains(response, KEY)
        self.assertContains(response, "Belum ada verifikasi berhasil")
        self.assertContains(response, 'id="reconciliation_enabled"')

    def test_http_policy_needs_owner_and_recent_mfa(self):
        url = "/settings/payment-gateway/reconciliation/"
        data = {"enabled":"on","interval":60,"version":0,"reviewed":"on"}
        session = self.client.session
        session["mfa_verified_at"] = time.time()-301
        session.save()
        self.assertRedirects(self.client.post(url,data), "/account/security/")
        Membership.objects.filter(user=self.owners[0]).update(role="finance")
        verified_login(self.client,self.owners[0])
        self.assertEqual(self.client.post(url,data).status_code, 403)
        self.assertFalse(GatewayReconciliationPolicy.objects.exists())

    def test_worker_once_reports_failures_and_releases_lease(self):
        self.order()
        policy = self.policy()
        output = StringIO()
        with patch("core.gateway.SandboxClient.status", side_effect=GatewayUnavailable), self.assertRaises(CommandError):
            call_command("run_gateway_worker", once=True, stdout=output, stderr=StringIO())
        policy.refresh_from_db()
        self.assertEqual(policy.last_status, "failed")
        self.assertIsNone(policy.lease_token)
        self.assertGreater(policy.next_run_at, timezone.now())
        for limit in [0,101]:
            with self.assertRaises(CommandError):
                call_command("run_gateway_worker", once=True, limit=limit, stdout=StringIO())

    def test_failed_check_keeps_last_verified_time_and_shows_warning(self):
        order, policy = self.order(), self.policy()
        with patch("core.gateway.SandboxClient.status",return_value=status_payload(order,"pending")):
            process_policy(policy.pk)
        order.refresh_from_db()
        verified_at = order.last_verified_at
        self.assertIsNotNone(verified_at)
        self.due(policy)
        with patch("core.gateway.SandboxClient.status",side_effect=GatewayUnavailable):
            process_policy(policy.pk)
        order.refresh_from_db()
        self.assertEqual(order.last_verified_at,verified_at)
        self.assertTrue(order.verification_failed)
        response=self.client.get(f"/payments/orders/{order.pk}/")
        self.assertContains(response,"Pemeriksaan terakhir gagal")
        self.assertFalse(Payment.objects.exists())
        self.due(policy)
        with patch("core.gateway.SandboxClient.status",return_value=status_payload(order,"pending")):
            process_policy(policy.pk)
        order.refresh_from_db()
        self.assertFalse(order.verification_failed)
        self.assertGreater(order.last_verified_at,verified_at)


@override_settings(SIMULATION=True)
class GatewayWorkerConcurrencyTests(TransactionTestCase):
    def test_active_lease_allows_one_worker_and_http_has_no_transaction(self):
        tenants, owners, customers = fixtures()
        invoice, _ = issue_invoice(tenants[0], customers[0].pk, date(2026,8,1), owners[0])
        GatewayConfig.objects.create(tenant=tenants[0],merchant_id="G-TEST",enabled=True)
        with override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=account_keys(tenants[0])):
            order = reserve_order(owners[0],invoice.pk)
            policy = save_policy(owners[0],True,False,60,0,time.time(),1)
            started, release = Event(), Event()
            def remote(current):
                self.assertFalse(connection.in_atomic_block)
                started.set()
                if not release.wait(5):
                    raise AssertionError("Second worker did not complete")
                return status_payload(current)
            def run():
                close_old_connections()
                try:
                    return process_policy(policy.pk)
                finally:
                    close_old_connections()
            with patch("core.gateway.SandboxClient.status",side_effect=remote) as status, ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(run)
                try:
                    self.assertTrue(started.wait(5))
                    self.assertIsNone(process_policy(policy.pk))
                finally:
                    release.set()
                self.assertEqual(future.result().last_status,"completed")
            self.assertEqual(status.call_count,1)
        self.assertEqual(Payment.objects.count(),1)
        self.assertEqual(NetworkJob.objects.count(),1)


class GatewayVerificationMigrationTests(TransactionTestCase):
    def test_backfill_uses_event_evidence_and_preserves_ledger(self):
        executor=MigrationExecutor(connection)
        latest=executor.loader.graph.leaf_nodes()
        previous=[("core","0010_gatewayreconciliationpolicy")]
        executor.migrate(previous)
        try:
            apps=executor.loader.project_state(previous).apps
            OldOrder=apps.get_model("core","GatewayOrder")
            OldEvent=apps.get_model("core","GatewayEvent")
            tenants,owners,customers=fixtures()
            invoice,_=issue_invoice(tenants[0],customers[0].pk,date(2026,10,1),owners[0])
            verified=OldOrder.objects.create(tenant_id=tenants[0].pk,invoice_id=invoice.pk,
                created_by_id=owners[0].pk,merchant_id="G-TEST",amount=invoice.total,status="expire",updated_at=timezone.now())
            unknown=OldOrder.objects.create(tenant_id=tenants[0].pk,invoice_id=invoice.pk,
                created_by_id=owners[0].pk,merchant_id="G-TEST",amount=invoice.total,status="creating")
            evidence_time=timezone.now()-timedelta(days=5)
            OldEvent.objects.create(order=verified,fingerprint="a"*64,gateway_status="expire",outcome="expire",created_at=evidence_time)
            MigrationExecutor(connection).migrate(latest)
            restored=GatewayOrder.objects.get(pk=verified.pk)
            self.assertEqual(restored.last_verified_at,evidence_time)
            self.assertEqual(restored.last_checked_at,evidence_time)
            self.assertIsNone(GatewayOrder.objects.get(pk=unknown.pk).last_verified_at)
            self.assertEqual(invoice.balance,invoice.total)
            self.assertFalse(Payment.objects.exists())
        finally:
            MigrationExecutor(connection).migrate(latest)
