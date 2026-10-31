"""Isolated Chromium MFA + role verification, followed by the billing smoke test.

Creates a temporary database and local server. Never changes the current demo accounts.
Secrets exist only in process memory and the temporary private data directory.
"""
import os
import runpy
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import pyotp
from playwright.sync_api import sync_playwright, expect

root = Path(__file__).resolve().parent.parent
password = "Isolated-browser-password-2026!"
evidence = root / "docs/evidence"
with tempfile.TemporaryDirectory(prefix="tagihnet-security-") as temporary:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "TAGIHNET_DATA_DIR": temporary, "TAGIHNET_DEMO_PASSWORD": password,
           "TAGIHNET_DEBUG": "1", "TAGIHNET_SIMULATION": "1"}
    for command in [["migrate", "--noinput"], ["seed_demo"]]:
        subprocess.run([sys.executable, "manage.py", *command], cwd=root, env=env, check=True, stdout=subprocess.DEVNULL)
    server = subprocess.Popen([sys.executable, "manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"],
        cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        with sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=os.getenv("CHROMIUM_PATH") or
                ("/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None), headless=True)
            owner = browser.new_context(viewport={"width": 1366, "height": 1000})
            for attempt in range(200):
                try:
                    if owner.request.get(base + "/health/").ok:
                        break
                except Exception:
                    time.sleep(.1)
            else:
                raise RuntimeError("Server uji tidak siap.")
            page = owner.new_page()
            failures = []
            page.on("pageerror", lambda error: failures.append(str(error)))

            def password_step(target, username, secret_password):
                target.goto(base + "/login/")
                target.get_by_label("Username", exact=True).fill(username)
                target.get_by_label("Kata sandi", exact=True).fill(secret_password)
                target.get_by_role("button", name="Masuk ke workspace").click()
                target.wait_for_url(lambda url: "/login/" not in url)

            def enroll(target, username, secret_password):
                password_step(target, username, secret_password)
                expect(target.get_by_role("heading", name="Aktifkan authenticator")).to_be_visible()
                assert target.context.request.get(base + "/customers/", max_redirects=0).status == 302
                secret = target.locator('.provisioning-secret').text_content().strip()
                target.get_by_label("Kode authenticator (6 digit)").fill(pyotp.TOTP(secret).at(time.time() - 30))
                target.get_by_role("button", name="Aktifkan MFA", exact=True).click()
                expect(target.get_by_role("heading", name="Simpan kode pemulihan")).to_be_visible()
                codes = target.locator('.recovery-codes code').all_text_contents()
                assert len(codes) == 10
                target.get_by_role("link", name="Saya sudah menyimpan kode").click()
                return secret, codes

            owner_secret, owner_codes = enroll(page, "owner", password)
            page.goto(base + "/settings/access/roles/new/")
            page.get_by_label("Nama peran").fill("Pembaca pelanggan")
            page.get_by_label("Lihat pelanggan", exact=True).check()
            page.get_by_role("button", name="Simpan", exact=True).click()
            page.wait_for_url(base + "/settings/access/")
            page.get_by_role("link", name="Tambah akun").click()
            page.get_by_label("Username").fill("operator-uji")
            page.get_by_label("Email pemulihan").fill("operator-uji@example.invalid")
            page.get_by_label("Kata sandi awal").fill(password)
            page.get_by_label("Preset akun").select_option("ops")
            page.get_by_label("Peran kustom").select_option(label="Pembaca pelanggan")
            page.get_by_role("button", name="Simpan", exact=True).click()
            page.wait_for_url(base + "/settings/access/")
            page.screenshot(path=str(evidence / "access-desktop.png"), full_page=True)
            staff = browser.new_context()
            staff_page = staff.new_page()
            staff_secret, _ = enroll(staff_page, "operator-uji", password)
            staff_page.wait_for_url(base + "/customers/")
            for route in ["/invoices/", "/invoices/1/pdf/", "/settings/access/"]:
                assert staff.request.get(base + route).status == 403
            page.get_by_role("link", name="Ubah peran Pembaca pelanggan", exact=True).click()
            page.get_by_label("Lihat pelanggan", exact=True).uncheck()
            page.get_by_label("Lihat faktur dan PDF", exact=True).check()
            page.get_by_role("button", name="Simpan", exact=True).click()
            page.wait_for_url(base + "/settings/access/")
            assert staff.request.get(base + "/customers/", max_redirects=0).status == 302
            password_step(staff_page, "operator-uji", password)
            staff_page.get_by_label("Kode authenticator atau kode pemulihan").fill(pyotp.TOTP(staff_secret).now())
            staff_page.get_by_role("button", name="Verifikasi dan masuk").click()
            staff_page.wait_for_url(base + "/invoices/")
            assert staff.request.get(base + "/customers/").status == 403
            old_owner = browser.new_context(storage_state=owner.storage_state())
            page.goto(base + "/account/security/")
            page.screenshot(path=str(evidence / "account-security.png"), full_page=True)
            page.get_by_label("Kata sandi saat ini").fill(password)
            page.get_by_label("Kode authenticator atau kode pemulihan").fill(owner_codes[0])
            page.on("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Buat ulang kode pemulihan").click()
            expect(page.get_by_role("heading", name="Simpan kode pemulihan")).to_be_visible()
            new_codes = page.locator('.recovery-codes code').all_text_contents()
            assert set(new_codes).isdisjoint(owner_codes)
            assert old_owner.request.get(base + "/", max_redirects=0).status == 302
            page.get_by_role("link", name="Saya sudah menyimpan kode").click()
            for width in [360, 768, 1366, 1920]:
                page.set_viewport_size({"width": width, "height": 920})
                for route in ["/settings/access/", "/settings/access/roles/new/", "/settings/access/members/new/", "/account/security/"]:
                    page.goto(base + route)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, route)
                if width == 360:
                    page.goto(base + "/settings/access/")
                    page.screenshot(path=str(evidence / "access-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1366, "height": 1000})
            page.goto(base + "/account/security/")
            page.get_by_label("Kata sandi saat ini").fill(password)
            page.get_by_label("Kode authenticator atau kode pemulihan").fill(owner_codes[1])
            page.get_by_role("button", name="Verifikasi ulang", exact=True).click()
            expect(page.get_by_text("Verifikasi tidak berhasil. Periksa kata sandi dan kode.", exact=True)).to_be_visible()
            page.get_by_label("Kata sandi saat ini").fill(password)
            page.get_by_label("Kode authenticator atau kode pemulihan").fill(new_codes[0])
            page.get_by_role("button", name="Verifikasi ulang", exact=True).click()
            page.wait_for_url(base + "/settings/access/")
            page.get_by_role("button", name="Keluar dari akun").click()
            password_step(page, "owner", password)
            page.screenshot(path=str(evidence / "mfa-challenge.png"), full_page=True)
            assert failures == [], failures
            browser.close()
        # A fresh browser must perform the real password + MFA flow for the existing billing smoke.
        os.environ.update(TAGIHNET_TEST_URL=base, TAGIHNET_DEMO_PASSWORD=password, TAGIHNET_MFA_CODE=new_codes[1])
        runpy.run_path(str(root / "scripts/browser_smoke.py"), run_name="__main__")
        print("PASS: isolated MFA enrollment/login, custom-role creation/assignment, session revocation, recovery regeneration/rejection, 4 responsive viewports; no factors saved to evidence.")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
