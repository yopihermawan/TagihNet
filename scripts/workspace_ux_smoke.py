"""Grouped navigation and scoped drawer, real Chromium/MFA, fictional temporary DB only."""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit
import pyotp
from playwright.sync_api import expect, sync_playwright

ROOT=Path(__file__).resolve().parent.parent
evidence=Path(os.getenv('TAGIHNET_EVIDENCE_DIR',str(ROOT/'data/evidence/workspace-ux-browser')))
evidence.mkdir(parents=True,exist_ok=True)
phase='startup'
with tempfile.TemporaryDirectory(prefix='tagihnet-workspace-ux-') as temporary:
    with socket.socket() as listener:listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    base=f'http://127.0.0.1:{port}';password='Workspace-fictional-browser-2026!'
    env={**os.environ,'TAGIHNET_DB_ENGINE':'sqlite','TAGIHNET_DATA_DIR':temporary,
        'TAGIHNET_DEBUG':'1','TAGIHNET_SIMULATION':'1','TAGIHNET_DEMO_PASSWORD':password}
    for args in (('migrate','--noinput'),('seed_demo',)):
        subprocess.run([sys.executable,'manage.py',*args],cwd=ROOT,env=env,check=True,stdout=subprocess.DEVNULL)
    dbfile=Path(temporary)/'tagihnet.sqlite3'
    with sqlite3.connect(dbfile) as db:
        tenant=db.execute("SELECT tenant_id FROM core_membership m JOIN auth_user u ON u.id=m.user_id WHERE u.username='owner'").fetchone()[0]
        customers=db.execute('SELECT id,name FROM core_customer WHERE tenant_id=? ORDER BY name,id LIMIT 2',(tenant,)).fetchall()
    first,second=customers
    def ledger():
        with sqlite3.connect(dbfile) as db:
            tables=('core_customer','core_invoice','core_invoiceline','core_payment','core_paymentreversal','core_networkjob','core_fund','core_cashmovement','core_payrollrun','core_voucher','core_lead','core_customerendpoint')
            return {table:db.execute('SELECT * FROM '+table+' ORDER BY id').fetchall() for table in tables}
    before=ledger()
    server=subprocess.Popen([sys.executable,'manage.py','runserver',f'127.0.0.1:{port}','--noreload'],cwd=ROOT,env=env,
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.STDOUT)
    try:
        with sync_playwright() as driver:
            browser=driver.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH') or ('/usr/bin/chromium' if Path('/usr/bin/chromium').exists() else None),headless=True)
            owner=browser.new_context(viewport={'width':1366,'height':1000},reduced_motion='reduce');page=owner.new_page()
            errors=[];external=set()
            owner.on('request',lambda req:external.add(urlsplit(req.url).hostname) if urlsplit(req.url).hostname not in (None,'127.0.0.1','localhost') else None)
            page.on('pageerror',lambda error:errors.append(type(error).__name__))
            for _ in range(200):
                try:
                    if owner.request.get(base+'/health/').ok:break
                except Exception:time.sleep(.1)
            else:raise RuntimeError('Temporary server not ready')
            phase='mfa'
            page.goto(base+'/login/');page.get_by_label('Username',exact=True).fill('owner');page.get_by_label('Kata sandi',exact=True).fill(password)
            page.get_by_role('button',name='Masuk ke workspace').click();page.wait_for_url(base+'/mfa/setup/')
            secret=page.locator('.provisioning-secret').text_content().strip()
            page.get_by_label('Kode authenticator (6 digit)').fill(pyotp.TOTP(secret).now());page.get_by_role('button',name='Aktifkan MFA',exact=True).click()
            page.get_by_role('link',name='Saya sudah menyimpan kode').click()
            phase='navigation'
            finance=page.locator('[data-nav-group="finance"]');finance.locator('summary').click()
            expect(finance).not_to_have_attribute('open','')
            page.reload();expect(finance).not_to_have_attribute('open','')
            search=page.get_by_label('Cari menu navigasi',exact=True)
            search.fill('payroll');expect(page.get_by_role('link',name='Payroll',exact=True)).to_be_visible()
            search.fill('');expect(finance).not_to_have_attribute('open','')
            search.fill('keuangan');expect(page.get_by_role('link',name='Kas & dana',exact=True)).to_be_visible()
            search.fill('menu-yang-tidak-ada');expect(page.get_by_text('Menu tidak ditemukan.',exact=True)).to_be_visible()
            search.fill('');expect(page.get_by_text('Menu tidak ditemukan.',exact=True)).not_to_be_visible()
            phase='drawer'
            list_path='/customers/?size=25&sort=name'
            page.goto(base+list_path)
            dialog=page.get_by_role('dialog',name='Ringkasan cepat pelanggan',exact=True)
            first_link=page.get_by_role('link',name='Ringkasan '+first[1],exact=True)
            first_link.click();expect(dialog.get_by_role('heading',name=first[1],exact=True)).to_be_visible()
            expect(dialog.get_by_role('button',name='Tutup ringkasan pelanggan')).to_be_focused()
            page.keyboard.press('Shift+Tab');expect(dialog.get_by_role('button',name='Kembali ke daftar')).to_be_focused()
            page.keyboard.press('Tab');expect(dialog.get_by_role('button',name='Tutup ringkasan pelanggan')).to_be_focused()
            assert page.url==base+list_path
            page.screenshot(path=str(evidence/'customer-drawer-desktop.png'),full_page=False)
            page.keyboard.press('Escape');expect(dialog).not_to_be_visible();expect(first_link).to_be_focused()
            assert page.locator('#customer-preview-content').text_content()==''
            first_link.click();expect(dialog.get_by_role('heading',name=first[1],exact=True)).to_be_visible()
            page.mouse.click(300,400);expect(dialog).not_to_be_visible()
            phase='late_response'
            page.evaluate('''() => {
              window.workspaceOriginalFetch=window.fetch;
              window.fetch=(input,options)=>window.workspaceOriginalFetch(input,{...options,signal:undefined}).then(response=>new Promise(resolve=>setTimeout(()=>resolve(response),400)));
            }''')
            first_link.click();page.get_by_role('button',name='Tutup ringkasan pelanggan').click()
            page.get_by_role('link',name='Ringkasan '+second[1],exact=True).click()
            expect(dialog.get_by_role('heading',name=second[1],exact=True)).to_be_visible()
            expect(dialog.get_by_role('heading',name=first[1],exact=True)).not_to_be_visible()
            page.keyboard.press('Escape');page.evaluate('window.fetch=window.workspaceOriginalFetch')
            phase='error'
            endpoint=base+f'/customers/{first[0]}/preview/'
            page.route(endpoint,lambda route:route.fulfill(status=500,content_type='text/html',body='<script>bad()</script>PRIVATE ERROR'))
            first_link.click();expect(dialog.get_by_role('alert')).to_have_text('Ringkasan tidak dapat dimuat. Buka detail pelanggan untuk melanjutkan.')
            assert 'PRIVATE ERROR' not in dialog.text_content()
            page.keyboard.press('Escape');page.unroute(endpoint)
            phase='responsive'
            for width in (360,768,1366,1920):
                page.set_viewport_size({'width':width,'height':1000})
                first_link.click();expect(dialog.get_by_role('heading',name=first[1],exact=True)).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert dialog.evaluate('(element)=>element.scrollWidth<=element.clientWidth')
                if width==360:page.screenshot(path=str(evidence/'customer-drawer-mobile.png'),full_page=False)
                page.keyboard.press('Escape')
                if width<=850:
                    page.get_by_role('button',name='Buka atau tutup navigasi').click()
                    nav=page.get_by_role('dialog',name='Navigasi utama')
                    nav.get_by_label('Cari menu navigasi mobile',exact=True).fill('paket')
                    expect(nav.get_by_role('link',name='Paket layanan',exact=True)).to_be_visible()
                    nav.get_by_label('Cari menu navigasi mobile',exact=True).fill('')
                    for _ in range(18):
                        page.keyboard.press('Tab')
                        assert page.evaluate('document.querySelector("#sidebar").contains(document.activeElement)')
                    page.keyboard.press('Escape');expect(page.get_by_role('button',name='Buka atau tutup navigasi')).to_be_focused()
                    assert page.locator('.app').evaluate('(element)=>element.inert') is False
            page.get_by_role('button',name='Ganti mode terang atau gelap').click()
            first_link.click();expect(dialog.get_by_role('heading',name=first[1],exact=True)).to_be_visible()
            expect(page.locator('html')).to_have_attribute('data-theme','dark');page.keyboard.press('Escape')
            phase='detail_return'
            first_link.click();expect(dialog.get_by_role('heading',name=first[1],exact=True)).to_be_visible()
            dialog.get_by_role('link',name='Buka detail pelanggan',exact=True).click();page.wait_for_url(base+f'/customers/{first[0]}/')
            page.get_by_role('link',name='Kembali ke pelanggan',exact=False).click();page.wait_for_url(base+list_path)
            phase='storage_and_nojs'
            saved=owner.storage_state()
            private=browser.new_context(storage_state=saved)
            private.add_init_script("Storage.prototype.getItem=()=>{throw new Error('Disabled storage')};Storage.prototype.setItem=()=>{throw new Error('Disabled storage')}")
            private_page=private.new_page();private_page.goto(base+'/customers/')
            private_page.get_by_role('link',name='Ringkasan '+first[1],exact=True).click()
            expect(private_page.get_by_role('dialog').get_by_role('heading',name=first[1],exact=True)).to_be_visible();private.close()
            nojs=browser.new_context(storage_state=saved,java_script_enabled=False,viewport={'width':360,'height':1000})
            plain=nojs.new_page();plain.goto(base+'/customers/')
            expect(plain.get_by_role('link',name='Pelanggan & layanan',exact=True)).to_be_visible()
            plain.get_by_role('link',name='Ringkasan '+first[1],exact=True).click();plain.wait_for_url(base+f'/customers/{first[0]}/')
            expect(plain.get_by_role('heading',name='Ringkasan pelanggan',exact=True)).to_be_visible();nojs.close()
            phase='expired_session'
            subprocess.run([sys.executable,'manage.py','shell','-c',"from core.models import Membership; from django.db.models import F; Membership.objects.filter(user__username='owner').update(session_version=F('session_version')+1)"],cwd=ROOT,env=env,check=True,stdout=subprocess.DEVNULL)
            page.get_by_role('link',name='Ringkasan '+first[1],exact=True).click()
            expect(page.get_by_role('dialog').get_by_role('alert')).to_be_visible()
            assert not page.get_by_role('dialog').locator('input[type="password"]').count()
            assert ledger()==before and not external and not errors
            browser.close()
        print('PASS: grouped role-aware navigation/persistence/search/mobile focus; scoped drawer/open/close/Escape/backdrop/focus return/race/error/session expiry/canonical links/list URL;4 widths/dark/no storage/no JS; ledger unchanged, fictional data only.',flush=True)
    except Exception as error:
        line=traceback.extract_tb(error.__traceback__)[0].lineno
        raise SystemExit('Workspace UX browser failed at '+phase+': '+type(error).__name__+'; source line '+str(line)+'; no factors printed.') from None
    finally:
        server.terminate()
        try:server.wait(timeout=5)
        except subprocess.TimeoutExpired:server.kill();server.wait(timeout=5)
