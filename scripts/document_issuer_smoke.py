"""Real login/MFA and historical/legacy HTML/PDF reprints on fictional disposable data."""
import hashlib
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from io import BytesIO
from pathlib import Path

import pyotp
from playwright.sync_api import expect, sync_playwright
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
evidence = Path(os.getenv('TAGIHNET_EVIDENCE_DIR', str(ROOT/'docs/evidence')))
evidence.mkdir(parents=True, exist_ok=True)

with tempfile.TemporaryDirectory(prefix='tagihnet-document-issuers-browser-') as temporary:
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    password = 'Disposable-document-browser-2026!'
    env = {**os.environ, 'TAGIHNET_DB_ENGINE':'sqlite', 'TAGIHNET_DATA_DIR':temporary,
           'TAGIHNET_DEMO_PASSWORD':password, 'TAGIHNET_DEBUG':'1', 'TAGIHNET_SIMULATION':'1',
           'TAGIHNET_SECRET_KEY':'', 'TAGIHNET_MFA_KEY':''}
    for args in (('migrate', '--noinput'), ('seed_demo',)):
        subprocess.run([sys.executable, 'manage.py', *args], cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
    fixture = '''
from datetime import date
import uuid
from django.contrib.auth import get_user_model
from core.billing import issue_invoice,record_payment
from core.models import Tenant,InvoiceIssuerSnapshot,PaymentIssuerSnapshot
actor=get_user_model().objects.get(username='owner');tenant=actor.membership.tenant
customer=tenant.customer_set.order_by('pk').first()
Tenant.objects.filter(pk=tenant.pk).update(name='ISP Faktur Fiktif',address='Alamat penerbit faktur fiktif')
invoice,_=issue_invoice(tenant,customer.pk,date(2040,1,1),actor)
Tenant.objects.filter(pk=tenant.pk).update(name='ISP Kuitansi Fiktif',address='Alamat penerbit kuitansi fiktif')
payment,_=record_payment(tenant,invoice.pk,10000,uuid.uuid4(),actor)
legacy,_=issue_invoice(tenant,customer.pk,date(2040,2,1),actor)
legacy_payment,_=record_payment(tenant,legacy.pk,10000,uuid.uuid4(),actor)
InvoiceIssuerSnapshot.objects.filter(invoice=legacy).delete()
PaymentIssuerSnapshot.objects.filter(payment=legacy_payment).delete()
Tenant.objects.filter(pk=tenant.pk).update(name='ISP Profil Fiktif',address='Alamat profil terkini fiktif')
'''
    subprocess.run([sys.executable, 'manage.py', 'shell', '-c', fixture], cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
    database = Path(temporary)/'tagihnet.sqlite3'
    with sqlite3.connect(database) as db:
        ids = dict(db.execute("SELECT period,id FROM core_invoice WHERE period IN ('2040-01-01','2040-02-01')"))
        payments = dict(db.execute('SELECT invoice_id,id FROM core_payment WHERE invoice_id IN (?,?)', tuple(ids.values())))

    def ledger():
        with sqlite3.connect(database) as db:
            return {table:hashlib.sha256(repr(db.execute('SELECT * FROM '+table+' ORDER BY id').fetchall()).encode()).hexdigest()
                    for table in ('core_tenant', 'core_invoice', 'core_invoiceline', 'core_payment',
                                  'core_invoiceissuersnapshot', 'core_paymentissuersnapshot', 'core_networkjob')}

    before = ledger()
    server = subprocess.Popen([sys.executable, 'manage.py', 'runserver', f'127.0.0.1:{port}', '--noreload'],
        cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        with sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH') or
                ('/usr/bin/chromium' if Path('/usr/bin/chromium').exists() else None), headless=True)
            context = browser.new_context(viewport={'width':1366, 'height':1000}, reduced_motion='reduce')
            page = context.new_page(); failures = []
            page.on('pageerror', lambda error:failures.append(type(error).__name__))
            for _ in range(200):
                try:
                    if context.request.get(base+'/health/').ok:break
                except Exception:time.sleep(.1)
            else:raise RuntimeError('Disposable server not ready')
            page.goto(base+'/login/')
            page.get_by_label('Username', exact=True).fill('owner')
            page.get_by_label('Kata sandi', exact=True).fill(password)
            page.get_by_role('button', name='Masuk ke workspace').click()
            page.wait_for_url(base+'/mfa/setup/')
            secret = page.locator('.provisioning-secret').text_content().strip()
            page.get_by_label('Kode authenticator (6 digit)').fill(pyotp.TOTP(secret).now())
            page.get_by_role('button', name='Aktifkan MFA', exact=True).click()
            expect(page.get_by_role('heading', name='Simpan kode pemulihan')).to_be_visible()
            page.get_by_role('link', name='Saya sudah menyimpan kode').click()
            for period, historical in (('2040-01-01', True), ('2040-02-01', False)):
                invoice = ids[period]; payment = payments[invoice]
                for path, paper, name in ((f'/invoices/{invoice}/', '.invoice-paper', 'ISP Faktur Fiktif'),
                                         (f'/invoices/{invoice}/payments/{payment}/receipt/', '.receipt-paper', 'ISP Kuitansi Fiktif')):
                    page.goto(base+path)
                    expected = name if historical else 'ISP Profil Fiktif'
                    expect(page.locator(paper).get_by_text(expected, exact=False)).to_be_visible()
                    notice = page.locator(paper).get_by_text('Identitas penerbit historis belum tersimpan;', exact=False)
                    expect(notice).to_have_count(0 if historical else 1)
                    for width in (360, 768, 1366, 1920):
                        page.set_viewport_size({'width':width, 'height':1000})
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    if historical and paper == '.receipt-paper':
                        page.screenshot(path=str(evidence/'document-issuer-receipt-desktop.png'), full_page=True)
                    if not historical and paper == '.invoice-paper':
                        page.set_viewport_size({'width':360, 'height':1000})
                        page.screenshot(path=str(evidence/'document-issuer-legacy-mobile.png'), full_page=True)
                    pdf_url = page.locator('.page-heading a.button').get_attribute('href')
                    if pdf_url is None:raise AssertionError('Print link missing')
                    response = context.request.get(base+pdf_url)
                    assert response.ok and response.headers['content-type'] == 'application/pdf'
                    pdf = PdfReader(BytesIO(response.body()))
                    text = ' '.join(' '.join(p.extract_text().split()) for p in pdf.pages)
                    assert expected in text
                    assert ('Identitas penerbit historis belum tersimpan;' in text) == (not historical)
                    assert all(not p.get('/Annots') for p in pdf.pages)
            assert ledger() == before and not failures
            browser.close()
        print('PASS: real MFA, independent invoice/payment issuer snapshots, legacy notices, HTML/PDF, four widths and unchanged business rows; disposable instance cleaned.', flush=True)
    except Exception as error:
        raise SystemExit('Document issuer browser failed: '+type(error).__name__+'; no diagnostics/factors printed.') from None
    finally:
        server.terminate(); server.wait(timeout=10)
