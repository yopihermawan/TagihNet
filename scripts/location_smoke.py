"""Coordinates/assignment/activation Chromium flow, own fictional DB and no map traffic."""
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pyotp
from playwright.sync_api import expect, sync_playwright

ROOT=Path(__file__).resolve().parent.parent
evidence=Path(os.environ.get('TAGIHNET_EVIDENCE_DIR',str(ROOT/'docs/evidence')))
evidence.mkdir(parents=True,exist_ok=True)
phase='startup'
with tempfile.TemporaryDirectory(prefix='tagihnet-location-browser-') as temporary:
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    password='Isolated-location-browser-2026!'
    env={**os.environ,'TAGIHNET_DB_ENGINE':'sqlite','TAGIHNET_DATA_DIR':temporary,
        'TAGIHNET_DEMO_PASSWORD':password,'TAGIHNET_DEBUG':'1','TAGIHNET_SIMULATION':'1'}
    for args in (('migrate','--noinput'),('seed_demo',)):
        subprocess.run([sys.executable,'manage.py',*args],cwd=ROOT,env=env,check=True,stdout=subprocess.DEVNULL)
    fixture="""
import json
from django.contrib.auth import get_user_model
from core.tests.test_operations_workflows import technician,installed_order
owner=get_user_model().objects.get(username='owner');tenant=owner.membership.tenant
tech=technician(tenant,'geo-tech');tech.user.set_password('Isolated-location-browser-2026!');tech.user.save(update_fields=['password'])
other=technician(tenant,'geo-other')
order=installed_order(owner,tech,92);hidden=installed_order(owner,other,93)
print('LOCATION:'+json.dumps({'order':order.pk,'hidden':hidden.pk,'customer':tenant.customer_set.order_by('pk').first().pk}))
"""
    result=subprocess.run([sys.executable,'manage.py','shell','-c',fixture],cwd=ROOT,env=env,check=True,text=True,capture_output=True)
    ids=json.loads(next(line.removeprefix('LOCATION:') for line in result.stdout.splitlines() if line.startswith('LOCATION:')))
    database=Path(temporary)/'tagihnet.sqlite3'
    def ledger():
        with sqlite3.connect(database) as db:
            return {table:hashlib.sha256(repr(db.execute('SELECT * FROM '+table+' ORDER BY id').fetchall()).encode()).hexdigest()
                for table in ('core_invoice','core_invoiceline','core_payment','core_paymentreversal','core_networkjob')}
    before=ledger()
    server=subprocess.Popen([sys.executable,'manage.py','runserver',f'127.0.0.1:{port}','--noreload'],
        cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.STDOUT)
    try:
        with sync_playwright() as driver:
            browser=driver.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH') or
                ('/usr/bin/chromium' if Path('/usr/bin/chromium').exists() else None),headless=True)
            owner=browser.new_context(viewport={'width':1366,'height':1000},reduced_motion='reduce')
            page=owner.new_page();failures=[];external=set()
            def observe(p):
                p.on('pageerror',lambda error:failures.append(type(error).__name__))
                p.on('request',lambda request:external.add(urlsplit(request.url).hostname) if urlsplit(request.url).hostname not in (None,'127.0.0.1','localhost') else None)
            observe(page)
            for _ in range(200):
                try:
                    if owner.request.get(base+'/health/').ok:break
                except Exception:time.sleep(.1)
            else:raise RuntimeError('Temporary server not ready')
            def enroll(p,username):
                p.goto(base+'/login/');p.get_by_label('Username',exact=True).fill(username)
                p.get_by_label('Kata sandi',exact=True).fill(password)
                p.get_by_role('button',name='Masuk ke workspace').click();p.wait_for_url(base+'/mfa/setup/')
                secret=p.locator('.provisioning-secret').text_content().strip()
                p.get_by_label('Kode authenticator (6 digit)').fill(pyotp.TOTP(secret).now())
                p.get_by_role('button',name='Aktifkan MFA',exact=True).click()
                expect(p.get_by_role('heading',name='Simpan kode pemulihan')).to_be_visible()
                p.get_by_role('link',name='Saya sudah menyimpan kode').click()
            phase='owner_enrollment';enroll(page,'owner')
            phase='customer_location'
            page.goto(base+f"/customers/{ids['customer']}/?tab=location")
            expect(page.get_by_text('Koordinat belum diisi.',exact=True)).to_be_visible()
            page.get_by_role('link',name='Ubah koordinat pelanggan',exact=True).click()
            token=page.locator('input[name="token"]').input_value()
            csrf=page.locator('input[name="csrfmiddlewaretoken"]').first.input_value()
            page.get_by_label('Lintang (latitude)',exact=True).fill('-6.917464')
            page.get_by_role('button',name='Simpan koordinat',exact=True).click()
            expect(page.get_by_text('Isi lintang dan bujur bersama, atau kosongkan keduanya.',exact=True)).to_be_visible()
            page.get_by_label('Bujur (longitude)',exact=True).fill('107.619123')
            page.get_by_role('button',name='Simpan koordinat',exact=True).click()
            page.wait_for_url(base+f"/customers/{ids['customer']}/?tab=location")
            expect(page.get_by_text('-6.917464',exact=True)).to_be_visible()
            link=page.get_by_role('link',name='Buka koordinat di peta',exact=False)
            assert urlsplit(link.get_attribute('href')).hostname=='www.openstreetmap.org'
            stale=owner.request.post(base+f"/customers/{ids['customer']}/location/",form={'token':token,'latitude':'1','longitude':'2','csrfmiddlewaretoken':csrf},
                headers={'Origin':base,'Referer':base+'/'})
            assert stale.status==409 and ledger()==before
            for width in (360,768,1366,1920):
                page.set_viewport_size({'width':width,'height':1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                if width in (360,1366):page.screenshot(path=str(evidence/('customer-location-mobile.png' if width==360 else 'customer-location-desktop.png')),full_page=True)
            page.get_by_role('button',name='Ganti mode terang atau gelap').click()
            expect(page.locator('html')).to_have_attribute('data-theme','dark')
            page.get_by_role('button',name='Ganti mode terang atau gelap').click()
            phase='psb_old_quote'
            order_url=base+f"/operations/installations/{ids['order']}/"
            page.goto(order_url+'activate/')
            old_quote=page.locator('input[name="quote"]').input_value()
            owner_csrf=page.locator('input[name="csrfmiddlewaretoken"]').first.input_value()
            phase='technician_location'
            technician=browser.new_context(viewport={'width':360,'height':1000},reduced_motion='reduce')
            field=technician.new_page();observe(field);enroll(field,'geo-tech')
            assert technician.request.get(base+f"/operations/installations/{ids['hidden']}/location/").status==404
            assert technician.request.get(base+f"/customers/{ids['customer']}/location/").status==403
            field.goto(order_url);field.get_by_role('link',name='Ubah koordinat PSB',exact=True).click()
            field.get_by_label('Lintang (latitude)',exact=True).fill('-6.900001')
            field.get_by_label('Bujur (longitude)',exact=True).fill('107.600001')
            field.get_by_role('button',name='Simpan koordinat',exact=True).click();field.wait_for_url(order_url)
            assert ledger()==before
            stale=owner.request.post(order_url+'activate/',form={'quote':old_quote,'reviewed':'on','csrfmiddlewaretoken':owner_csrf},headers={'Origin':base,'Referer':order_url})
            assert 'Muat ulang pratinjau aktivasi' in stale.text()
            assert ledger()==before
            phase='activation_copy'
            page.reload();expect(page.get_by_text('-6.900001',exact=True)).to_be_visible()
            page.get_by_label('Saya telah meninjau identitas pelanggan',exact=False).check()
            page.once('dialog',lambda dialog:dialog.accept())
            page.get_by_role('button',name='Aktivasi dan buat faktur awal').click();page.wait_for_url(order_url)
            with sqlite3.connect(database) as db:
                row=db.execute('SELECT c.latitude,c.longitude,i.total,o.status FROM core_installation o JOIN core_customer c ON o.customer_id=c.id JOIN core_invoice i ON o.invoice_id=i.id WHERE o.id=?',(ids['order'],)).fetchone()
            assert tuple(Decimal(str(v)).quantize(Decimal('.000001')) for v in row[:2])==(Decimal('-6.900001'),Decimal('107.600001'))
            assert row[2:]==(150000,'activated')
            assert technician.request.get(order_url+'location/').status==403
            assert not external and not failures
            browser.close()
        print('PASS: real MFA/customer coordinates/half-pair/stale409/fixed map link/no external requests, assigned PSB edit/scope/old quote refusal/activation copy,4 widths/dark; only fictitious data and no factors saved.',flush=True)
    except Exception as error:
        raise SystemExit('Location browser failed at '+phase+': '+type(error).__name__+'; no diagnostic/factors printed.') from None
    finally:
        server.terminate()
        try:server.wait(timeout=5)
        except subprocess.TimeoutExpired:server.kill();server.wait(timeout=5)
