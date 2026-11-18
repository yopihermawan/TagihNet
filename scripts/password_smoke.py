"""Own-password Chromium flow on its own disposable instance, including real authentication limits."""
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pyotp
from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parent.parent
evidence = Path(os.environ.get("TAGIHNET_EVIDENCE_DIR", str(root / "docs/evidence")))
evidence.mkdir(parents=True, exist_ok=True)
password = "Isolated-password-browser-original-2026!"
changed = "Changed-password-browser-passphrase-2026!"
phase = "startup"
with tempfile.TemporaryDirectory(prefix="tagihnet-password-browser-") as temporary:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0)); port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "TAGIHNET_DB_ENGINE": "sqlite", "TAGIHNET_DATA_DIR": temporary,
           "TAGIHNET_DEMO_PASSWORD": password, "TAGIHNET_DEBUG": "1", "TAGIHNET_SIMULATION": "1"}
    for args in (("migrate", "--noinput"), ("seed_demo",)):
        subprocess.run([sys.executable, "manage.py", *args], cwd=root, env=env, check=True, stdout=subprocess.DEVNULL)
    server = subprocess.Popen([sys.executable, "manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"],
        cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        with sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=os.getenv("CHROMIUM_PATH") or
                ("/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None), headless=True)
            context = browser.new_context(viewport={"width":1366,"height":1000}, reduced_motion="reduce")
            failures = []
            page = context.new_page(); page.on("pageerror", lambda error: failures.append(type(error).__name__))
            for attempt in range(200):
                try:
                    if context.request.get(base + "/health/").ok:break
                except Exception:time.sleep(.1)
            else:raise RuntimeError("Temporary server not ready")
            def login(current):
                page.goto(base + "/login/")
                page.get_by_label("Username",exact=True).fill("owner")
                page.get_by_label("Kata sandi",exact=True).fill(current)
                page.get_by_role("button",name="Masuk ke workspace").click()
                page.wait_for_url(lambda url: "/login/" not in url)
            phase = "enrollment"
            login(password)
            secret = page.locator('.provisioning-secret').text_content().strip()
            page.get_by_label("Kode authenticator (6 digit)").fill(pyotp.TOTP(secret).now())
            page.get_by_role("button",name="Aktifkan MFA",exact=True).click()
            expect(page.get_by_role("heading",name="Simpan kode pemulihan")).to_be_visible()
            codes = page.locator('.recovery-codes code').all_text_contents(); assert len(codes)==10
            page.get_by_role("link",name="Saya sudah menyimpan kode").click()
            old_context = browser.new_context(storage_state=context.storage_state())
            phase = "profile_form"
            page.locator('.sidebar').get_by_role("link",name="Profil saya",exact=True).click()
            expect(page.locator('.sidebar .nav-link.selected')).to_have_text("Profil saya")
            assert page.locator('input[name="email"]').count()==0
            profile_token=page.locator('input[name="token"]').input_value()
            csrf=page.locator('input[name="csrfmiddlewaretoken"]').first.input_value()
            for width in (360,768,1366,1920):
                page.set_viewport_size({"width":width,"height":1000})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                if width in (360,1366):
                    page.screenshot(path=str(evidence / ("account-profile-mobile.png" if width==360 else "account-profile-desktop.png")),full_page=True)
            page.get_by_role("button",name="Ganti mode terang atau gelap").click()
            expect(page.locator('html')).to_have_attribute('data-theme','dark')
            page.get_by_role("button",name="Ganti mode terang atau gelap").click()
            page.get_by_label("Nama depan atau panggilan:",exact=True).fill("Ayu Profil Fiktif")
            page.get_by_label("Nama belakang:",exact=True).fill("Utami")
            page.get_by_role("button",name="Simpan nama tampilan",exact=True).click()
            expect(page.get_by_text("Nama tampilan disimpan.",exact=True)).to_be_visible()
            expect(page.locator('.sidebar .account strong')).to_have_text("Ayu Profil Fiktif")
            page.reload()
            expect(page.get_by_label("Nama depan atau panggilan:",exact=True)).to_have_value("Ayu Profil Fiktif")
            stale=context.request.post(base+'/account/profile/',form={'token':profile_token,'first_name':'Older form',
                'last_name':'','csrfmiddlewaretoken':csrf},headers={'Origin':base,'Referer':base+'/account/profile/'})
            assert stale.status==409
            assert old_context.request.get(base+'/account/profile/',max_redirects=0).status==200
            phase = "password_form"
            page.goto(base + "/account/security/")
            page.get_by_role("link",name="Ubah kata sandi",exact=True).click()
            for width in (360,768,1366,1920):
                page.set_viewport_size({"width":width,"height":1000})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                if width in (360,1366):
                    page.screenshot(path=str(evidence / ("account-password-mobile.png" if width==360 else "account-password-desktop.png")),full_page=True)
            page.get_by_role("button",name="Ganti mode terang atau gelap").click()
            expect(page.locator('html')).to_have_attribute('data-theme','dark')
            page.get_by_role("button",name="Ganti mode terang atau gelap").click()
            def submit(current,code):
                page.get_by_label("Kata sandi saat ini:",exact=True).fill(current)
                page.get_by_label("Kata sandi baru:",exact=True).fill(changed)
                page.get_by_label("Ulangi kata sandi baru:",exact=True).fill(changed)
                page.get_by_label("Kode authenticator atau kode pemulihan:",exact=True).fill(code)
                page.get_by_role("button",name="Simpan sandi baru dan keluar",exact=True).click()
            phase = "wrong_proof"
            submit("incorrect-proof",codes[0])
            expect(page.get_by_text("Verifikasi tidak berhasil. Periksa kata sandi dan kode.",exact=True)).to_be_visible()
            assert page.get_by_label("Kata sandi baru:",exact=True).input_value()==""
            phase = "password_change"
            submit(password,codes[0]); page.wait_for_url(base + "/login/")
            assert old_context.request.get(base + "/",max_redirects=0).status==302
            phase = "new_login_and_replay"
            login(changed)
            page.screenshot(path=str(evidence / "mfa-challenge.png"),full_page=True)
            page.get_by_label("Kode authenticator atau kode pemulihan").fill(codes[0])
            page.get_by_role("button",name="Verifikasi dan masuk").click()
            expect(page.get_by_text("Kode tidak valid, telah dipakai, atau kedaluwarsa.",exact=True)).to_be_visible()
            page.get_by_label("Kode authenticator atau kode pemulihan").fill(codes[1])
            page.get_by_role("button",name="Verifikasi dan masuk").click()
            page.wait_for_url(base + "/")
            assert failures==[]
            browser.close()
        print("PASS: isolated browser profile allowlist/session retention/stale-form rejection and own password/current proof/recovery replay/revocation, dark mode and4 viewports; no factors saved.",flush=True)
    except Exception as error:
        raise SystemExit("Password browser failed at " + phase + ": " + type(error).__name__ + "; no diagnostic/factors printed.") from None
    finally:
        server.terminate()
        try:server.wait(timeout=5)
        except subprocess.TimeoutExpired:server.kill();server.wait(timeout=5)
