import time
import uuid
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from core import router_simulation as simulation
from core.access import save_role
from core.models import AuditEvent, Customer, Membership, MFADevice, NetworkJob, NetworkJobRetry
from core.network import run_job
from core.network_recovery import confirm_retry, review_retry

from .auth_helpers import verified_login
from .test_workflow import fixtures

REASON = 'Skenario koneksi simulasi diperiksa oleh operator.'


@override_settings(SIMULATION=True)
class RouterSimulationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        tenants, owners, customers = fixtures()
        cls.actor, cls.customer = owners[0], customers[0]
        cls.job = NetworkJob.objects.create(tenant=tenants[0], customer=customers[0])
        cls.foreign_job = NetworkJob.objects.create(tenant=tenants[1], customer=customers[1])
        cls.detail = f'/network/jobs/{cls.job.pk}/'
        cls.editor = cls.detail + 'router-simulation/'
        cls.confirmation = cls.editor + 'confirm/'

    def setUp(self):
        verified_login(self.client, self.actor)

    def review(self, available):
        return simulation.review_scenario(self.actor, self.job.pk, available, REASON)[1]

    def submit(self, token):
        return self.client.post(self.confirmation, {'token': token, 'reviewed': 'on'})

    def events(self):
        return AuditEvent.objects.filter(action=simulation.ACTION)

    def test_get_and_review_do_not_write_and_confirm_updates_only_the_scenario(self):
        baseline = Customer.objects.values().get(pk=self.customer.pk)
        job_baseline = list(NetworkJob.objects.values())
        self.assertContains(self.client.get(self.detail), 'Ubah skenario router simulasi')
        self.assertContains(self.client.get(self.editor), 'Dapat dijangkau')
        response = self.client.post(self.editor, {'available': '0', 'reason': REASON})
        self.assertContains(response, 'Tinjau skenario router simulasi')
        self.assertContains(response, 'Tidak dapat dijangkau')
        self.assertEqual(Customer.objects.values().get(pk=self.customer.pk), baseline)
        self.assertFalse(self.events().exists())
        token = response.context['form'].initial['token']
        self.assertRedirects(self.submit(token), self.detail, fetch_redirect_response=False)
        self.assertEqual(Customer.objects.values().get(pk=self.customer.pk), {**baseline, 'router_available': False})
        self.assertEqual(list(NetworkJob.objects.values()), job_baseline)
        event = self.events().get()
        self.assertEqual((event.tenant_id, event.actor_id, event.target),
                         (self.customer.tenant_id, self.actor.pk, str(self.customer.pk)))
        self.assertIn('Dapat dijangkau → Tidak dapat dijangkau', event.detail)
        self.assertIn(REASON, event.detail)

    def test_scope_and_custom_permission_are_enforced(self):
        foreign_editor = f'/network/jobs/{self.foreign_job.pk}/router-simulation/'
        self.assertEqual(self.client.get(foreign_editor).status_code, 404)
        self.assertEqual(self.client.post(foreign_editor+'confirm/', {'token': self.review(False)}).status_code, 404)
        corrupt = NetworkJob.objects.create(tenant_id=self.customer.tenant_id, customer=self.foreign_job.customer)
        self.assertEqual(self.client.get(f'/network/jobs/{corrupt.pk}/router-simulation/').status_code, 404)
        with self.assertRaises(ValidationError):
            save_role(self.actor, 'Tanpa akses jaringan', {simulation.CAPABILITY})
        role = save_role(self.actor, 'Operator skenario', simulation.RIGHTS)
        Membership.objects.filter(user=self.actor).update(role='noc')
        self.assertNotContains(self.client.get(self.detail), 'Ubah skenario router simulasi')
        self.assertEqual(self.client.get(self.editor).status_code, 403)
        self.assertEqual(self.client.post(self.editor, {'available': '0', 'reason': REASON}).status_code, 403)
        Membership.objects.filter(user=self.actor).update(access_role=role)
        self.assertContains(self.client.get(self.editor), 'Ubah skenario router simulasi')
        self.assertEqual(self.client.post(self.detail+'retry/', {'reason': REASON}).status_code, 403)
        self.assertEqual(self.submit(self.review(False)).status_code, 302)

    def test_confirmation_checks_csrf_method_checkbox_and_mfa_freshness(self):
        token = self.review(False)
        self.assertEqual(self.client.get(self.confirmation).status_code, 405)
        self.assertEqual(self.client.post(self.confirmation, {'token': token}).status_code, 400)
        csrf = Client(enforce_csrf_checks=True)
        verified_login(csrf, self.actor)
        self.assertEqual(csrf.post(self.confirmation, {'token': token, 'reviewed': 'on'}).status_code, 403)
        session = self.client.session
        session['mfa_verified_at'] = time.time() - 301
        session.save()
        self.assertRedirects(self.submit(token), '/account/security/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['stepup_return'], self.editor)
        self.assertFalse(self.events().exists())

    def test_review_rejects_tampering_wrong_job_and_changed_access(self):
        token = self.review(False)
        self.assertRedirects(self.submit('x'+token), self.editor, fetch_redirect_response=False)
        other_job = NetworkJob.objects.create(tenant_id=self.customer.tenant_id, customer=self.customer)
        self.assertEqual(self.client.post(f'/network/jobs/{other_job.pk}/router-simulation/confirm/',
                                         {'token': token, 'reviewed': 'on'}).status_code, 403)
        MFADevice.objects.filter(user=self.actor).update(revision=uuid.uuid4())
        verified_login(self.client, self.actor)
        self.assertEqual(self.submit(token).status_code, 403)
        token = self.review(False)
        Membership.objects.filter(user=self.actor).update(session_version=2)
        verified_login(self.client, self.actor)
        self.assertEqual(self.submit(token).status_code, 403)
        self.assertFalse(self.events().exists())

    def test_noop_stale_and_replayed_reviews_leave_the_latest_state(self):
        self.assertEqual(self.client.post(self.editor, {'available': '1', 'reason': REASON}).status_code, 400)
        older = self.review(False)
        token = self.review(False)
        self.assertEqual(self.submit(token).status_code, 302)
        self.assertEqual(self.submit(token).status_code, 409)
        self.assertEqual(self.submit(self.review(True)).status_code, 302)
        self.assertEqual(self.submit(older).status_code, 409)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.router_available)
        self.assertEqual(self.events().count(), 2)

    def test_audit_and_flag_change_are_atomic_and_require_simulation(self):
        token = self.review(False)
        with patch('core.router_simulation.audit', side_effect=RuntimeError), self.assertRaises(RuntimeError):
            simulation.confirm_scenario(self.actor, self.job.pk, token, time.time(), 1)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.router_available)
        self.assertFalse(self.events().exists())
        with override_settings(SIMULATION=False):
            self.assertNotContains(self.client.get(self.detail), 'Ubah skenario router simulasi')
            self.assertEqual(self.client.get(self.editor).status_code, 403)
            self.assertEqual(self.submit(token).status_code, 403)
            with self.assertRaises(PermissionDenied):
                simulation.confirm_scenario(self.actor, self.job.pk, token, time.time(), 1)
        self.assertEqual(self.submit(token).status_code, 302)

    def test_worker_can_confirm_a_replacement_after_reviewed_router_repair(self):
        self.assertEqual(self.submit(self.review(False)).status_code, 302)
        for _ in range(3):
            NetworkJob.objects.filter(pk=self.job.pk).update(run_after=timezone.now())
            self.assertEqual(run_job(self.job.pk).status, 'failed')
        original = NetworkJob.objects.values().get(pk=self.job.pk)
        self.assertEqual(original['attempts'], 3)
        self.assertEqual(self.submit(self.review(True)).status_code, 302)
        self.assertEqual(run_job(self.job.pk).status, 'failed')
        self.assertFalse(NetworkJobRetry.objects.exists())
        token = review_retry(self.actor, self.job.pk, REASON)
        replacement, _ = confirm_retry(self.actor, self.job.pk, token, time.time(), 1)
        self.assertEqual(run_job(replacement.pk).status, 'confirmed')
        self.assertEqual(NetworkJob.objects.values().get(pk=self.job.pk), original)
