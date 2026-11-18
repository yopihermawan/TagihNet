import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from unittest.mock import patch

import pyotp
from django.contrib.auth import HASH_SESSION_KEY, get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase

from core.account_profile import ProfileConflict, SALT, read_token, review_token, save_profile
from core.bulk_billing import confirm_batch, create_preview, process_batch
from core.models import AuditEvent, Invoice, MFADevice, Membership
from core.security import change_own_password
from .auth_helpers import TEST_TOTP_SECRET, verified_login
from .test_workflow import fixtures

PATH = "/account/profile/"


class AccountProfileTests(TestCase):
    @classmethod
    def setUpTestData(cls):cls.tenants, cls.owners, cls.customers = fixtures()

    def setUp(self):
        self.user = self.owners[0]
        verified_login(self.client, self.user)

    def token(self, client=None):return (client or self.client).get(PATH).context["form"]["token"].value()

    def proof(self):
        session = self.client.session
        return {"session_key": session.session_key, "access_version": session["access_version"],
                "mfa_revision": session["mfa_revision"], "auth_hash": session[HASH_SESSION_KEY]}

    def state(self):
        return {"user": get_user_model().objects.values().get(pk=self.user.pk),
                "member": Membership.objects.values().get(user=self.user),
                "device": MFADevice.objects.values().filter(user=self.user).first(),
                "audit": list(AuditEvent.objects.filter(action="account.profile_updated").values())}

    def post(self, token=None, **fields):
        return self.client.post(PATH, {"token": token or self.token(), "first_name": "Ayu", "last_name": "Utami", **fields})

    def test_anonymous_and_password_only_sessions_are_rejected(self):
        client = Client()
        self.assertEqual(client.get(PATH).status_code, 302)
        self.assertEqual(client.post(PATH, {}).status_code, 302)
        client.force_login(self.user)
        self.assertEqual(client.get(PATH).status_code, 302)
        self.assertNotIn("_auth_user_id", client.session)

    def test_get_is_private_read_only_and_linked_from_navigation(self):
        before = self.state()
        response = self.client.get(PATH)
        self.assertContains(response, "Profil saya")
        self.assertContains(response, 'autocomplete="given-name"')
        self.assertContains(response, 'autocomplete="family-name"')
        self.assertContains(response, self.user.username)
        self.assertContains(response, self.user.email)
        self.assertContains(response, self.tenants[0].name)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertContains(self.client.get("/account/security/"), 'href="/account/profile/"')
        self.assertTrue(self.state() == before)
        self.assertEqual(self.client.put(PATH).status_code, 405)

    def test_success_changes_only_names_and_retains_sessions_and_mfa(self):
        other = Client(); verified_login(other, self.user)
        before = self.state(); key = self.client.session.session_key
        self.assertRedirects(self.post(), PATH)
        after = self.state()
        self.assertEqual(after["user"], {**before["user"], "first_name": "Ayu", "last_name": "Utami"})
        self.assertTrue(after["member"] == before["member"] and after["device"] == before["device"])
        self.assertEqual(self.client.session.session_key, key)
        self.assertEqual(other.get(PATH).status_code, 200)
        self.assertContains(other.get(PATH), 'value="Ayu"')
        event = AuditEvent.objects.get(action="account.profile_updated")
        self.assertEqual((event.actor_id, event.tenant_id, event.target), (self.user.pk, self.tenants[0].pk, str(self.user.pk)))
        self.assertEqual(event.detail, "Nama tampilan diperbarui.")
        self.assertNotIn(self.user.email, event.detail)

    def test_spoofed_target_and_authentication_fields_are_ignored(self):
        before = self.state()
        other = get_user_model().objects.values().get(pk=self.owners[1].pk)
        response = self.post(user_id=self.owners[1].pk, tenant_id=self.tenants[1].pk,
            username="forged", email="forged@example.invalid", password="forged", role="owner", is_superuser="1", is_staff="1")
        self.assertRedirects(response, PATH)
        after = self.state()
        self.assertEqual(after["user"], {**before["user"], "first_name": "Ayu", "last_name": "Utami"})
        self.assertTrue(get_user_model().objects.values().get(pk=self.owners[1].pk) == other)

    def test_missing_tampered_and_oversized_tokens_do_not_write(self):
        token = self.token(); before = self.state()
        for invalid in ("", "invalid", token + "x", "x" * 2049):
            response = self.client.post(PATH, {"token": invalid, "first_name": "Changed"})
            self.assertEqual(response.status_code, 400)
            self.assertTrue(self.state() == before)

    def test_expired_form_is_rejected(self):
        token = self.token(); before = self.state()
        with patch("django.core.signing.time.time", return_value=time.time() + 901):
            self.assertEqual(self.post(token).status_code, 400)
        self.assertTrue(self.state() == before)

    def test_signed_foreign_account_form_is_rejected(self):
        other = Client(); verified_login(other, self.owners[1])
        token = self.token(other); before = self.state()
        self.assertEqual(self.post(token).status_code, 403)
        self.assertTrue(self.state() == before)

    def test_form_is_bound_to_its_own_session(self):
        other = Client(); verified_login(other, self.user)
        token = self.token(other); before = self.state()
        self.assertEqual(self.post(token).status_code, 409)
        self.assertTrue(self.state() == before)

    def test_stale_form_does_not_overwrite_new_profile_or_refresh_token(self):
        first, stale = self.token(), self.token()
        self.assertRedirects(self.post(first), PATH)
        before = self.state()
        response = self.post(stale, first_name="Older form")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.context["form"]["token"].value(), stale)
        self.assertContains(response, "Muat ulang profil", status_code=409)
        self.assertTrue(self.state() == before)

    def test_replay_after_change_has_no_duplicate_audit(self):
        token = self.token()
        self.assertRedirects(self.post(token), PATH)
        before = self.state()
        self.assertEqual(self.post(token).status_code, 409)
        self.assertTrue(self.state() == before)

    def test_noop_has_no_audit_and_names_can_be_cleared(self):
        self.assertRedirects(self.post(first_name="", last_name=""), PATH)
        self.assertFalse(AuditEvent.objects.filter(action="account.profile_updated").exists())
        self.post(); self.post(first_name="", last_name="")
        self.assertEqual(get_user_model().objects.get(pk=self.user.pk).get_short_name(), "")
        self.assertEqual(AuditEvent.objects.filter(action="account.profile_updated").count(), 2)
        self.assertContains(self.client.get(PATH), self.user.username)

    def test_names_are_trimmed_and_normalized_without_altering_internal_spaces(self):
        self.assertRedirects(self.post(first_name="  Anae\u0301  ", last_name="De  Vries-O'Neil"), PATH)
        user = get_user_model().objects.get(pk=self.user.pk)
        self.assertEqual((user.first_name, user.last_name), ("Anaé", "De  Vries-O'Neil"))

    def test_control_direction_and_overlong_names_are_rejected(self):
        token = self.token(); before = self.state()
        for name in ("x" * 151, "Bad\nName", "Bad\u202eName", "Bad\u200bName", "Bad\x00Name"):
            self.assertEqual(self.post(token, first_name=name).status_code, 400)
            self.assertTrue(self.state() == before)
        with self.assertRaises(ValidationError):save_profile(self.user, token, "\ud800", "", **self.proof())

    def test_markup_is_rendered_as_text(self):
        name = '<img src=x onerror="alert(1)">'
        self.assertRedirects(self.post(first_name=name), PATH)
        response = self.client.get(PATH)
        self.assertNotContains(response, name)
        self.assertContains(response, "&lt;img")
        self.assertEqual(AuditEvent.objects.get(action="account.profile_updated").detail, "Nama tampilan diperbarui.")

    def test_all_operator_presets_have_own_profile_without_access_editor(self):
        for role in ("owner", "ops", "finance", "cashier", "noc", "technician"):
            Membership.objects.filter(user=self.user).update(role=role)
            verified_login(self.client, self.user)
            self.assertRedirects(self.post(first_name="Name " + role), PATH)
            self.assertEqual(Membership.objects.get(user=self.user).role, role)

    def test_portal_without_mfa_can_edit_display_name(self):
        MFADevice.objects.filter(user=self.user).delete()
        Membership.objects.filter(user=self.user).update(role="customer", customer=self.customers[0])
        self.client = Client()
        self.assertRedirects(self.client.post("/login/", {"username": self.user.username, "password": "Local-test-secret-2026!"}), "/portal/")
        self.assertRedirects(self.post(), PATH)
        self.assertFalse(MFADevice.objects.filter(user=self.user).exists())
        self.assertEqual(self.client.get("/settings/access/").status_code, 403)

    def test_csrf_is_required(self):
        client = Client(enforce_csrf_checks=True); verified_login(client, self.user)
        token = self.token(client); before = self.state()
        self.assertEqual(client.post(PATH, {"token": token, "first_name": "Changed"}).status_code, 403)
        self.assertTrue(self.state() == before)

    def test_stale_access_and_device_revision_are_rechecked(self):
        token = self.token(); proof = self.proof()
        Membership.objects.filter(user=self.user).update(session_version=proof["access_version"] + 1)
        before = self.state()
        with self.assertRaises(PermissionDenied):save_profile(self.user, token, "Changed", "", **proof)
        self.assertTrue(self.state() == before)
        Membership.objects.filter(user=self.user).update(session_version=proof["access_version"])
        MFADevice.objects.filter(user=self.user).update(revision=uuid.uuid4())
        before = self.state()
        with self.assertRaises(PermissionDenied):save_profile(self.user, token, "Changed", "", **proof)
        self.assertTrue(self.state() == before)

    def test_password_change_between_session_check_and_save_is_refused(self):
        token = self.token(); proof = self.proof()
        user = get_user_model().objects.get(pk=self.user.pk)
        user.set_password("Another-local-passphrase-2026!"); user.save(update_fields=["password"])
        before = self.state()
        with self.assertRaises(PermissionDenied):save_profile(self.user, token, "Changed", "", **proof)
        self.assertTrue(self.state() == before)

    def test_disabled_or_unenrolled_operator_cannot_save(self):
        token = self.token(); proof = self.proof()
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):save_profile(self.user, token, "Changed", "", **proof)
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=True)
        MFADevice.objects.filter(user=self.user).delete(); proof["mfa_revision"] = "none"
        with self.assertRaises(PermissionDenied):save_profile(self.user, token, "Changed", "", **proof)

    def test_signed_unknown_format_and_bad_types_fail_closed(self):
        value = read_token(self.token())
        for data in ([], {**value, "extra": 1}, {**value, "v": 2}, {**value, "access": True}, {**value, "profile": []}):
            with self.assertRaises(ValidationError):read_token(signing.dumps(data, salt=SALT))

    def test_audit_failure_rolls_back_name_update(self):
        token = self.token(); before = self.state()
        with patch("core.account_profile.audit", side_effect=RuntimeError), self.assertRaises(RuntimeError):
            save_profile(self.user, token, "Changed", "", **self.proof())
        self.assertTrue(self.state() == before)

    def test_queued_billing_consent_and_customer_invoice_name_are_preserved(self):
        batch = create_preview(self.user, date(2026, 2, 1), {"state": "active", "plan": None, "area": ""})
        confirm_batch(self.user, batch.pk, time.time(), self.proof()["access_version"])
        self.assertRedirects(self.post(), PATH)
        self.assertEqual(process_batch(batch.pk), 1)
        batch.refresh_from_db(); self.assertEqual(batch.status, "completed")
        self.assertEqual(Invoice.objects.get().customer_name, self.customers[0].name)


class AccountProfileConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.tenants, self.owners, self.customers = fixtures()
        self.user = self.owners[0]; verified_login(self.client, self.user)
        session = self.client.session
        self.proof = {"session_key": session.session_key, "access_version": session["access_version"],
                      "mfa_revision": session["mfa_revision"], "auth_hash": session[HASH_SESSION_KEY]}
        self.token = review_token(self.user, Membership.objects.get(user=self.user), session.session_key)

    def parallel(self, callback):
        def run(index):
            close_old_connections()
            try:return callback(index)
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(run, range(2)))

    def test_same_profile_form_race_has_one_winner_and_one_audit(self):
        def change(index):
            try:save_profile(self.user, self.token, "Name " + str(index), "", **self.proof)
            except ProfileConflict:return False
            return True
        self.assertEqual(sorted(self.parallel(change)), [False, True])
        self.assertEqual(AuditEvent.objects.filter(action="account.profile_updated").count(), 1)

    def test_password_race_never_restores_old_password_or_changes_access_version(self):
        new_password = "Different-profile-race-passphrase-2026!"
        code = pyotp.TOTP(TEST_TOTP_SECRET).now()
        def change(index):
            if index:
                proof = {key:value for key,value in self.proof.items() if key != "session_key"}
                change_own_password(self.user, "Local-test-secret-2026!", new_password, code, **proof)
                return True
            try:save_profile(self.user, self.token, "Ayu", "", **self.proof)
            except PermissionDenied:return False
            return True
        result = self.parallel(change)
        self.assertTrue(result[1])
        user = get_user_model().objects.get(pk=self.user.pk)
        self.assertTrue(user.check_password(new_password))
        self.assertEqual(user.membership.session_version, self.proof["access_version"] + 1)
        self.assertEqual(AuditEvent.objects.filter(action="account.profile_updated").count(), int(result[0]))
