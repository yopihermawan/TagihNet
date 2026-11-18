import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase

from core.billing import issue_invoice
from core.bulk_billing import confirm_batch, create_preview, process_batch
from core.customer_import import confirm_import, preview_import
from core.forms import CustomerForm
from core.models import AuditEvent, Customer, Installation, Invoice, MFADevice, Membership, NetworkJob, Plan, Tenant
from core.operation_forms import InstallationCreationForm, InstallationEditForm
from core.operations import activate_installation, activation_quote, create_installation, edit_installation
from core.plan_availability import SALT, confirm_availability, read_availability, review_availability
from core.plan_selection import assignment_plan
from core.plans import confirm_change, review_change, values_for
from .auth_helpers import verified_login
from .test_workflow import fixtures


class PlanAvailabilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):cls.tenants, cls.owners, cls.customers = fixtures()

    def setUp(self):
        self.actor = self.owners[0]; self.plan = self.customers[0].plan
        verified_login(self.client, self.actor)

    def review(self, active=False):
        self.plan.refresh_from_db()
        return review_availability(self.actor, self.plan.pk, self.plan.version, active)[0]

    def confirm(self, token=None, **values):
        return confirm_availability(self.actor, token or self.review(),
            values.get('verified_at',time.time()), values.get('access_version',Membership.objects.get(user=self.actor).session_version))

    def customer_values(self, **changes):
        c=self.customers[0]
        return {'name':c.name,'email':c.email,'phone':c.phone,'username':c.username,'plan':c.plan_id,
                'nas':'','area':c.area,'address':c.address,**changes}

    def installation_values(self, **changes):
        return {'name':'Prospek fiktif','email':'','phone':'081299988810','username':'psb-archive',
                'address':'Alamat fiktif','area':'Arsip','plan_id':self.plan.pk,'assigned_id':None,**changes}

    def test_get_review_has_no_catalog_or_business_writes(self):
        response=self.client.get(f'/plans/{self.plan.pk}/archive/')
        self.assertContains(response,'Tinjau pengarsipan paket')
        self.assertEqual(response['Cache-Control'],'no-store')
        self.plan.refresh_from_db(); self.assertTrue(self.plan.is_active); self.assertEqual(self.plan.version,1)
        self.assertFalse(AuditEvent.objects.filter(action='plan.archived').exists())
        self.assertFalse(Invoice.objects.exists()); self.assertFalse(NetworkJob.objects.exists())

    def test_archive_changes_only_availability_and_version(self):
        before=Plan.objects.values().get(pk=self.plan.pk)
        member=Membership.objects.values().get(user=self.actor)
        device=MFADevice.objects.values().get(user=self.actor)
        self.confirm()
        self.assertEqual(Plan.objects.values().get(pk=self.plan.pk),{**before,'is_active':False,'version':2})
        self.assertEqual(self.customers[0].plan_id,self.plan.pk)
        self.assertTrue(Membership.objects.values().get(user=self.actor)==member)
        self.assertTrue(MFADevice.objects.values().get(user=self.actor)==device)
        self.assertEqual(AuditEvent.objects.filter(action='plan.archived').count(),1)
        self.assertFalse(NetworkJob.objects.exists())

    def test_reactivation_keeps_id_name_price_and_rejects_old_archive_replay(self):
        token=self.review(); self.confirm(token)
        saved=self.confirm(self.review(True))
        self.assertTrue(saved.is_active); self.assertEqual(saved.pk,self.plan.pk); self.assertEqual(saved.version,3)
        self.assertEqual(saved.price,225000)
        with self.assertRaises(ValidationError):self.confirm(token)
        self.assertEqual(AuditEvent.objects.filter(action='plan.reactivated').count(),1)

    def test_same_review_replay_or_competing_review_cannot_write_twice(self):
        first,second=self.review(),self.review()
        self.confirm(first)
        for token in (first,second):
            with self.assertRaises(ValidationError):self.confirm(token)
        self.assertEqual(AuditEvent.objects.filter(action='plan.archived').count(),1)

    def test_noop_does_not_increment_version_or_audit(self):
        saved=self.confirm(self.review(True))
        self.assertEqual(saved.version,1)
        self.assertFalse(AuditEvent.objects.filter(action__in=['plan.archived','plan.reactivated']).exists())

    def test_price_change_invalidates_archive_review(self):
        token=self.review()
        pricing,_=review_change(self.actor,{**values_for(self.plan),'price':300000},1,self.plan.pk)
        confirm_change(self.actor,pricing,time.time(),1)
        with self.assertRaises(ValidationError):self.confirm(token)
        self.plan.refresh_from_db();self.assertTrue(self.plan.is_active);self.assertEqual(self.plan.price,300000)

    def test_archival_invalidates_old_price_review(self):
        pricing,_=review_change(self.actor,{**values_for(self.plan),'price':300000},1,self.plan.pk)
        self.confirm()
        with self.assertRaises(ValidationError):confirm_change(self.actor,pricing,time.time(),1)

    def test_direct_state_change_without_version_invalidates_review(self):
        token=self.review();Plan.objects.filter(pk=self.plan.pk).update(name='Changed outside UI')
        with self.assertRaises(ValidationError):self.confirm(token)

    def test_wrong_tenant_and_actor_are_rejected(self):
        with self.assertRaises(Plan.DoesNotExist):review_availability(self.actor,self.customers[1].plan_id,1,False)
        token=self.review()
        with self.assertRaises(PermissionDenied):read_availability(self.owners[1],token)
        self.assertEqual(self.client.get(f'/plans/{self.customers[1].plan_id}/archive/').status_code,404)

    def test_readonly_role_can_read_archives_but_cannot_manage(self):
        token=self.review();Membership.objects.filter(user=self.actor).update(role='ops')
        self.assertEqual(self.client.get('/plans/?availability=archived').status_code,200)
        self.assertEqual(self.client.get(f'/plans/{self.plan.pk}/archive/').status_code,403)
        self.assertEqual(self.client.post('/plans/availability/confirm/',{'token':token,'reviewed':'on'}).status_code,403)
        with self.assertRaises(PermissionDenied):self.confirm(token)

    def test_missing_mfa_device_or_changed_revision_is_refused(self):
        token=self.review();MFADevice.objects.filter(user=self.actor).update(revision=uuid.uuid4())
        with self.assertRaises(PermissionDenied):self.confirm(token)
        MFADevice.objects.filter(user=self.actor).delete()
        with self.assertRaises(PermissionDenied):self.review()

    def test_changed_access_or_disabled_actor_cannot_confirm(self):
        token=self.review();Membership.objects.filter(user=self.actor).update(session_version=2)
        with self.assertRaises(PermissionDenied):self.confirm(token)
        Membership.objects.filter(user=self.actor).update(session_version=1)
        get_user_model().objects.filter(pk=self.actor.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):self.confirm(token)

    def test_recent_mfa_and_current_access_version_required(self):
        token=self.review()
        for timestamp in (time.time()-301,time.time()+20,None,True,float('nan'),float('inf')):
            with self.subTest(timestamp_type=type(timestamp).__name__),self.assertRaises(PermissionDenied):
                self.confirm(token,verified_at=timestamp)
        for version in (True,999):
            with self.assertRaises(PermissionDenied):self.confirm(token,access_version=version)

    def test_http_stepup_preserves_archive_action(self):
        token=self.review();session=self.client.session;session['mfa_verified_at']=time.time()-301;session.save()
        response=self.client.post('/plans/availability/confirm/',{'token':token,'reviewed':'on'})
        self.assertRedirects(response,'/account/security/')
        self.assertEqual(self.client.session['stepup_return'],f'/plans/{self.plan.pk}/archive/')
        self.plan.refresh_from_db();self.assertTrue(self.plan.is_active)

    def test_csrf_checkbox_and_signed_target_required(self):
        token=self.review();client=Client(enforce_csrf_checks=True);verified_login(client,self.actor)
        self.assertEqual(client.post('/plans/availability/confirm/',{'token':token,'reviewed':'on'}).status_code,403)
        self.assertEqual(self.client.post('/plans/availability/confirm/',{'token':token}).status_code,400)
        response=self.client.post('/plans/availability/confirm/',{'token':token,'reviewed':'on','is_active':'true','plan':self.customers[1].plan_id})
        self.assertRedirects(response,'/plans/?availability=archived')
        self.plan.refresh_from_db();self.assertFalse(self.plan.is_active)
        self.assertTrue(Plan.objects.get(pk=self.customers[1].plan_id).is_active)

    def test_expired_tampered_and_unknown_token_formats_refused(self):
        token=self.review()
        with patch('django.core.signing.time.time',return_value=time.time()+901):
            with self.assertRaises(ValidationError):self.confirm(token)
        for invalid in ('invalid',token+'x','x'*4097,signing.dumps([],salt=SALT)):
            with self.assertRaises(ValidationError):self.confirm(invalid)
        with self.assertRaises(ValidationError):review_availability(self.actor,self.plan.pk,True,False)
        with self.assertRaises(ValidationError):review_availability(self.actor,self.plan.pk,1,'false')

    def test_audit_failure_rolls_back_flag_and_version(self):
        token=self.review()
        with patch('core.plan_availability.audit',side_effect=RuntimeError),self.assertRaises(RuntimeError):self.confirm(token)
        self.plan.refresh_from_db();self.assertTrue(self.plan.is_active);self.assertEqual(self.plan.version,1)

    def test_active_archived_all_search_filters_and_tenant_scope(self):
        self.confirm()
        self.assertNotContains(self.client.get('/plans/'),'Rumah')
        self.assertContains(self.client.get('/plans/?availability=archived'),'Rumah')
        self.assertEqual(len(self.client.get('/plans/?availability=all').context['page']),1)
        self.assertEqual(len(self.client.get('/plans/?availability=archived&q=missing').context['page']),0)
        self.assertEqual(self.client.get('/plans/?availability=invalid').context['availability'],'active')

    def test_archived_plan_blocked_for_new_customer_but_retained_by_existing(self):
        self.confirm()
        values=self.customer_values(phone='081299999103',username='new-retail')
        self.assertFalse(CustomerForm(values,tenant=self.tenants[0]).is_valid())
        self.assertTrue(CustomerForm(self.customer_values(),tenant=self.tenants[0],instance=self.customers[0]).is_valid())
        response=self.client.post(f'/customers/{self.customers[0].pk}/edit/',self.customer_values(name='Retained legacy'))
        self.assertEqual(response.status_code,302)
        self.customers[0].refresh_from_db();self.assertEqual(self.customers[0].plan_id,self.plan.pk)

    def test_customer_plan_archive_after_validation_is_rechecked_before_save(self):
        original=CustomerForm.is_valid
        def validate_then_archive(form):
            valid=original(form)
            if valid:self.confirm()
            return valid
        with patch.object(CustomerForm,'is_valid',validate_then_archive):
            response=self.client.post('/customers/new/',self.customer_values(phone='081299999103',username='new-retail'))
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Paket sudah diarsipkan')
        self.assertFalse(Customer.objects.filter(username='new-retail').exists())

    def test_customer_form_does_not_overwrite_concurrent_service_state(self):
        original=CustomerForm.is_valid
        def validate_then_isolate(form):
            valid=original(form)
            Customer.objects.filter(pk=self.customers[0].pk).update(service_state='isolated')
            return valid
        with patch.object(CustomerForm,'is_valid',validate_then_isolate):
            self.client.post(f'/customers/{self.customers[0].pk}/edit/',self.customer_values(name='Identity edit'))
        self.customers[0].refresh_from_db();self.assertEqual(self.customers[0].service_state,'isolated')

    def test_import_preview_before_archive_becomes_stale_without_partial_create(self):
        csv=('nama,email,telepon,username,paket_id,wilayah,alamat\nNew retail,,081299999103,new-retail,'+str(self.plan.pk)+',Area,Address\n').encode()
        batch=preview_import(self.actor,csv,uuid.uuid4());self.confirm()
        saved=confirm_import(self.actor,batch.pk,time.time(),1)
        self.assertEqual(saved.status,'stale');self.assertFalse(Customer.objects.filter(username='new-retail').exists())
        second=preview_import(self.actor,csv,uuid.uuid4())
        self.assertEqual(second.rows.get().status,'invalid')

    def test_new_installation_refused_existing_order_and_replay_retained(self):
        values=self.installation_values();key=uuid.uuid4()
        order=create_installation(self.actor,values,key)
        self.confirm()
        self.assertEqual(create_installation(self.actor,values,key).pk,order.pk)
        with self.assertRaises(ValidationError):create_installation(self.actor,{**values,'username':'different'},uuid.uuid4())
        edited={field:getattr(order,field) for field in ('name','email','phone','username','address','area','plan_id')}
        edited['name']='Corrected existing prospect'
        updated=edit_installation(self.actor,order.pk,edited,'Correct identity on existing order',order.version,uuid.uuid4())
        self.assertEqual(updated.plan_id,self.plan.pk)
        self.assertFalse(InstallationCreationForm(tenant=self.tenants[0]).fields['plan'].queryset.exists())
        self.assertTrue(InstallationEditForm(tenant=self.tenants[0],current_plan_id=self.plan.pk).fields['plan'].queryset.filter(pk=self.plan.pk).exists())

    def test_old_and_future_invoices_and_approved_batch_keep_billing(self):
        invoice,_=issue_invoice(self.tenants[0],self.customers[0].pk,date(2026,2,1),self.actor)
        old=Invoice.objects.values().get(pk=invoice.pk)
        batch=create_preview(self.actor,date(2026,3,1),{'state':'active','plan':None,'area':''})
        confirm_batch(self.actor,batch.pk,time.time(),1)
        self.confirm()
        self.assertEqual(Invoice.objects.values().get(pk=invoice.pk),old)
        self.assertEqual(process_batch(batch.pk),1)
        self.assertEqual(Invoice.objects.get(period=date(2026,3,1)).total,249750)
        newer,created=issue_invoice(self.tenants[0],self.customers[0].pk,date(2026,4,1),self.actor)
        self.assertTrue(created);self.assertEqual(newer.total,249750)

    def test_new_schedule_run_still_bills_existing_archived_plan(self):
        from zoneinfo import ZoneInfo
        from core.billing_scheduler import review_change, confirm_change, run_schedule
        self.confirm()
        token,_=review_change(self.actor,{'enabled':True,'day':1,'state':'active','plan':self.plan.pk,'area':''},0)
        schedule=confirm_change(self.actor,token,time.time(),1)
        run=run_schedule(schedule.pk,datetime(2026,5,1,10,tzinfo=ZoneInfo('Asia/Jakarta')))
        self.assertEqual(process_batch(run.batch_id),1)
        self.assertEqual(Invoice.objects.get(period=date(2026,5,1)).total,249750)

    def test_existing_installed_order_can_activate_after_archive(self):
        order=create_installation(self.actor,self.installation_values(),uuid.uuid4())
        Installation.objects.filter(pk=order.pk).update(status='installed')
        order.refresh_from_db();self.confirm()
        _,fingerprint=activation_quote(order,date(2026,5,1))
        activated=activate_installation(self.actor,order.pk,date(2026,5,1),fingerprint,time.time(),1)
        self.assertEqual(activated.customer.plan_id,self.plan.pk)
        self.assertEqual(activated.invoice.total,249750)

    def test_reactivation_restores_new_customer_and_psb_choices(self):
        self.confirm();self.confirm(self.review(True))
        self.assertTrue(CustomerForm(self.customer_values(username='reactivated',phone='081299999103'),tenant=self.tenants[0]).is_valid())
        self.assertTrue(InstallationCreationForm(tenant=self.tenants[0]).fields['plan'].queryset.filter(pk=self.plan.pk).exists())


class PlanAvailabilityConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.tenants,self.owners,self.customers=fixtures();self.actor=self.owners[0];self.plan=self.customers[0].plan
        verified_login(self.client,self.actor)
        self.token=review_availability(self.actor,self.plan.pk,1,False)[0]

    def parallel(self,callback):
        def run(index):
            close_old_connections()
            try:return callback(index)
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(run,range(2)))

    def test_two_archive_confirmations_have_one_winner(self):
        def action(index):
            try:confirm_availability(self.actor,self.token,time.time(),1)
            except ValidationError:return False
            return True
        self.assertEqual(sorted(self.parallel(action)),[False,True])
        self.assertEqual(AuditEvent.objects.filter(action='plan.archived').count(),1)

    def test_new_assignment_and_archive_serialize(self):
        def action(index):
            if index:
                confirm_availability(self.actor,self.token,time.time(),1);return True
            try:
                with transaction.atomic():
                    Tenant.objects.select_for_update(no_key=True).get(pk=self.tenants[0].pk)
                    plan=assignment_plan(self.tenants[0],self.plan.pk,lock=True)
                    Customer.objects.create(tenant=self.tenants[0],plan=plan,code='RACE',name='Race fixture',username='race-retail',phone='6281299999901',address='Fixture',area='Fixture')
            except ValidationError:return False
            except OperationalError:
                # SQLite can refuse an overlapping write; the HTTP form also refuses safely.
                if connection.vendor!='sqlite':raise
                return False
            return True
        result=self.parallel(action)
        self.assertTrue(result[1]);self.plan.refresh_from_db();self.assertFalse(self.plan.is_active)
        self.assertEqual(Customer.objects.filter(username='race-retail').count(),int(result[0]))


class PlanAvailabilityMigrationTests(TransactionTestCase):
    def test_forward_migration_preserves_legacy_rows_foreign_keys_and_plan_sequence(self):
        old_target=[('core','0011_billing_schedule')];new_target=[('core','0012_plan_availability')]
        executor=MigrationExecutor(connection);executor.migrate(old_target)
        try:
            apps=executor.loader.project_state(old_target).apps
            T=apps.get_model('core','Tenant');P=apps.get_model('core','Plan');C=apps.get_model('core','Customer');I=apps.get_model('core','Invoice')
            tenant=T.objects.create(name='Legacy migration fixture')
            plan=P.objects.create(tenant=tenant,name='Legacy',speed_mbps=20,price=150000,version=7,creation_key=uuid.uuid4())
            deleted=P.objects.create(pk=7777,tenant=tenant,name='Deleted unused ID fixture',speed_mbps=1,price=1);deleted.delete()
            customer=C.objects.create(tenant=tenant,plan=plan,code='OLD',name='Original customer',phone='6281299999902',username='legacy-row',address='Original',area='Original')
            invoice=I.objects.create(tenant=tenant,customer=customer,period=date(2026,2,1),due_date=date(2026,2,20),customer_name='Snapshot',customer_address='Snapshot',subtotal=150000,total=150000,footer='Original')
            before={model._meta.db_table:list(model.objects.values()) for model in (T,P,C,I)}
            with connection.cursor() as cursor:
                if connection.vendor=='sqlite':cursor.execute("SELECT seq FROM sqlite_sequence WHERE name='core_plan'")
                else:
                    cursor.execute("SELECT setval(pg_get_serial_sequence('core_plan','id'),7777,true)")
                    cursor.execute('SELECT last_value FROM core_plan_id_seq')
                sequence=cursor.fetchone()[0]
            executor=MigrationExecutor(connection);executor.migrate(new_target)
            apps=executor.loader.project_state(new_target).apps
            for name in ('Tenant','Plan','Customer','Invoice'):
                model=apps.get_model('core',name);rows=list(model.objects.values())
                if name=='Plan':
                    self.assertTrue(all(row.pop('is_active') is True for row in rows))
                self.assertEqual(rows,before[model._meta.db_table])
            with connection.cursor() as cursor:
                cursor.execute("SELECT seq FROM sqlite_sequence WHERE name='core_plan'" if connection.vendor=='sqlite' else 'SELECT last_value FROM core_plan_id_seq')
                self.assertEqual(cursor.fetchone()[0],sequence)
            self.assertEqual(apps.get_model('core','Customer').objects.get(pk=customer.pk).plan_id,plan.pk)
        finally:MigrationExecutor(connection).migrate(new_target)
