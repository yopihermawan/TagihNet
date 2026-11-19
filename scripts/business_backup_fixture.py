"""Fictional business data for disposable backup drills; never for persistent user projects."""
FIXTURE='''
from core import models as m
from core.business import form_token as business_form,review as business_review,confirm as business_confirm
from core.business_services import voucher_cipher
from core.operations import activate_installation,activation_quote
from datetime import timedelta
import time
def business_action(action,values=None,pk=None):
    token,_=business_form(actor,action,pk)
    token,_=business_review(actor,action,pk,token,values or {})
    return business_confirm(actor,token,time.time(),access_version)[0]
fund=m.Fund.objects.get(pk=business_action('fund_create',{'name':'Dana Restore Fiktif','kind':'cash','active':True}))
business_action('cash_post',{'fund':fund.pk,'kind':'funding','amount':1000000,'business_date':timezone.localdate().isoformat(),'note':'Modal fiktif restore'})
expense=business_action('cash_post',{'fund':fund.pk,'kind':'expense','amount':10000,'business_date':timezone.localdate().isoformat(),'note':'Pengeluaran fiktif restore'})
business_action('cash_correct',{'reason':'Koreksi pengeluaran fiktif untuk latihan restore'},expense)
employee=business_action('employee_create',{'name':'Anggota Restore Fiktif','department':'Operasi','phone':'','email':'','member':actor.membership.pk,'active':True})
business_action('salary',{'base':100000,'allowance':20000,'deduction':10000},employee)
payroll=business_action('payroll_create',{'period':timezone.localdate().strftime('%Y-%m')})
business_action('payroll_approve',{},payroll);business_action('payroll_pay',{'fund':fund.pk},payroll)
profile=business_action('hotspot_create',{'name':'Hotspot Restore Fiktif','speed_mbps':10,'duration_minutes':60,'activate_within_days':7,'price':5000,'active':True})
batch_id=business_action('voucher_sell',{'profile':profile,'quantity':2,'fund':fund.pk})
voucher=m.Voucher.objects.filter(batch_id=batch_id).order_by('pk').first()
business_action('voucher_activate',{},voucher.pk)
assert voucher_cipher().decrypt(voucher.encrypted_password.encode())
odc=business_action('node_create',{'name':'ODC Restore Fiktif','kind':'odc','parent':'','capacity':4,'address':'Alamat fiktif','latitude':'','longitude':'','active':True})
odp=business_action('node_create',{'name':'ODP Restore Fiktif','kind':'odp','parent':odc,'capacity':8,'address':'Alamat fiktif','latitude':'-6.9','longitude':'107.6','active':True})
business_action('endpoint_set',{'node':odp,'port':1},customer.pk)
active_plan=m.Plan.objects.filter(tenant=tenant,is_active=True).order_by('pk').first()
from django.contrib.auth import get_user_model
sales_user=get_user_model().objects.create_user(username='backup-sales-fictitious',password=None)
sales_member=m.Membership.objects.create(tenant=tenant,user=sales_user,role='sales')
lead_id=business_action('lead_create',{'name':'Prospek Restore Fiktif','phone':'081298760003','email':'','username':'restore-prospect',
    'address':'Alamat prospek fiktif','area':'Restore','plan':active_plan.pk,'status':'qualified','note':'Fixture backup','assigned_to':sales_member.pk,'commission_amount':5000})
campaign=business_action('campaign_create',{'title':'Promo Restore Fiktif','description':'Penawaran fiktif latihan pemulihan','contact':'081298760004',
    'plan':active_plan.pk,'starts':timezone.localdate().isoformat(),'ends':timezone.localdate().isoformat(),'published':True})
business_action('lead_convert',{},lead_id)
lead=m.Lead.objects.get(pk=lead_id);order=lead.installation
# Earlier tests exercise the stage transitions; this fixture starts at installed.
order.status='installed';order.save(update_fields=['status'])
period=timezone.localdate().replace(day=1)
activate_installation(actor,order.pk,period,activation_quote(order,period)[1],time.time(),access_version)
commission=m.Commission.objects.get(lead=lead);assert commission.amount==5000
payment=m.Payment.objects.filter(tenant=tenant).order_by('pk').first()
m.PaymentFunding.objects.create(tenant=tenant,payment=payment,fund=fund)
previous=(timezone.localdate().replace(day=1)-timedelta(days=1)).replace(day=1)
business_action('cash_close',{'period':previous.strftime('%Y-%m')})
assert all(model.objects.exists() for model in (m.Fund,m.PaymentFunding,m.CashMovement,m.CashCorrection,m.CashClose,m.Employee,m.SalaryTerms,
    m.PayrollRun,m.PayrollLine,m.Campaign,m.Lead,m.Commission,m.HotspotProfile,m.VoucherBatch,m.Voucher,m.NetworkNode,m.CustomerEndpoint,m.BusinessAction))
'''

CHECK='''
from core import models as m
from core.business_services import voucher_cipher,voucher_status
from core.cash import balance
fund=m.Fund.objects.get(name='Dana Restore Fiktif')
assert balance(fund.tenant,fund.pk)==1000000-110000+10000+100000
run=m.PayrollRun.objects.get(tenant=fund.tenant);assert run.status=='paid' and run.total==110000
assert run.lines.get().name=='Anggota Restore Fiktif'
batch=m.VoucherBatch.objects.get(tenant=fund.tenant);assert batch.quantity==2 and batch.snapshot['price']==5000
voucher=batch.vouchers.order_by('pk').first();assert voucher_cipher().decrypt(voucher.encrypted_password.encode())
assert voucher.activated_at is not None and voucher.expires_at>voucher.activated_at
assert m.CustomerEndpoint.objects.get(tenant=fund.tenant).port==1
assert m.Commission.objects.get(tenant=fund.tenant).amount==5000
assert m.CashClose.objects.filter(tenant=fund.tenant).count()==1
assert m.Campaign.objects.get(tenant=fund.tenant).published
print('PASS: business cash/payroll/slip/commission/campaign/encrypted vouchers/topology/closing restored')
'''
