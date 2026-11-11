import time
import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch
import pyotp
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from config.public_url import public_origin
from core.checkout_return import SESSION_KEY, pending_checkout_return, resume_checkout
from core.billing import issue_invoice
from core.gateway import apply_status, reserve_order
from core.midtrans import SandboxClient
from core.models import GatewayConfig, GatewayEvent, GatewayOrder, MFADevice, Membership, NetworkJob, Payment
from core.security import decrypt_secret, encrypt_secret
from .auth_helpers import TEST_TOTP_SECRET, verified_login
from .test_payment_gateway import URL, account_keys, status_payload
from .test_workflow import fixtures

PASSWORD = "Local-test-secret-2026!"


class ReturnOriginTests(SimpleTestCase):
    def test_https_origin_and_explicit_debug_loopback(self):
        for value in ["https://billing.example.test", "https://billing.example.test/", "https://billing.example.test:443"]:
            self.assertEqual(public_origin(value, debug=False), value.rstrip("/"))
        for value in ["http://localhost:8000", "http://127.0.0.1:8000/", "http://[::1]:8000"]:
            self.assertEqual(public_origin(value, debug=True), value.rstrip("/"))
        self.assertEqual(public_origin("", debug=False), "")

    def test_invalid_origin_rejected_without_echoing_input(self):
        invalid = ["http://billing.example.test", "//billing.example.test", "javascript:alert(1)",
            "https://user:secret@billing.example.test", "https://billing.example.test/path", "https://billing.example.test?",
            "https://billing.example.test#", "https://billing.example.test?next=https://evil.test", "https://billing.example.test/#finish",
            "https://billing.example.test:0", "https://billing.example.test:99999", " https://billing.example.test", "https://billing.example.test\n", None]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ImproperlyConfigured) as caught:
                public_origin(value, debug=True)
            self.assertNotIn("secret@", str(caught.exception))
        for value in ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"]:
            with self.subTest(value=value), self.assertRaises(ImproperlyConfigured):
                public_origin(value, debug=False)

    def test_only_uuid_and_original_deadline_can_survive_login(self):
        order, created = str(uuid.uuid4()), time.time()
        request = SimpleNamespace(session={SESSION_KEY: {"order": order, "created": created, "url": "https://evil.test", "factor": "discard"}})
        self.assertEqual(pending_checkout_return(request), {"order": order, "created": created})
        self.assertEqual(resume_checkout(request), f"/payments/orders/{order}/return/")
        self.assertNotIn(SESSION_KEY, request.session)
        self.assertIsNone(resume_checkout(request))

    def test_expired_future_or_malformed_resume_is_discarded(self):
        order = str(uuid.uuid4())
        for value in [None, "https://evil.test", {}, {"order": order, "created": time.time()-601},
                {"order": order, "created": time.time()+100}, {"order": order, "created": "now"},
                {"order": order, "created": True}, {"order": "//evil.test", "created": time.time()}]:
            request = SimpleNamespace(session={SESSION_KEY: value})
            self.assertIsNone(resume_checkout(request))
            self.assertNotIn(SESSION_KEY, request.session)


@override_settings(SIMULATION=True)
class GatewayReturnTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()
        cls.invoice, _ = issue_invoice(cls.tenants[0], cls.customers[0].pk, date(2026, 8, 1), cls.owners[0])
        GatewayConfig.objects.create(tenant=cls.tenants[0], merchant_id="G-TEST", enabled=True)
        cls.portal = get_user_model().objects.create_user(username="return-customer", password=PASSWORD)
        Membership.objects.create(user=cls.portal, tenant=cls.tenants[0], role="customer", customer=cls.customers[0])

    def setUp(self):
        self.keys = override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=account_keys(self.tenants[0]))
        self.keys.enable()
        self.addCleanup(self.keys.disable)
        self.order = reserve_order(self.owners[0], self.invoice.pk)
        self.url = f"/payments/orders/{self.order.pk}/return/"
        self.detail = f"/payments/orders/{self.order.pk}/"

    def password(self, username="return-customer", **changes):
        return self.client.post("/login/", {"username": username, "password": PASSWORD, **changes})

    def enroll_owner(self):
        MFADevice.objects.create(user=self.owners[0], encrypted_secret=encrypt_secret(TEST_TOTP_SECRET))

    @override_settings(PUBLIC_ORIGIN="https://billing.example.test")
    def test_snap_finish_uses_configured_origin_and_persisted_uuid(self):
        with patch.object(SandboxClient, "request", return_value={"redirect_url": URL}) as request:
            self.assertEqual(SandboxClient().create(self.order), URL)
        payload = request.call_args.args[2]
        self.assertEqual(payload["callbacks"], {"finish": "https://billing.example.test"+self.url})
        self.assertEqual(payload["transaction_details"]["gross_amount"], self.invoice.balance)

    @override_settings(PUBLIC_ORIGIN="")
    def test_unconfigured_origin_keeps_existing_snap_behavior(self):
        with patch.object(SandboxClient, "request", return_value={"redirect_url": URL}) as request:
            SandboxClient().create(self.order)
        self.assertNotIn("callbacks", request.call_args.args[2])

    @override_settings(PUBLIC_ORIGIN="https://billing.example.test", ALLOWED_HOSTS=["testserver", "poison.example.test"])
    def test_browser_host_forwarded_host_and_post_fields_cannot_choose_finish_url(self):
        verified_login(self.client, self.owners[0])
        with patch.object(SandboxClient, "request", return_value={"redirect_url": URL}) as request:
            self.client.post(f"/invoices/{self.invoice.pk}/checkout/", {"callbacks": "https://evil.test", "next": "https://evil.test"},
                HTTP_HOST="poison.example.test", HTTP_X_FORWARDED_HOST="evil.test")
        self.assertEqual(request.call_args.args[2]["callbacks"]["finish"], "https://billing.example.test"+self.url)

    def test_authenticated_return_ignores_all_query_status_and_does_not_contact_provider(self):
        verified_login(self.client, self.owners[0])
        with patch("core.gateway.SandboxClient.status") as status, patch("core.gateway.SandboxClient.create") as create:
            response = self.client.get(self.url+"?transaction_status=settlement&status_code=200&order_id=forged&next=https://evil.test", follow=True)
        self.assertEqual(response.redirect_chain, [(self.detail, 302)])
        self.assertContains(response, "kembali dari checkout belum membuktikan pembayaran berhasil")
        status.assert_not_called()
        create.assert_not_called()
        self.assertEqual(self.invoice.balance, self.invoice.total)
        self.assertEqual(GatewayEvent.objects.count(), 0)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(NetworkJob.objects.count(), 0)

    def test_anonymous_return_does_not_query_order_or_reveal_its_existence(self):
        for url in [self.url, f"/payments/orders/{uuid.uuid4()}/return/"]:
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, "/login/")
            self.assertFalse(any("core_gatewayorder" in item["sql"].lower() for item in queries))
            self.assertNotIn(self.invoice.customer_name, response.content.decode())

    def test_return_is_get_only_and_does_not_capture_posted_target(self):
        self.assertEqual(self.client.post(self.url, {"next": "https://evil.test"}).status_code, 405)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_customer_password_login_resumes_same_order_once(self):
        self.client.get(self.url)
        old_key = self.client.session.session_key
        response = self.password()
        self.assertEqual(response.url, self.url)
        self.assertNotEqual(self.client.session.session_key, old_key)
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertEqual(self.client.get(response.url).url, self.detail)
        self.assertEqual(self.client.get("/login/").url, "/portal/")

    def test_wrong_password_does_not_consume_resume_or_log_in(self):
        self.client.get(self.url)
        created = self.client.session[SESSION_KEY]["created"]
        self.assertEqual(self.password(password="incorrect").status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.session[SESSION_KEY]["created"], created)
        self.assertEqual(self.password().url, self.url)

    def test_login_still_requires_csrf(self):
        secure = Client(enforce_csrf_checks=True)
        secure.get(self.url)
        self.assertEqual(secure.post("/login/", {"username": "return-customer", "password": PASSWORD}).status_code, 403)
        self.assertNotIn("_auth_user_id", secure.session)

    def test_mfa_challenge_preserves_uuid_without_other_pre_auth_session_data(self):
        self.enroll_owner()
        self.client.get(self.url)
        session = self.client.session
        session["untrusted"] = "discard"
        session.save()
        old_key = session.session_key
        response = self.password("owner0")
        self.assertEqual(response.url, "/mfa/verify/")
        self.assertNotEqual(self.client.session.session_key, old_key)
        self.assertNotIn("untrusted", self.client.session)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.session[SESSION_KEY]["order"], str(self.order.pk))
        pending_key = self.client.session.session_key
        response = self.client.post("/mfa/verify/", {"code": pyotp.TOTP(TEST_TOTP_SECRET).now()})
        self.assertEqual(response.url, self.url)
        self.assertNotEqual(self.client.session.session_key, pending_key)
        self.assertNotIn("pending_auth", self.client.session)
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertEqual(self.client.get(response.url).url, self.detail)

    def test_failed_factor_keeps_resume_without_authentication(self):
        self.enroll_owner()
        self.client.get(self.url)
        self.password("owner0")
        self.assertEqual(self.client.post("/mfa/verify/", {"code": "invalid"}).status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertIn(SESSION_KEY, self.client.session)

    def test_revoked_login_clears_resume_and_cannot_bypass_factor(self):
        self.enroll_owner()
        self.client.get(self.url)
        self.password("owner0")
        Membership.objects.filter(user=self.owners[0]).update(session_version=2)
        response = self.client.post("/mfa/verify/", {"code": pyotp.TOTP(TEST_TOTP_SECRET).now()})
        self.assertEqual(response.url, "/login/")
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_first_mfa_enrollment_preserves_return_until_recovery_codes_acknowledged(self):
        self.client.get(self.url)
        self.assertEqual(self.password("owner0").url, "/mfa/setup/")
        self.client.get("/mfa/setup/")
        secret = decrypt_secret(self.client.session["mfa_setup"]["encrypted"])
        response = self.client.post("/mfa/setup/", {"code": pyotp.TOTP(secret).now()})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Simpan kode pemulihan")
        self.assertIn(SESSION_KEY, self.client.session)
        self.assertEqual(self.client.get("/login/").url, self.url)
        self.assertEqual(self.client.get(self.url).url, self.detail)

    def test_expired_resume_falls_back_to_portal(self):
        self.client.get(self.url)
        session = self.client.session
        session[SESSION_KEY]["created"] = time.time()-601
        session.save()
        self.assertEqual(self.password().url, "/portal/")

    def test_current_customer_scope_checked_after_login(self):
        self.client.get(self.url)
        Membership.objects.filter(user=self.portal).update(customer=self.customers[1])
        # Membership/tenant mismatch must fail closed at authentication itself.
        self.assertEqual(self.password().status_code, 403)
        self.assertFalse(Payment.objects.exists())

    def test_foreign_tenant_and_customer_cannot_read_return_target(self):
        verified_login(self.client, self.owners[1])
        self.assertEqual(self.client.get(self.url).status_code, 404)
        verified_login(self.client, self.portal)
        self.assertEqual(self.client.get(self.url).url, self.detail)
        other = self.customers[0].__class__.objects.create(tenant=self.tenants[0], code="TN-RETURN-OTHER", name="Other", phone="081299911111",
            username="return-other", address="Alamat", area="Lab", plan=self.customers[0].plan)
        Membership.objects.filter(user=self.portal).update(customer=other)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_settled_payment_remains_settled_without_rechecking_on_return(self):
        apply_status(self.order.pk, status_payload(self.order))
        verified_login(self.client, self.portal)
        with patch("core.gateway.SandboxClient.status") as status:
            self.assertEqual(self.client.get(self.url).url, self.detail)
        status.assert_not_called()
        self.assertEqual(self.invoice.balance, 0)
        self.assertEqual(Payment.objects.count(), 1)

    def test_arbitrary_next_urls_are_not_preserved_or_followed(self):
        response = self.client.get("/login/?next=https://evil.test")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertEqual(self.password(next="//evil.test").url, "/portal/")

    @override_settings(PUBLIC_ORIGIN="https://billing.example.test")
    def test_owner_sees_configured_notification_url_without_credentials(self):
        verified_login(self.client, self.owners[0])
        response = self.client.get("/settings/payment-gateway/")
        self.assertContains(response, "https://billing.example.test/payments/midtrans/notifications/")
        self.assertNotContains(response, "SB-Mid-server-")
        verified_login(self.client, self.portal)
        self.assertEqual(self.client.get("/settings/payment-gateway/").status_code, 403)
