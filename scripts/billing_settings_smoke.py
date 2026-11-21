"""Reviewed billing settings through real login/MFA on an owned disposable instance."""
import hashlib
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pyotp
from playwright.sync_api import expect, sync_playwright

ROOT=Path(__file__).resolve().parent.parent
evidence=Path(os.environ.get('TAGIHNET_EVIDENCE_DIR',str(ROOT/'docs/evidence')))
evidence.mkdir(parents=True,exist_ok=True)
phase='startup'
with tempfile.TemporaryDirectory(prefix='tagihnet-billing-settings-browser-') as temporary:
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    password='Isolated-billing-settings-browser-2026!'
    env={**os.environ,'TAGIHNET_DB_ENGINE':'sqlite','TAGIHNET_DATA_DIR':temporary,
        'TAGIHNET_DEMO_PASSWORD':password,'TAGIHNET_DEBUG':'1','TAGIHNET_SIMULATION':'1',
        'TAGIHNET_SECRET_KEY':'','TAGIHNET_MFA_KEY':''}
    for args in (('migrate','--noinput'),('seed_demo',)):
        subprocess.run([sys.executable,'manage.py',*args],cwd=ROOT,env=env,check=True,stdout=subprocess.DEVNULL)
    database=Path(temporary)/'tagihnet.sqlite3'
    def ledger():
        with sqlite3.connect(database) as db:
            return {table:hashlib.sha256(repr(db.execute('SELECT * FROM '+table+' ORDER BY id').fetchall()).encode()).hexdigest()
                for table in ('core_customer','core_plan','core_invoice','core_invoiceline','core_payment','core_paymentreversal','core_networkjob')}
    before=ledger()
    with sqlite3.connect(database) as db:
        tenant_id=db.execute("SELECT tenant_id FROM core_membership m JOIN auth_user u ON m.user_id=u.id WHERE u.username='owner'").fetchone()[0]
        invoice_id=db.execute('SELECT id FROM core_invoice WHERE tenant_id=? ORDER BY id LIMIT 1',(tenant_id,)).fetchone()[0]
        original_issuer=db.execute('SELECT name FROM core_invoiceissuersnapshot WHERE invoice_id=?',(invoice_id,)).fetchone()[0]
    server=subprocess.Popen([sys.executable,'manage.py','runserver',f'127.0.0.1:{port}','--noreload'],
        cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.STDOUT)
    try:
        with sync_playwright() as driver:
            browser=driver.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH') or
                ('/usr/bin/chromium' if Path('/usr/bin/chromium').exists() else None),headless=True)
            context=browser.new_context(viewport={'width':1366,'height':1000},reduced_motion='reduce')
            page=context.new_page();failures=[]
            page.on('pageerror',lambda error:failures.append(type(error).__name__))
            for _ in range(200):
                try:
                    if context.request.get(base+'/health/').ok:break
                except Exception:time.sleep(.1)
            else:raise RuntimeError('Temporary server not ready')
            phase='enrollment'
            page.goto(base+'/login/')
            page.get_by_label('Username',exact=True).fill('owner')
            page.get_by_label('Kata sandi',exact=True).fill(password)
            page.get_by_role('button',name='Masuk ke workspace').click()
            page.wait_for_url(base+'/mfa/setup/')
            secret=page.locator('.provisioning-secret').text_content().strip()
            page.get_by_label('Kode authenticator (6 digit)').fill(pyotp.TOTP(secret).now())
            page.get_by_role('button',name='Aktifkan MFA',exact=True).click()
            expect(page.get_by_role('heading',name='Simpan kode pemulihan')).to_be_visible()
            page.get_by_role('link',name='Saya sudah menyimpan kode').click()
            phase='review'
            page.goto(base+'/settings/billing/')
            page.get_by_label('Bulan contoh').fill('2024-02')
            page.get_by_role('button',name='Lihat contoh',exact=True).click()
            settings_url=base+'/settings/billing/?period=2024-02'
            page.wait_for_url(settings_url)
            timeline=page.get_by_role('region',name='Linimasa tagihan')
            expect(timeline.get_by_text('Pengingat billing nonaktif.')).to_be_visible()
            expect(timeline.locator('time[datetime="2024-02-20"]')).to_be_visible()
            expect(timeline.locator('time[datetime="2024-02-21"]')).to_be_visible()
            old_form=page.locator('input[name="token"]').input_value()
            page.get_by_label('Nama ISP').fill('Nusantara Net Ditinjau')
            page.get_by_label('Alamat ISP').fill('Jl. Contoh Staging No. 19\nBandung · Data fiktif')
            page.get_by_label('Tanggal jatuh tempo (1–31)').fill('31')
            page.get_by_label('Tenggang isolir (hari)').fill('3')
            page.get_by_label('Pajak dalam basis poin (100 bp = 1%)').fill('1100')
            page.get_by_label('Footer faktur (teks biasa)').fill('Terima kasih.\nTagihNet · Data fiktif.')
            page.get_by_role('button',name='Tinjau perubahan',exact=True).click()
            expect(page.get_by_role('heading',name='Tinjau setelan billing',exact=True)).to_be_visible()
            expect(timeline.locator('time[datetime="2024-02-20"]')).to_be_visible()
            expect(timeline.locator('time[datetime="2024-02-29"]')).to_be_visible()
            expect(timeline.locator('time[datetime="2024-03-04"]')).to_be_visible()
            assert ledger()==before
            with sqlite3.connect(database) as db:
                assert db.execute('SELECT tax_bp FROM core_tenant WHERE id=?',(tenant_id,)).fetchone()[0]==0
            token=page.locator('input[name="token"]').input_value()
            csrf=page.locator('input[name="csrfmiddlewaretoken"]').first.input_value()
            sla=context.request.post(base+'/operations/sla/',form={'sla_low_hours':'72','sla_normal_hours':'24',
                'sla_high_hours':'8','sla_urgent_hours':'17','csrfmiddlewaretoken':csrf},
                headers={'Origin':base,'Referer':base+'/operations/sla/'})
            assert sla.status==200
            missing=context.request.post(base+'/settings/billing/confirm/',form={'token':token,'csrfmiddlewaretoken':csrf},headers={'Origin':base,'Referer':base+'/settings/billing/'})
            assert missing.status==400
            for width in (360,768,1366,1920):
                page.set_viewport_size({'width':width,'height':1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                if width in (360,1366):page.screenshot(path=str(evidence/('billing-settings-review-mobile.png' if width==360 else 'billing-settings-review-desktop.png')),full_page=True)
            page.get_by_role('button',name='Ganti mode terang atau gelap').click()
            expect(page.locator('html')).to_have_attribute('data-theme','dark')
            page.get_by_role('button',name='Ganti mode terang atau gelap').click()
            phase='confirmation'
            page.get_by_role('checkbox').check()
            page.get_by_role('button',name='Konfirmasi setelan billing',exact=True).click()
            page.wait_for_url(settings_url)
            expect(page.get_by_label('Nama ISP')).to_have_value('Nusantara Net Ditinjau')
            expect(page.get_by_label('Pajak dalam basis poin (100 bp = 1%)')).to_have_value('1100')
            expect(page.get_by_label('Tenggang isolir (hari)')).to_have_value('3')
            assert ledger()==before
            with sqlite3.connect(database) as db:
                assert db.execute('SELECT sla_urgent_hours FROM core_tenant WHERE id=?',(tenant_id,)).fetchone()[0]==17
                assert db.execute("SELECT count(*) FROM core_auditevent WHERE action='billing.settings_updated'").fetchone()[0]==1
            fields={'name':'Old form overwrite','address':'Alamat fiktif','due_day':'1','tax_bp':'0','invoice_footer':'Lama',
                'isolation_grace_days':'0','token':old_form,'csrfmiddlewaretoken':csrf}
            headers={'Origin':base,'Referer':base+'/settings/billing/'}
            assert context.request.post(base+'/settings/billing/',form=fields,headers=headers).status==409
            assert context.request.post(base+'/settings/billing/confirm/',form={'token':token,'reviewed':'on','csrfmiddlewaretoken':csrf},headers=headers).status==409
            phase='existing_invoice'
            page.goto(base+f'/invoices/{invoice_id}/')
            expect(page.locator('.invoice-paper').get_by_text(original_issuer,exact=False)).to_be_visible()
            assert ledger()==before and not failures
            browser.close()
        print('PASS: isolated real MFA/settings review, grace across leap-month boundary, saved grace, stale form/review409, concurrent SLA, unchanged ledger and issuer, dark mode and four widths.',flush=True)
    except Exception as error:
        raise SystemExit('Billing settings browser failed at '+phase+': '+type(error).__name__+'; no diagnostic/factors printed.') from None
    finally:
        server.terminate()
        try:server.wait(timeout=5)
        except subprocess.TimeoutExpired:server.kill();server.wait(timeout=5)
