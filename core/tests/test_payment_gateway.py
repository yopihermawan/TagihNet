import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from io import StringIO
from unittest.mock import patch
from types import SimpleNamespace
import urllib.error
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import close_old_connections
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from core.billing import issue_invoice, record_payment, reverse_payment
from core.gateway import apply_status, refresh_order, reserve_order, save_config, start_checkout
from core.midtrans import GatewayUnavailable, SandboxClient, checkout_url, credentials, json_object
from core.models import Customer, GatewayConfig, GatewayEvent, GatewayOrder, Membership, NetworkJob, Payment
from core.network import run_job
from .auth_helpers import verified_login
from .test_workflow import fixtures

KEY = "SB-Mid-server-local-test-only"
URL = "https://app.sandbox.midtrans.com/snap/v4/redirection/fixture-token"


def account_keys(tenant):
    return {str(tenant.pk): {"merchant_id": "G-TEST", "server_key": KEY}}


def status_payload(order, status="settlement", **changes):
    payload = {"order_id": order.order_id, "merchant_id": "G-TEST", "currency": "IDR",
        "gross_amount": f"{order.amount}.00", "transaction_id": f"txn-{order.pk.hex}",
        "transaction_status": status, "status_code": "200", "fraud_status": "accept"}
    payload.update(changes)
    return payload


def notification_payload(order, **changes):
    payload = status_payload(order)
    payload.update(changes)
    payload["signature_key"] = hashlib.sha512((payload["order_id"] + payload["status_code"] + payload["gross_amount"] + KEY).encode()).hexdigest()
    return payload


@override_settings(SIMULATION=True)
class PaymentGatewayTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        cls.invoice, _ = issue_invoice(cls.tenants[0], cls.customers[0].pk, date(2026, 4, 1), cls.owners[0])
        cls.other_invoice, _ = issue_invoice(cls.tenants[1], cls.customers[1].pk, date(2026, 4, 1), cls.owners[1])
        GatewayConfig.objects.create(tenant=cls.tenants[0], merchant_id="G-TEST", enabled=True)

    def setUp(self):
        self.keys = override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=account_keys(self.tenants[0]))
        self.keys.enable()
        self.addCleanup(self.keys.disable)
        verified_login(self.client, self.owners[0])

    def order(self):
        return reserve_order(self.owners[0], self.invoice.pk)

    def notify(self, order, payload=None):
        return Client(enforce_csrf_checks=True).post("/payments/midtrans/notifications/",
            data=json.dumps(payload or notification_payload(order)), content_type="application/json")

    def test_checkout_amount_and_tenant_are_server_owned(self):
        with patch("core.gateway.SandboxClient.create", return_value=URL) as create:
            response = self.client.post(f"/invoices/{self.invoice.pk}/checkout/", {"amount": 1, "tenant_id": self.tenants[1].pk})
        order = GatewayOrder.objects.get()
        self.assertEqual(order.amount, self.invoice.balance)
        self.assertEqual(order.tenant, self.tenants[0])
        self.assertRedirects(response, f"/payments/orders/{order.pk}/")
        create.assert_called_once()
        self.assertEqual(Payment.objects.count(), 0)

    def test_repeated_checkout_reuses_order_and_checkout(self):
        with patch("core.gateway.SandboxClient.create", return_value=URL) as create:
            a = start_checkout(self.owners[0], self.invoice.pk)
            b = start_checkout(self.owners[0], self.invoice.pk)
        self.assertEqual(a.pk, b.pk)
        create.assert_called_once()
        self.assertEqual(GatewayOrder.objects.count(), 1)

    def test_timeout_keeps_same_order_and_blocks_other_payment(self):
        with patch("core.gateway.SandboxClient.create", side_effect=GatewayUnavailable):
            with self.assertRaises(GatewayUnavailable):
                start_checkout(self.owners[0], self.invoice.pk)
        order = GatewayOrder.objects.get()
        self.assertEqual(order.status, "creating")
        with self.assertRaises(ValidationError):
            record_payment(self.tenants[0], self.invoice.pk, 1000, uuid.uuid4(), self.owners[0])
        with patch("core.gateway.SandboxClient.create", return_value=URL) as create:
            self.assertEqual(start_checkout(self.owners[0], self.invoice.pk).pk, order.pk)
        self.assertEqual(create.call_args.args[0].order_id, order.order_id)

    def test_partial_payment_checkout_uses_remaining_balance(self):
        record_payment(self.tenants[0], self.invoice.pk, 1000, uuid.uuid4(), self.owners[0])
        order = self.order()
        self.assertEqual(order.amount, self.invoice.total - 1000)

    def test_paid_invoice_cannot_start_checkout(self):
        record_payment(self.tenants[0], self.invoice.pk, self.invoice.total, uuid.uuid4(), self.owners[0])
        with self.assertRaises(ValidationError):
            self.order()

    def test_disabled_gateway_prevents_checkout(self):
        GatewayConfig.objects.update(enabled=False)
        with self.assertRaises(ValidationError):
            self.order()

    def test_cross_tenant_checkout_and_receipt_rejected(self):
        self.assertEqual(self.client.post(f"/invoices/{self.other_invoice.pk}/checkout/").status_code, 404)
        order = self.order()
        verified_login(self.client, self.owners[1])
        self.assertEqual(self.client.get(f"/payments/orders/{order.pk}/").status_code, 404)
        self.assertEqual(self.client.post(f"/payments/orders/{order.pk}/refresh/").status_code, 404)

    def test_portal_customer_only_own_invoice_and_order(self):
        user = get_user_model().objects.create_user(username="gateway-customer")
        Membership.objects.create(user=user, tenant=self.tenants[0], role="customer", customer=self.customers[0])
        verified_login(self.client, user)
        with patch("core.gateway.SandboxClient.create", return_value=URL):
            self.assertEqual(self.client.post(f"/invoices/{self.invoice.pk}/checkout/").status_code, 302)
        order = GatewayOrder.objects.get()
        self.assertEqual(self.client.get(f"/payments/orders/{order.pk}/").status_code, 200)
        sibling = Customer.objects.create(tenant=self.tenants[0], code="TN-SIBLING", name="Pelanggan lain",
            phone="081299977711", username="gateway-sibling", address="Alamat", area="Lab", plan=self.customers[0].plan)
        Membership.objects.filter(user=user).update(customer=sibling)
        self.assertEqual(self.client.get(f"/payments/orders/{order.pk}/").status_code, 404)
        self.assertEqual(self.client.post(f"/invoices/{self.invoice.pk}/checkout/").status_code, 404)
        Membership.objects.filter(user=user).update(customer=self.customers[1])
        self.assertEqual(self.client.get(f"/payments/orders/{order.pk}/").status_code, 403)

    def test_invoice_reader_cannot_start_or_refresh(self):
        order = self.order()
        Membership.objects.filter(user=self.owners[0]).update(role="ops")
        self.assertEqual(self.client.post(f"/invoices/{self.invoice.pk}/checkout/").status_code, 403)
        self.assertEqual(self.client.post(f"/payments/orders/{order.pk}/refresh/").status_code, 403)

    def test_csrf_and_http_methods_for_checkout_and_refresh(self):
        order = self.order()
        secure = Client(enforce_csrf_checks=True)
        verified_login(secure, self.owners[0])
        for url in [f"/invoices/{self.invoice.pk}/checkout/", f"/payments/orders/{order.pk}/refresh/"]:
            self.assertEqual(secure.post(url).status_code, 403)
            self.assertEqual(self.client.get(url).status_code, 405)

    def test_get_receipt_and_forged_return_query_do_not_settle(self):
        order = self.order()
        with patch("core.gateway.SandboxClient.status") as status:
            self.assertEqual(self.client.get(f"/payments/orders/{order.pk}/?transaction_status=settlement&status_code=200").status_code, 200)
        status.assert_not_called()
        self.assertEqual(Payment.objects.count(), 0)

    def test_operator_checkout_requires_recent_mfa(self):
        session = self.client.session
        session["mfa_verified_at"] = time.time() - 301
        session.save()
        self.assertRedirects(self.client.post(f"/invoices/{self.invoice.pk}/checkout/"), "/account/security/")
        self.assertEqual(GatewayOrder.objects.count(), 0)

    def test_invalid_signature_cannot_call_status_or_write_ledger(self):
        order = self.order()
        payload = notification_payload(order)
        payload["signature_key"] = "0" * 128
        with patch("core.gateway.SandboxClient.status") as status:
            self.assertEqual(self.notify(order, payload).status_code, 403)
        status.assert_not_called()
        self.assertEqual(Payment.objects.count(), 0)

    def test_signed_callback_requires_authoritative_settlement(self):
        order = self.order()
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(order, "pending")):
            self.assertEqual(self.notify(order).status_code, 200)
        self.assertEqual(Payment.objects.count(), 0)

    def test_settlement_and_callback_replay_create_one_payment_and_job(self):
        order = self.order()
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(order)):
            for _ in range(3):
                self.assertEqual(self.notify(order).status_code, 200)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(NetworkJob.objects.count(), 1)
        self.assertEqual(GatewayEvent.objects.count(), 1)
        self.assertEqual(Payment.objects.get().source, "midtrans_sandbox")
        self.assertNotEqual(Payment.objects.get().event_key, order.pk)
        self.assertEqual(self.invoice.balance, 0)

    def test_capture_requires_fraud_accept(self):
        order = self.order()
        apply_status(order.pk, status_payload(order, "capture", fraud_status="challenge"))
        self.assertEqual(Payment.objects.count(), 0)
        apply_status(order.pk, status_payload(order, "capture"))
        self.assertEqual(Payment.objects.count(), 1)
        apply_status(order.pk, status_payload(order))
        self.assertEqual(Payment.objects.count(), 1)

    def test_capture_missing_fraud_and_settlement_non_success_never_credit(self):
        order = self.order()
        payload = status_payload(order, "capture")
        payload.pop("fraud_status")
        apply_status(order.pk, payload)
        apply_status(order.pk, status_payload(order, status_code="201"))
        self.assertFalse(Payment.objects.exists())

    def test_mismatched_status_is_atomic_and_rejected(self):
        order = self.order()
        for changes in [{"merchant_id": "other"}, {"order_id": "other"}, {"currency": "USD"},
                        {"gross_amount": "1.00"}, {"gross_amount": "249750.01"}, {"gross_amount": 249750},
                        {"transaction_id": "x/y"}, {"transaction_status": "unknown"}, {"transaction_status": []}]:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                apply_status(order.pk, status_payload(order, **changes))
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(GatewayEvent.objects.exists())

    def test_out_of_order_status_does_not_undo_payment(self):
        order = self.order()
        apply_status(order.pk, status_payload(order))
        for state in ["pending", "expire", "cancel", "deny"]:
            result = apply_status(order.pk, status_payload(order, state))
            self.assertEqual(result.status, "settled")
        self.assertEqual(self.invoice.balance, 0)

    def test_transaction_id_cannot_change(self):
        order = self.order()
        apply_status(order.pk, status_payload(order, "pending"))
        with self.assertRaises(ValidationError):
            apply_status(order.pk, status_payload(order, transaction_id="new-transaction"))
        self.assertFalse(Payment.objects.exists())

    def test_expiry_releases_reservation_and_new_checkout_gets_new_id(self):
        order = self.order()
        apply_status(order.pk, status_payload(order, "expire"))
        new = self.order()
        self.assertNotEqual(order.pk, new.pk)

    def test_provider_refund_preserves_ledger_and_requires_review(self):
        order = self.order()
        apply_status(order.pk, status_payload(order))
        result = apply_status(order.pk, status_payload(order, "refund"))
        self.assertEqual(result.status, "review")
        self.assertEqual(self.invoice.balance, 0)
        with self.assertRaises(ValidationError):
            reverse_payment(self.tenants[0], result.payment_id, "Koreksi bukan refund", self.owners[0])

    def test_overpayment_becomes_review_without_duplicate_ledger(self):
        order = self.order()
        Payment.objects.create(tenant=self.tenants[0], invoice=self.invoice, amount=self.invoice.total,
            event_key=uuid.uuid4(), recorded_by=self.owners[0])
        result = apply_status(order.pk, status_payload(order))
        self.assertEqual(result.status, "review")
        self.assertIsNone(result.payment_id)
        self.assertEqual(Payment.objects.count(), 1)

    def test_gateway_outage_returns_retryable_webhook_response(self):
        order = self.order()
        with patch("core.gateway.SandboxClient.status", side_effect=GatewayUnavailable):
            self.assertEqual(self.notify(order).status_code, 503)
        self.assertFalse(Payment.objects.exists())
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(order)):
            self.assertEqual(self.notify(order).status_code, 200)

    def test_disabling_new_checkout_does_not_drop_callback(self):
        order = self.order()
        GatewayConfig.objects.update(enabled=False)
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(order)):
            self.assertEqual(self.notify(order).status_code, 200)
        self.assertEqual(Payment.objects.count(), 1)

    def test_router_failure_keeps_gateway_payment(self):
        order = self.order()
        apply_status(order.pk, status_payload(order))
        customer = self.customers[0]
        customer.router_available = False
        customer.save()
        self.assertEqual(run_job(NetworkJob.objects.get().pk).status, "failed")
        self.assertEqual(self.invoice.balance, 0)

    def test_settings_owner_mfa_stale_version_and_private_key(self):
        config = GatewayConfig.objects.get()
        response = self.client.get("/settings/payment-gateway/")
        self.assertContains(response, "G-TEST")
        self.assertNotContains(response, KEY)
        save_config(self.owners[0], False, config.version, time.time(), 1)
        with self.assertRaises(ValidationError):
            save_config(self.owners[0], True, config.version, time.time(), 1)
        with self.assertRaises(PermissionDenied):
            save_config(self.owners[0], True, config.version + 1, time.time() - 301, 1)
        Membership.objects.filter(user=self.owners[0]).update(role="finance")
        self.assertEqual(self.client.get("/settings/payment-gateway/").status_code, 403)

    def test_merchant_switch_blocked_after_order(self):
        self.order()
        keys = {str(self.tenants[0].pk): {"merchant_id": "G-OTHER", "server_key": KEY}}
        with override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=keys), self.assertRaises(ValidationError):
            save_config(self.owners[0], True, 1, time.time(), 1)

    def test_reconcile_command_does_not_create_checkouts(self):
        order = self.order()
        output = StringIO()
        with patch("core.gateway.SandboxClient.status", return_value=status_payload(order)), patch("core.gateway.SandboxClient.create") as create:
            call_command("reconcile_gateway_payments", stdout=output)
        self.assertIn("verified=1", output.getvalue())
        create.assert_not_called()
        self.assertEqual(Payment.objects.count(), 1)
        pending = []
        for month in [6, 7]:
            invoice, _ = issue_invoice(self.tenants[0], self.customers[0].pk, date(2026, month, 1), self.owners[0])
            pending.append(reserve_order(self.owners[0], invoice.pk))
        def remote(order):
            if order.pk == pending[0].pk:
                raise GatewayUnavailable
            return status_payload(order)
        with patch("core.gateway.SandboxClient.status", side_effect=remote):
            call_command("reconcile_gateway_payments", limit=1, stdout=StringIO())
            call_command("reconcile_gateway_payments", limit=1, stdout=StringIO())
        pending[1].refresh_from_db()
        self.assertEqual(pending[1].status, "settled")

    def test_malformed_webhook_has_no_external_calls(self):
        bad = ['[]', '{"order_id":1}', '{"order_id":"TN-SBX-no"}', '{"order_id":"x","order_id":"y"}', '{"order_id":NaN}', 'x' * 16385, '[' * 1100 + ']' * 1100]
        with patch("core.gateway.SandboxClient.status") as status:
            for body in bad:
                self.assertEqual(self.client.post("/payments/midtrans/notifications/", data=body, content_type="application/json").status_code, 400)
        status.assert_not_called()
        self.assertEqual(self.client.get("/payments/midtrans/notifications/").status_code, 405)

    def test_signature_with_wrong_amount_is_rejected(self):
        order = self.order()
        with patch("core.gateway.SandboxClient.status") as status:
            self.assertEqual(self.notify(order, notification_payload(order, gross_amount="1.00")).status_code, 403)
        status.assert_not_called()

    def test_receipt_is_private_and_has_no_open_redirect_from_query(self):
        order = self.order()
        client = Client()
        self.assertEqual(client.get(f"/payments/orders/{order.pk}/").status_code, 403)
        response = self.client.get(f"/payments/orders/{order.pk}/?redirect_url=https://evil.invalid/")
        self.assertNotContains(response, "evil.invalid")
        self.assertEqual(response["Cache-Control"], "no-store")


class MidtransBoundaryTests(SimpleTestCase):
    def client_order(self):
        return SimpleNamespace(tenant_id=1, merchant_id="G-TEST", invoice_id=1, order_id="TN-SBX-" + "a" * 32, amount=225000)

    @override_settings(SIMULATION=True, MIDTRANS_SANDBOX_ACCOUNT_KEYS={"1": {"merchant_id": "G-TEST", "server_key": KEY}})
    def test_transport_uses_sandbox_basic_auth_and_server_amount(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.status = 201
        response.read.return_value = json.dumps({"redirect_url": URL, "token": "private"}).encode()
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch("core.midtrans.urllib.request.build_opener", return_value=opener):
            self.assertEqual(SandboxClient().create(self.client_order()), URL)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://app.sandbox.midtrans.com/snap/v1/transactions")
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))
        self.assertEqual(json.loads(request.data)["transaction_details"]["gross_amount"], 225000)
        self.assertTrue(json.loads(request.data)["credit_card"]["secure"])

    @override_settings(SIMULATION=True, MIDTRANS_SANDBOX_ACCOUNT_KEYS={"1": {"merchant_id": "G-TEST", "server_key": KEY}})
    def test_transport_status_by_persisted_order_id_and_bounded_response(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.status = 200
        response.read.return_value = b"x" * 65537
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch("core.midtrans.urllib.request.build_opener", return_value=opener), self.assertRaises(GatewayUnavailable):
            SandboxClient().status(self.client_order())
        self.assertEqual(opener.open.call_args.args[0].full_url, f"https://api.sandbox.midtrans.com/v2/{self.client_order().order_id}/status")
        self.assertEqual(response.read.call_args.args, (65537,))

    @override_settings(SIMULATION=True, MIDTRANS_SANDBOX_ACCOUNT_KEYS={"1": {"merchant_id": "G-TEST", "server_key": KEY}})
    def test_http_error_body_is_not_exposed_or_treated_as_success(self):
        from unittest.mock import MagicMock
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError("https://api.sandbox.midtrans.com", 302, "redirect", {}, None)
        with patch("core.midtrans.urllib.request.build_opener", return_value=opener), self.assertRaises(GatewayUnavailable):
            SandboxClient().status(self.client_order())

    def test_checkout_redirects_are_restricted_to_sandbox(self):
        self.assertEqual(checkout_url(URL), URL)
        for url in ["https://app.midtrans.com/snap/v4/redirection/a", "https://evil.invalid/a", "http://app.sandbox.midtrans.com/snap/v4/redirection/a",
                    URL + "?next=evil", "https://app.sandbox.midtrans.com@evil.invalid/snap/v4/redirection/a", "//app.sandbox.midtrans.com/a", "javascript:alert(1)", "https://[bad", None]:
            with self.subTest(url=url), self.assertRaises(GatewayUnavailable):
                checkout_url(url)

    @override_settings(SIMULATION=False, MIDTRANS_SANDBOX_ACCOUNT_KEYS={})
    def test_sandbox_cannot_run_in_live_mode(self):
        with self.assertRaises(ValidationError):
            credentials(1)

    def test_production_key_and_merchant_reuse_are_rejected(self):
        for keys in [{"1": {"merchant_id": "G-TEST", "server_key": "Mid-server-live"}},
                     {"1": {"merchant_id": "G-TEST", "server_key": KEY}, "2": {"merchant_id": "G-TEST", "server_key": KEY}}]:
            with override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=keys), self.assertRaises(ValidationError):
                credentials(1)

    def test_duplicate_json_fields_and_constants_are_rejected(self):
        for raw in ['{"a":1,"a":2}', '{"a":Infinity}', '[]']:
            with self.assertRaises(ValueError):
                json_object(raw)


@override_settings(SIMULATION=True)
class GatewayConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.tenants, self.owners, self.customers = fixtures()
        self.invoice, _ = issue_invoice(self.tenants[0], self.customers[0].pk, date(2026, 5, 1), self.owners[0])
        GatewayConfig.objects.create(tenant=self.tenants[0], merchant_id="G-TEST", enabled=True)
        self.keys = override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=account_keys(self.tenants[0]))
        self.keys.enable()
        self.addCleanup(self.keys.disable)

    def parallel(self, operation):
        def run(_):
            close_old_connections()
            try:
                return operation()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(run, range(2)))

    def test_two_reservations_have_one_order(self):
        actor_id, invoice_id = self.owners[0].pk, self.invoice.pk
        ids = self.parallel(lambda: reserve_order(get_user_model().objects.get(pk=actor_id), invoice_id).pk)
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(GatewayOrder.objects.count(), 1)

    def test_two_settlements_have_one_payment_and_network_job(self):
        order = reserve_order(self.owners[0], self.invoice.pk)
        payload = status_payload(order)
        self.parallel(lambda: apply_status(order.pk, payload))
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(NetworkJob.objects.count(), 1)
        self.assertEqual(GatewayEvent.objects.count(), 1)
