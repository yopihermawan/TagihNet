from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import time
import uuid
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase, override_settings

from core.billing import issue_invoice, record_payment
from core.isolation_schedule import plan_schedule
from core.models import AuditEvent, Customer, IsolationSchedule, Membership, MFADevice, NetworkJob, NetworkJobRetry, ScheduledIsolation, TenantIsolationPolicy
from core.network import run_job
from core.network_recovery import attention_jobs, confirm_retry, review_retry

from .auth_helpers import verified_login
from .test_isolation_scheduler import FIRST_ELIGIBLE, enable
from .test_workflow import fixtures

REASON = 'Koneksi perangkat simulasi sudah diperiksa.'


def path(job):
    return f'/network/jobs/{job.pk}/'


@override_settings(SIMULATION=True)
class NetworkRecoveryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()

    def setUp(self):
        self.tenant, self.actor, self.customer = self.tenants[0], self.owners[0], self.customers[0]
        verified_login(self.client, self.actor)

    def failed_job(self, index=0, attempts=3):
        return NetworkJob.objects.create(tenant=self.tenants[index], customer=self.customers[index],
            status='failed', attempts=attempts, result='Router simulasi tidak dapat dijangkau.')

    def retry(self, job, token=None):
        token = token or review_retry(self.actor, job.pk, REASON)
        return confirm_retry(self.actor, job.pk, token, time.time(), 1)

    def scheduled_failure(self):
        TenantIsolationPolicy.objects.create(tenant=self.tenant, grace_days=3)
        invoice, _ = issue_invoice(self.tenant, self.customer.pk, date(2024, 2, 1), self.actor)
        schedule = enable(self.actor)
        self.assertEqual(plan_schedule(schedule.pk, FIRST_ELIGIBLE), 1)
        trigger = ScheduledIsolation.objects.get(invoice=invoice)
        job = trigger.job
        job.status, job.attempts = 'failed', 3
        job.save()
        return invoice, trigger, job

    def test_filters_and_detail_are_tenant_scoped_and_read_only_for_noc(self):
        failed = self.failed_job()
        retryable = self.failed_job(attempts=2)
        foreign = self.failed_job(index=1)
        Membership.objects.filter(user=self.actor).update(role='noc')
        listing = self.client.get('/network/', {'status': 'exhausted', 'q': self.customer.code})
        self.assertContains(listing, f'href="{path(failed)}"')
        for job in (retryable, foreign):
            self.assertNotContains(listing, f'href="{path(job)}"')
        self.assertContains(self.client.get('/network/', {'q': f'#{retryable.pk}'}), f'href="{path(retryable)}"')
        self.assertContains(self.client.get(path(failed)), 'Router simulasi tidak dapat dijangkau.')
        self.assertEqual(self.client.get(path(foreign)).status_code, 404)
        self.assertEqual(self.client.post(path(failed)+'retry/', {'reason': REASON}).status_code, 403)
        self.assertFalse(NetworkJobRetry.objects.exists())

    def test_owner_needs_review_checkbox_csrf_and_recent_factor(self):
        job = self.failed_job()
        response = self.client.post(path(job)+'retry/', {'reason': REASON})
        self.assertContains(response, 'Tinjau pemulihan job')
        token = response.context['form'].initial['token']
        confirm = path(job)+'retry/confirm/'
        self.assertEqual(self.client.get(confirm).status_code, 405)
        self.assertEqual(self.client.post(confirm, {'token': token}).status_code, 400)
        csrf = Client(enforce_csrf_checks=True)
        verified_login(csrf, self.actor)
        self.assertEqual(csrf.post(confirm, {'token': token, 'reviewed': 'on'}).status_code, 403)
        session = self.client.session
        session['mfa_verified_at'] = time.time()-301
        session.save()
        self.assertRedirects(self.client.post(confirm, {'token': token, 'reviewed': 'on'}), '/account/security/',
                             fetch_redirect_response=False)
        self.assertEqual(NetworkJob.objects.count(), 1)

    def test_replay_returns_one_replacement_and_keeps_the_original_attempts(self):
        job = self.failed_job()
        before = NetworkJob.objects.values().get(pk=job.pk)
        token = review_retry(self.actor, job.pk, REASON)
        url = path(job)+'retry/confirm/'
        first = self.client.post(url, {'token': token, 'reviewed': 'on', 'reason': 'Nilai asing yang harus diabaikan'})
        second = self.client.post(url, {'token': token, 'reviewed': 'on'})
        self.assertEqual(first.status_code, 302)
        self.assertEqual(first.url, second.url)
        request = NetworkJobRetry.objects.get(failed_job=job)
        self.assertEqual(request.reason, REASON)
        self.assertEqual(NetworkJob.objects.values().get(pk=job.pk), before)
        self.assertEqual(request.job.status, 'pending')
        self.assertEqual(request.job.attempts, 0)
        self.assertEqual(AuditEvent.objects.filter(action='network.retry_queued').count(), 1)
        self.assertContains(self.client.get(path(job)), f'href="{path(request.job)}"')
        self.assertContains(self.client.get(path(request.job)), f'href="{path(job)}"')
        self.assertEqual(list(attention_jobs(self.tenant.pk)), [request.job])
        self.assertEqual(run_job(request.job_id).status, 'confirmed')
        self.assertFalse(attention_jobs(self.tenant.pk).exists())

    def test_newer_job_or_changed_failure_invalidates_review(self):
        failure = self.failed_job()
        token = review_retry(self.actor, failure.pk, REASON)
        failure.result = 'Skenario perangkat simulasi berubah.'
        failure.save()
        with self.assertRaises(ValidationError):
            self.retry(failure, token)
        token = review_retry(self.actor, failure.pk, REASON)
        self.failed_job(attempts=2)
        with self.assertRaises(ValidationError):
            self.retry(failure, token)
        self.assertFalse(NetworkJobRetry.objects.exists())

    def test_signed_review_cannot_be_confirmed_for_another_job(self):
        original = self.failed_job()
        token = review_retry(self.actor, original.pk, REASON)
        foreign = self.failed_job(index=1)
        self.assertEqual(self.client.post(path(foreign)+'retry/confirm/', {'token': token, 'reviewed': 'on'}).status_code, 404)
        newer = self.failed_job()
        self.assertEqual(self.client.post(path(newer)+'retry/confirm/', {'token': token, 'reviewed': 'on'}).status_code, 403)
        self.assertFalse(NetworkJobRetry.objects.exists())

    def test_retry_and_link_roll_back_if_audit_fails(self):
        job = self.failed_job()
        token = review_retry(self.actor, job.pk, REASON)
        with patch('core.network_recovery.audit', side_effect=RuntimeError), self.assertRaises(RuntimeError):
            self.retry(job, token)
        self.assertEqual(NetworkJob.objects.count(), 1)
        self.assertFalse(NetworkJobRetry.objects.exists())
        self.assertTrue(self.retry(job, token)[1])

    def test_worker_skips_retry_after_operator_mfa_changes(self):
        job, _ = self.retry(self.failed_job())
        MFADevice.objects.filter(user=self.actor).update(revision=uuid.uuid4())
        self.assertEqual(run_job(job.pk).status, 'skipped')
        job.refresh_from_db()
        self.assertEqual(job.attempts, 0)

    def test_paid_trigger_does_not_reconcile_other_debt_on_scheduled_retry(self):
        issue_invoice(self.tenant, self.customer.pk, date(2024, 1, 1), self.actor)
        invoice, trigger, original = self.scheduled_failure()
        replacement, _ = self.retry(original)
        self.assertEqual(replacement.retry_request.trigger, trigger)
        record_payment(self.tenant, invoice.pk, invoice.total, uuid.uuid4(), self.actor)
        self.assertEqual(run_job(replacement.pk).status, 'skipped')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.service_state, 'active')

    def test_scheduled_retry_is_held_when_schedule_approval_is_revoked(self):
        _, trigger, original = self.scheduled_failure()
        replacement, _ = self.retry(original)
        IsolationSchedule.objects.filter(pk=trigger.schedule_id).update(enabled=False)
        self.assertEqual(run_job(replacement.pk).status, 'skipped')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.service_state, 'active')

    def test_replacement_failure_preserves_schedule_hold_and_can_be_reviewed_again(self):
        _, trigger, original = self.scheduled_failure()
        replacement, _ = self.retry(original)
        Customer.objects.filter(pk=self.customer.pk).update(router_available=False)
        for _ in range(3):
            NetworkJob.objects.filter(pk=replacement.pk).update(run_after=FIRST_ELIGIBLE)
            self.assertEqual(run_job(replacement.pk).status, 'failed')
        newer_invoice, _ = issue_invoice(self.tenant, self.customer.pk, date(2024, 3, 1), self.actor)
        self.assertEqual(plan_schedule(trigger.schedule_id, FIRST_ELIGIBLE + timedelta(days=45)), 0)
        self.assertFalse(ScheduledIsolation.objects.filter(invoice=newer_invoice).exists())
        Customer.objects.filter(pk=self.customer.pk).update(router_available=True)
        next_job, _ = self.retry(replacement)
        self.assertEqual(next_job.retry_request.trigger_id, trigger.pk)
        self.assertEqual(run_job(next_job.pk).status, 'confirmed')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.service_state, 'isolated')


@override_settings(SIMULATION=True)
class NetworkRecoveryConcurrencyTests(TransactionTestCase):
    def test_concurrent_confirmation_replays_into_one_linked_job(self):
        tenants, owners, customers = fixtures()
        actor = owners[0]
        verified_login(self.client, actor)
        job = NetworkJob.objects.create(tenant=tenants[0], customer=customers[0], status='failed', attempts=3)
        token = review_retry(actor, job.pk, REASON)

        def confirm(_):
            close_old_connections()
            try:
                replacement, created = confirm_retry(actor, job.pk, token, time.time(), 1)
                return replacement.pk, created
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as workers:
            first, second = workers.map(confirm, range(2))
        self.assertEqual(first[0], second[0])
        self.assertEqual(sorted([first[1], second[1]]), [False, True])
        self.assertEqual(NetworkJobRetry.objects.count(), 1)
        self.assertEqual(NetworkJob.objects.count(), 2)
