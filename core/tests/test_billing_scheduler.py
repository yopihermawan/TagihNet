import time
import uuid
from datetime import date, datetime, timedelta
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.access import save_role
from core.billing import issue_invoice
from core.billing_scheduler import (confirm_change, manager, read_review, review_change,
    run_schedule, run_schedules)
from core.bulk_billing import create_preview, process_batch, process_next
from core.models import (AuditEvent, BillingBatch, BillingSchedule, BillingScheduleRun,
    Customer, Invoice, MFADevice, Membership, NetworkJob, Payment, Plan)
from .auth_helpers import verified_login
from .test_workflow import fixtures

WIB = ZoneInfo("Asia/Jakarta")
NOW = datetime(2026, 2, 15, 10, tzinfo=WIB)
VALUES = {"enabled":True, "day":1, "state":"active", "plan":None, "area":""}


@override_settings(SIMULATION=True)
class BillingScheduleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants, cls.owners, cls.customers = fixtures()

    def setUp(self):
        verified_login(self.client, self.owners[0])
        self.clock = patch("core.billing_scheduler.timezone.now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def approve(self, values=None, actor=None):
        actor = actor or self.owners[0]
        schedule = BillingSchedule.objects.filter(tenant=actor.membership.tenant).first()
        token, payload = review_change(actor, values if values is not None else VALUES.copy(), schedule.version if schedule else 0)
        return confirm_change(actor, token, time.time(), actor.membership.session_version)

    def add_customer(self, index=2, **changes):
        values = dict(tenant=self.tenants[0], plan=self.customers[0].plan, code=f"SCH-{index}",
            name=f"Jadwal {index}", username=f"schedule-{index}", phone=f"08129999{index:04d}", address="Alamat", area="Uji")
        return Customer.objects.create(**{**values, **changes})

    def test_default_get_and_review_do_not_persist_or_issue(self):
        self.assertContains(self.client.get(reverse("billing_schedule")), "Nonaktif")
        token, _ = review_change(self.owners[0], VALUES.copy(), 0)
        self.assertTrue(token)
        self.assertFalse(BillingSchedule.objects.exists())
        self.assertFalse(BillingBatch.objects.exists())
        self.assertFalse(Invoice.objects.exists())
        self.assertEqual(run_schedules(), 0)

    def test_signed_review_confirmation_and_replay(self):
        token, payload = review_change(self.owners[0], VALUES.copy(), 0)
        self.assertEqual(payload["total"], 249750)
        schedule = confirm_change(self.owners[0], token, time.time(), 1)
        replay = confirm_change(self.owners[0], token, time.time(), 1)
        self.assertEqual(schedule.pk, replay.pk)
        self.assertEqual(schedule.version, 1)
        self.assertEqual(AuditEvent.objects.filter(action="billing.schedule_configured").count(), 1)
        self.assertFalse(Invoice.objects.exists())

    def test_signed_review_tamper_expiry_and_actor_binding(self):
        token, _ = review_change(self.owners[0], VALUES.copy(), 0)
        with self.assertRaises(ValidationError):
            read_review(self.owners[0], token+"x")
        with patch("django.core.signing.time.time", return_value=time.time()+901), self.assertRaises(ValidationError):
            read_review(self.owners[0], token)
        with self.assertRaises(PermissionDenied):
            read_review(self.owners[1], token)

    def test_mfa_and_access_version_required(self):
        token, _ = review_change(self.owners[0], VALUES.copy(), 0)
        for age, version in ((301,1), (-10,1), (0,2)):
            with self.subTest(age=age), self.assertRaises(PermissionDenied):
                confirm_change(self.owners[0], token, time.time()-age, version)
        MFADevice.objects.filter(user=self.owners[0]).delete()
        with self.assertRaises(PermissionDenied):
            confirm_change(self.owners[0], token, time.time(), 1)

    def test_stale_price_and_configuration_reviews_rejected(self):
        token, _ = review_change(self.owners[0], VALUES.copy(), 0)
        Plan.objects.filter(pk=self.customers[0].plan_id).update(price=300000)
        with self.assertRaises(ValidationError):
            confirm_change(self.owners[0], token, time.time(), 1)
        a, _ = review_change(self.owners[0], VALUES.copy(), 0)
        b, _ = review_change(self.owners[0], {**VALUES,"day":2}, 0)
        confirm_change(self.owners[0], a, time.time(), 1)
        with self.assertRaises(ValidationError):
            confirm_change(self.owners[0], b, time.time(), 1)

    def test_review_month_rollover_rejected(self):
        token, _ = review_change(self.owners[0], VALUES.copy(), 0)
        with patch("core.billing_scheduler.timezone.now", return_value=NOW.replace(month=3)), self.assertRaises(ValidationError):
            confirm_change(self.owners[0], token, time.time(), 1)

    def test_values_validate_range_type_tenant_and_unknown_fields(self):
        for values in ({**VALUES,"day":0}, {**VALUES,"day":32}, {**VALUES,"day":True}, {**VALUES,"enabled":1},
                       {**VALUES,"state":"pending"}, {**VALUES,"state":[]}, {**VALUES,"area":"x"*81},
                       {**VALUES,"area":"x\ny"}, {**VALUES,"area":"x\x7fy"},
                       {**VALUES,"plan":self.customers[1].plan_id}, {**VALUES,"plan":True}, {**VALUES,"unexpected":1}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                review_change(self.owners[0], values, 0)

    def test_only_owner_or_complete_custom_capabilities(self):
        rights = ["manage_billing_schedule","bulk_issue","issue","invoices","customers"]
        role = save_role(self.owners[0], "Jadwal", rights)
        user = get_user_model().objects.create_user(username="scheduler-ops")
        Membership.objects.create(user=user, tenant=self.tenants[0], role="ops", access_role=role)
        verified_login(self.client, user)
        self.assertEqual(self.client.get(reverse("billing_schedule")).status_code, 200)
        role.capabilities = ["manage_billing_schedule"]
        role.save()
        with self.assertRaises(PermissionDenied):
            manager(user)
        self.assertEqual(self.client.get(reverse("billing_schedule")).status_code, 403)
        with self.assertRaises(ValidationError):
            save_role(self.owners[0], "Tidak lengkap", ["manage_billing_schedule"])

    def test_other_presets_and_anonymous_cannot_access_directly(self):
        self.assertEqual(Client().get(reverse("billing_schedule")).status_code, 302)
        for role in ("ops","finance","cashier","noc","technician","customer"):
            user=get_user_model().objects.create_user(username=f"schedule-{role}")
            Membership.objects.create(user=user, tenant=self.tenants[0], role=role,
                customer=self.customers[0] if role=="customer" else None)
            verified_login(self.client, user)
            self.assertEqual(self.client.get(reverse("billing_schedule")).status_code, 403)
            self.assertEqual(self.client.post(reverse("billing_schedule_confirm"), {}).status_code, 403)

    def test_post_requires_csrf_and_confirmation_checkbox(self):
        client=Client(enforce_csrf_checks=True)
        verified_login(client,self.owners[0])
        self.assertEqual(client.post(reverse("billing_schedule"),{**VALUES,"plan":""}).status_code,403)
        token,_=review_change(self.owners[0],VALUES.copy(),0)
        self.assertEqual(self.client.post(reverse("billing_schedule_confirm"),{"token":token}).status_code,400)
        self.assertFalse(BillingSchedule.objects.exists())
        self.assertEqual(self.client.get(reverse("billing_schedule_confirm")).status_code,405)

    def test_disabled_and_simulation_off_have_no_issuance(self):
        schedule=self.approve({**VALUES,"enabled":False})
        self.assertIsNone(run_schedule(schedule.pk,NOW))
        schedule=self.approve()
        with override_settings(SIMULATION=False):
            self.assertIsNone(run_schedule(schedule.pk,NOW))
            self.assertEqual(run_schedules(),0)
            with self.assertRaises(ValidationError):
                review_change(self.owners[0],VALUES.copy(),schedule.version)
        self.assertFalse(BillingBatch.objects.exists())

    def test_due_day_creates_one_snapshot_cycle_and_worker_issues(self):
        schedule=self.approve()
        run=run_schedule(schedule.pk,NOW)
        self.assertEqual(run.selected,1)
        self.assertEqual(run.total,249750)
        self.assertFalse(Invoice.objects.exists())
        self.assertEqual(run.batch.status,"queued")
        self.assertIsNone(run_schedule(schedule.pk,NOW+timedelta(days=1)))
        process_batch(run.batch_id)
        self.assertEqual(Invoice.objects.count(),1)
        self.assertEqual(Invoice.objects.get().total,249750)
        self.assertEqual(Payment.objects.count(),0)
        self.assertEqual(NetworkJob.objects.count(),0)

    def test_before_day_and_february_29_to_31_clamping(self):
        for day in (29,30,31):
            schedule=self.approve({**VALUES,"day":day})
            self.assertIsNone(run_schedule(schedule.pk,NOW.replace(day=27)))
        self.assertIsNotNone(run_schedule(schedule.pk,NOW.replace(day=28)))
        self.assertEqual(BillingScheduleRun.objects.get().period,date(2026,2,1))

    def test_leap_february_and_short_month(self):
        schedule=self.approve({**VALUES,"day":31})
        for current, earlier in ((datetime(2028,2,29,tzinfo=WIB),datetime(2028,2,28,tzinfo=WIB)),
                                 (datetime(2028,4,30,tzinfo=WIB),datetime(2028,4,29,tzinfo=WIB))):
            self.assertIsNone(run_schedule(schedule.pk,earlier))
            self.assertIsNotNone(run_schedule(schedule.pk,current))

    def test_wib_boundary_no_backfill_and_clock_replay(self):
        schedule=self.approve()
        utc=ZoneInfo("UTC")
        self.assertIsNotNone(run_schedule(schedule.pk,datetime(2026,2,28,17,0,tzinfo=utc)))
        self.assertEqual(list(schedule.runs.values_list("period",flat=True)),[date(2026,3,1)])
        # Returning to a month already run cannot create another batch.
        self.assertIsNone(run_schedule(schedule.pk,datetime(2026,3,2,tzinfo=WIB)))
        self.assertEqual(schedule.runs.count(),1)
        with self.assertRaises(ValueError):
            run_schedule(schedule.pk,datetime(2026,3,2))

    def test_retries_manual_overlap_and_old_invoice_unchanged(self):
        invoice,_=issue_invoice(self.tenants[0],self.customers[0].pk,date(2026,2,1),self.owners[0])
        before=Invoice.objects.values().get(pk=invoice.pk)
        schedule=self.approve()
        run=run_schedule(schedule.pk,NOW)
        self.assertEqual(run.total,0)
        self.assertEqual(run.batch.items.get().status,"skipped")
        process_batch(run.batch_id)
        self.assertEqual(Invoice.objects.values().get(pk=invoice.pk),before)
        self.assertEqual(Invoice.objects.count(),1)

    def test_dynamic_scope_and_prices_freeze_when_batch_is_created(self):
        schedule=self.approve()
        self.add_customer()
        Plan.objects.filter(pk=self.customers[0].plan_id).update(price=300000)
        run=run_schedule(schedule.pk,NOW)
        self.assertEqual(run.selected,2)
        Plan.objects.filter(pk=self.customers[0].plan_id).update(price=400000)
        self.add_customer(3)
        process_batch(run.batch_id)
        self.assertEqual(list(Invoice.objects.values_list("total",flat=True)),[333000,333000])
        self.assertEqual(BillingScheduleRun.objects.count(),1)

    def test_service_plan_area_filter_and_cross_tenant_exclusion(self):
        self.customers[0].area="Uji"
        self.customers[0].save()
        self.add_customer(2,service_state="pending")
        self.add_customer(3,service_state="isolated")
        self.add_customer(4,area="Lain")
        schedule=self.approve({**VALUES,"state":"isolated","plan":self.customers[0].plan_id,"area":"Uji"})
        run=run_schedule(schedule.pk,NOW)
        self.assertEqual(run.selected,1)
        self.assertEqual(run.batch.items.get().customer.code,"SCH-3")

    def test_empty_cycle_is_recorded_once_without_batch(self):
        schedule=self.approve({**VALUES,"area":"Kosong"})
        run=run_schedule(schedule.pk,NOW)
        self.assertIsNone(run.batch_id)
        self.assertEqual(run.selected,0)
        self.add_customer(area="Kosong")
        self.assertIsNone(run_schedule(schedule.pk,NOW))
        self.assertEqual(BillingScheduleRun.objects.count(),1)
        self.assertFalse(Invoice.objects.exists())

    def test_over_limit_and_bad_price_hold_without_partial_cycle(self):
        schedule=self.approve()
        with patch("core.bulk_billing.MAX_CUSTOMERS",0):
            self.assertIsNone(run_schedule(schedule.pk,NOW))
        schedule.refresh_from_db()
        self.assertTrue(schedule.reason)
        self.assertFalse(BillingScheduleRun.objects.exists())
        Plan.objects.filter(pk=self.customers[0].plan_id).update(price=0)
        self.assertIsNone(run_schedule(schedule.pk,NOW))
        self.assertEqual(AuditEvent.objects.filter(action="billing.schedule_blocked").count(),1)
        self.assertFalse(Invoice.objects.exists())

    def test_revocation_blocks_generation_and_next_queued_row(self):
        self.add_customer()
        schedule=self.approve()
        run=run_schedule(schedule.pk,NOW)
        self.assertTrue(process_next(run.batch_id))
        Membership.objects.filter(user=self.owners[0]).update(session_version=2)
        self.assertFalse(process_next(run.batch_id))
        run.batch.refresh_from_db()
        self.assertEqual(run.batch.status,"blocked")
        self.assertEqual(Invoice.objects.count(),1)
        self.assertIsNone(run_schedule(schedule.pk,NOW.replace(month=3)))
        self.assertEqual(BillingScheduleRun.objects.count(),1)

    def test_mfa_revision_change_or_inactive_actor_blocks(self):
        schedule=self.approve()
        MFADevice.objects.filter(user=self.owners[0]).update(revision=uuid.uuid4())
        self.assertIsNone(run_schedule(schedule.pk,NOW))
        schedule=self.approve()
        run=run_schedule(schedule.pk,NOW)
        self.owners[0].is_active=False
        self.owners[0].save()
        self.assertFalse(process_next(run.batch_id))
        self.assertFalse(Invoice.objects.exists())

    def test_disable_or_change_cancels_only_remaining_rows(self):
        self.add_customer()
        schedule=self.approve()
        run=run_schedule(schedule.pk,NOW)
        self.assertTrue(process_next(run.batch_id))
        self.approve({**VALUES,"enabled":False})
        run.batch.refresh_from_db()
        self.assertEqual(run.batch.status,"cancelled")
        self.assertFalse(process_next(run.batch_id))
        self.assertEqual(Invoice.objects.count(),1)
        self.approve()
        self.assertIsNone(run_schedule(schedule.pk,NOW))
        self.assertEqual(schedule.runs.count(),1)

    def test_simulation_disabled_blocks_already_queued_schedule_batch(self):
        schedule=self.approve()
        run=run_schedule(schedule.pk,NOW)
        with override_settings(SIMULATION=False):
            self.assertFalse(process_next(run.batch_id))
        self.assertFalse(Invoice.objects.exists())

    def test_failed_run_rolls_back_batch_items_and_cycle(self):
        schedule=self.approve()
        with patch("core.billing_scheduler.audit",side_effect=RuntimeError("audit unavailable")), self.assertRaises(RuntimeError):
            run_schedule(schedule.pk,NOW)
        self.assertFalse(BillingBatch.objects.exists())
        self.assertFalse(BillingScheduleRun.objects.exists())

    def test_worker_once_and_restart_do_not_duplicate(self):
        self.approve()
        for _ in range(2):
            call_command("run_billing_worker",once=True,stdout=StringIO())
        self.assertEqual(Invoice.objects.count(),1)
        self.assertEqual(BillingScheduleRun.objects.count(),1)
        self.assertEqual(BillingBatch.objects.get().status,"completed")

    def test_limit_and_database_constraints(self):
        for limit in (0,101,True):
            with self.assertRaises(ValueError):
                run_schedules(limit)
        schedule=self.approve()
        run=run_schedule(schedule.pk,NOW)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            BillingScheduleRun.objects.create(schedule=schedule,period=run.period,schedule_version=1)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            BillingSchedule.objects.filter(pk=schedule.pk).update(day=32)

    def test_manual_batch_keeps_existing_confirmation_contract(self):
        batch=create_preview(self.owners[0],date(2026,2,1),{"state":"active"})
        batch.status="queued"
        batch.save()
        process_batch(batch.pk)
        self.assertEqual(Invoice.objects.count(),1)
        self.assertFalse(BillingScheduleRun.objects.exists())

    def test_own_tenant_history_and_confirmation_tenant_are_scoped(self):
        schedule=self.approve()
        run=run_schedule(schedule.pk,NOW)
        self.assertContains(self.client.get(reverse("billing_schedule")),"Rp249.750")
        verified_login(self.client,self.owners[1])
        response=self.client.get(reverse("billing_schedule"))
        self.assertNotContains(response,str(run.batch_id))
        self.assertNotContains(response,"Rp249.750")

    def test_expired_mfa_view_requests_stepup_without_save(self):
        token,_=review_change(self.owners[0],VALUES.copy(),0)
        session=self.client.session
        session["mfa_verified_at"]=time.time()-301
        session.save()
        self.assertRedirects(self.client.post(reverse("billing_schedule_confirm"),{"token":token,"reviewed":"on"}),reverse("account_security"))
        self.assertFalse(BillingSchedule.objects.exists())

    def test_config_and_review_plaintext_no_script_execution(self):
        values={**VALUES,"area":"<script>contoh</script>"}
        response=self.client.post(reverse("billing_schedule"),{**values,"version":0,"plan":""})
        self.assertContains(response,"&lt;script&gt;contoh&lt;/script&gt;")
        self.assertNotContains(response,"<script>contoh</script>")
