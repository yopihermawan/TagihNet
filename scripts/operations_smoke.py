"""Owner -> technician -> portal workflow on a temporary database/server only."""
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import pyotp
from playwright.sync_api import sync_playwright, expect

root = Path(__file__).resolve().parent.parent
evidence = root / "docs/evidence"
password = "Isolated-operations-password-2026!"
with tempfile.TemporaryDirectory(prefix="tagihnet-operations-") as directory:
    env = {**os.environ, "TAGIHNET_DATA_DIR": directory, "TAGIHNET_DEMO_PASSWORD": password,
           "TAGIHNET_DEBUG": "1", "TAGIHNET_SIMULATION": "1"}
    for args in [["migrate", "--noinput"], ["seed_demo"]]:
        subprocess.run([sys.executable, "manage.py", *args], cwd=root, env=env, check=True, stdout=subprocess.DEVNULL)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
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
                raise RuntimeError("Server uji operasional tidak siap.")
            failures = []

            def new_page(context):
                page = context.new_page()
                page.on("pageerror", lambda error: failures.append(str(error)))
                return page

            def login(page, username, enroll=False):
                page.goto(base + "/login/")
                page.get_by_label("Username", exact=True).fill(username)
                page.get_by_label("Kata sandi", exact=True).fill(password)
                page.get_by_role("button", name="Masuk ke workspace").click()
                page.wait_for_url(lambda url: "/login/" not in url)
                if enroll:
                    secret = page.locator('.provisioning-secret').text_content().strip()
                    page.get_by_label("Kode authenticator (6 digit)").fill(pyotp.TOTP(secret).at(time.time()-30))
                    page.get_by_role("button", name="Aktifkan MFA", exact=True).click()
                    expect(page.get_by_role("heading", name="Simpan kode pemulihan")).to_be_visible()
                    page.get_by_role("link", name="Saya sudah menyimpan kode").click()

            page = new_page(owner)
            login(page, "owner", enroll=True)
            page.goto(base + "/settings/access/members/new/")
            page.get_by_label("Username").fill("teknisi-uji")
            page.get_by_label("Email pemulihan").fill("teknisi-uji@example.invalid")
            page.get_by_label("Kata sandi awal").fill(password)
            page.get_by_label("Preset akun").select_option("technician")
            page.get_by_role("button", name="Simpan", exact=True).click()
            page.wait_for_url(base + "/settings/access/")

            def create_ticket(title, assigned):
                page.goto(base + "/operations/tickets/new/")
                page.get_by_label("Pelanggan", exact=False).first.select_option(index=1)
                page.get_by_label("Judul gangguan").fill(title)
                page.get_by_label("Penjelasan gangguan").fill("Koneksi terputus sejak pagi di lokasi fiktif.")
                page.get_by_label("Kategori").select_option("connection")
                page.get_by_label("Prioritas").select_option("high")
                if assigned:
                    page.get_by_label("Teknisi (opsional)").select_option(label="teknisi-uji")
                page.get_by_role("button", name="Buat tiket", exact=True).click()
                expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
                return page.url

            ticket_url = create_ticket("Internet lokasi fiktif terputus", True)
            page.get_by_label("Komentar", exact=False).fill("Catatan internal demo: rencana pemeriksaan jalur.")
            page.get_by_label("Catatan internal").check()
            page.get_by_role("button", name="Kirim komentar", exact=True).click()
            expect(page.get_by_text("Catatan internal demo: rencana pemeriksaan jalur.", exact=True)).to_be_visible()
            hidden_ticket = create_ticket("Tiket tanpa penugasan teknisi", False)
            technician = browser.new_context(viewport={"width": 1366, "height": 1000})
            field = new_page(technician)
            login(field, "teknisi-uji", enroll=True)
            field.wait_for_url(base + "/operations/tickets/")
            expect(field.get_by_text("Internet lokasi fiktif terputus", exact=True)).to_be_visible()
            expect(field.get_by_text("Tiket tanpa penugasan teknisi", exact=True)).to_have_count(0)
            assert technician.request.get(hidden_ticket).status == 404
            assert technician.request.get(base + "/customers/").status == 403
            assert technician.request.get(base + "/invoices/1/pdf/").status == 403
            field.goto(ticket_url)
            field.get_by_label("Status berikutnya").select_option("in_progress")
            field.get_by_label("Catatan penanganan").fill("Teknisi mulai memeriksa jalur pelanggan.")
            field.get_by_role("button", name="Simpan status tiket").click()
            expect(field.locator('.page-heading .eyebrow')).to_contain_text("DALAM PENANGANAN")
            assert "Rp" not in field.inner_text("main")
            field.screenshot(path=str(evidence / "technician-ticket-desktop.png"), full_page=True)

            page.goto(base + "/operations/installations/new/")
            page.get_by_label("Nama calon pelanggan").fill("Ruang Koneksi Fiktif")
            page.get_by_label("Telepon", exact=False).fill("081299900001")
            page.get_by_label("Username layanan").fill("psb-browser")
            page.get_by_label("Alamat pemasangan").fill("Jl. Pemasangan Fiktif No. 10")
            page.get_by_label("Wilayah").fill("Dago")
            page.get_by_label("Paket", exact=False).select_option(index=1)
            page.get_by_label("Teknisi (opsional saat prospek)").select_option(label="teknisi-uji")
            page.get_by_role("button", name="Buat PSB", exact=True).click()
            expect(page.get_by_role("heading", name="Ruang Koneksi Fiktif", exact=True)).to_be_visible()
            order_url = page.url
            field.goto(order_url)
            for status in ["survey", "scheduled", "installing", "installed"]:
                field.get_by_label("Tahap berikutnya").select_option(status)
                field.get_by_label("Catatan / alasan").fill("Tahap pemasangan telah diperiksa di lokasi fiktif.")
                if status == "scheduled":
                    schedule = datetime.now(ZoneInfo("Asia/Jakarta")) + timedelta(days=1)
                    field.get_by_label("Jadwal pemasangan").fill(schedule.strftime("%Y-%m-%dT%H:%M"))
                field.get_by_role("button", name="Simpan tahap PSB").click()
            expect(field.locator('.page-heading .eyebrow')).to_contain_text("TERPASANG")
            assert technician.request.get(order_url + "activate/").status == 403
            assert "Rp" not in field.inner_text("main")
            page.goto(order_url)
            page.get_by_role("link", name="Tinjau aktivasi & faktur").click()
            now = datetime.now(ZoneInfo("Asia/Jakarta"))
            year, month = now.year + now.month // 12, now.month % 12 + 1
            page.get_by_label("Bulan layanan awal").fill(f"{year:04d}-{month:02d}")
            page.get_by_role("button", name="Tinjau periode", exact=True).click()
            quote_url = page.url
            quote = page.locator('input[name="quote"]').get_attribute('value')
            page.screenshot(path=str(evidence / "psb-activation-preview.png"), full_page=True)
            page.get_by_label("Saya telah meninjau identitas pelanggan").check()
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Aktivasi dan buat faktur awal").click()
            page.wait_for_url(order_url)
            expect(page.get_by_text("Menunggu provisioning", exact=False)).to_be_visible()
            csrf = page.locator('input[name="csrfmiddlewaretoken"]').first.get_attribute('value')
            assert owner.request.post(quote_url, form={"quote": quote, "reviewed": "on", "csrfmiddlewaretoken": csrf}).ok
            with sqlite3.connect(f"file:{Path(directory) / 'tagihnet.sqlite3'}?mode=ro", uri=True) as db:
                customer_id = db.execute("SELECT id FROM core_customer WHERE username='psb-browser'").fetchone()[0]
                assert db.execute("SELECT COUNT(*) FROM core_customer WHERE username='psb-browser'").fetchone()[0] == 1
                assert db.execute("SELECT COUNT(*),SUM(total) FROM core_invoice WHERE customer_id=?", (customer_id,)).fetchone() == (1, 150000)
                assert db.execute("SELECT COUNT(*) FROM core_networkjob WHERE customer_id=?", (customer_id,)).fetchone()[0] == 1
            subprocess.run([sys.executable, "manage.py", "run_simulation_worker", "--once"], cwd=root, env=env, check=True, stdout=subprocess.DEVNULL)
            page.reload()
            expect(page.get_by_text("Status layanan: Aktif (simulasi).", exact=False)).to_be_visible()
            page.screenshot(path=str(evidence / "psb-activated-desktop.png"), full_page=True)

            customer = browser.new_context(viewport={"width": 360, "height": 900})
            portal = new_page(customer)
            login(portal, "pelanggan")
            portal.wait_for_url(base + "/portal/")
            portal.goto(ticket_url)
            expect(portal.get_by_text("Catatan internal demo: rencana pemeriksaan jalur.", exact=True)).to_have_count(0)
            expect(portal.get_by_label("Catatan internal")).to_have_count(0)
            portal.get_by_label("Komentar").fill("Pelanggan sudah berada di lokasi, silakan lanjutkan pemeriksaan.")
            portal.get_by_role("button", name="Kirim komentar", exact=True).click()
            portal.screenshot(path=str(evidence / "customer-ticket-mobile.png"), full_page=True)
            field.goto(ticket_url)
            expect(field.get_by_text("Pelanggan sudah berada di lokasi, silakan lanjutkan pemeriksaan.", exact=True)).to_be_visible()
            field.get_by_label("Status berikutnya").select_option("resolved")
            field.get_by_label("Catatan penanganan").fill("Layanan telah pulih dan pengujian koneksi berhasil.")
            field.get_by_role("button", name="Simpan status tiket").click()
            page.goto(ticket_url)
            page.get_by_label("Status berikutnya").select_option("closed")
            page.get_by_label("Catatan penanganan").fill("Pelanggan telah mengonfirmasi penyelesaian gangguan.")
            page.get_by_role("button", name="Simpan status tiket").click()
            expect(page.locator('.page-heading .eyebrow')).to_contain_text("DITUTUP")
            page.screenshot(path=str(evidence / "ticket-closed-desktop.png"), full_page=True)
            for width in [360, 768, 1366, 1920]:
                page.set_viewport_size({"width": width, "height": 1000})
                for route in ["/operations/tickets/", "/operations/installations/", "/operations/tickets/new/", "/operations/installations/new/", "/operations/sla/"]:
                    page.goto(base + route)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, route)
                for url in [ticket_url, order_url]:
                    page.goto(url)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, url)
            subprocess.run([sys.executable, "scripts/verify_restore.py"], cwd=root, env=env, check=True, stdout=subprocess.DEVNULL)
            assert failures == [], failures
            browser.close()
        print("PASS: owner/technician/customer ticket lifecycle, private-note isolation, assigned scope, PSB progression, signed activation/replay, single customer/invoice/job, simulated provisioning, 4 viewports, restore state; no MFA factors in evidence.")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
