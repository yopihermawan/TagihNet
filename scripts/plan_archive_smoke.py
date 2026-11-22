"""Real catalog archive/reactivation UI on an isolated SQLite database and server."""
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

ROOT = Path(__file__).resolve().parent.parent
evidence = Path(os.environ.get("TAGIHNET_EVIDENCE_DIR", str(ROOT / "docs/evidence")))
evidence.mkdir(parents=True, exist_ok=True)
phase = "startup"
with tempfile.TemporaryDirectory(prefix="tagihnet-plan-archive-browser-") as temporary:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0)); port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    password = "Isolated-catalog-browser-passphrase-2026!"
    env = {**os.environ, "TAGIHNET_DB_ENGINE":"sqlite", "TAGIHNET_DATA_DIR":temporary,
        "TAGIHNET_DEMO_PASSWORD":password, "TAGIHNET_DEBUG":"1", "TAGIHNET_SIMULATION":"1"}
    for args in (("migrate", "--noinput"), ("seed_demo",)):
        subprocess.run([sys.executable,"manage.py",*args],cwd=ROOT,env=env,check=True,stdout=subprocess.DEVNULL)
    database = Path(temporary)/"tagihnet.sqlite3"
    def business_digest():
        with sqlite3.connect(database) as db:
            return {table:hashlib.sha256(repr(db.execute('SELECT * FROM '+table+' ORDER BY id').fetchall()).encode()).hexdigest()
                for table in ('core_customer','core_invoice','core_payment','core_paymentreversal')}
    with sqlite3.connect(database) as db:
        plan_id = db.execute("SELECT id FROM core_plan WHERE name='Rumah Hemat' ORDER BY id LIMIT 1").fetchone()[0]
        customer_id = db.execute('SELECT id FROM core_customer WHERE plan_id=? ORDER BY id LIMIT 1',(plan_id,)).fetchone()[0]
    before = business_digest()
    server = subprocess.Popen([sys.executable,"manage.py","runserver",f"127.0.0.1:{port}","--noreload"],
        cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.STDOUT)
    try:
        with sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=os.getenv("CHROMIUM_PATH") or
                ("/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None),headless=True)
            context = browser.new_context(viewport={"width":1366,"height":1000},reduced_motion="reduce")
            page = context.new_page(); failures=[]
            page.on("pageerror",lambda error:failures.append(type(error).__name__))
            for attempt in range(200):
                try:
                    if context.request.get(base+'/health/').ok:break
                except Exception:time.sleep(.1)
            else:raise RuntimeError("Temporary server not ready")
            phase="enrollment"
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
            phase="archive_review"
            page.goto(base+'/plans/')
            page.get_by_role('link',name='Arsipkan paket Rumah Hemat',exact=True).click()
            expect(page.get_by_role('heading',name='Tinjau pengarsipan paket')).to_be_visible()
            token=page.locator('input[name="token"]').input_value()
            csrf=page.locator('input[name="csrfmiddlewaretoken"]').first.input_value()
            for width in (360,768,1366,1920):
                page.set_viewport_size({"width":width,"height":1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                if width in (360,1366):page.screenshot(path=str(evidence/('plan-archive-mobile.png' if width==360 else 'plan-archive-desktop.png')),full_page=True)
            page.get_by_role('button',name='Mode gelap').click()
            expect(page.locator('html')).to_have_attribute('data-theme','dark')
            page.get_by_role('button',name='Mode gelap').click()
            assert not page.get_by_role('checkbox').is_checked()
            page.get_by_role('checkbox').check()
            page.get_by_role('button',name='Konfirmasi pengarsipan paket',exact=True).click()
            page.wait_for_url(lambda url:'availability=archived' in url)
            expect(page.get_by_text('Diarsipkan',exact=True)).to_be_visible()
            stale=context.request.post(base+'/plans/availability/confirm/',form={'token':token,'reviewed':'on','csrfmiddlewaretoken':csrf},
                headers={'Origin':base,'Referer':base+'/plans/'})
            assert stale.status==400
            phase="assignment_guards"
            for path in ('/customers/new/','/operations/installations/new/'):
                page.goto(base+path)
                assert page.locator(f'select[name="plan"] option[value="{plan_id}"]').count()==0
            page.goto(base+f'/customers/{customer_id}/edit/')
            expect(page.locator('select[name="plan"]')).to_have_value(str(plan_id))
            expect(page.locator(f'select[name="plan"] option[value="{plan_id}"]')).to_have_text('Rumah Hemat · 15 Mbps · Diarsipkan')
            phase="reactivation"
            page.goto(base+'/plans/?availability=archived')
            page.get_by_role('link',name='Aktifkan kembali paket Rumah Hemat',exact=True).click()
            expect(page.get_by_role('heading',name='Tinjau pengaktifan paket')).to_be_visible()
            page.get_by_role('checkbox').check()
            page.get_by_role('button',name='Konfirmasi pengaktifan paket',exact=True).click()
            page.wait_for_url(lambda url:'availability=active' in url)
            expect(page.get_by_role('link',name='Arsipkan paket Rumah Hemat',exact=True)).to_be_visible()
            for width in (360,768,1366,1920):
                page.set_viewport_size({"width":width,"height":1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.goto(base+'/customers/new/')
            assert page.locator(f'select[name="plan"] option[value="{plan_id}"]').count()==1
            assert business_digest()==before and not failures
            browser.close()
        print('PASS: isolated real MFA/catalog archive/reactivate/replay/assignment guards, unchanged customers/ledger, dark mode and4 viewports; no factors saved.',flush=True)
    except Exception as error:
        raise SystemExit('Plan archive browser failed at '+phase+': '+type(error).__name__+'; no diagnostic/factors printed.') from None
    finally:
        server.terminate()
        try:server.wait(timeout=5)
        except subprocess.TimeoutExpired:server.kill();server.wait(timeout=5)
