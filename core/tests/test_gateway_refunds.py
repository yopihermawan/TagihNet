import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, close_old_connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from core.billing import issue_invoice, record_payment, reverse_payment
from core.gateway import apply_status, reserve_order
from core.gateway_refunds import reconcile_refund
from core.midtrans import GatewayUnavailable
from core.models import AuditEvent, GatewayConfig, GatewayOrder, GatewayRefundResolution, Membership, NetworkJob, Payment, PaymentReversal
from core.network import run_job
from .auth_helpers import verified_login
from .test_payment_gateway import account_keys, status_payload
from .test_workflow import fixtures

REASON = "Refund penuh telah dikonfirmasi pada dashboard sandbox merchant"


def refund_payload(order, **changes):
    data=status_payload(order,"refund",refund_amount=f"{order.amount}.00")
    data.update(changes)
    return data


@override_settings(SIMULATION=True)
class GatewayRefundTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants,cls.owners,cls.customers=fixtures()
        cls.invoice,_=issue_invoice(cls.tenants[0],cls.customers[0].pk,date(2026,8,1),cls.owners[0])
        GatewayConfig.objects.create(tenant=cls.tenants[0],merchant_id="G-TEST",enabled=True)

    def setUp(self):
        self.keys=override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=account_keys(self.tenants[0]))
        self.keys.enable()
        self.addCleanup(self.keys.disable)
        self.order=reserve_order(self.owners[0],self.invoice.pk)
        self.order=apply_status(self.order.pk,status_payload(self.order))
        self.key=uuid.uuid4()
        verified_login(self.client,self.owners[0])

    def record(self, key=None, reason=REASON):
        return reconcile_refund(self.owners[0],self.order.pk,key or self.key,reason,time.time(),1)

    def form(self, **changes):
        return {"request_key":self.key,"reason":REASON,"reviewed":"on",**changes}

    def test_verified_full_refund_reverses_one_payment_and_reopens_balance(self):
        with patch("core.gateway_refunds.SandboxClient.status",return_value=refund_payload(self.order)) as status, patch("core.gateway.SandboxClient.create") as create, patch("core.gateway_expiry.SandboxClient.expire") as expire:
            result=self.record()
        status.assert_called_once()
        create.assert_not_called()
        expire.assert_not_called()
        self.assertEqual(result.amount,self.order.amount)
        self.assertEqual(result.reversal.payment_id,self.order.payment_id)
        self.assertEqual(self.invoice.balance,self.order.amount)
        self.assertEqual(Payment.objects.count(),1)
        self.assertEqual(PaymentReversal.objects.count(),1)
        self.assertEqual(NetworkJob.objects.count(),2)
        self.assertEqual(GatewayOrder.objects.get(pk=self.order.pk).status,"refunded")
        self.assertEqual(len(result.proof_digest),64)

    def test_replay_keeps_one_resolution_and_does_not_contact_gateway(self):
        with patch("core.gateway_refunds.SandboxClient.status",return_value=refund_payload(self.order)):
            first=self.record()
        with patch("core.gateway_refunds.SandboxClient.status") as status:
            replay=self.record()
        status.assert_not_called()
        self.assertEqual(first.pk,replay.pk)
        self.assertEqual(GatewayRefundResolution.objects.count(),1)
        self.assertEqual(PaymentReversal.objects.count(),1)
        self.assertEqual(AuditEvent.objects.filter(action="gateway.refund_recorded").count(),1)

    def test_full_gateway_refund_reopens_only_its_allocation_on_partially_paid_invoice(self):
        invoice,_=issue_invoice(self.tenants[0],self.customers[0].pk,date(2026,9,1),self.owners[0])
        manual,_=record_payment(self.tenants[0],invoice.pk,100000,uuid.uuid4(),self.owners[0])
        order=reserve_order(self.owners[0],invoice.pk)
        apply_status(order.pk,status_payload(order))
        with patch("core.gateway_refunds.SandboxClient.status",return_value=refund_payload(order)):
            result=reconcile_refund(self.owners[0],order.pk,uuid.uuid4(),REASON,time.time(),1)
        self.assertEqual(result.amount,invoice.total-100000)
        self.assertEqual(invoice.balance,result.amount)
        self.assertEqual(invoice.paid,100000)
        self.assertFalse(PaymentReversal.objects.filter(payment=manual).exists())
        self.assertEqual(self.invoice.balance,0)

    def test_reused_nonce_or_second_request_cannot_change_reason(self):
        with patch("core.gateway_refunds.SandboxClient.status",return_value=refund_payload(self.order)):
            self.record()
        for key,reason in [(self.key,"Alasan berbeda setelah refund dicatat"),(uuid.uuid4(),REASON)]:
            with self.subTest(key=key),patch("core.gateway_refunds.SandboxClient.status") as status,self.assertRaises(ValidationError):
                self.record(key,reason)
            status.assert_not_called()
        self.assertEqual(PaymentReversal.objects.count(),1)

    def test_full_refund_needs_amount_and_success_status(self):
        variants=[{"refund_amount":"1.00"},{"refund_amount":str(self.order.amount+1)},
            {"refund_amount":self.order.amount},{"refund_amount":"249750.01"},
            {"refund_amount":None},{"status_code":"201"},{"transaction_status":"partial_refund"},
            {"transaction_status":"chargeback"},{"transaction_status":"settlement"}]
        for changes in variants:
            with self.subTest(changes=changes),patch("core.gateway_refunds.SandboxClient.status",return_value=refund_payload(self.order,**changes)),self.assertRaises(ValidationError):
                self.record()
        self.assertFalse(PaymentReversal.objects.exists())
        self.assertEqual(self.invoice.balance,0)

    def test_missing_refund_amount_fails_closed(self):
        payload=refund_payload(self.order)
        payload.pop("refund_amount")
        with patch("core.gateway_refunds.SandboxClient.status",return_value=payload),self.assertRaises(ValidationError):
            self.record()
        self.assertFalse(GatewayRefundResolution.objects.exists())

    def test_wrong_merchant_currency_order_transaction_or_gross_is_rejected(self):
        for changes in [{"merchant_id":"G-OTHER"},{"currency":"USD"},{"order_id":"other"},
                        {"transaction_id":"other"},{"gross_amount":"1.00"}]:
            with self.subTest(changes=changes),patch("core.gateway_refunds.SandboxClient.status",return_value=refund_payload(self.order,**changes)),self.assertRaises(ValidationError):
                self.record()
        self.assertFalse(PaymentReversal.objects.exists())

    def test_prior_chargeback_requires_manual_review_even_after_full_refund(self):
        apply_status(self.order.pk,status_payload(self.order,"partial_chargeback"))
        with patch("core.gateway_refunds.SandboxClient.status",return_value=refund_payload(self.order)),self.assertRaises(ValidationError):
            self.record()
        self.assertFalse(PaymentReversal.objects.exists())

    def test_partial_refund_can_be_recorded_only_when_provider_reports_full(self):
        apply_status(self.order.pk,refund_payload(self.order,transaction_status="partial_refund",refund_amount="1000.00"))
        with patch("core.gateway_refunds.SandboxClient.status",return_value=refund_payload(self.order)):
            self.assertEqual(self.record().amount,self.order.amount)
        self.assertEqual(PaymentReversal.objects.count(),1)

    def test_network_failure_does_not_change_ledger_and_same_request_can_retry(self):
        with patch("core.gateway_refunds.SandboxClient.status",side_effect=GatewayUnavailable),self.assertRaises(GatewayUnavailable):
            self.record()
        self.assertFalse(PaymentReversal.objects.exists())
        with patch("core.gateway_refunds.SandboxClient.status",return_value=refund_payload(self.order)):
            self.record()
        self.assertEqual(PaymentReversal.objects.count(),1)

    def test_already_reversed_or_wrong_payment_source_is_not_reversed_again(self):
        Payment.objects.filter(pk=self.order.payment_id).update(source="simulation")
        with patch("core.gateway_refunds.SandboxClient.status",return_value=refund_payload(self.order)),self.assertRaises(ValidationError):
            self.record()
        Payment.objects.filter(pk=self.order.payment_id).update(source="midtrans_sandbox")
        PaymentReversal.objects.create(payment_id=self.order.payment_id,recorded_by=self.owners[0],reason="Fixture correction")
        with patch("core.gateway_refunds.SandboxClient.status",return_value=refund_payload(self.order)),self.assertRaises(ValidationError):
            self.record()
        self.assertEqual(PaymentReversal.objects.count(),1)
        self.assertFalse(GatewayRefundResolution.objects.exists())

    def test_disabled_checkout_still_allows_recording_old_refund(self):
        GatewayConfig.objects.update(enabled=False)
        with patch("core.gateway_refunds.SandboxClient.status",return_value=refund_payload(self.order)):
            self.assertIsNotNone(self.record())

    def test_no_payment_missing_credentials_and_live_mode_are_rejected(self):
        for settings in [{"MIDTRANS_SANDBOX_ACCOUNT_KEYS":{}},{"SIMULATION":False}]:
            with self.subTest(settings=settings),override_settings(**settings),patch("core.gateway_refunds.SandboxClient.status") as status,self.assertRaises(ValidationError):
                self.record()
            status.assert_not_called()
        GatewayOrder.objects.filter(pk=self.order.pk).update(payment=None)
        with patch("core.gateway_refunds.SandboxClient.status") as status,self.assertRaises(ValidationError):
            self.record()
        status.assert_not_called()

    def test_stale_mfa_and_session_revoked_during_get_cannot_reverse(self):
        with self.assertRaises(PermissionDenied):
            reconcile_refund(self.owners[0],self.order.pk,self.key,REASON,time.time()-301,1)
        def remote(order):
            Membership.objects.filter(user=self.owners[0]).update(session_version=2)
            return refund_payload(order)
        with patch("core.gateway_refunds.SandboxClient.status",side_effect=remote),self.assertRaises(PermissionDenied):
            self.record()
        self.assertFalse(PaymentReversal.objects.exists())

    def test_late_success_cannot_undo_refund_or_create_payment(self):
        with patch("core.gateway_refunds.SandboxClient.status",return_value=refund_payload(self.order)):
            self.record()
        for state in ["pending","capture","settlement","refund"]:
            result=apply_status(self.order.pk,refund_payload(self.order,transaction_status=state))
            self.assertEqual(result.status,"refunded")
        self.assertEqual(Payment.objects.count(),1)
        self.assertEqual(PaymentReversal.objects.count(),1)
        self.assertEqual(NetworkJob.objects.count(),2)
        self.assertEqual(self.invoice.balance,self.invoice.total)

    def test_changed_refund_after_resolution_is_flagged_without_second_reversal(self):
        with patch("core.gateway_refunds.SandboxClient.status",return_value=refund_payload(self.order)):
            self.record()
        result=apply_status(self.order.pk,refund_payload(self.order,refund_amount="1.00"))
        self.assertEqual(result.status,"review")
        self.assertEqual(PaymentReversal.objects.count(),1)

    def test_network_job_rechecks_reopened_debt(self):
        with patch("core.gateway_refunds.SandboxClient.status",return_value=refund_payload(self.order)):
            self.record()
        run_job(NetworkJob.objects.latest("pk").pk)
        self.customers[0].refresh_from_db()
        self.assertEqual(self.customers[0].service_state,"isolated")

    def test_http_owner_tenant_csrf_post_and_review_are_required(self):
        url=f"/payments/orders/{self.order.pk}/record-refund/"
        secure=Client(enforce_csrf_checks=True)
        verified_login(secure,self.owners[0])
        self.assertEqual(secure.post(url,self.form()).status_code,403)
        self.assertEqual(self.client.get(url).status_code,405)
        verified_login(self.client,self.owners[1])
        self.assertEqual(self.client.post(url,self.form()).status_code,404)
        verified_login(self.client,self.owners[0])
        with patch("core.gateway_refunds.SandboxClient.status") as status:
            self.assertEqual(self.client.post(url,self.form(reviewed="")).status_code,400)
            self.assertEqual(self.client.post(url,self.form(reason="short")).status_code,400)
        status.assert_not_called()
        Membership.objects.filter(user=self.owners[0]).update(role="finance")
        self.assertEqual(self.client.post(url,self.form()).status_code,403)
        self.assertFalse(PaymentReversal.objects.exists())

    def test_browser_posted_status_or_amount_is_not_proof(self):
        url=f"/payments/orders/{self.order.pk}/record-refund/"
        with patch("core.gateway_refunds.SandboxClient.status",return_value=status_payload(self.order)):
            response=self.client.post(url,self.form(transaction_status="refund",refund_amount=self.order.amount))
        self.assertEqual(response.status_code,400)
        self.assertFalse(PaymentReversal.objects.exists())

    def test_http_stale_mfa_redirects_before_provider_read(self):
        session=self.client.session
        session["mfa_verified_at"]=time.time()-301
        session.save()
        with patch("core.gateway_refunds.SandboxClient.status") as status:
            response=self.client.post(f"/payments/orders/{self.order.pk}/record-refund/",self.form())
        self.assertRedirects(response,"/account/security/")
        status.assert_not_called()
        self.assertEqual(self.client.session["stepup_return"],f"/payments/orders/{self.order.pk}/")

    def test_owner_reason_is_private_and_portal_sees_generic_refund_summary(self):
        reason="Internal owner note <script>alert(1)</script>"
        with patch("core.gateway_refunds.SandboxClient.status",return_value=refund_payload(self.order)):
            self.record(reason=reason)
        response=self.client.get(f"/payments/orders/{self.order.pk}/")
        self.assertNotContains(response,"<script>alert(1)</script>")
        user=get_user_model().objects.create_user(username="refund-portal")
        Membership.objects.create(user=user,tenant=self.tenants[0],role="customer",customer=self.customers[0])
        verified_login(self.client,user)
        response=self.client.get(f"/payments/orders/{self.order.pk}/")
        self.assertContains(response,"Refund penuh telah dicatat")
        self.assertNotContains(response,"Internal owner note")
        self.assertNotContains(response,"Catat refund penuh yang sudah terjadi")
        invoice=self.client.get(f"/invoices/{self.invoice.pk}/")
        self.assertContains(invoice,"Refund penuh Midtrans Sandbox terverifikasi")
        self.assertNotContains(invoice,"Internal owner note")

    def test_get_form_has_no_network_and_invalid_reason_or_nonce_has_no_effect(self):
        with patch("core.gateway_refunds.SandboxClient.status") as status:
            self.assertEqual(self.client.get(f"/payments/orders/{self.order.pk}/").status_code,200)
            for reason,key in [("short",self.key),(REASON,"invalid"),("bad\x00reason",self.key)]:
                with self.subTest(reason=reason),self.assertRaises(ValidationError):
                    self.record(key,reason)
        status.assert_not_called()
        self.assertFalse(PaymentReversal.objects.exists())

    def test_audit_lock_rolls_back_and_retries_only_database(self):
        from core.billing import audit as write_audit
        failed=False
        def audit(*args,**kwargs):
            nonlocal failed
            if not failed:
                failed=True
                raise OperationalError("database is locked")
            return write_audit(*args,**kwargs)
        with patch("core.gateway_refunds.audit",side_effect=audit),patch("core.gateway_refunds.SandboxClient.status",return_value=refund_payload(self.order)) as status:
            self.record()
        self.assertEqual(status.call_count,1)
        self.assertEqual(PaymentReversal.objects.count(),1)
        self.assertEqual(GatewayRefundResolution.objects.count(),1)
        self.assertEqual(NetworkJob.objects.count(),2)


@override_settings(SIMULATION=True)
class RefundConcurrencyTests(TransactionTestCase):
    def test_two_refund_records_create_one_reversal_and_one_new_job(self):
        tenants,owners,customers=fixtures()
        invoice,_=issue_invoice(tenants[0],customers[0].pk,date(2026,8,1),owners[0])
        GatewayConfig.objects.create(tenant=tenants[0],merchant_id="G-TEST",enabled=True)
        with override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=account_keys(tenants[0])):
            order=reserve_order(owners[0],invoice.pk)
            apply_status(order.pk,status_payload(order))
            actor_id,key=owners[0].pk,uuid.uuid4()
            def remote(current):
                self.assertFalse(connection.in_atomic_block)
                return refund_payload(current)
            def run(_):
                close_old_connections()
                try:
                    actor=get_user_model().objects.get(pk=actor_id)
                    return reconcile_refund(actor,order.pk,key,REASON,time.time(),1).pk
                finally:
                    close_old_connections()
            from django.db import connection
            with patch("core.gateway_refunds.SandboxClient.status",side_effect=remote),ThreadPoolExecutor(max_workers=2) as pool:
                ids=list(pool.map(run,range(2)))
        self.assertEqual(ids[0],ids[1])
        self.assertEqual(PaymentReversal.objects.count(),1)
        self.assertEqual(GatewayRefundResolution.objects.count(),1)
        self.assertEqual(NetworkJob.objects.count(),2)
        self.assertEqual(invoice.balance,invoice.total)
