import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, transaction
from django.test import Client, TestCase, TransactionTestCase

from core.billing import issue_invoice
from core.billing_settings import (MAX_TOKEN, SALT, SettingsConflict, confirm_change, form_token,
    read_token, review_change, validate_values, values_for)
from core.bulk_billing import confirm_batch, create_preview, process_batch
from core.models import AuditEvent, Invoice, MFADevice, Membership, NetworkJob, Tenant
from core.operations import activate_installation, activation_quote, create_installation, save_sla_settings
from core.pdf import render_invoice
from .auth_helpers import verified_login
from .test_workflow import fixtures


class BillingSettingsReviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenants,cls.owners,cls.customers=fixtures()

    def setUp(self):
        self.actor=self.owners[0];self.tenant=self.tenants[0]
        verified_login(self.client,self.actor)

    def values(self,**changes):
        self.tenant.refresh_from_db()
        return {**values_for(self.tenant),**changes}

    def review(self,**changes):
        return review_change(self.actor,form_token(self.actor),self.values(**changes))[0]

    def confirm(self,token,**proof):
        return confirm_change(self.actor,token,proof.get('verified_at',time.time()),proof.get('access_version',1))

    def test_get_form_and_post_review_do_not_write(self):
        before=Tenant.objects.values().get(pk=self.tenant.pk)
        response=self.client.get('/settings/billing/')
        self.assertEqual(response['Cache-Control'],'no-store')
        token=response.context['form'].initial['token']
        review=self.client.post('/settings/billing/',{**self.values(tax_bp=0),'token':token})
        self.assertContains(review,'Tinjau setelan billing')
        self.assertContains(review,'Identitas penerbit disimpan pada dokumen baru.')
        self.assertEqual(Tenant.objects.values().get(pk=self.tenant.pk),before)
        self.assertFalse(AuditEvent.objects.filter(action='billing.settings_updated').exists())
        self.assertFalse(Invoice.objects.exists());self.assertFalse(NetworkJob.objects.exists())

    def test_legacy_direct_post_without_review_cannot_save(self):
        self.assertEqual(self.client.post('/settings/billing/',self.values(tax_bp=0)).status_code,400)
        self.tenant.refresh_from_db();self.assertEqual(self.tenant.tax_bp,1100)
        self.assertEqual(self.client.get('/settings/billing/confirm/').status_code,405)

    def test_checkbox_csrf_and_signed_values_are_enforced(self):
        token=self.review(tax_bp=0)
        csrf=Client(enforce_csrf_checks=True);verified_login(csrf,self.actor)
        self.assertEqual(csrf.post('/settings/billing/confirm/',{'token':token,'reviewed':'on'}).status_code,403)
        self.assertEqual(self.client.post('/settings/billing/confirm/',{'token':token}).status_code,400)
        response=self.client.post('/settings/billing/confirm/',{'token':token,'reviewed':'on','tax_bp':'9999',
            'tenant':self.tenants[1].pk,'sla_urgent_hours':'1'})
        self.assertRedirects(response,'/settings/billing/')
        self.tenant.refresh_from_db();self.assertEqual(self.tenant.tax_bp,0)
        self.tenants[1].refresh_from_db();self.assertEqual(self.tenants[1].tax_bp,1100)

    def test_save_only_changed_fields_preserves_sla_and_actor_security(self):
        token=self.review(tax_bp=0,invoice_footer='Teks baru')
        Tenant.objects.filter(pk=self.tenant.pk).update(sla_urgent_hours=17)
        member=Membership.objects.values().get(user=self.actor);device=MFADevice.objects.values().get(user=self.actor)
        self.assertTrue(self.confirm(token))
        self.tenant.refresh_from_db();self.assertEqual(self.tenant.sla_urgent_hours,17)
        self.assertEqual(self.tenant.tax_bp,0);self.assertEqual(self.tenant.invoice_footer,'Teks baru')
        self.assertEqual(Membership.objects.values().get(user=self.actor),member)
        self.assertEqual(MFADevice.objects.values().get(user=self.actor),device)
        audit=AuditEvent.objects.get(action='billing.settings_updated')
        self.assertNotIn('Teks baru',audit.detail)
        self.assertIn('tax_bp',audit.detail)

    def test_stale_sla_form_cannot_restore_old_billing_values(self):
        from core.operation_forms import SLASettingsForm
        token=self.review(tax_bp=0,invoice_footer='Footer baru')
        original=SLASettingsForm.is_valid
        def validate_then_confirm(form):
            valid=original(form)
            if valid:self.confirm(token)
            return valid
        with patch.object(SLASettingsForm,'is_valid',validate_then_confirm):
            response=self.client.post('/operations/sla/',{'sla_low_hours':72,'sla_normal_hours':24,'sla_high_hours':8,'sla_urgent_hours':17})
        self.assertRedirects(response,'/operations/sla/')
        self.tenant.refresh_from_db();self.assertEqual(self.tenant.tax_bp,0)
        self.assertEqual(self.tenant.invoice_footer,'Footer baru');self.assertEqual(self.tenant.sla_urgent_hours,17)

    def test_sla_save_uses_current_access_and_exact_fields_and_rolls_back_audit_failure(self):
        values={'sla_low_hours':72,'sla_normal_hours':24,'sla_high_hours':8,'sla_urgent_hours':17}
        for invalid in ({**values,'tax_bp':0},{**values,'sla_urgent_hours':True},{**values,'sla_urgent_hours':721}):
            with self.assertRaises(ValidationError):save_sla_settings(self.actor,invalid,1)
        for version in (True,2):
            with self.assertRaises(PermissionDenied):save_sla_settings(self.actor,values,version)
        with patch('core.operations.audit',side_effect=RuntimeError),self.assertRaises(RuntimeError):
            save_sla_settings(self.actor,values,1)
        self.tenant.refresh_from_db();self.assertEqual(self.tenant.sla_urgent_hours,2)
        Membership.objects.filter(user=self.actor).update(role='ops')
        with self.assertRaises(PermissionDenied):save_sla_settings(self.actor,values,1)

    def test_unchanged_values_are_noop_without_audit(self):
        token=self.review()
        self.assertFalse(self.confirm(token));self.assertFalse(self.confirm(token))
        self.assertFalse(AuditEvent.objects.filter(action='billing.settings_updated').exists())

    def test_stale_form_cannot_become_a_new_review_and_overwrite(self):
        token=form_token(self.actor)
        Tenant.objects.filter(pk=self.tenant.pk).update(tax_bp=500)
        response=self.client.post('/settings/billing/',{**self.values(tax_bp=0),'token':token})
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.context['form'].data['token'],token)
        self.tenant.refresh_from_db();self.assertEqual(self.tenant.tax_bp,500)

    def test_competing_review_and_saved_replay_return_conflict(self):
        first=self.review(tax_bp=0);second=self.review(due_day=15)
        self.assertTrue(self.confirm(first))
        for token in (first,second):
            with self.assertRaises(SettingsConflict):self.confirm(token)
            self.assertEqual(self.client.post('/settings/billing/confirm/',{'token':token,'reviewed':'on'}).status_code,409)
        self.assertEqual(AuditEvent.objects.filter(action='billing.settings_updated').count(),1)

    def test_fingerprint_compares_current_state_instead_of_version_history(self):
        token=self.review(tax_bp=0)
        Tenant.objects.filter(pk=self.tenant.pk).update(due_day=15)
        Tenant.objects.filter(pk=self.tenant.pk).update(due_day=31)
        self.assertTrue(self.confirm(token))

    def test_wrong_actor_and_tenant_are_refused(self):
        token=self.review(tax_bp=0)
        verified_login(Client(),self.owners[1])
        with self.assertRaises(PermissionDenied):read_token(self.owners[1],token,'review')
        values={**self.values(),'tenant':self.tenants[1].pk}
        with self.assertRaises(ValidationError):review_change(self.actor,form_token(self.actor),values)

    def test_missing_rights_cannot_read_or_post(self):
        token=self.review(tax_bp=0)
        Membership.objects.filter(user=self.actor).update(role='finance')
        for path in ('/settings/billing/','/settings/billing/confirm/'):
            self.assertEqual(self.client.post(path,{'token':token,'reviewed':'on'}).status_code,403)
        self.assertEqual(self.client.get('/settings/billing/').status_code,403)
        with self.assertRaises(PermissionDenied):self.confirm(token)

    def test_custom_role_with_settings_capability_can_review(self):
        from core.models import AccessRole
        role=AccessRole.objects.create(tenant=self.tenant,name='Pengaturan terbatas',capabilities=['settings'])
        Membership.objects.filter(user=self.actor).update(role='ops',access_role=role)
        self.assertTrue(self.confirm(self.review(tax_bp=0)))

    def test_inactive_or_changed_access_revision_is_refused(self):
        token=self.review(tax_bp=0)
        Membership.objects.filter(user=self.actor).update(session_version=2)
        with self.assertRaises(PermissionDenied):self.confirm(token,access_version=2)
        Membership.objects.filter(user=self.actor).update(session_version=1)
        get_user_model().objects.filter(pk=self.actor.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):self.confirm(token)

    def test_device_revision_change_or_missing_device_refuses_review(self):
        token=self.review(tax_bp=0)
        MFADevice.objects.filter(user=self.actor).update(revision=uuid.uuid4())
        with self.assertRaises(PermissionDenied):self.confirm(token)
        MFADevice.objects.filter(user=self.actor).delete()
        with self.assertRaises(PermissionDenied):form_token(self.actor)

    def test_fresh_factor_and_current_integer_access_version_required(self):
        token=self.review(tax_bp=0)
        for value in (None,True,time.time()-301,time.time()+20,float('nan'),float('inf')):
            with self.assertRaises(PermissionDenied):self.confirm(token,verified_at=value)
        for value in (True,999):
            with self.assertRaises(PermissionDenied):self.confirm(token,access_version=value)

    def test_http_stepup_returns_to_new_form_without_replaying_post(self):
        token=self.review(tax_bp=0)
        session=self.client.session;session['mfa_verified_at']=time.time()-301;session.save()
        self.assertRedirects(self.client.post('/settings/billing/confirm/',{'token':token,'reviewed':'on'}),'/account/security/')
        self.assertEqual(self.client.session['stepup_return'],'/settings/billing/')
        self.tenant.refresh_from_db();self.assertEqual(self.tenant.tax_bp,1100)

    def test_invalid_expired_oversized_wrong_kind_and_tampered_tokens_refused(self):
        token=self.review(tax_bp=0)
        for invalid in ('invalid',token+'x','x'*(MAX_TOKEN+1),form_token(self.actor),signing.dumps([],salt=SALT)):
            with self.assertRaises(ValidationError):self.confirm(invalid)
        with patch('django.core.signing.time.time',return_value=time.time()+901):
            with self.assertRaises(ValidationError):self.confirm(token)

    def test_form_token_expiration_and_kind_are_checked(self):
        token=form_token(self.actor)
        with patch('django.core.signing.time.time',return_value=time.time()+901):
            with self.assertRaises(ValidationError):review_change(self.actor,token,self.values())
        with self.assertRaises(ValidationError):review_change(self.actor,self.review(),self.values())

    def test_strict_token_shapes_and_after_values(self):
        token=self.review(tax_bp=0);payload=signing.loads(token,salt=SALT)
        for change in ({'v':True},{'tenant':True},{'state':'x'*64},{'mfa':'invalid'},{'before':[]},
                {'after':{**payload['after'],'tax_bp':True}},{'after':{**payload['after'],'name':' padded '}}):
            with self.assertRaises(ValidationError):self.confirm(signing.dumps({**payload,**change},salt=SALT))

    def test_validation_limits_zero_tax_and_multiline_plain_text(self):
        for values in (self.values(name=' '),self.values(address=''),self.values(due_day=0),self.values(due_day=32),
                self.values(tax_bp=-1),self.values(tax_bp=10001),self.values(tax_bp=True),self.values(name='x'*101),
                self.values(address='x'*251),self.values(invoice_footer='x'*1001)):
            with self.assertRaises(ValidationError):validate_values(values)
        valid=validate_values(self.values(name=' Cafe\u0301 ',address=' Baris1\r\nBaris2 ',invoice_footer=' Baris1\rBaris2 ',tax_bp=0))
        self.assertEqual(valid['name'],'Café');self.assertEqual(valid['address'],'Baris1\nBaris2')
        self.assertEqual(valid['invoice_footer'],'Baris1\nBaris2')
        self.assertTrue(self.confirm(self.review(**valid)))

    def test_controls_bidi_surrogates_and_unknown_fields_refused(self):
        for text in ('hidden\x00','hidden\t','hidden\u202e','hidden\ud800'):
            for field in ('name','address','invoice_footer'):
                with self.assertRaises(ValidationError):validate_values(self.values(**{field:text}))
        with self.assertRaises(ValidationError):validate_values({**self.values(),'sla_urgent_hours':1})

    def test_large_unicode_values_fit_bounded_review_token(self):
        token=self.review(name='😀'*100,address='😀'*250,invoice_footer='😀'*1000)
        self.assertLessEqual(len(token),MAX_TOKEN)
        self.assertTrue(self.confirm(token))

    def test_review_text_is_escaped_and_calendar_sample_clamps_february(self):
        token=self.review(name='<b>Nama literal</b>',invoice_footer='<script>window.test=1</script>',tax_bp=0,due_day=1)
        with patch('core.billing_settings_views.timezone.localdate',return_value=date(2026,2,15)):
            response=self.client.post('/settings/billing/confirm/',{'token':token})
        self.assertContains(response,'&lt;script&gt;',status_code=400)
        self.assertNotContains(response,'<script>window.test',status_code=400)
        self.assertEqual(response.context['examples']['before']['due'],date(2026,2,28))
        self.assertEqual(response.context['examples']['after']['total'],100000)
        self.assertEqual(response.context['examples']['before']['total'],111000)

    def test_audit_failure_rolls_back_all_changes(self):
        token=self.review(tax_bp=0,name='Gagal simpan');before=Tenant.objects.values().get(pk=self.tenant.pk)
        with patch('core.billing_settings.audit',side_effect=RuntimeError),self.assertRaises(RuntimeError):self.confirm(token)
        self.assertEqual(Tenant.objects.values().get(pk=self.tenant.pk),before)

    def test_database_busy_returns_safe_error_without_new_token_or_write(self):
        from django.db import OperationalError
        token=self.review(tax_bp=0)
        with patch('core.billing_settings_views.confirm_change',side_effect=OperationalError):
            response=self.client.post('/settings/billing/confirm/',{'token':token,'reviewed':'on'})
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.context['form'].data['token'],token)
        self.tenant.refresh_from_db();self.assertEqual(self.tenant.tax_bp,1100)

    def test_invoice_and_approved_batch_snapshots_unchanged_future_uses_new_settings(self):
        from zoneinfo import ZoneInfo
        from core import billing_scheduler as scheduler
        old,_=issue_invoice(self.tenant,self.customers[0].pk,date(2026,2,1),self.actor)
        before=Invoice.objects.values().get(pk=old.pk)
        approved=create_preview(self.actor,date(2026,3,1),{'state':'active','plan':None,'area':''})
        confirm_batch(self.actor,approved.pk,time.time(),1)
        pending=create_preview(self.actor,date(2026,4,1),{'state':'active','plan':None,'area':''})
        schedule_token,_=scheduler.review_change(self.actor,{'enabled':True,'day':1,'state':'active','plan':None,'area':''},0)
        schedule=scheduler.confirm_change(self.actor,schedule_token,time.time(),1)
        self.confirm(self.review(name='Nama penerbit baru',tax_bp=0,due_day=15,invoice_footer='Footer baru'))
        self.assertEqual(Invoice.objects.values().get(pk=old.pk),before)
        self.assertEqual(process_batch(approved.pk),1)
        saved=Invoice.objects.get(period=date(2026,3,1));self.assertEqual(saved.total,249750)
        self.assertEqual(saved.due_date,date(2026,3,31))
        self.assertEqual(confirm_batch(self.actor,pending.pk,time.time(),1).status,'stale')
        self.tenant.refresh_from_db()
        new,_=issue_invoice(self.tenant,self.customers[0].pk,date(2026,4,1),self.actor)
        self.assertEqual(new.total,225000);self.assertEqual(new.footer,'Footer baru');self.assertEqual(new.due_date,date(2026,4,15))
        scheduled=scheduler.run_schedule(schedule.pk,datetime(2027,2,1,10,tzinfo=ZoneInfo('Asia/Jakarta')))
        self.assertEqual(process_batch(scheduled.batch_id),1)
        self.assertEqual(Invoice.objects.get(period=date(2027,2,1)).total,225000)
        self.assertEqual(self.client.get(f'/invoices/{old.pk}/').context['issuer'].name, 'ISP 0')
        self.assertEqual(saved.issuer_snapshot.name, 'Nama penerbit baru')
        old.refresh_from_db();self.assertTrue(render_invoice(old,True).startswith(b'%PDF-'))

    def test_unapproved_psb_activation_quote_becomes_stale(self):
        order=create_installation(self.actor,{'name':'Prospek fiktif','email':'','phone':'081299988810','username':'psb-settings',
            'address':'Alamat fiktif','area':'Uji','plan_id':self.customers[0].plan_id,'assigned_id':None},uuid.uuid4())
        type(order).objects.filter(pk=order.pk).update(status='installed');order.refresh_from_db()
        _,fingerprint=activation_quote(order,date(2026,5,1))
        self.confirm(self.review(tax_bp=0))
        with self.assertRaises(ValidationError):activate_installation(self.actor,order.pk,date(2026,5,1),fingerprint,time.time(),1)
        self.assertFalse(Invoice.objects.exists())


class BillingSettingsConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.tenants,self.owners,self.customers=fixtures();self.actor=self.owners[0]
        verified_login(self.client,self.actor)

    def parallel(self,callback):
        def run(index):
            close_old_connections()
            try:return callback(index)
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(run,range(2)))

    def test_two_competing_confirmations_have_one_winner(self):
        tokens=[review_change(self.actor,form_token(self.actor),{**values_for(self.tenants[0]),'tax_bp':value})[0] for value in (0,500)]
        def action(index):
            try:return confirm_change(self.actor,tokens[index],time.time(),1)
            except SettingsConflict:return False
        self.assertEqual(sorted(self.parallel(action)),[False,True])
        self.assertEqual(AuditEvent.objects.filter(action='billing.settings_updated').count(),1)

    def test_sla_write_and_billing_confirm_preserve_both_changes(self):
        token=review_change(self.actor,form_token(self.actor),{**values_for(self.tenants[0]),'tax_bp':0})[0]
        def sla():
            save_sla_settings(self.actor,{'sla_low_hours':72,'sla_normal_hours':24,'sla_high_hours':8,'sla_urgent_hours':17},1)
        self.parallel(lambda index:confirm_change(self.actor,token,time.time(),1) if index else sla())
        self.tenants[0].refresh_from_db();self.assertEqual(self.tenants[0].tax_bp,0)
        self.assertEqual(self.tenants[0].sla_urgent_hours,17)
