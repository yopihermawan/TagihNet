import time
import uuid
from datetime import timedelta
from io import StringIO
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import pyotp
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone
from core.access import create_member, save_member, save_role
from core.models import AccessRole, AuditEvent, AuthRateBucket, MFADevice, Membership
from core.security import (confirm_enrollment, decrypt_secret, encrypt_secret, new_recovery_codes,
    recovery_digest, regenerate_codes, reserve_attempt, RateLimited, verify_factor)
from .auth_helpers import TEST_TOTP_SECRET, verified_login
from .test_workflow import fixtures


class SecurityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()

    def password_login(self, client=None, user=None):
        client = client or self.client
        user = user or self.owners[0]
        return client.post("/login/", {"username": user.username, "password": "Local-test-secret-2026!"})

    def enroll(self, user=None):
        user = user or self.owners[0]
        # Previous accepted time step leaves the current code available for a fresh login.
        code = pyotp.TOTP(TEST_TOTP_SECRET).at(time.time() - 30)
        return confirm_enrollment(user, encrypt_secret(TEST_TOTP_SECRET), code)

    def test_password_only_session_cannot_access_any_application_endpoint(self):
        self.assertRedirects(self.password_login(), "/mfa/setup/")
        self.assertNotIn("_auth_user_id", self.client.session)
        for route in ["/", "/customers/", "/invoices/", "/settings/access/", "/account/security/"]:
            self.assertEqual(self.client.get(route).status_code, 302)
        self.assertEqual(self.client.post(f"/invoices/1/pay/", {}).status_code, 302)

    def test_setup_confirms_before_login_and_shows_codes_once(self):
        self.password_login()
        response = self.client.get("/mfa/setup/")
        encrypted = self.client.session["mfa_setup"]["encrypted"]
        secret = decrypt_secret(encrypted)
        self.assertNotIn(secret, encrypted)
        self.assertFalse(MFADevice.objects.exists())
        response = self.client.post("/mfa/setup/", {"code": pyotp.TOTP(secret).now()})
        self.assertContains(response, "Simpan kode pemulihan")
        codes = response.context["codes"]
        self.assertEqual(len(codes), 10)
        self.assertEqual(len(set(codes)), 10)
        self.assertIn("_auth_user_id", self.client.session)
        device = MFADevice.objects.get(user=self.owners[0])
        self.assertNotIn(secret, device.encrypted_secret)
        self.assertNotIn(codes[0], str(device.recovery_hashes))
        self.assertNotIn("mfa_setup", self.client.session)
        self.assertNotIn("pending_auth", self.client.session)
        self.assertEqual(self.client.get("/mfa/setup/").status_code, 302)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertNotIn(secret, str(list(AuditEvent.objects.values())))
        self.assertNotIn(codes[0], str(list(AuditEvent.objects.values())))

    def test_invalid_setup_code_does_not_create_device(self):
        self.password_login()
        self.client.get("/mfa/setup/")
        response = self.client.post("/mfa/setup/", {"code": "invalid"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(MFADevice.objects.exists())
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_customer_can_opt_in_to_mfa_without_operator_enrollment_policy(self):
        Membership.objects.filter(user=self.owners[0]).update(role="customer", customer=self.customers[0])
        self.assertRedirects(self.password_login(), "/portal/")
        self.assertFalse(MFADevice.objects.exists())
        self.assertRedirects(self.client.post("/account/security/", {"password": "Local-test-secret-2026!"}), "/mfa/setup/")
        self.client.get("/mfa/setup/")
        secret = decrypt_secret(self.client.session["mfa_setup"]["encrypted"])
        response = self.client.post("/mfa/setup/", {"code": pyotp.TOTP(secret).now()})
        self.assertContains(response, "Simpan kode pemulihan")
        self.assertEqual(self.client.get("/portal/").status_code, 200)
        self.client.post("/logout/")
        self.assertRedirects(self.password_login(), "/mfa/verify/")

    def test_mfa_and_access_forms_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post("/mfa/setup/", {"code": "123456"}).status_code, 403)
        self.assertEqual(client.post("/mfa/verify/", {"code": "123456"}).status_code, 403)
        verified_login(client, self.owners[0])
        self.assertEqual(client.post("/settings/access/roles/new/", {"name": "Blocked"}).status_code, 403)

    def test_enrolled_login_requires_second_factor_and_rejects_replay(self):
        self.enroll()
        self.assertRedirects(self.password_login(), "/mfa/verify/")
        current = pyotp.TOTP(TEST_TOTP_SECRET).now()
        response = self.client.post("/mfa/verify/", {"code": current})
        self.assertRedirects(response, "/")
        self.client.post("/logout/")
        self.password_login()
        response = self.client.post("/mfa/verify/", {"code": current})
        self.assertContains(response, "telah dipakai")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_recovery_code_is_one_time_and_case_insensitive(self):
        codes = self.enroll()
        self.password_login()
        self.assertRedirects(self.client.post("/mfa/verify/", {"code": codes[0].lower()}), "/")
        self.client.post("/logout/")
        self.password_login()
        self.assertContains(self.client.post("/mfa/verify/", {"code": codes[0]}), "telah dipakai")
        self.assertEqual(len(MFADevice.objects.get(user=self.owners[0]).recovery_hashes), 9)

    def test_expired_pending_auth_cannot_confirm_factor(self):
        self.enroll()
        self.password_login()
        session = self.client.session
        pending = session["pending_auth"]
        pending["started"] = time.time() - 301
        session["pending_auth"] = pending
        session.save()
        self.assertRedirects(self.client.post("/mfa/verify/", {"code": pyotp.TOTP(TEST_TOTP_SECRET).now()}), "/login/")

    def test_password_reset_or_role_change_invalidates_pending_login(self):
        codes = self.enroll()
        self.password_login()
        Membership.objects.filter(user=self.owners[0]).update(session_version=99)
        self.assertRedirects(self.client.post("/mfa/verify/", {"code": codes[0]}), "/login/")
        self.password_login()
        self.owners[0].set_password("Another-test-secret-2026!")
        self.owners[0].save()
        self.assertRedirects(self.client.post("/mfa/verify/", {"code": codes[1]}), "/login/")

    def test_mfa_attempts_throttled_with_unspoofable_pending_identity(self):
        self.enroll()
        self.password_login()
        for index in range(5):
            self.assertEqual(self.client.post("/mfa/verify/", {"code": "invalid", "username": f"fake{index}"}).status_code, 200)
        response = self.client.post("/mfa/verify/", {"code": "invalid", "username": "fresh"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "900")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_unverified_or_pre_migration_session_is_revoked(self):
        self.client.force_login(self.owners[0])
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_changed_device_revision_revokes_other_sessions(self):
        verified_login(self.client, self.owners[0])
        other = Client()
        verified_login(other, self.owners[0])
        regenerate_codes(self.owners[0])
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(other.get("/").status_code, 302)

    def test_custom_role_enforced_on_direct_pdf_and_mutation_endpoints(self):
        user = get_user_model().objects.create_user(username="operator", password="Not-a-default-2026!")
        member = Membership.objects.create(user=user, tenant=self.tenants[0], role="ops")
        role = save_role(self.owners[0], "Hanya pelanggan", ["customers"])
        save_member(self.owners[0], member.pk, "ops", role.pk, None, True)
        verified_login(self.client, user)
        self.assertEqual(self.client.get("/customers/").status_code, 200)
        self.assertEqual(self.client.get("/invoices/").status_code, 403)
        self.assertEqual(self.client.get("/invoices/1/pdf/").status_code, 403)
        self.assertEqual(self.client.post("/customers/new/", {}).status_code, 403)
        self.assertEqual(self.client.get("/settings/access/").status_code, 403)

    def test_role_change_revokes_sessions_and_owner_can_not_delegate_access_editor(self):
        user = get_user_model().objects.create_user(username="staff")
        member = Membership.objects.create(user=user, tenant=self.tenants[0], role="ops")
        verified_login(self.client, user)
        role = save_role(self.owners[0], "Pembaca", ["invoices"])
        save_member(self.owners[0], member.pk, "ops", role.pk, None, True)
        self.assertEqual(self.client.get("/customers/").status_code, 302)
        with self.assertRaises(PermissionDenied):
            save_role(self.owners[0], "Eskalasi", ["manage_access"])
        with self.assertRaises(ValidationError):
            save_role(self.owners[0], "Izin tanpa prasyarat", ["pay"])

    def test_editing_role_caps_revokes_all_assigned_sessions(self):
        role = save_role(self.owners[0], "Pembaca", ["customers"])
        user = get_user_model().objects.create_user(username="staff")
        Membership.objects.create(user=user, tenant=self.tenants[0], role="ops", access_role=role)
        verified_login(self.client, user)
        save_role(self.owners[0], role.name, ["invoices"], role.pk)
        self.assertEqual(self.client.get("/customers/").status_code, 302)

    def test_last_active_owner_cannot_be_demoted_or_disabled(self):
        member = Membership.objects.get(user=self.owners[0])
        for role, active in [("ops", True), ("owner", False)]:
            with self.assertRaises(ValidationError):
                save_member(self.owners[0], member.pk, role, None, None, active)
        self.assertEqual(Membership.objects.get(pk=member.pk).role, "owner")

    def test_owner_can_promote_and_then_demote_with_another_owner_present(self):
        new = create_member(self.owners[0], "new-owner", "new@example.invalid", "Unique-local-2026!", "owner")
        member = Membership.objects.get(user=self.owners[0])
        save_member(self.owners[0], member.pk, "ops", None, None, True)
        self.assertEqual(Membership.objects.filter(tenant=self.tenants[0], role="owner").count(), 1)
        with self.assertRaises(PermissionDenied):
            save_role(self.owners[0], "No longer owner", ["invoices"])

    def test_cross_tenant_role_member_and_customer_assignment_blocked(self):
        verified_login(self.client, self.owners[0])
        other = Membership.objects.get(user=self.owners[1])
        other_role = save_role(self.owners[1], "Private", ["customers"])
        for route in [f"/settings/access/members/{other.pk}/", f"/settings/access/roles/{other_role.pk}/"]:
            self.assertEqual(self.client.get(route).status_code, 404)
            self.assertEqual(self.client.post(route, {}).status_code, 404)
        member = Membership.objects.get(user=self.owners[0])
        with self.assertRaises(AccessRole.DoesNotExist):
            save_member(self.owners[0], member.pk, "ops", other_role.pk, None, True)
        response = self.client.post(f"/settings/access/members/{member.pk}/", {"role": "customer", "customer": self.customers[1].pk, "active": "on"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Membership.objects.get(pk=member.pk).role, "owner")

    def test_nonowner_cannot_create_users_or_assign_owner_by_api_or_service(self):
        user = get_user_model().objects.create_user(username="ops")
        Membership.objects.create(user=user, tenant=self.tenants[0], role="ops")
        verified_login(self.client, user)
        self.assertEqual(self.client.post("/settings/access/members/new/", {"role": "owner"}).status_code, 403)
        with self.assertRaises(PermissionDenied):
            create_member(user, "bad", "bad@example.invalid", "Password-2026!", "owner")

    def test_sensitive_access_mutation_requires_recent_mfa(self):
        verified_login(self.client, self.owners[0])
        session = self.client.session
        session["mfa_verified_at"] = time.time() - 301
        session.save()
        response = self.client.post("/settings/access/roles/new/", {"name": "Blocked", "capabilities": ["invoices"]})
        self.assertRedirects(response, "/account/security/")
        self.assertFalse(AccessRole.objects.filter(name="Blocked").exists())

    def test_stepup_requires_password_and_fresh_factor(self):
        verified_login(self.client, self.owners[0])
        response = self.client.post("/account/security/", {"password": "wrong", "code": pyotp.TOTP(TEST_TOTP_SECRET).now()})
        self.assertContains(response, "Verifikasi tidak berhasil")
        self.assertRedirects(self.client.post("/account/security/", {"password": "Local-test-secret-2026!", "code": pyotp.TOTP(TEST_TOTP_SECRET).now()}), "/settings/access/")

    def test_recovery_regeneration_invalidates_old_codes_and_requires_current_proof(self):
        old = self.enroll()
        verified_login(self.client, self.owners[0])
        response = self.client.post("/account/security/", {"password": "Local-test-secret-2026!", "code": old[0], "action": "regenerate"})
        self.assertContains(response, "Simpan kode pemulihan")
        self.assertFalse(verify_factor(self.owners[0], old[1]))
        self.assertTrue(verify_factor(self.owners[0], response.context["codes"][0]))
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_new_account_creation_hashes_password_and_is_tenant_scoped(self):
        verified_login(self.client, self.owners[0])
        response = self.client.post("/settings/access/members/new/", {"username": "new-staff", "email": "new@example.invalid",
            "password": "Strong-new-local-2026!", "role": "cashier"})
        self.assertRedirects(response, "/settings/access/")
        user = get_user_model().objects.get(username="new-staff")
        self.assertEqual(user.membership.tenant_id, self.tenants[0].pk)
        self.assertTrue(user.check_password("Strong-new-local-2026!"))
        self.assertNotEqual(user.password, "Strong-new-local-2026!")
        self.assertFalse(user.is_superuser)

    def test_offline_reset_requires_exact_identity_and_revokes_sessions(self):
        verified_login(self.client, self.owners[0])
        reason = "Identitas pemilik diverifikasi melalui proses lokal uji"
        with self.assertRaises(CommandError):
            call_command("reset_mfa", username=self.owners[0].username, confirm_user_id=self.owners[1].pk, reason=reason, stdout=StringIO())
        self.assertTrue(MFADevice.objects.filter(user=self.owners[0]).exists())
        call_command("reset_mfa", username=self.owners[0].username, confirm_user_id=self.owners[0].pk, reason=reason, stdout=StringIO())
        self.assertFalse(MFADevice.objects.filter(user=self.owners[0]).exists())
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertRedirects(self.password_login(), "/mfa/setup/")

    def test_revocation_between_factor_and_session_creation_is_rejected(self):
        codes = self.enroll()
        self.password_login()
        original = verify_factor
        def verify_then_revoke(user, code):
            result = original(user, code)
            Membership.objects.filter(user=user).update(session_version=99)
            return result
        with patch("core.security_views.verify_factor", side_effect=verify_then_revoke):
            self.assertEqual(self.client.post("/mfa/verify/", {"code": codes[0]}).status_code, 403)
        self.assertNotIn("_auth_user_id", self.client.session)


class SecurityConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.tenants, self.owners, self.customers = fixtures()

    def parallel(self, function):
        def wrapped(index):
            close_old_connections()
            try:
                return function(index)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(wrapped, range(2)))

    def test_totp_replay_concurrently_only_one_acceptance(self):
        MFADevice.objects.create(user=self.owners[0], encrypted_secret=encrypt_secret(TEST_TOTP_SECRET))
        code = pyotp.TOTP(TEST_TOTP_SECRET).now()
        results = self.parallel(lambda _: verify_factor(self.owners[0], code))
        self.assertEqual(sorted(results), [False, True])

    def test_recovery_replay_concurrently_only_one_acceptance(self):
        codes, hashes = new_recovery_codes()
        MFADevice.objects.create(user=self.owners[0], encrypted_secret=encrypt_secret(TEST_TOTP_SECRET), recovery_hashes=hashes)
        results = self.parallel(lambda _: verify_factor(self.owners[0], codes[0]))
        self.assertEqual(sorted(results), [False, True])

    def test_simultaneous_self_demotion_preserves_one_owner(self):
        other = create_member(self.owners[0], "another-owner", "new@example.invalid", "Password-test-2026!", "owner")
        owners = [self.owners[0], other.user]
        member_ids = [Membership.objects.get(user=actor).pk for actor in owners]
        def demote(index):
            actor = owners[index]
            try:
                save_member(actor, member_ids[index], "ops", None, None, True)
                return True
            except ValidationError:
                return False
        self.assertEqual(sorted(self.parallel(demote)), [False, True])
        self.assertEqual(Membership.objects.filter(tenant=self.tenants[0], role="owner", user__is_active=True).count(), 1)

    def test_limiter_atomic_count_never_exceeds_limit(self):
        key = "shared-test-key"
        AuthRateBucket.objects.create(key=key, attempts=4, expires_at=timezone.now() + timedelta(minutes=15))
        def reserve(_):
            try:
                reserve_attempt([key])
                return True
            except RateLimited:
                return False
        self.assertEqual(sorted(self.parallel(reserve)), [False, True])
        self.assertEqual(AuthRateBucket.objects.get(key=key).attempts, 5)
