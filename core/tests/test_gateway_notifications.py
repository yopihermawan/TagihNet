import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event, local
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import OperationalError, close_old_connections, connection
from django.db.models.query import QuerySet
from django.test import Client, TestCase, TransactionTestCase, override_settings
from core.billing import issue_invoice
from core.gateway import refresh_order, reserve_order
from core.gateway_notifications import begin_delivery, finish_delivery, notification_overview
from core.gateway_worker import process_policy, save_policy
from core.midtrans import GatewayUnavailable
from core.models import AuditEvent, GatewayConfig, GatewayEvent, GatewayNotificationState, GatewayOrder, Membership, NetworkJob, Payment
from .auth_helpers import verified_login
from .test_payment_gateway import account_keys, notification_payload, status_payload
from .test_workflow import fixtures

ENDPOINT = "/payments/midtrans/notifications/"


@override_settings(SIMULATION=True)
class GatewayNotificationTests(TestCase):
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

    def notify(self, payload=None, client=None):
        return (client or Client(enforce_csrf_checks=True)).post(ENDPOINT,
            data=json.dumps(payload or notification_payload(self.order)), content_type="application/json")

    def state(self):
        return GatewayNotificationState.objects.get(order=self.order)

    def test_invalid_signature_or_payload_and_unknown_order_create_no_metadata(self):
        invalid = notification_payload(self.order)
        invalid["signature_key"] = "0"*128
        unknown = notification_payload(self.order, order_id="TN-SBX-"+uuid.uuid4().hex)
        with patch("core.gateway.SandboxClient.status") as status:
            self.assertEqual(self.notify(invalid).status_code, 403)
            self.assertEqual(self.notify(unknown).status_code, 404)
            self.assertEqual(self.client.post(ENDPOINT, "{bad", content_type="application/json").status_code, 400)
            self.assertEqual(self.client.get(ENDPOINT).status_code, 405)
        status.assert_not_called()
        self.assertFalse(GatewayNotificationState.objects.exists())

    def test_missing_credentials_or_mode_cannot_authenticate_delivery(self):
        with override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS={}):
            self.assertEqual(self.notify().status_code, 503)
        with override_settings(SIMULATION=False):
            self.assertEqual(self.notify().status_code, 503)
        self.assertFalse(GatewayNotificationState.objects.exists())

    def test_verified_delivery_can_be_pending_without_payment(self):
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(self.order, "pending")):
            self.assertEqual(self.notify().status_code, 200)
        state = self.state()
        self.assertEqual((state.delivery_count, state.last_outcome), (1, "verified"))
        self.assertIsNotNone(state.last_completed_at)
        self.assertEqual(GatewayOrder.objects.get(pk=self.order.pk).status, "pending")
        self.assertEqual(self.invoice.balance, self.invoice.total)
        self.assertFalse(Payment.objects.exists())

    def test_outage_is_recorded_and_provider_retry_still_required(self):
        with patch("core.gateway.SandboxClient.status", side_effect=GatewayUnavailable("PRIVATE TRANSPORT DETAIL")):
            response = self.notify()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "verification unavailable"})
        state = self.state()
        self.assertEqual((state.delivery_count, state.last_outcome), (1, "unavailable"))
        self.assertGreaterEqual(state.last_completed_at, state.last_received_at)
        self.assertEqual(self.invoice.balance, self.invoice.total)
        self.assertFalse(Payment.objects.exists())

    def test_failed_delivery_then_successful_retries_keep_one_payment(self):
        with patch("core.gateway.SandboxClient.status", side_effect=GatewayUnavailable):
            self.notify()
        first = self.state()
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(self.order)):
            self.assertEqual(self.notify().status_code, 200)
            self.assertEqual(self.notify().status_code, 200)
        state = self.state()
        self.assertEqual((state.delivery_count, state.last_outcome), (3, "verified"))
        self.assertGreater(state.last_received_at, first.last_received_at)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(NetworkJob.objects.count(), 1)
        self.assertEqual(GatewayEvent.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="payment.midtrans_sandbox").count(), 1)

    def test_api_identity_mismatch_is_not_a_verified_delivery(self):
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(self.order, merchant_id="WRONG")):
            self.assertEqual(self.notify().status_code, 503)
        self.assertEqual(self.state().last_outcome, "unavailable")
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(GatewayEvent.objects.exists())

    def test_refund_status_is_verified_but_requires_ledger_review(self):
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(self.order, "refund")):
            self.assertEqual(self.notify().status_code, 200)
        self.assertEqual(self.state().last_outcome, "verified")
        self.assertEqual(GatewayOrder.objects.get(pk=self.order.pk).status, "review")
        self.assertFalse(Payment.objects.exists())

    def test_invalid_replay_cannot_poison_existing_metadata(self):
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(self.order)):
            self.notify()
        before = self.state()
        payload = notification_payload(self.order)
        payload["signature_key"] = "0"*128
        self.assertEqual(self.notify(payload).status_code, 403)
        after = self.state()
        self.assertEqual((after.delivery_count, after.last_received_at, after.last_outcome, after.last_completed_at),
            (before.delivery_count, before.last_received_at, before.last_outcome, before.last_completed_at))

    def test_manual_and_worker_checks_do_not_replace_notification_result(self):
        with patch("core.gateway.SandboxClient.status", side_effect=GatewayUnavailable):
            self.notify()
        before = self.state()
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(self.order)):
            refresh_order(self.order)
            policy = save_policy(self.owners[0], True, True, 60, 0, time.time(), 1)
            self.assertEqual(process_policy(policy.pk).last_verified, 1)
        after = self.state()
        self.assertEqual((after.delivery_count, after.last_completed_at, after.last_outcome),
            (1, before.last_completed_at, "unavailable"))
        self.assertEqual(self.invoice.balance, 0)

    def test_late_success_cannot_replace_newer_failure(self):
        first, second = begin_delivery(self.order.pk), begin_delivery(self.order.pk)
        finish_delivery(self.order.pk, second, "unavailable")
        latest = self.state()
        finish_delivery(self.order.pk, first, "verified")
        self.assertEqual(self.state().last_outcome, "unavailable")
        self.assertEqual(self.state().last_completed_at, latest.last_completed_at)
        self.assertEqual(self.state().delivery_count, 2)

    def test_late_failure_or_duplicate_completion_cannot_replace_newer_success(self):
        first, second = begin_delivery(self.order.pk), begin_delivery(self.order.pk)
        finish_delivery(self.order.pk, second, "verified")
        latest = self.state()
        finish_delivery(self.order.pk, first, "unavailable")
        finish_delivery(self.order.pk, second, "unavailable")
        self.assertEqual(self.state().last_outcome, "verified")
        self.assertEqual(self.state().last_completed_at, latest.last_completed_at)

    def test_new_attempt_clears_only_latest_completion_and_unfinished_is_honest(self):
        first = begin_delivery(self.order.pk)
        finish_delivery(self.order.pk, first, "verified")
        second = begin_delivery(self.order.pk)
        state = self.state()
        self.assertEqual((second, state.last_outcome, state.last_completed_at), (2, "received", None))
        self.assertIsNone(GatewayOrder.objects.get(pk=self.order.pk).last_verified_at)
        verified_login(self.client, self.owners[0])
        response = self.client.get(f"/payments/orders/{self.order.pk}/")
        self.assertContains(response, "Proses dapat masih berjalan atau terputus")

    def test_unexpected_process_error_leaves_result_unknown_not_success(self):
        client = Client(raise_request_exception=False)
        with patch("core.gateway.SandboxClient.status", side_effect=RuntimeError("Interrupted fixture")):
            self.assertEqual(self.notify(client=client).status_code, 500)
        self.assertEqual((self.state().last_outcome, self.state().last_completed_at), ("received", None))
        self.assertFalse(Payment.objects.exists())

    def test_invalid_completion_is_rejected_without_mutation(self):
        begin_delivery(self.order.pk)
        for sequence, outcome in [(0, "verified"), (True, "verified"), (1, "settled"), ("1", "unavailable")]:
            with self.subTest(sequence=sequence), self.assertRaises(ValidationError):
                finish_delivery(self.order.pk, sequence, outcome)
        self.assertEqual(self.state().last_outcome, "received")

    def test_begin_failure_returns_retryable_response_without_provider_call(self):
        with patch("core.gateway_notifications.begin_delivery", side_effect=OperationalError("database unavailable")), patch("core.gateway.SandboxClient.status") as status:
            self.assertEqual(self.notify().status_code, 503)
        status.assert_not_called()
        self.assertFalse(GatewayNotificationState.objects.exists())

    def test_completion_failure_after_payment_commit_is_safe_to_retry(self):
        with patch("core.gateway_notifications.finish_delivery", side_effect=OperationalError("database unavailable")), patch("core.gateway.SandboxClient.status", return_value=status_payload(self.order)):
            self.assertEqual(self.notify().status_code, 503)
        self.assertEqual((self.state().last_outcome, self.state().last_completed_at), ("received", None))
        self.assertEqual(Payment.objects.count(), 1)
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(self.order)):
            self.assertEqual(self.notify().status_code, 200)
        self.assertEqual((self.state().delivery_count, self.state().last_outcome), (2, "verified"))
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(NetworkJob.objects.count(), 1)

    def test_locked_metadata_insert_retries_database_without_repeating_http(self):
        original, failed = QuerySet.create, False
        def create(query, **kwargs):
            nonlocal failed
            if query.model is GatewayNotificationState and not failed:
                failed = True
                raise OperationalError("database is locked")
            return original(query, **kwargs)
        with patch.object(QuerySet, "create", create), patch("core.gateway.SandboxClient.status", return_value=status_payload(self.order)) as status:
            self.assertEqual(self.notify().status_code, 200)
        self.assertEqual(self.state().delivery_count, 1)
        self.assertEqual(status.call_count, 1)

    def test_payload_signature_keys_and_exception_text_are_never_persisted(self):
        payload = notification_payload(self.order, private_marker="PRIVATE BODY MARKER")
        with patch("core.gateway.SandboxClient.status", side_effect=GatewayUnavailable("PRIVATE EXCEPTION DETAIL")):
            response = self.notify(payload)
        stored = json.dumps(list(GatewayNotificationState.objects.values()), default=str)
        stored += json.dumps(list(AuditEvent.objects.values()), default=str)+response.content.decode()
        for value in ["PRIVATE BODY MARKER", "PRIVATE EXCEPTION DETAIL", payload["signature_key"], "SB-Mid-server-"]:
            self.assertNotIn(value, stored)

    def test_order_and_overview_are_owner_only_scoped_and_get_has_no_side_effects(self):
        with patch("core.gateway.SandboxClient.status", side_effect=GatewayUnavailable):
            self.notify()
        verified_login(self.client, self.owners[0])
        with patch("core.gateway.SandboxClient.status") as status:
            response = self.client.get(f"/payments/orders/{self.order.pk}/")
            settings = self.client.get("/settings/payment-gateway/")
        status.assert_not_called()
        self.assertContains(response, "Notifikasi Midtrans")
        self.assertContains(response, "Verifikasi pengiriman terakhir belum berhasil")
        self.assertContains(settings, "Penerimaan notifikasi")
        self.assertEqual(settings.context["notifications"]["deliveries"], 1)
        verified_login(self.client, self.owners[1])
        self.assertEqual(self.client.get(f"/payments/orders/{self.order.pk}/").status_code, 404)
        self.assertEqual(self.client.get("/settings/payment-gateway/").context["notifications"]["orders"], 0)
        for role in ["finance", "customer"]:
            user = get_user_model().objects.create_user(username="notification-"+role)
            Membership.objects.create(user=user, tenant=self.tenants[0], role=role, customer=self.customers[0] if role == "customer" else None)
            verified_login(self.client, user)
            self.assertNotContains(self.client.get(f"/payments/orders/{self.order.pk}/"), "Notifikasi Midtrans")

    def test_no_metadata_is_not_presented_as_proof_no_notification_ever_arrived(self):
        verified_login(self.client, self.owners[0])
        response = self.client.get(f"/payments/orders/{self.order.pk}/")
        self.assertContains(response, "Belum ada catatan pengiriman terautentikasi")
        self.assertContains(self.client.get("/settings/payment-gateway/"), "Konfigurasi alamat tidak membuktikan notifikasi sudah diterima")
        self.assertFalse(GatewayNotificationState.objects.exists())

    def test_overview_counts_latest_outcomes_per_order_not_settled_payments(self):
        first = begin_delivery(self.order.pk)
        finish_delivery(self.order.pk, first, "verified")
        begin_delivery(self.order.pk)
        overview = notification_overview(self.tenants[0])
        self.assertEqual((overview["orders"], overview["deliveries"], overview["unfinished"], overview["verified"]), (1, 2, 1, 0))
        self.assertFalse(Payment.objects.exists())


@override_settings(SIMULATION=True)
class NotificationConcurrencyTests(TransactionTestCase):
    def test_two_deliveries_and_out_of_order_completion_preserve_latest_failure_and_payment(self):
        tenants, owners, customers = fixtures()
        invoice, _ = issue_invoice(tenants[0], customers[0].pk, date(2026, 8, 1), owners[0])
        GatewayConfig.objects.create(tenant=tenants[0], merchant_id="G-TEST", enabled=True)
        with override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=account_keys(tenants[0])):
            order = reserve_order(owners[0], invoice.pk)
            first_started, second_finished, role = Event(), Event(), local()
            def status(current):
                self.assertFalse(connection.in_atomic_block)
                if role.number == 0:
                    first_started.set()
                    if not second_finished.wait(10):
                        raise AssertionError("Second callback did not finish")
                    return status_payload(current)
                raise GatewayUnavailable
            def send(number):
                close_old_connections()
                role.number = number
                try:
                    if number and not first_started.wait(10):
                        raise AssertionError("First callback did not start")
                    return Client(enforce_csrf_checks=True).post(ENDPOINT, data=json.dumps(notification_payload(order)), content_type="application/json").status_code
                finally:
                    if number:
                        second_finished.set()
                    close_old_connections()
            with patch("core.gateway.SandboxClient.status", side_effect=status), ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(send, [0, 1]))
        self.assertEqual(responses, [200, 503])
        state = GatewayNotificationState.objects.get(order=order)
        self.assertEqual((state.delivery_count, state.last_outcome), (2, "unavailable"))
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(NetworkJob.objects.count(), 1)
        self.assertEqual(invoice.balance, 0)
