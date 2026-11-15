import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from unittest.mock import patch

import pyotp
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase

from core.bulk_billing import confirm_batch, create_preview, process_batch
from core.models import AuditEvent, Invoice, MFADevice, Membership
from core.security import change_own_password, new_recovery_codes, regenerate_codes
from .auth_helpers import TEST_TOTP_SECRET, verified_login
from .test_workflow import fixtures

OLD = "Local-test-secret-2026!"
NEW = "Different-local-passphrase-2026!"
PATH = "/account/password/"


class OwnPasswordTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()

    def setUp(self):
        self.user = self.owners[0]
        verified_login(self.client, self.user)

    def payload(self, **changes):
        return {"old_password": OLD, "new_password1": NEW, "new_password2": NEW,
                "code": pyotp.TOTP(TEST_TOTP_SECRET).now(), **changes}

    def proof(self):
        device = MFADevice.objects.filter(user=self.user).first()
        user = get_user_model().objects.get(pk=self.user.pk)
        return {"access_version": user.membership.session_version,
                "mfa_revision": str(device.revision) if device else "none", "auth_hash": user.get_session_auth_hash()}

    def state(self):
        return {"password": get_user_model().objects.get(pk=self.user.pk).password,
                "member": Membership.objects.values().get(user=self.user),
                "device": MFADevice.objects.values().filter(user=self.user).first(),
                "audit": list(AuditEvent.objects.filter(action="auth.password_changed").values())}

    def change(self, **changes):
        values = {"old_password": OLD, "new_password": NEW, "code": pyotp.TOTP(TEST_TOTP_SECRET).now(), **changes}
        return change_own_password(self.user, **values, **self.proof())

    def test_anonymous_and_password_only_sessions_cannot_access(self):
        client = Client()
        self.assertEqual(client.get(PATH).status_code, 302)
        self.assertEqual(client.post(PATH, self.payload()).status_code, 302)
        client.force_login(self.user)
        self.assertEqual(client.get(PATH).status_code, 302)
        self.assertNotIn("_auth_user_id", client.session)

    def test_get_is_private_read_only_and_linked_from_security(self):
        before = self.state()
        response = self.client.get(PATH)
        self.assertContains(response, "Simpan sandi baru dan keluar")
        self.assertContains(response, 'autocomplete="current-password"')
        self.assertContains(response, 'autocomplete="new-password"', count=2)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertContains(self.client.get("/account/security/"), 'href="/account/password/"')
        self.assertTrue(self.state() == before)
        self.assertEqual(self.client.put(PATH).status_code, 405)

    def test_success_hashes_password_revokes_sessions_and_pending_login(self):
        other = Client(); verified_login(other, self.user)
        pending = Client()
        self.assertRedirects(pending.post("/login/", {"username": self.user.username, "password": OLD}), "/mfa/verify/")
        device = MFADevice.objects.get(user=self.user)
        encrypted, revision = device.encrypted_secret, device.revision
        token = default_token_generator.make_token(self.user)
        before_version = self.proof()["access_version"]
        response = self.client.post(PATH, self.payload())
        self.assertRedirects(response, "/login/")
        user = get_user_model().objects.get(pk=self.user.pk)
        self.assertTrue(user.check_password(NEW)); self.assertFalse(user.check_password(OLD))
        self.assertTrue(user.password != NEW)
        self.assertEqual(user.membership.session_version, before_version + 1)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(other.get("/").status_code, 302)
        self.assertRedirects(pending.post("/mfa/verify/", {"code": "invalid"}), "/login/")
        self.assertFalse(default_token_generator.check_token(user, token))
        fresh = Client()
        self.assertRedirects(fresh.post("/login/", {"username": user.username, "password": NEW}), "/mfa/verify/")
        self.assertNotIn("_auth_user_id", fresh.session)
        device.refresh_from_db()
        self.assertTrue(device.encrypted_secret == encrypted); self.assertEqual(device.revision, revision)
        self.assertGreater(device.last_step, -1)
        event = AuditEvent.objects.get(action="auth.password_changed")
        self.assertEqual((event.actor_id, event.tenant_id, event.target), (user.pk, self.tenants[0].pk, str(user.pk)))
        for secret in (OLD, NEW, TEST_TOTP_SECRET, self.payload()["code"]):
            self.assertNotIn(secret, event.detail)

    def test_wrong_current_password_does_not_consume_factor_or_echo_secrets(self):
        before = self.state()
        response = self.client.post(PATH, self.payload(old_password="Wrong-private-proof-2026!"))
        self.assertContains(response, "Verifikasi tidak berhasil")
        for secret in ("Wrong-private-proof-2026!", NEW, self.payload()["code"]):self.assertNotContains(response, 'value="' + secret + '"')
        self.assertTrue(self.state() == before)

    def test_mfa_required_even_when_session_was_recently_verified(self):
        before = self.state()
        for code in ("", "invalid"):
            response = self.client.post(PATH, self.payload(code=code))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(self.state() == before)

    def test_password_policy_and_reuse_failure_do_not_consume_factor(self):
        before = self.state()
        for password in ("short", "12345678901234567890", OLD, "x" * 257):
            with self.subTest(case=len(password)), self.assertRaises(ValidationError):self.change(new_password=password)
            self.assertTrue(self.state() == before)

    def test_confirmation_mismatch_and_empty_post_are_rejected(self):
        before = self.state()
        self.assertContains(self.client.post(PATH, self.payload(new_password2="Different-confirmation-2026!")), "Kedua kata sandi baru harus sama")
        self.assertEqual(self.client.post(PATH, {}).status_code, 200)
        self.assertTrue(self.state() == before)

    def test_password_spaces_and_unicode_are_preserved(self):
        password = "  Rahasia-pribadi-é-2026!  "
        self.assertRedirects(self.client.post(PATH, self.payload(new_password1=password, new_password2=password)), "/login/")
        user = get_user_model().objects.get(pk=self.user.pk)
        self.assertTrue(user.check_password(password)); self.assertFalse(user.check_password(password.strip()))

    def test_portal_without_mfa_can_change_own_password(self):
        MFADevice.objects.filter(user=self.user).delete()
        Membership.objects.filter(user=self.user).update(role="customer", customer=self.customers[0])
        self.client = Client()
        self.assertRedirects(self.client.post("/login/", {"username": self.user.username, "password": OLD}), "/portal/")
        self.assertNotIn("code", self.client.get(PATH).context["form"].fields)
        self.assertRedirects(self.client.post(PATH, self.payload(code="")), "/login/")
        self.assertFalse(MFADevice.objects.filter(user=self.user).exists())

    def test_portal_with_mfa_cannot_omit_factor(self):
        Membership.objects.filter(user=self.user).update(role="customer", customer=self.customers[0])
        before = self.state()
        self.assertEqual(self.client.post(PATH, self.payload(code="")).status_code, 200)
        self.assertTrue(self.state() == before)

    def test_low_privilege_account_can_change_only_itself_despite_forged_target(self):
        Membership.objects.filter(user=self.user).update(role="cashier")
        other = get_user_model().objects.get(pk=self.owners[1].pk)
        other_hash = other.password
        self.assertRedirects(self.client.post(PATH, self.payload(user_id=other.pk, tenant_id=self.tenants[1].pk, role="owner", is_superuser="1")), "/login/")
        self.assertEqual(Membership.objects.get(user=self.user).role, "cashier")
        other.refresh_from_db(); self.assertTrue(other.password == other_hash)
        self.assertFalse(get_user_model().objects.get(pk=self.user.pk).is_superuser)

    def test_csrf_is_required_and_rejection_does_not_change_account(self):
        client = Client(enforce_csrf_checks=True); verified_login(client, self.user)
        before = self.state()
        self.assertEqual(client.post(PATH, self.payload()).status_code, 403)
        self.assertTrue(self.state() == before)

    def test_attempt_limit_uses_authenticated_identity_and_ip(self):
        before = self.state()
        for index in range(5):
            self.assertEqual(self.client.post(PATH, self.payload(old_password="wrong", username=f"spoof-{index}", user_id=index)).status_code, 200)
        response = self.client.post(PATH, self.payload(username="new-spoof", user_id=999))
        self.assertEqual(response.status_code, 429); self.assertEqual(response["Retry-After"], "900")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertTrue(self.state() == before)

    def test_recovery_code_is_consumed_once_without_changing_mfa_key(self):
        codes, hashes = new_recovery_codes()
        MFADevice.objects.filter(user=self.user).update(recovery_hashes=hashes)
        self.change(code=codes[0])
        self.assertEqual(len(MFADevice.objects.get(user=self.user).recovery_hashes), 9)
        before = self.state()
        with self.assertRaises(ValidationError):self.change(old_password=NEW, new_password="Third-distinct-passphrase-2026!", code=codes[0])
        self.assertTrue(self.state() == before)

    def test_stale_access_or_factor_revision_is_rechecked_under_lock(self):
        proof = self.proof()
        Membership.objects.filter(user=self.user).update(session_version=proof["access_version"] + 1)
        before = self.state()
        with self.assertRaises(PermissionDenied):change_own_password(self.user, OLD, NEW, self.payload()["code"], **proof)
        self.assertTrue(self.state() == before)
        proof = self.proof()
        MFADevice.objects.filter(user=self.user).update(revision=uuid.uuid4())
        before = self.state()
        with self.assertRaises(PermissionDenied):change_own_password(self.user, OLD, NEW, self.payload()["code"], **proof)
        self.assertTrue(self.state() == before)

    def test_password_changed_after_session_check_is_rejected(self):
        proof = self.proof()
        user = get_user_model().objects.get(pk=self.user.pk)
        user.set_password("Concurrent-reset-passphrase-2026!"); user.save(update_fields=["password"])
        before = self.state()
        with self.assertRaises(PermissionDenied):
            change_own_password(self.user, "Concurrent-reset-passphrase-2026!", NEW, self.payload()["code"], **proof)
        self.assertTrue(self.state() == before)

    def test_disabled_account_and_operator_without_device_are_refused(self):
        proof = self.proof()
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):change_own_password(self.user, OLD, NEW, "", **proof)
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=True)
        MFADevice.objects.filter(user=self.user).delete()
        with self.assertRaises(PermissionDenied):self.change(code="")

    def test_audit_failure_rolls_back_password_version_and_recovery_consumption(self):
        codes, hashes = new_recovery_codes()
        MFADevice.objects.filter(user=self.user).update(recovery_hashes=hashes)
        before = self.state()
        with patch("core.security.audit", side_effect=RuntimeError), self.assertRaises(RuntimeError):self.change(code=codes[0])
        self.assertTrue(self.state() == before)

    def test_queued_billing_is_held_after_password_change(self):
        batch = create_preview(self.user, date(2026, 2, 1), {"state": "active", "plan": None, "area": ""})
        confirm_batch(self.user, batch.pk, time.time(), self.proof()["access_version"])
        self.change()
        self.assertEqual(process_batch(batch.pk), 0)
        batch.refresh_from_db(); self.assertEqual(batch.status, "blocked")
        self.assertFalse(Invoice.objects.exists())


class OwnPasswordConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.tenants, self.owners, self.customers = fixtures()
        self.user = self.owners[0]
        verified_login(self.client, self.user)
        device = MFADevice.objects.get(user=self.user)
        self.proof = {"access_version": self.user.membership.session_version,
                      "mfa_revision": str(device.revision), "auth_hash": self.user.get_session_auth_hash()}

    def parallel(self, callback):
        def run(index):
            close_old_connections()
            try:return callback(index)
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(run, range(2)))

    def test_two_password_changes_allow_one_commit_and_one_audit(self):
        code = pyotp.TOTP(TEST_TOTP_SECRET).now()
        def change(index):
            try:change_own_password(self.user, OLD, NEW + str(index), code, **self.proof)
            except (PermissionDenied, ValidationError):return False
            return True
        self.assertEqual(sorted(self.parallel(change)), [False, True])
        self.assertEqual(Membership.objects.get(user=self.user).session_version, self.proof["access_version"] + 1)
        self.assertEqual(AuditEvent.objects.filter(action="auth.password_changed").count(), 1)

    def test_recovery_regeneration_and_password_change_finish_without_deadlock(self):
        code = pyotp.TOTP(TEST_TOTP_SECRET).now()
        def action(index):
            if index:
                regenerate_codes(self.user)
                return True
            try:change_own_password(self.user, OLD, NEW, code, **self.proof)
            except (PermissionDenied, ValidationError):return False
            return True
        results = self.parallel(action)
        self.assertTrue(results[1])
        self.assertEqual(AuditEvent.objects.filter(action="auth.password_changed").count(), int(results[0]))
        self.assertEqual(Membership.objects.get(user=self.user).session_version, self.proof["access_version"] + 1 + int(results[0]))
