import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Event
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from core.billing import issue_invoice, record_payment
from core.gateway import apply_status, reserve_order
from core.gateway_expiry import claim_expiry, expire_order, finish_expiry, prepare_expiry, renew_expiry
from core.midtrans import GatewayUnavailable
from core.models import GatewayConfig, GatewayExpiry, GatewayOrder, Payment
from .auth_helpers import verified_login
from .test_gateway_expiry import REASON
from .test_payment_gateway import account_keys, status_payload
from .test_workflow import fixtures


@override_settings(SIMULATION=True)
class ExpiryLeaseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        cls.invoice, _ = issue_invoice(cls.tenants[0], cls.customers[0].pk, date(2026, 9, 1), cls.owners[0])
        GatewayConfig.objects.create(tenant=cls.tenants[0], merchant_id="G-TEST", enabled=True)

    def setUp(self):
        self.keys = override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=account_keys(self.tenants[0]))
        self.keys.enable()
        self.addCleanup(self.keys.disable)
        self.order = reserve_order(self.owners[0], self.invoice.pk)
        self.key = uuid.uuid4()
        verified_login(self.client, self.owners[0])

    def intent(self, key=None):
        return prepare_expiry(self.owners[0], self.order.pk, key or self.key, REASON, time.time(), 1)

    def expire(self, key=None):
        return expire_order(self.owners[0], self.order.pk, key or self.key, REASON, time.time(), 1)

    def expire_lease(self, expiry):
        GatewayExpiry.objects.filter(pk=expiry.pk).update(lease_until=timezone.now()-timedelta(seconds=1))

    def test_claim_persists_token_deadline_pair(self):
        before = timezone.now()
        claimed, token = claim_expiry(self.intent().pk)
        self.assertEqual(claimed.lease_token, token)
        self.assertGreaterEqual(claimed.lease_until, before+timedelta(seconds=60))
        self.assertLessEqual(claimed.lease_until, timezone.now()+timedelta(seconds=60))

    def test_live_lease_coalesces_same_and_different_intents_without_http(self):
        claimed, token = claim_expiry(self.intent().pk)
        with patch("core.gateway.SandboxClient.status") as status, patch("core.gateway_expiry.SandboxClient.expire") as post:
            same = self.expire()
            different = self.expire(uuid.uuid4())
        status.assert_not_called()
        post.assert_not_called()
        self.assertIn("sedang diproses", same.result)
        self.assertIn("sedang diproses", different.result)
        claimed.refresh_from_db()
        self.assertEqual(claimed.lease_token, token)
        self.assertEqual(claimed.result, "")

    def test_verified_terminal_releases_lease_but_never_marks_paid(self):
        with patch("core.gateway.SandboxClient.status", side_effect=[status_payload(self.order,"pending"),status_payload(self.order,"expire")]), patch("core.gateway_expiry.SandboxClient.expire", return_value={}):
            result = self.expire()
        self.assertEqual(result.status, "confirmed")
        self.assertIsNone(result.lease_token)
        self.assertIsNone(result.lease_until)
        self.assertEqual(self.invoice.balance, self.invoice.total)
        self.assertFalse(Payment.objects.exists())

    def test_known_outage_releases_lease_but_not_invoice_reservation(self):
        with patch("core.gateway.SandboxClient.status", side_effect=GatewayUnavailable):
            result = self.expire()
        self.assertEqual(result.status, "unavailable")
        self.assertIsNone(result.lease_token)
        self.assertEqual(reserve_order(self.owners[0],self.invoice.pk).pk,self.order.pk)
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            record_payment(self.tenants[0],self.invoice.pk,1000,uuid.uuid4(),self.owners[0])

    def test_process_loss_leaves_lease_and_expired_retry_reuses_request_and_order(self):
        with patch("core.gateway.SandboxClient.status", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.expire()
        pending = GatewayExpiry.objects.get()
        self.assertIsNotNone(pending.lease_token)
        with patch("core.gateway.SandboxClient.status") as status:
            self.expire()
        status.assert_not_called()
        self.expire_lease(pending)
        with patch("core.gateway.SandboxClient.status", side_effect=[status_payload(self.order,"pending"),status_payload(self.order,"expire")]), patch("core.gateway_expiry.SandboxClient.expire",return_value={}) as post:
            completed = self.expire()
        self.assertEqual(completed.pk,pending.pk)
        self.assertEqual(post.call_args.args[0].pk,self.order.pk)
        self.assertEqual(GatewayOrder.objects.count(),1)

    def test_same_intent_takeover_fences_old_completion(self):
        first, old = claim_expiry(self.intent().pk)
        self.expire_lease(first)
        second, new = claim_expiry(first.pk)
        self.assertNotEqual(old,new)
        stale = finish_expiry(first.pk,self.owners[0],old,revoked=True)
        self.assertEqual((stale.status,stale.lease_token),("requested",new))
        self.assertFalse(renew_expiry(first.pk,old))
        finished = finish_expiry(second.pk,self.owners[0],new)
        self.assertEqual(finished.status,"unavailable")
        self.assertIsNone(finished.lease_token)

    def test_different_intent_takeover_clears_old_lease_and_fences_completion(self):
        first, old = claim_expiry(self.intent().pk)
        self.expire_lease(first)
        second, new = claim_expiry(self.intent(uuid.uuid4()).pk)
        first.refresh_from_db()
        self.assertIsNone(first.lease_token)
        finish_expiry(first.pk,self.owners[0],old)
        second.refresh_from_db()
        self.assertEqual(second.lease_token,new)
        self.assertEqual(self.invoice.balance,self.invoice.total)

    def test_renewal_requires_live_token_pending_order_and_extends_deadline(self):
        apply_status(self.order.pk,status_payload(self.order,"pending"))
        claimed, token = claim_expiry(self.intent().pk)
        deadline=timezone.now()+timedelta(seconds=30)
        GatewayExpiry.objects.filter(pk=claimed.pk).update(lease_until=deadline)
        self.assertFalse(renew_expiry(claimed.pk,uuid.uuid4()))
        self.assertTrue(renew_expiry(claimed.pk,token))
        claimed.refresh_from_db()
        self.assertGreater(claimed.lease_until,deadline)
        self.expire_lease(claimed)
        self.assertFalse(renew_expiry(claimed.pk,token))

    def test_payment_before_renewal_blocks_write_and_preserves_ledger(self):
        apply_status(self.order.pk,status_payload(self.order,"pending"))
        claimed, token = claim_expiry(self.intent().pk)
        apply_status(self.order.pk,status_payload(self.order))
        self.assertFalse(renew_expiry(claimed.pk,token))
        result=finish_expiry(claimed.pk,self.owners[0],token)
        self.assertEqual(result.status,"blocked")
        self.assertIsNone(result.lease_token)
        self.assertEqual(Payment.objects.count(),1)

    def test_takeover_during_preflight_prevents_stale_post_and_finish(self):
        def status(current):
            original=GatewayExpiry.objects.get()
            self.expire_lease(original)
            claim_expiry(original.pk)
            return status_payload(current,"pending")
        with patch("core.gateway.SandboxClient.status",side_effect=status), patch("core.gateway_expiry.SandboxClient.expire") as post:
            result=self.expire()
        post.assert_not_called()
        self.assertEqual(result.status,"requested")
        self.assertIsNotNone(result.lease_token)

    def test_unclaimed_finish_cannot_change_intent(self):
        expiry=self.intent()
        result=finish_expiry(expiry.pk,self.owners[0],None,revoked=True)
        self.assertEqual(result.status,"requested")

    def test_owner_get_hides_busy_action_and_token_even_with_newer_legacy_intent(self):
        claimed, token=claim_expiry(self.intent().pk)
        self.intent(uuid.uuid4())
        response=self.client.get(f"/payments/orders/{self.order.pk}/")
        self.assertEqual(response.context["expiry_form"].initial["request_key"],self.key)
        self.assertContains(response,"Muat ulang permintaan pengakhiran")
        self.assertNotContains(response,"Minta pengakhiran pembayaran")
        self.assertNotContains(response,str(token))

    def test_busy_post_ignores_browser_lease_fields_and_cannot_dispatch(self):
        claimed, token=claim_expiry(self.intent().pk)
        with patch("core.gateway.SandboxClient.status") as status,patch("core.gateway_expiry.SandboxClient.expire") as post:
            response=self.client.post(f"/payments/orders/{self.order.pk}/expire/",{
                "request_key":self.key,"reason":REASON,"reviewed":"on","lease_token":uuid.uuid4(),"lease_until":"2099-01-01"})
        self.assertEqual(response.status_code,302)
        status.assert_not_called()
        post.assert_not_called()
        claimed.refresh_from_db()
        self.assertEqual(claimed.lease_token,token)

    def test_database_rejects_half_lease(self):
        expiry=self.intent()
        with self.assertRaises(IntegrityError),transaction.atomic():
            GatewayExpiry.objects.filter(pk=expiry.pk).update(lease_token=uuid.uuid4())


@override_settings(SIMULATION=True)
class ExpiryLeaseConcurrencyTests(TransactionTestCase):
    def test_overlapping_same_intent_sends_one_post_outside_transactions(self):
        tenants, owners, customers = fixtures()
        invoice, _ = issue_invoice(tenants[0], customers[0].pk, date(2026, 9, 1), owners[0])
        GatewayConfig.objects.create(tenant=tenants[0], merchant_id="G-TEST", enabled=True)
        active, release, terminal = Event(), Event(), Event()
        posts, key = [], uuid.uuid4()
        with override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=account_keys(tenants[0])):
            order = reserve_order(owners[0], invoice.pk)
            def status(current):
                self.assertFalse(connection.in_atomic_block)
                return status_payload(current, "expire" if terminal.is_set() else "pending")
            def post(current):
                self.assertFalse(connection.in_atomic_block)
                posts.append(current.order_id)
                if len(posts) == 1:
                    active.set()
                    if not release.wait(10):
                        raise TimeoutError("Expiry overlap gate timed out")
                terminal.set()
                return {}
            def run():
                close_old_connections()
                try:
                    actor = get_user_model().objects.get(pk=owners[0].pk)
                    return expire_order(actor, order.pk, key, REASON, time.time(), 1)
                finally:
                    close_old_connections()
            with patch("core.gateway.SandboxClient.status", side_effect=status), patch("core.gateway_expiry.SandboxClient.expire", side_effect=post), ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(run)
                try:
                    self.assertTrue(active.wait(10))
                    second = run()
                    self.assertEqual(posts, [order.order_id])
                    self.assertIn(second.status, {"requested", "unavailable"})
                finally:
                    release.set()
                completed = first.result(timeout=10)
        self.assertEqual(completed.status, "confirmed")
        self.assertEqual(GatewayExpiry.objects.count(), 1)
        self.assertFalse(Payment.objects.exists())
