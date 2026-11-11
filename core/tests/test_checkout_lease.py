import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Event, local
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from core.billing import issue_invoice, record_payment
from core.gateway import apply_status, prepare_checkout, release_checkout, reserve_order, start_checkout, store_checkout
from core.midtrans import GatewayUnavailable
from core.models import AuditEvent, GatewayConfig, GatewayOrder, Membership, NetworkJob, Payment
from .auth_helpers import verified_login
from .test_payment_gateway import URL, account_keys, status_payload
from .test_workflow import fixtures


@override_settings(SIMULATION=True)
class CheckoutLeaseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        cls.invoice, _ = issue_invoice(cls.tenants[0], cls.customers[0].pk, date(2026, 8, 1), cls.owners[0])
        GatewayConfig.objects.create(tenant=cls.tenants[0], merchant_id="G-TEST", enabled=True)

    def setUp(self):
        self.keys = override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=account_keys(self.tenants[0]))
        self.keys.enable()
        self.addCleanup(self.keys.disable)
        self.order = reserve_order(self.owners[0], self.invoice.pk)
        verified_login(self.client, self.owners[0])

    def prepare(self):
        return prepare_checkout(self.owners[0], self.invoice.pk)

    def expire_lease(self):
        GatewayOrder.objects.filter(pk=self.order.pk).update(checkout_lease_until=timezone.now()-timedelta(seconds=1))

    def current(self):
        return GatewayOrder.objects.get(pk=self.order.pk)

    def test_success_releases_lease_and_reuses_private_checkout_url(self):
        with patch("core.gateway.SandboxClient.create", return_value=URL) as create:
            first = start_checkout(self.owners[0], self.invoice.pk)
            second = start_checkout(self.owners[0], self.invoice.pk)
        self.assertEqual((first.pk, second.pk), (self.order.pk, self.order.pk))
        self.assertEqual((second.checkout_url, second.status), (URL, "pending"))
        self.assertIsNone(second.checkout_lease_token)
        self.assertIsNone(second.checkout_lease_until)
        create.assert_called_once()
        self.assertFalse(Payment.objects.exists())

    def test_active_lease_coalesces_requests_without_new_remote_write(self):
        first, token = self.prepare()
        with patch("core.gateway.SandboxClient.create") as create:
            second = start_checkout(self.owners[0], self.invoice.pk)
        create.assert_not_called()
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.checkout_lease_token, token)
        self.assertTrue(second.checkout_in_progress)
        self.assertEqual(second.checkout_url, "")

    def test_timeout_can_retry_same_id_without_freeing_invoice(self):
        with patch("core.gateway.SandboxClient.create", side_effect=GatewayUnavailable):
            with self.assertRaises(GatewayUnavailable):
                start_checkout(self.owners[0], self.invoice.pk)
        self.assertIsNone(self.current().checkout_lease_token)
        with self.assertRaises(ValidationError):
            record_payment(self.tenants[0], self.invoice.pk, self.invoice.total, uuid.uuid4(), self.owners[0])
        with patch("core.gateway.SandboxClient.create", return_value=URL) as create:
            self.assertEqual(start_checkout(self.owners[0], self.invoice.pk).pk, self.order.pk)
        self.assertEqual(create.call_args.args[0].order_id, self.order.order_id)
        self.assertEqual(GatewayOrder.objects.count(), 1)

    def test_invalid_redirect_never_persists_and_releases_only_its_lease(self):
        with patch("core.gateway.SandboxClient.create", return_value="https://evil.example.test/checkout"):
            with self.assertRaises(GatewayUnavailable):
                start_checkout(self.owners[0], self.invoice.pk)
        self.assertEqual(self.current().checkout_url, "")
        self.assertIsNone(self.current().checkout_lease_token)
        self.assertEqual(self.current().status, "creating")

    def test_process_loss_leaves_bounded_lease_then_reclaims_same_order(self):
        with patch("core.gateway.SandboxClient.create", side_effect=RuntimeError("Process interrupted")):
            with self.assertRaises(RuntimeError):
                start_checkout(self.owners[0], self.invoice.pk)
        abandoned = self.current()
        self.assertTrue(abandoned.checkout_in_progress)
        self.assertLessEqual((abandoned.checkout_lease_until-timezone.now()).total_seconds(), 60)
        with patch("core.gateway.SandboxClient.create") as create:
            start_checkout(self.owners[0], self.invoice.pk)
        create.assert_not_called()
        self.expire_lease()
        with patch("core.gateway.SandboxClient.create", return_value=URL) as create:
            recovered = start_checkout(self.owners[0], self.invoice.pk)
        self.assertEqual(recovered.pk, self.order.pk)
        self.assertNotEqual(create.call_args.args[0].checkout_lease_token, abandoned.checkout_lease_token)
        self.assertEqual(recovered.checkout_url, URL)

    def test_old_failure_cannot_release_new_lease(self):
        _, old = self.prepare()
        self.expire_lease()
        _, current = self.prepare()
        release_checkout(self.order.pk, old)
        self.assertEqual(self.current().checkout_lease_token, current)
        self.assertTrue(self.current().checkout_in_progress)

    def test_old_success_cannot_publish_or_overwrite_after_takeover(self):
        _, old = self.prepare()
        self.expire_lease()
        _, current = self.prepare()
        old_url = URL.replace("fixture-token", "old-token")
        self.assertEqual(store_checkout(self.order.pk, old_url, old).checkout_url, "")
        self.assertEqual(self.current().checkout_lease_token, current)
        store_checkout(self.order.pk, URL, current)
        self.assertEqual(store_checkout(self.order.pk, old_url, old).checkout_url, URL)
        self.assertIsNone(self.current().checkout_lease_token)

    def test_expired_owner_can_finish_if_no_takeover_happened(self):
        _, token = self.prepare()
        self.expire_lease()
        result = store_checkout(self.order.pk, URL, token)
        self.assertEqual(result.checkout_url, URL)
        self.assertIsNone(result.checkout_lease_token)

    def test_pending_status_during_creation_allows_own_response(self):
        _, token = self.prepare()
        apply_status(self.order.pk, status_payload(self.order, "pending"))
        self.assertEqual(store_checkout(self.order.pk, URL, token).checkout_url, URL)
        self.assertFalse(Payment.objects.exists())

    def test_payment_wins_race_and_late_checkout_cannot_reopen_it(self):
        def create(order):
            apply_status(order.pk, status_payload(order))
            return URL
        with patch("core.gateway.SandboxClient.create", side_effect=create):
            result = start_checkout(self.owners[0], self.invoice.pk)
        self.assertEqual((result.status, result.checkout_url), ("settled", ""))
        self.assertIsNone(result.checkout_lease_token)
        self.assertEqual(self.invoice.balance, 0)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(NetworkJob.objects.count(), 1)

    def test_terminal_or_review_status_cannot_be_reopened_by_creation_result(self):
        for month, status in enumerate(["expire", "cancel", "deny", "refund"], 1):
            invoice, _ = issue_invoice(self.tenants[0], self.customers[0].pk, date(2027, month, 1), self.owners[0])
            order, token = prepare_checkout(self.owners[0], invoice.pk)
            verified = apply_status(order.pk, status_payload(order, status))
            result = store_checkout(order.pk, URL, token)
            self.assertEqual(result.status, verified.status)
            self.assertEqual(result.checkout_url, "")
            self.assertIsNone(result.checkout_lease_token)
            self.assertEqual(invoice.balance, invoice.total)

    def test_lease_expiry_does_not_release_reservation_or_allow_manual_payment(self):
        self.prepare()
        self.expire_lease()
        self.assertEqual(reserve_order(self.owners[0], self.invoice.pk).pk, self.order.pk)
        with self.assertRaises(ValidationError):
            record_payment(self.tenants[0], self.invoice.pk, 1, uuid.uuid4(), self.owners[0])
        self.assertEqual(self.invoice.balance, self.invoice.total)
        self.assertEqual(GatewayOrder.objects.count(), 1)

    def test_disabled_gateway_or_mode_cannot_take_over_existing_lease(self):
        _, token = self.prepare()
        self.expire_lease()
        GatewayConfig.objects.update(enabled=False)
        with patch("core.gateway.SandboxClient.create") as create, self.assertRaises(ValidationError):
            start_checkout(self.owners[0], self.invoice.pk)
        create.assert_not_called()
        self.assertEqual(self.current().checkout_lease_token, token)
        GatewayConfig.objects.update(enabled=True)
        with override_settings(SIMULATION=False), self.assertRaises(ValidationError):
            start_checkout(self.owners[0], self.invoice.pk)

    def test_get_resume_ui_claims_nothing_and_expired_lease_can_post(self):
        self.prepare()
        active = self.current()
        with patch("core.gateway.SandboxClient.create") as create:
            response = self.client.get(f"/payments/orders/{self.order.pk}/")
        create.assert_not_called()
        self.assertContains(response, "Muat ulang halaman checkout")
        self.assertNotContains(response, "Lanjutkan checkout yang sama")
        self.assertNotContains(response, str(active.checkout_lease_token))
        self.expire_lease()
        self.assertContains(self.client.get(f"/payments/orders/{self.order.pk}/"), "Lanjutkan checkout yang sama")
        with patch("core.gateway.SandboxClient.create", return_value=URL):
            response = self.client.post(f"/payments/orders/{self.order.pk}/resume/")
        self.assertEqual(response.url, f"/payments/orders/{self.order.pk}/")
        self.assertEqual(self.current().checkout_url, URL)

    def test_resume_post_still_requires_csrf_and_staff_recent_mfa(self):
        secure = Client(enforce_csrf_checks=True)
        verified_login(secure, self.owners[0])
        self.assertEqual(secure.post(f"/payments/orders/{self.order.pk}/resume/").status_code, 403)
        self.assertEqual(self.client.get(f"/payments/orders/{self.order.pk}/resume/").status_code, 405)
        session = self.client.session
        session["mfa_verified_at"] = time.time()-301
        session.save()
        with patch("core.gateway.SandboxClient.create") as create:
            response = self.client.post(f"/payments/orders/{self.order.pk}/resume/")
        self.assertEqual(response.url, "/account/security/")
        create.assert_not_called()
        self.assertIsNone(self.current().checkout_lease_token)

    def test_scope_and_payment_rights_checked_before_claim(self):
        other, _ = issue_invoice(self.tenants[1], self.customers[1].pk, date(2026, 8, 1), self.owners[1])
        foreign = GatewayOrder.objects.create(tenant=self.tenants[1], invoice=other, created_by=self.owners[1], merchant_id="G-OTHER", amount=other.total)
        self.assertEqual(self.client.post(f"/payments/orders/{foreign.pk}/resume/").status_code, 404)
        Membership.objects.filter(user=self.owners[0]).update(role="ops")
        self.assertEqual(self.client.post(f"/payments/orders/{self.order.pk}/resume/").status_code, 403)
        self.assertIsNone(self.current().checkout_lease_token)
        self.assertNotContains(self.client.get(f"/payments/orders/{self.order.pk}/"), "Lanjutkan checkout yang sama")

    def test_resume_control_hidden_when_checkout_disabled_or_url_already_stored(self):
        GatewayConfig.objects.update(enabled=False)
        self.assertNotContains(self.client.get(f"/payments/orders/{self.order.pk}/"), "Lanjutkan checkout yang sama")
        GatewayConfig.objects.update(enabled=True)
        with patch("core.gateway.SandboxClient.create", return_value=URL):
            start_checkout(self.owners[0], self.invoice.pk)
        self.assertNotContains(self.client.get(f"/payments/orders/{self.order.pk}/"), "Lanjutkan checkout yang sama")

    def test_browser_cannot_supply_lease_identity_or_expiry(self):
        forged = uuid.uuid4()
        def create(order):
            self.assertNotEqual(order.checkout_lease_token, forged)
            self.assertLess((order.checkout_lease_until-timezone.now()).total_seconds(), 61)
            return URL
        with patch("core.gateway.SandboxClient.create", side_effect=create):
            response = self.client.post(f"/payments/orders/{self.order.pk}/resume/", {
                "checkout_lease_token": str(forged), "checkout_lease_until": "2099-01-01", "amount": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.current().amount, self.invoice.total)

    def test_stale_resume_after_terminal_status_never_creates_replacement_order(self):
        apply_status(self.order.pk, status_payload(self.order, "expire"))
        with patch("core.gateway.SandboxClient.create") as create:
            response = self.client.post(f"/payments/orders/{self.order.pk}/resume/")
        create.assert_not_called()
        self.assertEqual(response.url, f"/payments/orders/{self.order.pk}/")
        self.assertEqual(GatewayOrder.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="gateway.reserved").count(), 1)
        self.assertEqual(self.current().status, "expire")

    def test_stale_resume_cannot_claim_another_replacement_already_reserved(self):
        apply_status(self.order.pk, status_payload(self.order, "expire"))
        replacement = reserve_order(self.owners[0], self.invoice.pk)
        with patch("core.gateway.SandboxClient.create") as create:
            self.client.post(f"/payments/orders/{self.order.pk}/resume/")
        create.assert_not_called()
        replacement.refresh_from_db()
        self.assertIsNone(replacement.checkout_lease_token)
        self.assertEqual(GatewayOrder.objects.count(), 2)

    def test_store_database_retry_does_not_repeat_provider_post(self):
        original, failed = GatewayOrder.save, False
        def save(order, *args, **kwargs):
            nonlocal failed
            if "checkout_url" in (kwargs.get("update_fields") or []) and not failed:
                failed = True
                raise OperationalError("database is locked")
            return original(order, *args, **kwargs)
        with patch.object(GatewayOrder, "save", save), patch("core.gateway.SandboxClient.create", return_value=URL) as create:
            result = start_checkout(self.owners[0], self.invoice.pk)
        create.assert_called_once()
        self.assertEqual(result.checkout_url, URL)
        self.assertIsNone(result.checkout_lease_token)

    def test_prepare_database_retry_never_sends_before_lease_is_persisted(self):
        original, failed = GatewayOrder.save, False
        def save(order, *args, **kwargs):
            nonlocal failed
            fields = kwargs.get("update_fields") or []
            if "checkout_lease_token" in fields and "checkout_url" not in fields and not failed:
                failed = True
                raise OperationalError("database is locked")
            return original(order, *args, **kwargs)
        def create(order):
            self.assertEqual(self.current().checkout_lease_token, order.checkout_lease_token)
            return URL
        with patch.object(GatewayOrder, "save", save), patch("core.gateway.SandboxClient.create", side_effect=create) as remote:
            start_checkout(self.owners[0], self.invoice.pk)
        remote.assert_called_once()
        self.assertEqual(GatewayOrder.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="gateway.reserved").count(), 1)


@override_settings(SIMULATION=True)
class CheckoutLeaseConcurrencyTests(TransactionTestCase):
    def test_parallel_creation_has_one_provider_post_and_http_is_outside_transaction(self):
        tenants, owners, customers = fixtures()
        invoice, _ = issue_invoice(tenants[0], customers[0].pk, date(2026, 8, 1), owners[0])
        GatewayConfig.objects.create(tenant=tenants[0], merchant_id="G-TEST", enabled=True)
        first_started, second_finished, role = Event(), Event(), local()
        with override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=account_keys(tenants[0])):
            def create(order):
                self.assertFalse(connection.in_atomic_block)
                self.assertEqual(role.number, 0)
                first_started.set()
                if not second_finished.wait(10):
                    raise AssertionError("Second creator did not finish")
                return URL
            def start(number):
                close_old_connections()
                role.number = number
                try:
                    if number and not first_started.wait(10):
                        raise AssertionError("First creator did not start")
                    result = start_checkout(get_user_model().objects.get(pk=owners[0].pk), invoice.pk)
                    return result.pk, bool(result.checkout_url)
                finally:
                    if number:
                        second_finished.set()
                    close_old_connections()
            with patch("core.gateway.SandboxClient.create", side_effect=create) as remote, ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(start, [0, 1]))
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual((results[0][1], results[1][1]), (True, False))
        self.assertEqual(remote.call_count, 1)
        self.assertEqual(GatewayOrder.objects.count(), 1)
        self.assertIsNone(GatewayOrder.objects.get().checkout_lease_token)
        self.assertFalse(Payment.objects.exists())
