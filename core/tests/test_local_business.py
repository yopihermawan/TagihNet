import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, close_old_connections, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from core import business as actions
from core import models as m
from core.access import save_member, save_role
from core.billing import issue_invoice, record_payment, reverse_payment
from core.business_services import voucher_cipher, voucher_status
from core.cash import balance, cash_summary
from core.operations import activate_installation, activation_quote
from .auth_helpers import verified_login
from .test_workflow import fixtures


class BusinessFixture:
    @classmethod
    def setUpTestData(cls):cls.tenants,cls.owners,cls.customers=fixtures()

    def setUp(self):
        if not hasattr(self,'tenants'):self.tenants,self.owners,self.customers=fixtures()
        self.actor=self.owners[0];self.tenant=self.tenants[0];verified_login(self.client,self.actor)

    def proposal(self,action,values=None,pk=None,actor=None):
        actor=actor or self.actor
        token,_=actions.form_token(actor,action,pk)
        return actions.review(actor,action,pk,token,values or {})[0]

    def run_action(self,action,values=None,pk=None,actor=None):
        actor=actor or self.actor
        token=self.proposal(action,values,pk,actor)
        return actions.confirm(actor,token,time.time(),m.Membership.objects.get(user=actor).session_version)[0]

    def fund(self,name='Kas'):
        return m.Fund.objects.get(pk=self.run_action('fund_create',{'name':name,'kind':'cash','active':True}))

    def post(self,fund,amount=1000,kind='funding',when=None):
        return self.run_action('cash_post',{'fund':fund.pk,'kind':kind,'amount':amount,
            'business_date':(when or timezone.localdate()).isoformat(),'note':'Pencatatan uji lokal'})

    def employee(self,member=None,name='Anggota uji',base=100):
        pk=self.run_action('employee_create',{'name':name,'department':'Operasi','phone':'','email':'','member':member.pk if member else '', 'active':True})
        self.run_action('salary',{'base':base,'allowance':20,'deduction':10},pk)
        return m.Employee.objects.get(pk=pk)

    def sales(self,name='sales-local'):
        user=get_user_model().objects.create_user(username=name,password='Local-test-secret-2026!')
        member=m.Membership.objects.create(tenant=self.tenant,user=user,role='sales')
        verified_login(Client(),user)
        return member

    def lead_values(self,**extra):
        return {'name':'Prospek uji','phone':'081298765432','email':'','username':'prospek-uji',
            'address':'Jl. Prospek','area':'Bandung','plan':self.customers[0].plan_id,'status':'qualified',
            'note':'Catatan prospek','commission_amount':25,'assigned_to':'',**extra}

    def hotspot(self):
        return self.run_action('hotspot_create',{'name':'Harian','speed_mbps':10,'duration_minutes':60,
            'activate_within_days':7,'price':50,'active':True})

    def node(self,name='ODC 1',kind='odc',parent='',capacity=4):
        return self.run_action('node_create',{'name':name,'kind':kind,'parent':parent,'capacity':capacity,
            'address':'Jl. Jaringan','latitude':'-6.9','longitude':'107.6','active':True})


class BusinessReviewTests(BusinessFixture,TransactionTestCase):
    def test_gets_have_no_business_writes_and_are_private(self):
        for module in ('cash','team','payroll','sales','hotspot','topology'):
            response=self.client.get(f'/business/{module}/')
            self.assertEqual(response.status_code,200,module);self.assertIn('no-store',response['Cache-Control'])
        self.assertEqual(m.BusinessAction.objects.count(),0)
        self.assertEqual(m.AuditEvent.objects.count(),0)

    def test_form_review_confirm_replay_via_http(self):
        response=self.client.get('/business/actions/fund_create/')
        token=response.context['form']['token'].value()
        values={'name':'Dana HTTP','kind':'bank','active':'on','token':token}
        response=self.client.post('/business/actions/fund_create/',values)
        self.assertContains(response,'Tinjau',html=False)
        self.assertFalse(m.Fund.objects.exists())
        token=response.context['form']['token'].value()
        self.assertEqual(self.client.post('/business/actions/confirm/',{'token':token,'reviewed':'on'}).status_code,302)
        self.assertEqual(self.client.post('/business/actions/confirm/',{'token':token,'reviewed':'on'}).status_code,302)
        self.assertEqual(m.Fund.objects.count(),1);self.assertEqual(m.BusinessAction.objects.count(),1)

    def test_confirmation_checkbox_required(self):
        token=self.proposal('fund_create',{'name':'Dana','kind':'cash','active':True})
        self.assertEqual(self.client.post('/business/actions/confirm/',{'token':token}).status_code,400)
        self.assertFalse(m.Fund.objects.exists())

    def test_actor_tenant_mfa_and_access_bound(self):
        token=self.proposal('fund_create',{'name':'Dana','kind':'cash','active':True})
        verified_login(Client(),self.owners[1])
        with self.assertRaises(PermissionDenied):actions.confirm(self.owners[1],token,time.time(),1)
        m.Membership.objects.filter(user=self.actor).update(session_version=2)
        with self.assertRaises(PermissionDenied):actions.confirm(self.actor,token,time.time(),2)
        m.Membership.objects.filter(user=self.actor).update(session_version=1)
        m.MFADevice.objects.filter(user=self.actor).update(revision=uuid.uuid4())
        with self.assertRaises(PermissionDenied):actions.confirm(self.actor,token,time.time(),1)

    def test_expiry_tamper_and_fresh_factor(self):
        token=self.proposal('fund_create',{'name':'Dana','kind':'cash','active':True})
        with self.assertRaises(ValidationError):actions.confirm(self.actor,token+'x',time.time(),1)
        with self.assertRaises(PermissionDenied):actions.confirm(self.actor,token,time.time()-301,1)
        with patch('django.core.signing.time.time',return_value=time.time()+901):
            with self.assertRaises(ValidationError):actions.read(self.actor,token,'review')
        self.assertFalse(m.Fund.objects.exists())

    def test_stale_form_and_stale_review_rejected(self):
        fund=self.fund();token,_=actions.form_token(self.actor,'fund_edit',fund.pk)
        values={'name':'Ubah','kind':'cash','active':True}
        self.run_action('fund_edit',values,fund.pk)
        with self.assertRaises(ValidationError):actions.review(self.actor,'fund_edit',fund.pk,token,values)
        token=self.proposal('fund_edit',values,fund.pk)
        m.Fund.objects.filter(pk=fund.pk).update(version=100)
        with self.assertRaises(ValidationError):actions.confirm(self.actor,token,time.time(),1)

    def test_all_modules_deny_ordinary_presets_except_noc_read_topology(self):
        member=m.Membership.objects.get(user=self.actor)
        for role in ('ops','finance','cashier','technician','customer','noc','sales'):
            member.role=role;member.customer=self.customers[0] if role=='customer' else None;member.save()
            verified_login(self.client,self.actor)
            for module in ('cash','team','payroll','sales','hotspot','topology'):
                expected=200 if (role,module) in {('noc','topology'),('sales','sales')} else 403
                self.assertEqual(self.client.get(f'/business/{module}/').status_code,expected,(role,module))

    def test_custom_role_dependencies_and_sales_scope_protected(self):
        with self.assertRaises(ValidationError):save_role(self.actor,'Tidak lengkap',['manage_payroll','payroll'])
        role=save_role(self.actor,'Lihat dana',['cash'])
        sales=self.sales()
        with self.assertRaises(ValidationError):save_member(self.actor,sales.pk,'sales',role.pk,None,True)

    def test_csrf_and_unknown_action_are_rejected(self):
        client=Client(enforce_csrf_checks=True);verified_login(client,self.actor)
        self.assertEqual(client.post('/business/actions/fund_create/',{'name':'X'}).status_code,403)
        self.assertEqual(self.client.get('/business/actions/arbitrary_model/').status_code,403)

    @override_settings(SIMULATION=False)
    def test_local_actions_cannot_be_enabled_as_a_production_adapter(self):
        self.assertEqual(self.client.get('/business/actions/fund_create/').status_code,403)

    def test_cash_range_validation_is_a_client_error(self):
        self.assertEqual(self.client.get('/business/cash/',{'start':'2026-12-01','end':'2026-01-01'}).status_code,400)

    def test_dates_before_reporting_epoch_are_rejected_before_signing(self):
        for action in ('cash_close','payroll_create'):
            with self.assertRaisesMessage(ValidationError,'Gunakan tanggal sejak tahun 2000'):
                self.proposal(action,{'period':'0001-01'})
        fund=self.fund()
        with self.assertRaisesMessage(ValidationError,'Gunakan tanggal sejak tahun 2000'):
            self.post(fund,100,when=date(1,1,1))


class CashTests(BusinessFixture,TestCase):
    def test_funding_expense_transfer_and_pair_correction(self):
        source=self.fund();dest=self.fund('Bank');self.post(source,1000)
        self.post(source,100,'expense')
        transfer=self.run_action('cash_transfer',{'fund':source.pk,'destination':dest.pk,'amount':200,'business_date':timezone.localdate().isoformat(),'note':'Transfer uji'})
        self.assertEqual(balance(self.tenant,source.pk),700);self.assertEqual(balance(self.tenant,dest.pk),200)
        self.run_action('cash_correct',{'reason':'Transfer dibatalkan setelah pemeriksaan'},transfer)
        self.assertEqual(balance(self.tenant,source.pk),900);self.assertEqual(balance(self.tenant,dest.pk),0)
        self.assertEqual(m.CashCorrection.objects.count(),2)

    def test_insufficient_funds_future_and_same_account_rejected_atomically(self):
        fund=self.fund()
        with self.assertRaises(ValidationError):self.post(fund,1,'expense')
        with self.assertRaises(ValidationError):self.post(fund,1,when=timezone.localdate()+timedelta(days=1))
        with self.assertRaises(ValidationError):self.run_action('cash_transfer',{'fund':fund.pk,'destination':fund.pk,'amount':1,'business_date':timezone.localdate().isoformat(),'note':'Transfer gagal'})
        self.assertEqual(m.CashMovement.objects.count(),0)

    def test_payment_mapping_is_immutable_and_tenant_scoped(self):
        fund=self.fund();invoice,_=issue_invoice(self.tenant,self.customers[0].pk,date(2026,1,1),self.actor)
        key=uuid.uuid4();payment,_=record_payment(self.tenant,invoice.pk,100,key,self.actor,fund_id=fund.pk)
        self.assertEqual(balance(self.tenant,fund.pk),100)
        self.assertFalse(record_payment(self.tenant,invoice.pk,100,key,self.actor,fund_id=fund.pk)[1])
        with self.assertRaises(ValidationError):record_payment(self.tenant,invoice.pk,100,key,self.actor)
        other=m.Fund.objects.create(tenant=self.tenants[1],name='Lain',kind='cash')
        with self.assertRaises(ValidationError):record_payment(self.tenant,invoice.pk,1,uuid.uuid4(),self.actor,fund_id=other.pk)
        self.assertEqual(m.PaymentFunding.objects.count(),1)

    def test_legacy_unmapped_payments_and_reversal_keep_event_dates(self):
        invoice,_=issue_invoice(self.tenant,self.customers[0].pk,date(2026,1,1),self.actor)
        payment,_=record_payment(self.tenant,invoice.pk,100,uuid.uuid4(),self.actor)
        then=timezone.now()-timedelta(days=40);m.Payment.objects.filter(pk=payment.pk).update(created_at=then)
        reverse_payment(self.tenant,payment.pk,'Koreksi pembayaran pengujian',self.actor)
        old=cash_summary(self.tenant,end=timezone.localdate()-timedelta(days=1))[-1]
        now=cash_summary(self.tenant,start=timezone.localdate())[-1]
        self.assertEqual(old['closing'],100);self.assertEqual(now['opening'],100);self.assertEqual(now['out'],100);self.assertEqual(now['closing'],0)

    def test_reversal_can_expose_deficit_without_deleting_history(self):
        fund=self.fund();invoice,_=issue_invoice(self.tenant,self.customers[0].pk,date(2026,1,1),self.actor)
        payment,_=record_payment(self.tenant,invoice.pk,100,uuid.uuid4(),self.actor,fund_id=fund.pk)
        self.post(fund,100,'expense');reverse_payment(self.tenant,payment.pk,'Koreksi transaksi yang keliru',self.actor)
        self.assertEqual(balance(self.tenant,fund.pk),-100)

    def test_close_is_immutable_and_blocks_backdated_manual_entries(self):
        fund=self.fund();period=(timezone.localdate().replace(day=1)-timedelta(days=1)).replace(day=1)
        self.post(fund,500,when=period)
        pk=self.run_action('cash_close',{'period':period.strftime('%Y-%m')})
        snapshot=m.CashClose.objects.get(pk=pk).snapshot
        self.assertEqual(snapshot[0]['closing'],500)
        with self.assertRaises(ValidationError):self.post(fund,10,when=period)
        self.post(fund,100)
        self.assertEqual(m.CashClose.objects.get(pk=pk).snapshot,snapshot)
        with self.assertRaises(ValidationError):self.run_action('cash_close',{'period':timezone.localdate().strftime('%Y-%m')})

    def test_close_review_detects_new_historical_events(self):
        fund=self.fund();period=(timezone.localdate().replace(day=1)-timedelta(days=1)).replace(day=1)
        token=self.proposal('cash_close',{'period':period.strftime('%Y-%m')});self.post(fund,20,when=period)
        with self.assertRaises(ValidationError):actions.confirm(self.actor,token,time.time(),1)

    def test_invalid_amount_and_short_correction_reasons_refused(self):
        fund=self.fund()
        for amount in (0,-1,2_000_000_001,'1.2'):
            with self.assertRaises(ValidationError):self.post(fund,amount)
        movement=self.post(fund)
        with self.assertRaises(ValidationError):self.run_action('cash_correct',{'reason':'pendek'},movement)
        for name in ('Dana\u202e menipu','Dana\ud800','\u0344'*80):
            with self.assertRaises(ValidationError):self.proposal('fund_create',{'name':name,'kind':'cash','active':True})

    def test_reason_is_preserved_in_audit(self):
        fund=self.fund();movement=self.post(fund)
        reason='Koreksi karena penerimaan tercatat pada dana yang keliru.'
        self.run_action('cash_correct',{'reason':reason},movement)
        self.assertEqual(m.AuditEvent.objects.get(action='business.cash_correct').detail,reason)

    def test_audit_failure_rolls_back_posting_and_receipt_then_same_token_can_retry(self):
        fund=self.fund();before=m.BusinessAction.objects.count()
        token=self.proposal('cash_post',{'fund':fund.pk,'kind':'funding','amount':100,
            'business_date':timezone.localdate().isoformat(),'note':'Uji kegagalan audit'})
        with patch('core.business.audit',side_effect=RuntimeError('Synthetic audit failure')):
            with self.assertRaises(RuntimeError):actions.confirm(self.actor,token,time.time(),1)
        self.assertFalse(m.CashMovement.objects.exists());self.assertEqual(m.BusinessAction.objects.count(),before)
        self.assertEqual(balance(self.tenant,fund.pk),0)
        self.assertTrue(actions.confirm(self.actor,token,time.time(),1)[1]);self.assertEqual(balance(self.tenant,fund.pk),100)


class PayrollTests(BusinessFixture,TestCase):
    def prepare(self):
        self.employee();fund=self.fund();self.post(fund,1000)
        run=self.run_action('payroll_create',{'period':timezone.localdate().strftime('%Y-%m')})
        return run,fund

    def test_preview_approve_pay_replay_and_reverse(self):
        run,fund=self.prepare();self.assertEqual(m.PayrollRun.objects.get(pk=run).total,110)
        with self.assertRaises(ValidationError):self.run_action('payroll_pay',{'fund':fund.pk},run)
        self.run_action('payroll_approve',{},run)
        token=self.proposal('payroll_pay',{'fund':fund.pk},run)
        self.assertTrue(actions.confirm(self.actor,token,time.time(),1)[1]);self.assertFalse(actions.confirm(self.actor,token,time.time(),1)[1])
        self.assertEqual(balance(self.tenant,fund.pk),890)
        self.run_action('payroll_reverse',{'reason':'Koreksi payroll setelah rekonsiliasi'},run)
        self.assertEqual(balance(self.tenant,fund.pk),1000);self.assertEqual(m.PayrollRun.objects.get(pk=run).status,'reversed')

    def test_terms_change_stales_preview_and_approval(self):
        run,fund=self.prepare();self.run_action('payroll_approve',{},run)
        employee=m.Employee.objects.get(tenant=self.tenant)
        self.run_action('salary',{'base':200,'allowance':20,'deduction':10},employee.pk)
        with self.assertRaises(ValidationError):self.run_action('payroll_pay',{'fund':fund.pk},run)
        self.assertEqual(m.PayrollLine.objects.get(run_id=run).net,110)
        self.assertEqual(balance(self.tenant,fund.pk),1000)

    def test_access_and_mfa_changes_require_reapproval(self):
        run,fund=self.prepare();self.run_action('payroll_approve',{},run)
        m.Membership.objects.filter(user=self.actor).update(session_version=2)
        with self.assertRaises(ValidationError):self.run_action('payroll_pay',{'fund':fund.pk},run)
        self.run_action('payroll_approve',{},run);self.run_action('payroll_pay',{'fund':fund.pk},run)
        self.assertEqual(balance(self.tenant,fund.pk),890)

    def test_duplicate_period_requires_cancel_or_reverse(self):
        run,_=self.prepare()
        with self.assertRaises(IntegrityError),transaction.atomic():self.run_action('payroll_create',{'period':timezone.localdate().strftime('%Y-%m')})
        self.run_action('payroll_cancel',{'reason':'Batalkan pratinjau untuk perbaikan'},run)
        next_run=self.run_action('payroll_create',{'period':timezone.localdate().strftime('%Y-%m')})
        self.assertNotEqual(run,next_run)

    def test_own_payslip_scope_uses_historical_member(self):
        member=self.sales();employee=self.employee(member);fund=self.fund();self.post(fund,1000)
        run=self.run_action('payroll_create',{'period':timezone.localdate().strftime('%Y-%m')})
        verified_login(self.client,member.user)
        self.assertNotContains(self.client.get('/account/payslips/'),'Anggota uji')
        self.run_action('payroll_approve',{},run);self.run_action('payroll_pay',{'fund':fund.pk},run)
        self.assertContains(self.client.get('/account/payslips/'),'Anggota uji')
        m.Employee.objects.filter(pk=employee.pk).update(member=None,name='Nama diubah')
        self.assertContains(self.client.get('/account/payslips/'),'Anggota uji')
        verified_login(self.client,self.owners[1]);self.assertNotContains(self.client.get('/account/payslips/'),'Anggota uji')

    def test_team_permission_does_not_reveal_salaries(self):
        self.employee(base=1234567)
        role=save_role(self.actor,'Tim saja',['team'])
        user=get_user_model().objects.create_user(username='team-reader');m.Membership.objects.create(tenant=self.tenant,user=user,role='ops',access_role=role)
        verified_login(self.client,user)
        self.assertNotContains(self.client.get('/business/team/'),'1.234.567')
        self.assertEqual(self.client.get('/business/payroll/').status_code,403)

    def test_missing_terms_and_invalid_net_refused(self):
        with self.assertRaises(ValidationError):self.run_action('payroll_create',{'period':timezone.localdate().strftime('%Y-%m')})
        pk=self.run_action('employee_create',{'name':'Belum gaji','department':'','phone':'','email':'','member':'','active':True})
        with self.assertRaises(ValidationError):self.run_action('payroll_create',{'period':timezone.localdate().strftime('%Y-%m')})
        with self.assertRaises(ValidationError):self.run_action('salary',{'base':10,'allowance':0,'deduction':10},pk)


class SalesTests(BusinessFixture,TestCase):
    def test_sales_own_scope_and_reassignment_revokes_access(self):
        first=self.sales();second=self.sales('sales-second')
        pk=self.run_action('lead_create',self.lead_values(assigned_to=first.pk))
        verified_login(self.client,first.user);self.assertContains(self.client.get('/business/sales/'),'Prospek uji')
        self.assertEqual(self.client.get(f'/business/sales/lead/{pk}/').status_code,200)
        self.run_action('lead_edit',self.lead_values(assigned_to=second.pk),pk)
        self.assertNotContains(self.client.get('/business/sales/'),'Prospek uji')
        self.assertEqual(self.client.get(f'/business/sales/lead/{pk}/').status_code,404)
        with self.assertRaises(m.Lead.DoesNotExist):self.proposal('lead_convert',{},pk,first.user)

    def test_sales_cannot_set_commission_or_assign_someone_else(self):
        sales=self.sales()
        pk=self.run_action('lead_create',self.lead_values(assigned_to=self.owners[0].membership.pk,commission_amount=999999),actor=sales.user)
        lead=m.Lead.objects.get(pk=pk);self.assertEqual(lead.assigned_to,sales);self.assertEqual(lead.commission_amount,0)

    def test_conversion_then_activation_accrues_once_and_payment_is_separate(self):
        sales=self.sales();pk=self.run_action('lead_create',self.lead_values(assigned_to=sales.pk))
        token=self.proposal('lead_convert',{},pk,sales.user)
        actions.confirm(sales.user,token,time.time(),1);actions.confirm(sales.user,token,time.time(),1)
        lead=m.Lead.objects.get(pk=pk);order=lead.installation
        self.assertEqual(m.Installation.objects.filter(tenant=self.tenant).count(),1);self.assertFalse(m.Commission.objects.exists())
        self.assertEqual(order.created_by,sales.user)
        order.status='installed';order.save();period=timezone.localdate().replace(day=1)
        quote=activation_quote(order,period)[1]
        activate_installation(self.actor,order.pk,period,quote,time.time(),1)
        activate_installation(self.actor,order.pk,period,quote,time.time(),1)
        commission=m.Commission.objects.get(lead=lead);self.assertEqual(commission.amount,25);self.assertEqual(commission.member,sales)
        fund=self.fund();self.post(fund,100)
        self.run_action('commission_pay',{'fund':fund.pk},commission.pk)
        self.assertEqual(balance(self.tenant,fund.pk),75)
        self.run_action('commission_reverse',{'reason':'Koreksi komisi setelah pemeriksaan'},commission.pk)
        self.assertEqual(balance(self.tenant,fund.pk),100)

    def test_sales_cannot_mutate_psb_after_conversion(self):
        sales=self.sales();pk=self.run_action('lead_create',self.lead_values(assigned_to=sales.pk))
        self.run_action('lead_convert',{},pk,sales.user);lead=m.Lead.objects.get(pk=pk)
        verified_login(self.client,sales.user)
        self.assertEqual(self.client.get(f'/operations/installations/{lead.installation_id}/').status_code,403)
        with self.assertRaises(ValidationError):self.run_action('lead_edit',self.lead_values(),pk)

    def test_public_campaign_opt_in_dates_catalog_and_no_private_data(self):
        values={'title':'Promo uji','description':'<script>alert(1)</script>','contact':'081234567890',
            'plan':self.customers[0].plan_id,'starts':timezone.localdate().isoformat(),'ends':timezone.localdate().isoformat(),'published':False}
        pk=self.run_action('campaign_create',values);campaign=m.Campaign.objects.get(pk=pk);path=f'/promo/{campaign.public_key}/'
        client=Client();self.assertEqual(client.get(path).status_code,404)
        self.run_action('campaign_edit',{**values,'published':True},pk)
        response=client.get(path);self.assertContains(response,'Promo uji');self.assertNotContains(response,'<script>alert(1)</script>')
        self.assertNotContains(response,'Pelanggan 0');self.assertIn('no-store',response['Cache-Control'])
        m.Plan.objects.filter(pk=campaign.plan_id).update(is_active=False);self.assertEqual(client.get(path).status_code,404)


class HotspotTests(BusinessFixture,TestCase):
    def batch(self):
        fund=self.fund();profile=self.hotspot()
        pk=self.run_action('voucher_sell',{'profile':profile,'quantity':2,'fund':fund.pk})
        return m.VoucherBatch.objects.get(pk=pk),fund

    def test_sale_replay_snapshot_and_encrypted_credentials(self):
        fund=self.fund();profile=self.hotspot();token=self.proposal('voucher_sell',{'profile':profile,'quantity':2,'fund':fund.pk})
        pk=actions.confirm(self.actor,token,time.time(),1)[0];actions.confirm(self.actor,token,time.time(),1)
        batch=m.VoucherBatch.objects.get(pk=pk);self.assertEqual(batch.vouchers.count(),2);self.assertEqual(balance(self.tenant,fund.pk),100)
        voucher=batch.vouchers.first();password=voucher_cipher().decrypt(voucher.encrypted_password.encode()).decode()
        self.assertNotIn(password,voucher.encrypted_password)
        response=self.client.get(f'/business/hotspot/batch/{pk}/');self.assertNotContains(response,password)
        self.assertNotContains(response,voucher.encrypted_password)
        self.assertFalse(m.AuditEvent.objects.filter(detail__contains=password).exists())
        m.HotspotProfile.objects.filter(pk=profile).update(price=1000,duration_minutes=1000)
        self.assertEqual(batch.snapshot['price'],50);self.assertEqual(batch.snapshot['duration_minutes'],60)

    def test_profile_change_stales_sale_review(self):
        fund=self.fund();profile=self.hotspot();token=self.proposal('voucher_sell',{'profile':profile,'quantity':2,'fund':fund.pk})
        m.HotspotProfile.objects.filter(pk=profile).update(price=100)
        with self.assertRaises(ValidationError):actions.confirm(self.actor,token,time.time(),1)
        self.assertFalse(m.VoucherBatch.objects.exists());self.assertFalse(m.CashMovement.objects.exists())

    def test_activation_once_never_extends_and_expiry_is_read_only(self):
        batch,_=self.batch();voucher=batch.vouchers.first();token=self.proposal('voucher_activate',{},voucher.pk)
        actions.confirm(self.actor,token,time.time(),1);voucher.refresh_from_db();expires=voucher.expires_at
        actions.confirm(self.actor,token,time.time(),1);voucher.refresh_from_db();self.assertEqual(voucher.expires_at,expires)
        with self.assertRaises(ValidationError):self.run_action('voucher_activate',{},voucher.pk)
        with patch('core.business_services.timezone.now',return_value=expires+timedelta(seconds=1)):
            self.assertEqual(voucher_status(voucher),'Kedaluwarsa')
        voucher.refresh_from_db();self.assertFalse(voucher.revoked)

    def test_refund_corrects_cash_and_revokes_all_vouchers(self):
        batch,fund=self.batch();voucher=batch.vouchers.first();self.run_action('voucher_activate',{},voucher.pk)
        self.run_action('voucher_refund',{'reason':'Koreksi penjualan voucher simulasi'},batch.pk)
        self.assertEqual(balance(self.tenant,fund.pk),0);self.assertEqual(batch.vouchers.filter(revoked=True).count(),2)
        self.assertEqual(self.client.post(f'/business/vouchers/{batch.pk}/print/').status_code,404)
        with self.assertRaises(ValidationError):self.run_action('voucher_activate',{},voucher.pk)
        with self.assertRaises(ValidationError):self.run_action('cash_correct',{'reason':'Koreksi tidak melalui sumber'},batch.movement_id)

    def test_print_post_only_fresh_mfa_and_scope(self):
        batch,_=self.batch();path=f'/business/vouchers/{batch.pk}/print/'
        self.assertEqual(self.client.get(path).status_code,405)
        response=self.client.post(path);self.assertEqual(response.status_code,200);self.assertTrue(response.content.startswith(b'%PDF'))
        self.assertIn('no-store',response['Cache-Control']);self.assertIn('attachment',response['Content-Disposition'])
        session=self.client.session;session['mfa_verified_at']=time.time()-301;session.save()
        self.assertEqual(self.client.post(path).status_code,302)
        verified_login(self.client,self.owners[1]);self.assertEqual(self.client.post(path).status_code,404)

    def test_expired_activation_and_quantity_limits(self):
        batch,_=self.batch();voucher=batch.vouchers.first()
        m.Voucher.objects.filter(pk=voucher.pk).update(activation_deadline=timezone.now()-timedelta(seconds=1))
        with self.assertRaises(ValidationError):self.run_action('voucher_activate',{},voucher.pk)
        for quantity in (0,101):
            with self.assertRaises(ValidationError):self.proposal('voucher_sell',{'profile':batch.profile_id,'quantity':quantity,'fund':batch.movement.fund_id})


class TopologyTests(BusinessFixture,TestCase):
    def test_tree_mapping_capacity_move_and_unmap(self):
        parent=self.node();node=self.node('ODP 1','odp',parent,2)
        self.run_action('endpoint_set',{'node':node,'port':2},self.customers[0].pk)
        self.assertEqual(m.CustomerEndpoint.objects.get(customer=self.customers[0]).port,2)
        response=self.client.get(f'/business/topology/node/{node}/');self.assertContains(response,'Pelanggan 0');self.assertContains(response,'openstreetmap.org')
        values={'name':'ODP 1','kind':'odp','parent':parent,'capacity':1,'address':'','latitude':'','longitude':'','active':True}
        with self.assertRaises(ValidationError):self.run_action('node_edit',values,node)
        with self.assertRaises(ValidationError):self.run_action('node_edit',{**values,'capacity':2,'active':False},node)
        self.run_action('endpoint_remove',{'reason':'Pelanggan dipindah ke sambungan lain'},self.customers[0].pk)
        self.run_action('node_edit',{**values,'capacity':2,'active':False},node)
        self.assertFalse(m.CustomerEndpoint.objects.exists())

    def test_port_unique_tenant_capacity_and_odc_parent(self):
        parent=self.node();node=self.node('ODP 1','odp',parent,1)
        second=m.Customer.objects.create(tenant=self.tenant,code='PORT-2',name='Pelanggan kedua',phone='081211111111',username='port2',address='Jl. Port',area='Bandung',plan=self.customers[0].plan)
        self.run_action('endpoint_set',{'node':node,'port':1},self.customers[0].pk)
        with self.assertRaises(ValidationError):self.run_action('endpoint_set',{'node':node,'port':1},second.pk)
        with self.assertRaises(ValidationError):self.run_action('endpoint_set',{'node':node,'port':2},second.pk)
        with self.assertRaises(m.Customer.DoesNotExist):self.run_action('endpoint_set',{'node':node,'port':1},self.customers[1].pk)
        with self.assertRaises(ValidationError):self.node('Tanpa ODC','odp','',1)
        with self.assertRaises(ValidationError):self.node('Parent ODP','odp',node,1)

    def test_odc_capacity_and_archive_requires_empty_children(self):
        parent=self.node(capacity=1);self.node('ODP 1','odp',parent,1)
        with self.assertRaises(ValidationError):self.node('ODP 2','odp',parent,1)
        with self.assertRaises(ValidationError):self.run_action('node_edit',{'name':'ODC 1','kind':'odc','parent':'','capacity':1,'address':'','latitude':'','longitude':'','active':False},parent)

    def test_mapping_review_stales_when_customer_mapping_changes(self):
        parent=self.node();node=self.node('ODP 1','odp',parent,3)
        token=self.proposal('endpoint_set',{'node':node,'port':1},self.customers[0].pk)
        self.run_action('endpoint_set',{'node':node,'port':2},self.customers[0].pk)
        with self.assertRaises(ValidationError):actions.confirm(self.actor,token,time.time(),1)


class CashConcurrencyTests(BusinessFixture,TransactionTestCase):
    def setUp(self):
        self.tenants,self.owners,self.customers=fixtures();super().setUp()

    def parallel(self,tokens):
        actor_id=self.actor.pk
        def work(token):
            close_old_connections()
            try:
                actor=get_user_model().objects.get(pk=actor_id)
                return actions.confirm(actor,token,time.time(),1)
            except ValidationError:return 'rejected'
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(work,tokens))

    def test_competing_spends_cannot_overdraw(self):
        fund=self.fund();self.post(fund,200)
        values={'fund':fund.pk,'kind':'expense','amount':150,'business_date':timezone.localdate().isoformat(),'note':'Pengeluaran paralel'}
        results=self.parallel([self.proposal('cash_post',values),self.proposal('cash_post',values)])
        self.assertEqual(results.count('rejected'),1);self.assertEqual(balance(self.tenant,fund.pk),50)

    def test_same_signed_request_commits_once(self):
        fund=self.fund();token=self.proposal('cash_post',{'fund':fund.pk,'kind':'funding','amount':100,'business_date':timezone.localdate().isoformat(),'note':'Permintaan paralel sama'})
        results=self.parallel([token,token]);self.assertEqual(sorted(result[1] for result in results),[False,True])
        self.assertEqual(balance(self.tenant,fund.pk),100)
