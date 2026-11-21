from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import time
import uuid
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase, override_settings

from core.billing import issue_invoice, record_payment
from core.isolation_schedule import confirm_change, plan_schedule, plan_schedules, review_change
from core.models import Customer, IsolationSchedule, Membership, MFADevice, NetworkJob, ScheduledIsolation, TenantIsolationPolicy
from core.network import run_job

from .auth_helpers import verified_login
from .test_workflow import fixtures

WIB = ZoneInfo('Asia/Jakarta')
START = date(2024, 3, 3)
FIRST_ELIGIBLE = datetime(2024, 3, 4, 10, tzinfo=WIB)
URL = '/network/isolation-schedule/'


def enable(actor, version=0, starts_on=START):
    with patch('core.isolation_schedule.timezone.localdate', return_value=starts_on):
        token, _ = review_change(actor, True, version)
        confirm_change(actor, token, time.time(), 1)
    return IsolationSchedule.objects.get(tenant=actor.membership.tenant)


@override_settings(SIMULATION=True)
class IsolationSchedulerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()

    def setUp(self):
        self.tenant, self.actor, self.customer = self.tenants[0], self.owners[0], self.customers[0]
        verified_login(self.client, self.actor)

    def issue(self, month=2, grace=3, index=0):
        tenant = self.tenants[index]
        TenantIsolationPolicy.objects.update_or_create(tenant=tenant, defaults={'grace_days': grace})
        return issue_invoice(tenant, self.customers[index].pk, date(2024, month, 1), self.owners[index])[0]

    def settings(self, enable_flag='on', version=0):
        return self.client.post(URL, {'enabled': enable_flag, 'version': version})

    def test_enable_needs_preview_checkbox_fresh_mfa_and_csrf(self):
        self.assertContains(self.client.get(URL), 'Jadwal nonaktif')
        review = self.settings()
        self.assertContains(review, 'Tinjau jadwal isolir')
        self.assertFalse(IsolationSchedule.objects.exists())
        token = review.context['form'].initial['token']
        target = URL + 'confirm/'
        self.assertEqual(self.client.post(target, {'token': token}).status_code, 400)
        csrf_client = Client(enforce_csrf_checks=True)
        verified_login(csrf_client, self.actor)
        self.assertEqual(csrf_client.post(target, {'token': token, 'reviewed': 'on'}).status_code, 403)
        session = self.client.session
        session['mfa_verified_at'] = time.time() - 301
        session.save()
        self.assertRedirects(self.client.post(target, {'token': token, 'reviewed': 'on'}), '/account/security/', fetch_redirect_response=False)
        self.assertFalse(IsolationSchedule.objects.exists())
        session = self.client.session
        session['mfa_verified_at'] = time.time()
        session.save()
        confirmed = self.client.post(target, {'token': token, 'reviewed': 'on', 'enabled': ''})
        self.assertRedirects(confirmed, URL)
        self.assertTrue(IsolationSchedule.objects.get(tenant=self.tenant).enabled)
        self.assertEqual(self.client.post(target, {'token': token, 'reviewed': 'on'}).status_code, 409)

    def test_review_cannot_move_between_tenants_or_confirm_after_business_date_changes(self):
        with patch('core.isolation_schedule.timezone.localdate', return_value=START):
            token = self.settings().context['form'].initial['token']
        verified_login(self.client, self.owners[1])
        self.assertEqual(self.client.post(URL+'confirm/', {'token': token, 'reviewed': 'on'}).status_code, 403)
        verified_login(self.client, self.actor)
        with patch('core.isolation_schedule.timezone.localdate', return_value=START + timedelta(days=1)):
            self.assertEqual(self.client.post(URL+'confirm/', {'token': token, 'reviewed': 'on'}).status_code, 409)
        self.assertFalse(IsolationSchedule.objects.exists())

    def test_noc_can_see_queue_but_not_schedule_settings(self):
        Membership.objects.filter(user=self.actor).update(role='noc')
        self.assertNotContains(self.client.get('/network/'), 'Jadwal isolir simulasi')
        self.assertEqual(self.client.get(URL).status_code, 403)
        self.assertEqual(self.client.post(URL, {'enabled': 'on', 'version': 0}).status_code, 403)

    def test_worker_timezone_grace_and_initial_date_exclude_backlog_and_foreign_debt(self):
        older = self.issue(month=1, grace=0)
        invoice = self.issue()
        foreign = self.issue(index=1)
        self.assertEqual(plan_schedules(), 0)
        schedule = enable(self.actor)
        still_march_3 = datetime.fromisoformat('2024-03-03T16:59:00+00:00')
        march_4_midnight = datetime.fromisoformat('2024-03-03T17:00:00+00:00')
        self.assertEqual(plan_schedule(schedule.pk, still_march_3), 0)
        self.assertEqual(plan_schedule(schedule.pk, march_4_midnight), 1)
        dispatch = ScheduledIsolation.objects.get()
        self.assertEqual(dispatch.invoice, invoice)
        self.assertNotIn(dispatch.invoice_id, [older.pk, foreign.pk])
        self.assertEqual(dispatch.eligible_on, date(2024, 3, 4))
        self.assertEqual(self.customer.service_state, 'active')
        self.assertEqual(plan_schedule(schedule.pk, FIRST_ELIGIBLE), 0)
        with patch('core.network.timezone.localdate', return_value=date(2024, 3, 4)):
            self.assertEqual(run_job(dispatch.job_id).status, 'confirmed')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.service_state, 'isolated')

    def test_payment_after_planning_skips_even_if_legacy_invoice_is_unpaid(self):
        self.issue(month=1, grace=0)
        invoice = self.issue()
        schedule = enable(self.actor)
        self.assertEqual(plan_schedule(schedule.pk, FIRST_ELIGIBLE), 1)
        job = ScheduledIsolation.objects.get().job
        record_payment(self.tenant, invoice.pk, invoice.total, uuid.uuid4(), self.actor)
        self.assertEqual(run_job(job.pk).status, 'skipped')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.service_state, 'active')
        self.assertEqual(run_job(job.pk).attempts, 0)

    def test_disabled_or_mfa_revoked_schedule_cannot_isolate_already_queued_jobs(self):
        self.issue()
        schedule = enable(self.actor)
        self.assertEqual(plan_schedule(schedule.pk, FIRST_ELIGIBLE), 1)
        first = ScheduledIsolation.objects.get().job
        token, _ = review_change(self.actor, False, schedule.version)
        self.assertTrue(confirm_change(self.actor, token, time.time(), 1))
        self.assertEqual(run_job(first.pk).status, 'skipped')
        schedule.refresh_from_db()
        schedule = enable(self.actor, schedule.version)
        self.assertEqual(plan_schedule(schedule.pk, FIRST_ELIGIBLE), 1)
        queued = ScheduledIsolation.objects.latest('pk').job
        MFADevice.objects.filter(user=self.actor).update(revision=uuid.uuid4())
        self.assertEqual(run_job(queued.pk).status, 'skipped')
        self.assertEqual(plan_schedule(schedule.pk, FIRST_ELIGIBLE + timedelta(minutes=1)), 0)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.service_state, 'active')

    def test_pending_provisioning_and_exhausted_service_do_not_create_new_jobs(self):
        invoice = self.issue()
        schedule = enable(self.actor)
        prior = NetworkJob.objects.create(tenant=self.tenant, customer=self.customer)
        self.assertEqual(plan_schedule(schedule.pk, FIRST_ELIGIBLE), 0)
        prior.status = 'confirmed'
        prior.save(update_fields=['status'])
        Customer.objects.filter(pk=self.customer.pk).update(service_state='pending')
        self.assertEqual(plan_schedule(schedule.pk, FIRST_ELIGIBLE + timedelta(minutes=1)), 0)
        Customer.objects.filter(pk=self.customer.pk).update(service_state='active', router_available=False)
        self.assertEqual(plan_schedule(schedule.pk, FIRST_ELIGIBLE + timedelta(minutes=2)), 1)
        dispatch = ScheduledIsolation.objects.get(invoice=invoice)
        for _ in range(3):
            NetworkJob.objects.filter(pk=dispatch.job_id).update(run_after=FIRST_ELIGIBLE)
            self.assertEqual(run_job(dispatch.job_id).status, 'failed')
        later = self.issue(month=3, grace=0)
        self.assertEqual(plan_schedule(schedule.pk, FIRST_ELIGIBLE + timedelta(days=40)), 0)
        self.assertFalse(ScheduledIsolation.objects.filter(invoice=later).exists())

    def test_already_paid_invoice_is_excluded_by_planner(self):
        invoice = self.issue()
        record_payment(self.tenant, invoice.pk, invoice.total, uuid.uuid4(), self.actor)
        # Simulate an already completed payment job so the balance is what excludes the invoice.
        NetworkJob.objects.filter(customer=self.customer).update(status='confirmed')
        schedule = enable(self.actor)
        self.assertEqual(plan_schedule(schedule.pk, FIRST_ELIGIBLE), 0)
        self.assertFalse(ScheduledIsolation.objects.exists())

    @override_settings(SIMULATION=False)
    def test_simulation_off_prevents_planning_and_skips_jobs(self):
        self.issue()
        with override_settings(SIMULATION=True):
            schedule = enable(self.actor)
            self.assertEqual(plan_schedule(schedule.pk, FIRST_ELIGIBLE), 1)
        self.assertEqual(plan_schedule(schedule.pk, FIRST_ELIGIBLE + timedelta(minutes=1)), 0)
        self.assertEqual(run_job(ScheduledIsolation.objects.get().job_id).status, 'skipped')


@override_settings(SIMULATION=True)
class IsolationPlannerConcurrencyTests(TransactionTestCase):
    def test_two_planners_queue_one_job_and_one_trigger(self):
        tenants, owners, customers = fixtures()
        verified_login(self.client, owners[0])
        TenantIsolationPolicy.objects.create(tenant=tenants[0], grace_days=3)
        issue_invoice(tenants[0], customers[0].pk, date(2024, 2, 1), owners[0])
        schedule = enable(owners[0])

        def plan(_):
            close_old_connections()
            try:
                return plan_schedule(schedule.pk, FIRST_ELIGIBLE)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as workers:
            self.assertEqual(sorted(workers.map(plan, range(2))), [0, 1])
        self.assertEqual(NetworkJob.objects.count(), 1)
        self.assertEqual(ScheduledIsolation.objects.count(), 1)
