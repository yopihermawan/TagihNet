"""Real Chromium/MFA business flows on a disposable fictional database only."""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlsplit
import pyotp
from playwright.sync_api import expect, sync_playwright

ROOT=Path(__file__).resolve().parent.parent
phase='startup'
with tempfile.TemporaryDirectory(prefix='tagihnet-business-browser-') as temporary:
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}';password='Isolated-business-browser-2026!'
    env={**os.environ,'TAGIHNET_DB_ENGINE':'sqlite','TAGIHNET_DATA_DIR':temporary,'TAGIHNET_DEMO_PASSWORD':password,'TAGIHNET_DEBUG':'1','TAGIHNET_SIMULATION':'1'}
    for args in (('migrate','--noinput'),('seed_demo',)):
        subprocess.run([sys.executable,'manage.py',*args],cwd=ROOT,env=env,check=True,stdout=subprocess.DEVNULL)
    fixture="""
from django.contrib.auth import get_user_model
from core.models import Membership
owner=get_user_model().objects.get(username='owner')
sales=get_user_model().objects.create_user(username='business-sales',password='Isolated-business-browser-2026!')
Membership.objects.create(user=sales,tenant=owner.membership.tenant,role='sales')
"""
    subprocess.run([sys.executable,'manage.py','shell','-c',fixture],cwd=ROOT,env=env,check=True,stdout=subprocess.DEVNULL)
    database=Path(temporary)/'tagihnet.sqlite3'
    def scalar(query,values=()):
        with sqlite3.connect(database) as db:return db.execute(query,values).fetchone()[0]
    tenant=scalar("SELECT tenant_id FROM core_membership m JOIN auth_user u ON u.id=m.user_id WHERE u.username='owner'")
    owner_id=scalar("SELECT m.id FROM core_membership m JOIN auth_user u ON u.id=m.user_id WHERE u.username='owner'")
    sales_id=scalar("SELECT m.id FROM core_membership m JOIN auth_user u ON u.id=m.user_id WHERE u.username='business-sales'")
    customer=scalar('SELECT id FROM core_customer WHERE tenant_id=? ORDER BY id LIMIT 1',(tenant,))
    plan=scalar('SELECT id FROM core_plan WHERE tenant_id=? ORDER BY id LIMIT 1',(tenant,))
    server=subprocess.Popen([sys.executable,'manage.py','runserver',f'127.0.0.1:{port}','--noreload'],cwd=ROOT,env=env,
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.STDOUT)
    try:
        with sync_playwright() as driver:
            browser=driver.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH') or ('/usr/bin/chromium' if Path('/usr/bin/chromium').exists() else None),headless=True)
            context=browser.new_context(viewport={'width':1366,'height':1000},reduced_motion='reduce');page=context.new_page()
            external=set();errors=[]
            context.on('request',lambda req:external.add(urlsplit(req.url).hostname) if urlsplit(req.url).hostname not in (None,'127.0.0.1','localhost') else None)
            page.on('pageerror',lambda error:errors.append(type(error).__name__))
            for _ in range(200):
                try:
                    if context.request.get(base+'/health/').ok:break
                except Exception:time.sleep(.1)
            else:raise RuntimeError('Temporary server not ready')
            def enroll(p,username):
                p.goto(base+'/login/');p.get_by_label('Username',exact=True).fill(username);p.get_by_label('Kata sandi',exact=True).fill(password)
                p.get_by_role('button',name='Masuk ke workspace').click();p.wait_for_url(base+'/mfa/setup/')
                secret=p.locator('.provisioning-secret').text_content().strip()
                p.get_by_label('Kode authenticator (6 digit)').fill(pyotp.TOTP(secret).now());p.get_by_role('button',name='Aktifkan MFA',exact=True).click()
                p.get_by_role('link',name='Saya sudah menyimpan kode').click()
            def action(name,values=None,pk=None,p=None):
                p=p or page;p.goto(base+'/business/actions/'+name+'/'+(str(pk)+'/' if pk else ''))
                for field,value in (values or {}).items():
                    control=p.locator('[name="'+field+'"]')
                    tag=control.evaluate('(element)=>element.tagName')
                    if tag=='SELECT':control.select_option(str(value))
                    elif control.get_attribute('type')=='checkbox':control.set_checked(bool(value))
                    else:control.fill(str(value))
                p.get_by_role('button',name='Tinjau perubahan').click()
                expect(p.get_by_role('heading',name='Rincian perubahan',exact=True)).to_be_visible()
                p.get_by_label('Saya telah memeriksa rincian',exact=False).check()
                p.get_by_role('button',name='Konfirmasi pencatatan lokal',exact=True).click()
                expect(p.get_by_text('Pencatatan lokal tersimpan.',exact=True)).to_be_visible()
            phase='owner_mfa';enroll(page,'owner')
            phase='fund_cash'
            action('fund_create',{'name':'Kas browser','kind':'cash','active':True})
            fund=scalar("SELECT id FROM core_fund WHERE tenant_id=? AND name='Kas browser'",(tenant,))
            today=datetime.now(ZoneInfo('Asia/Jakarta')).date().isoformat()
            action('cash_post',{'fund':fund,'kind':'funding','amount':1000000,'business_date':today,'note':'Saldo awal browser fiktif'})
            action('cash_post',{'fund':fund,'kind':'expense','amount':25000,'business_date':today,'note':'Pengeluaran browser fiktif'})
            phase='team_payroll'
            action('employee_create',{'name':'Anggota browser','department':'Operasi','member':owner_id,'active':True})
            employee=scalar('SELECT id FROM core_employee WHERE tenant_id=?',(tenant,))
            action('salary',{'base':100000,'allowance':20000,'deduction':10000},employee)
            action('payroll_create',{'period':today[:7]})
            payroll=scalar('SELECT id FROM core_payrollrun WHERE tenant_id=?',(tenant,))
            action('payroll_approve',pk=payroll);action('payroll_pay',{'fund':fund},payroll)
            page.goto(base+'/account/payslips/');expect(page.get_by_text('Anggota browser',exact=True)).to_be_visible()
            phase='lead_conversion'
            action('lead_create',{'name':'Prospek browser','phone':'081298760001','username':'browser-prospect','address':'Jl. Browser','area':'Bandung','plan':plan,'status':'qualified','assigned_to':sales_id,'commission_amount':5000})
            lead=scalar('SELECT id FROM core_lead WHERE tenant_id=?',(tenant,))
            sales_context=browser.new_context(viewport={'width':360,'height':1000});sales_page=sales_context.new_page();enroll(sales_page,'business-sales')
            action('lead_convert',pk=lead,p=sales_page)
            assert sales_context.request.get(base+'/business/payroll/').status==403
            assert scalar('SELECT count(*) FROM core_installation WHERE tenant_id=?',(tenant,))==1
            phase='hotspot'
            action('hotspot_create',{'name':'Voucher browser','speed_mbps':10,'duration_minutes':60,'activate_within_days':7,'price':5000,'active':True})
            profile=scalar('SELECT id FROM core_hotspotprofile WHERE tenant_id=?',(tenant,))
            action('voucher_sell',{'profile':profile,'quantity':2,'fund':fund})
            batch=scalar('SELECT id FROM core_voucherbatch WHERE tenant_id=?',(tenant,));voucher=scalar('SELECT id FROM core_voucher WHERE batch_id=? ORDER BY id LIMIT 1',(batch,))
            page.goto(base+f'/business/hotspot/batch/{batch}/')
            with page.expect_download() as download:page.get_by_role('button',name='Unduh PDF voucher simulasi').click()
            assert Path(download.value.path()).read_bytes().startswith(b'%PDF')
            action('voucher_activate',pk=voucher)
            action('voucher_refund',{'reason':'Koreksi penjualan fiktif browser'},batch)
            assert scalar('SELECT count(*) FROM core_voucher WHERE batch_id=? AND revoked=1',(batch,))==2
            phase='topology'
            action('node_create',{'name':'ODC browser','kind':'odc','capacity':4,'active':True})
            odc=scalar("SELECT id FROM core_networknode WHERE tenant_id=? AND kind='odc'",(tenant,))
            action('node_create',{'name':'ODP browser','kind':'odp','parent':odc,'capacity':8,'latitude':'-6.917464','longitude':'107.619123','active':True})
            odp=scalar("SELECT id FROM core_networknode WHERE tenant_id=? AND kind='odp'",(tenant,))
            action('endpoint_set',{'node':odp,'port':1},customer)
            page.goto(base+f'/business/topology/node/{odp}/');expect(page.get_by_role('heading',name='Port terpakai')).to_be_visible()
            phase='campaign'
            action('campaign_create',{'title':'Promo browser','description':'Penawaran fiktif untuk pengujian.','contact':'081298760002','plan':plan,'starts':today,'ends':today,'published':True})
            public_key=scalar('SELECT public_key FROM core_campaign WHERE tenant_id=?',(tenant,))
            public_key=str(__import__('uuid').UUID(public_key))
            public=browser.new_context();promo=public.new_page();promo.goto(base+f'/promo/{public_key}/')
            expect(promo.get_by_role('heading',name='Promo browser',exact=True)).to_be_visible()
            assert 'staging-owner' not in promo.content()
            phase='responsive'
            for module in ('cash','team','payroll','sales','hotspot','topology'):
                page.goto(base+'/business/'+module+'/')
                for width in (360,768,1366,1920):
                    page.set_viewport_size({'width':width,'height':1000})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),module
            page.get_by_role('button',name='Mode gelap').click();expect(page.locator('html')).to_have_attribute('data-theme','dark')
            assert not external and not errors
            browser.close()
        print('PASS: real MFA/review/confirmation, cash/team/payroll/own payslip/sales conversion/scope/voucher private PDF/activation/refund/topology port/public promo;6 modules ×4 widths/dark/no external requests; disposable data only.',flush=True)
    except Exception as error:raise SystemExit('Business browser failed at '+phase+': '+type(error).__name__+'; no diagnostic/factors printed.') from None
    finally:
        server.terminate()
        try:server.wait(timeout=5)
        except subprocess.TimeoutExpired:server.kill();server.wait(timeout=5)
