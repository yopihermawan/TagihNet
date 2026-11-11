import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event
from unittest.mock import MagicMock, patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, close_old_connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from core.billing import issue_invoice, record_payment
from core.gateway import apply_status, reserve_order
from core.gateway_expiry import expire_order, prepare_expiry
from core.midtrans import GatewayUnavailable, SandboxClient
from core.models import AuditEvent, GatewayConfig, GatewayEvent, GatewayExpiry, GatewayOrder, Membership, NetworkJob, Payment
from .auth_helpers import verified_login
from .test_payment_gateway import KEY, account_keys, status_payload
from .test_workflow import fixtures

REASON = "Pelanggan meminta mengganti metode pembayaran sandbox"


@override_settings(SIMULATION=True)
class GatewayExpiryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        cls.invoice, _ = issue_invoice(cls.tenants[0], cls.customers[0].pk, date(2026, 7, 1), cls.owners[0])
        GatewayConfig.objects.create(tenant=cls.tenants[0], merchant_id="G-TEST", enabled=True)

    def setUp(self):
        self.keys = override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=account_keys(self.tenants[0]))
        self.keys.enable()
        self.addCleanup(self.keys.disable)
        self.order = reserve_order(self.owners[0], self.invoice.pk)
        self.key = uuid.uuid4()
        verified_login(self.client, self.owners[0])

    def expire(self, key=None, reason=REASON):
        return expire_order(self.owners[0], self.order.pk, key or self.key, reason, time.time(), 1)

    def form(self, **changes):
        return {"request_key": self.key, "reason": REASON, "reviewed": "on", **changes}

    def test_verified_expiry_releases_checkout_without_marking_paid(self):
        with patch("core.gateway.SandboxClient.status", side_effect=[status_payload(self.order, "pending"), status_payload(self.order, "expire")]), patch("core.gateway_expiry.SandboxClient.expire", return_value={}) as remote:
            result = self.expire()
        self.assertEqual(result.status, "confirmed")
        remote.assert_called_once()
        self.assertEqual(remote.call_args.args[0].order_id, self.order.order_id)
        self.assertEqual(self.invoice.balance, self.invoice.total)
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(NetworkJob.objects.exists())
        self.assertNotEqual(reserve_order(self.owners[0], self.invoice.pk).pk, self.order.pk)

    def test_successful_write_response_cannot_replace_status_verification(self):
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(self.order, "pending")), patch("core.gateway_expiry.SandboxClient.expire", return_value=status_payload(self.order, "expire")):
            result = self.expire()
        self.assertEqual(result.status, "unavailable")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "pending")
        self.assertEqual(reserve_order(self.owners[0], self.invoice.pk).pk, self.order.pk)
        with self.assertRaises(ValidationError):
            record_payment(self.tenants[0], self.invoice.pk, 1000, uuid.uuid4(), self.owners[0])

    def test_unknown_status_never_dispatches_expiry_or_releases_order(self):
        with patch("core.gateway.SandboxClient.status", side_effect=GatewayUnavailable), patch("core.gateway_expiry.SandboxClient.expire") as remote:
            result = self.expire()
        remote.assert_not_called()
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(reserve_order(self.owners[0], self.invoice.pk).pk, self.order.pk)

    def test_write_timeout_followed_by_terminal_status_is_confirmed(self):
        with patch("core.gateway.SandboxClient.status", side_effect=[status_payload(self.order, "pending"), status_payload(self.order, "expire")]), patch("core.gateway_expiry.SandboxClient.expire", side_effect=GatewayUnavailable):
            self.assertEqual(self.expire().status, "confirmed")

    def test_unverified_write_is_resumable_with_same_request(self):
        with patch("core.gateway.SandboxClient.status", side_effect=[status_payload(self.order, "pending"), GatewayUnavailable]), patch("core.gateway_expiry.SandboxClient.expire", return_value={}):
            first = self.expire()
        self.assertEqual(first.status, "unavailable")
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(self.order, "expire")), patch("core.gateway_expiry.SandboxClient.expire") as remote:
            second = self.expire()
        remote.assert_not_called()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.status, "confirmed")
        self.assertEqual(GatewayExpiry.objects.count(), 1)

    def test_confirmed_replay_does_not_call_gateway_or_duplicate_audit(self):
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(self.order, "expire")):
            first = self.expire()
        with patch("core.gateway.SandboxClient.status") as status, patch("core.gateway_expiry.SandboxClient.expire") as remote:
            second = self.expire()
        self.assertEqual(first.pk, second.pk)
        status.assert_not_called()
        remote.assert_not_called()
        self.assertEqual(GatewayExpiry.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="gateway.expiry_confirmed").count(), 1)

    def test_audit_lock_retries_intent_atomically_without_repeating_network(self):
        from core.billing import audit as write_audit
        failed = False
        def audit(tenant, actor, action, *args, **kwargs):
            nonlocal failed
            if action == "gateway.expiry_attempt" and not failed:
                failed = True
                raise OperationalError("database is locked")
            return write_audit(tenant, actor, action, *args, **kwargs)
        with patch("core.gateway_expiry.audit", side_effect=audit), patch("core.gateway.SandboxClient.status", side_effect=[status_payload(self.order,"pending"),status_payload(self.order,"expire")]), patch("core.gateway_expiry.SandboxClient.expire", return_value={}) as remote:
            self.assertEqual(self.expire().status, "confirmed")
        remote.assert_called_once()
        self.assertTrue(failed)
        self.assertEqual(GatewayExpiry.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="gateway.expiry_requested").count(), 1)

    def test_payment_before_write_is_recorded_and_expiry_blocked(self):
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(self.order)), patch("core.gateway_expiry.SandboxClient.expire") as remote:
            result = self.expire()
        remote.assert_not_called()
        self.assertEqual(result.status, "blocked")
        self.assertEqual(self.invoice.balance, 0)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(NetworkJob.objects.count(), 1)

    def test_payment_racing_expiry_keeps_one_payment_and_blocks_release(self):
        with patch("core.gateway.SandboxClient.status", side_effect=[status_payload(self.order, "pending"), status_payload(self.order)]), patch("core.gateway_expiry.SandboxClient.expire", side_effect=GatewayUnavailable):
            result = self.expire()
        self.assertEqual(result.status, "blocked")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "settled")
        self.assertEqual(self.invoice.balance, 0)
        self.assertEqual(Payment.objects.count(), 1)

    def test_disputed_order_discovered_before_dispatch_is_not_expired(self):
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(self.order, "refund")), patch("core.gateway_expiry.SandboxClient.expire") as remote:
            self.assertEqual(self.expire().status, "blocked")
        remote.assert_not_called()
        self.assertFalse(Payment.objects.exists())

    def test_mismatched_confirmation_does_not_release_reservation(self):
        with patch("core.gateway.SandboxClient.status", side_effect=[status_payload(self.order, "pending"), status_payload(self.order, "expire", gross_amount="1.00")]), patch("core.gateway_expiry.SandboxClient.expire", return_value={}):
            self.assertEqual(self.expire().status, "unavailable")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "pending")

    def test_revoked_session_after_preflight_cannot_dispatch_write(self):
        def preflight(order):
            Membership.objects.filter(user=self.owners[0]).update(session_version=2)
            return status_payload(order, "pending")
        with patch("core.gateway.SandboxClient.status", side_effect=preflight), patch("core.gateway_expiry.SandboxClient.expire") as remote:
            self.assertEqual(self.expire().status, "blocked")
        remote.assert_not_called()

    def test_disabling_new_checkout_allows_existing_order_expiry(self):
        GatewayConfig.objects.update(enabled=False)
        with patch("core.gateway.SandboxClient.status", side_effect=[status_payload(self.order, "pending"), status_payload(self.order, "expire")]), patch("core.gateway_expiry.SandboxClient.expire", return_value={}):
            self.assertEqual(self.expire().status, "confirmed")

    def test_request_key_cannot_be_reused_with_different_reason(self):
        with patch("core.gateway.SandboxClient.status", side_effect=GatewayUnavailable):
            self.expire()
        with patch("core.gateway.SandboxClient.status") as status, self.assertRaises(ValidationError):
            self.expire(reason="Alasan lain yang tidak sama dengan permintaan awal")
        status.assert_not_called()
        self.assertEqual(GatewayExpiry.objects.count(), 1)

    def test_missing_credentials_or_live_mode_fail_before_network(self):
        for settings in [{"MIDTRANS_SANDBOX_ACCOUNT_KEYS": {}}, {"SIMULATION": False}]:
            with self.subTest(settings=settings), override_settings(**settings), patch("core.gateway.SandboxClient.status") as status, self.assertRaises(ValidationError):
                self.expire()
            status.assert_not_called()
        self.assertFalse(GatewayExpiry.objects.exists())

    def test_domain_rejects_short_reason_bad_key_and_stale_mfa(self):
        for reason, key in [("short", self.key), (REASON, "bad-key"), ("text\x00invalid", self.key)]:
            with self.subTest(reason=reason), self.assertRaises(ValidationError):
                prepare_expiry(self.owners[0], self.order.pk, key, reason, time.time(), 1)
        with self.assertRaises(PermissionDenied):
            prepare_expiry(self.owners[0], self.order.pk, self.key, REASON, time.time()-301, 1)
        self.assertFalse(GatewayExpiry.objects.exists())

    def test_http_requires_owner_scope_csrf_and_post(self):
        url = f"/payments/orders/{self.order.pk}/expire/"
        secure = Client(enforce_csrf_checks=True)
        verified_login(secure, self.owners[0])
        self.assertEqual(secure.post(url, self.form()).status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 405)
        verified_login(self.client, self.owners[1])
        self.assertEqual(self.client.post(url, self.form()).status_code, 404)
        for role in ["finance", "ops", "customer"]:
            Membership.objects.filter(user=self.owners[0]).update(role=role, customer=self.customers[0] if role=="customer" else None)
            verified_login(self.client, self.owners[0])
            self.assertEqual(self.client.post(url, self.form()).status_code, 403)
        self.assertFalse(GatewayExpiry.objects.exists())

    def test_http_requires_review_and_reason_and_fresh_mfa(self):
        url = f"/payments/orders/{self.order.pk}/expire/"
        with patch("core.gateway.SandboxClient.status") as status:
            self.assertEqual(self.client.post(url, self.form(reviewed="")).status_code, 400)
            self.assertEqual(self.client.post(url, self.form(reason="bad")).status_code, 400)
            session = self.client.session
            session["mfa_verified_at"] = time.time()-301
            session.save()
            self.assertRedirects(self.client.post(url, self.form()), "/account/security/")
        status.assert_not_called()
        self.assertEqual(self.client.session["stepup_return"], f"/payments/orders/{self.order.pk}/")
        self.assertFalse(GatewayExpiry.objects.exists())

    def test_http_expiry_and_replay_keep_one_request_and_plaintext_reason(self):
        url = f"/payments/orders/{self.order.pk}/expire/"
        reason = "Permintaan <script>alert(1)</script> tidak akan dijalankan"
        with patch("core.gateway.SandboxClient.status", side_effect=[status_payload(self.order, "pending"), status_payload(self.order, "expire")]), patch("core.gateway_expiry.SandboxClient.expire", return_value={}):
            self.assertRedirects(self.client.post(url, self.form(reason=reason)), f"/payments/orders/{self.order.pk}/")
        with patch("core.gateway.SandboxClient.status") as status:
            self.assertRedirects(self.client.post(url, self.form(reason=reason)), f"/payments/orders/{self.order.pk}/")
        status.assert_not_called()
        response = self.client.get(f"/payments/orders/{self.order.pk}/")
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "Dikonfirmasi berakhir")
        self.assertNotContains(response, "Minta pengakhiran pembayaran")
        self.assertEqual(GatewayExpiry.objects.count(), 1)

    def test_get_form_does_not_call_gateway_and_retry_preserves_request(self):
        with patch("core.gateway.SandboxClient.status", side_effect=GatewayUnavailable):
            expiry = self.expire()
        with patch("core.gateway.SandboxClient.status") as status:
            response = self.client.get(f"/payments/orders/{self.order.pk}/")
        status.assert_not_called()
        self.assertEqual(response.context["expiry_form"].initial["request_key"], expiry.request_key)
        self.assertTrue(response.context["expiry_form"].fields["reason"].disabled)

    def test_transport_expiry_is_fixed_sandbox_endpoint_with_empty_json(self):
        response = MagicMock()
        response.status, response.read.return_value = 200, b'{}'
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch("core.midtrans.urllib.request.build_opener", return_value=opener):
            SandboxClient().expire(self.order)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, f"https://api.sandbox.midtrans.com/v2/{self.order.order_id}/expire")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b'{}')


@override_settings(SIMULATION=True)
class ExpiryConcurrencyTests(TransactionTestCase):
    def test_same_request_can_finish_twice_without_duplicate_effects(self):
        tenants, owners, customers = fixtures()
        invoice, _ = issue_invoice(tenants[0], customers[0].pk, date(2026, 8, 1), owners[0])
        GatewayConfig.objects.create(tenant=tenants[0], merchant_id="G-TEST", enabled=True)
        with override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=account_keys(tenants[0])):
            order = reserve_order(owners[0], invoice.pk)
            remote_expired = Event()
            key, owner_id, order_id = uuid.uuid4(), owners[0].pk, order.pk
            def status(current):
                return status_payload(current, "expire" if remote_expired.is_set() else "pending")
            def expire(current):
                self.assertEqual(current.order_id, order.order_id)
                remote_expired.set()
                return {}
            def run(_):
                close_old_connections()
                try:
                    actor = get_user_model().objects.get(pk=owner_id)
                    result = expire_order(actor, order_id, key, REASON, time.time(), 1)
                    return result.pk, result.status
                finally:
                    close_old_connections()
            with patch("core.gateway.SandboxClient.status", side_effect=status), patch("core.gateway_expiry.SandboxClient.expire", side_effect=expire), ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(run, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][1], "confirmed")
        self.assertEqual(GatewayExpiry.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="gateway.expiry_confirmed").count(), 1)
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(NetworkJob.objects.exists())
