"""Owner -> technician -> portal workflow on a temporary database/server only."""
import csv
import io
import uuid
from PIL import Image
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
    env = {**os.environ, "TAGIHNET_DB_ENGINE": "sqlite", "TAGIHNET_DATA_DIR": directory, "TAGIHNET_DEMO_PASSWORD": password,
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
            owner = browser.new_context(viewport={"width": 1366, "height": 1000}, reduced_motion="reduce")
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

            # Package review is effect-free; confirmation/replay uses the same owner MFA session.
            db_path = Path(directory) / "tagihnet.sqlite3"
            page.goto(base + "/plans/new/")
            page.get_by_label("Nama paket", exact=False).fill("Paket browser fiktif")
            page.get_by_label("Kecepatan (Mbps)").fill("75")
            page.get_by_label("Harga bulanan sebelum pajak (rupiah)").fill("350000")
            page.get_by_role("button", name="Tinjau perubahan").click()
            expect(page.get_by_role("heading", name="Tinjau perubahan paket")).to_be_visible()
            with sqlite3.connect(db_path) as db:
                assert db.execute("SELECT COUNT(*) FROM core_plan WHERE name='Paket browser fiktif'").fetchone()[0] == 0
                invoices_before_package = db.execute("SELECT * FROM core_invoice ORDER BY id").fetchall()
            token = page.locator('[name="token"]').input_value()
            page.get_by_role("button", name="Konfirmasi paket").click()
            expect(page.get_by_role("heading", name="Tinjau perubahan paket")).to_be_visible()
            page.get_by_label("Saya menyetujui rincian paket", exact=False).check()
            page.get_by_role("button", name="Konfirmasi paket").click()
            page.wait_for_url(base + "/plans/")
            csrf = next(cookie['value'] for cookie in owner.cookies() if cookie['name']=='csrftoken')
            assert owner.request.post(base + "/plans/confirm/", form={"token": token, "reviewed": "on", "csrfmiddlewaretoken": csrf}, max_redirects=0).status == 302
            with sqlite3.connect(db_path) as db:
                assert db.execute("SELECT COUNT(*) FROM core_plan WHERE name='Paket browser fiktif'").fetchone()[0] == 1
                package_id = db.execute("SELECT id FROM core_plan WHERE name='Paket browser fiktif'").fetchone()[0]
            package_edit = f"/plans/{package_id}/edit/"
            page.goto(base + package_edit)
            page.get_by_label("Harga bulanan sebelum pajak (rupiah)").fill("400000")
            page.get_by_role("button", name="Tinjau perubahan").click()
            expect(page.get_by_role("region", name="Perubahan paket")).to_contain_text("Rp350.000")
            expect(page.get_by_role("region", name="Perubahan paket")).to_contain_text("Rp400.000")
            page.screenshot(path=str(evidence / "plans-review-desktop.png"), full_page=True)
            for width in [360, 768, 1366, 1920]:
                page.set_viewport_size({"width": width, "height": 1000})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, "package review")
                if width <= 850:
                    page.wait_for_function("document.querySelector('#sidebar').getBoundingClientRect().right <= 1")
                assert page.locator("main h1").evaluate("el => el.getBoundingClientRect().right <= innerWidth && el.scrollWidth <= el.clientWidth"), (width, "review heading clipped")
                if width == 360:
                    page.screenshot(path=str(evidence / "plans-review-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1366, "height": 1000})
            page.get_by_label("Saya menyetujui rincian paket", exact=False).check()
            page.get_by_role("button", name="Konfirmasi paket").click()
            page.wait_for_url(base + "/plans/")
            page.get_by_label("Cari nama paket").fill("browser fiktif")
            page.get_by_role("button", name="Cari", exact=True).click()
            expect(page.locator('tbody tr')).to_have_count(1)
            expect(page.locator('tbody')).to_contain_text("Rp400.000")
            page.screenshot(path=str(evidence / "plans-list-desktop.png"), full_page=True)
            with sqlite3.connect(db_path) as db:
                assert db.execute("SELECT * FROM core_invoice ORDER BY id").fetchall() == invoices_before_package
                assert db.execute("SELECT version FROM core_plan WHERE id=?", (package_id,)).fetchone()[0] == 2

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
            # Verified photos stay private in the temporary database and follow work visibility.
            photo_buffer = io.BytesIO(); Image.new("RGB", (24, 18), "#13796b").save(photo_buffer, format="PNG")
            photo_source = photo_buffer.getvalue()
            ticket_photos = ticket_url+"photos/"
            def attach_photo(current, caption, internal=None):
                current.get_by_label("Keterangan foto", exact=False).fill(caption)
                current.get_by_label("Foto JPG/PNG", exact=False).set_input_files({"name": "bukti.png", "mimeType": "image/png", "buffer": photo_source})
                if internal is not None:
                    current.get_by_label("Internal ·", exact=False).set_checked(internal)
                current.get_by_role("button", name="Simpan foto", exact=True).click()
                expect(current.get_by_text(caption, exact=True)).to_be_visible()
            page.goto(ticket_photos)
            attach_photo(page, "Foto internal jalur fiktif", True)
            private_photo_url = page.get_by_role("link", name="Unduh foto Foto internal jalur fiktif", exact=True).get_attribute('href')
            attach_photo(page, "Foto publik jalur fiktif", False)
            public_photo_url = page.get_by_role("link", name="Unduh foto Foto publik jalur fiktif", exact=True).get_attribute('href')
            photo_response = owner.request.get(base+public_photo_url)
            assert photo_response.ok and photo_response.headers['content-type']=='image/png'
            assert 'attachment;' in photo_response.headers['content-disposition'] and 'sandbox' in photo_response.headers['content-security-policy']
            assert photo_response.body().startswith(b'\x89PNG')
            page.screenshot(path=str(evidence / "work-photos-owner-desktop.png"), full_page=True)
            page.goto(ticket_url)
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
            assert technician.request.get(base+private_photo_url).status == 200
            assert technician.request.get(hidden_ticket+"photos/").status == 404

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
            page.get_by_role("link", name="Ubah data PSB", exact=True).click()
            page.get_by_label("Alamat pemasangan").fill("Jl. Pemasangan Fiktif No. 11")
            page.get_by_label("Alasan perubahan").fill("Alamat diperbaiki sesuai survei awal pelanggan.")
            page.get_by_role("button", name="Simpan", exact=True).click()
            page.wait_for_url(order_url)
            expect(page.get_by_text("Data PSB disimpan.", exact=False)).to_be_visible()
            field.goto(order_url)
            for status in ["survey", "scheduled", "installing", "installed"]:
                field.get_by_label("Tahap berikutnya").select_option(status)
                field.get_by_label("Catatan / alasan").fill("Tahap pemasangan telah diperiksa di lokasi fiktif.")
                if status == "scheduled":
                    schedule = datetime.now(ZoneInfo("Asia/Jakarta")) + timedelta(days=1)
                    field.get_by_label("Jadwal pemasangan").fill(schedule.strftime("%Y-%m-%dT%H:%M"))
                field.get_by_role("button", name="Simpan tahap PSB").click()
                if status == "scheduled":
                    page.goto(order_url)
                    page.get_by_role("link", name="Jadwalkan ulang", exact=False).click()
                    page.get_by_label("Jadwal baru").fill((schedule+timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"))
                    page.get_by_label("Alasan penjadwalan ulang").fill("Pelanggan meminta teknisi datang sehari kemudian.")
                    page.get_by_role("button", name="Simpan", exact=True).click()
                    page.wait_for_url(order_url)
                    expect(page.get_by_text("Jadwal baru disimpan.", exact=False)).to_be_visible()
                    page.screenshot(path=str(evidence / "psb-rescheduled-desktop.png"), full_page=True)
                    page.get_by_role("link", name="Ubah data PSB", exact=True).click()
                    page.get_by_label("Alamat pemasangan").fill("Jl. Pemasangan Fiktif No. 12")
                    page.get_by_label("Alasan perubahan").fill("Lokasi akhir berubah sehingga perlu survei ulang.")
                    page.get_by_role("button", name="Simpan", exact=True).click()
                    page.wait_for_url(order_url)
                    expect(page.locator('.page-heading .eyebrow')).to_contain_text("SURVEI")
                    field.reload()
                    field.get_by_label("Tahap berikutnya").select_option("scheduled")
                    field.get_by_label("Catatan / alasan").fill("Survei ulang selesai dan jadwal baru disepakati.")
                    field.get_by_label("Jadwal pemasangan").fill((schedule+timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"))
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
            psb_photos = order_url+"photos/"
            page.goto(psb_photos)
            attach_photo(page, "Foto pemasangan internal fiktif")
            psb_photo_url = page.get_by_role("link", name="Unduh foto Foto pemasangan internal fiktif", exact=True).get_attribute('href')
            page.goto(order_url)


            customer = browser.new_context(viewport={"width": 360, "height": 900})
            portal = new_page(customer)
            login(portal, "pelanggan")
            portal.wait_for_url(base + "/portal/")
            # One partial transaction, printable by its operator and owning portal account.
            with sqlite3.connect(db_path) as db:
                receipt_invoice = db.execute("SELECT i.id FROM core_invoice i JOIN core_membership m "
                    "ON m.customer_id=i.customer_id JOIN auth_user u ON u.id=m.user_id "
                    "WHERE u.username='pelanggan' AND NOT EXISTS (SELECT 1 FROM core_payment p WHERE p.invoice_id=i.id) "
                    "ORDER BY i.id LIMIT 1").fetchone()[0]
            page.goto(base+f"/invoices/{receipt_invoice}/")
            page.get_by_label("Nominal pembayaran (rupiah)").fill("10000")
            page.get_by_label("Catatan (opsional)").fill("Pembayaran sebagian untuk uji kuitansi.")
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Catat pembayaran simulasi", exact=True).click()
            page.get_by_role("link", name="Lihat kuitansi", exact=True).click()
            receipt_url = page.url
            receipt_number = page.locator('.page-heading h1').inner_text()
            expect(page.locator('.receipt-paper')).to_contain_text("Rp10.000")
            expect(page.locator('.receipt-words')).to_have_text("Sepuluh ribu rupiah")
            page.screenshot(path=str(evidence / "payment-receipt-desktop.png"), full_page=True)
            receipt_pdf_url = page.get_by_role("link", name="Cetak / PDF A4", exact=True).get_attribute('href')
            pdf_response = owner.request.get(base+receipt_pdf_url)
            assert pdf_response.ok and pdf_response.headers['content-type']=='application/pdf'
            assert 'private' in pdf_response.headers['cache-control'] and 'no-store' in pdf_response.headers['cache-control']
            from pypdf import PdfReader
            assert receipt_number in PdfReader(io.BytesIO(pdf_response.body())).pages[0].extract_text()
            portal.goto(base+f"/invoices/{receipt_invoice}/")
            portal.get_by_role("link", name="Lihat kuitansi", exact=True).click()
            expect(portal.locator('.receipt-paper')).to_contain_text("Rp10.000")
            assert customer.request.get(base+receipt_pdf_url).status == 200
            assert technician.request.get(receipt_url).status == 403
            portal.screenshot(path=str(evidence / "payment-receipt-portal-mobile.png"), full_page=True)
            portal.goto(ticket_photos)
            expect(portal.get_by_text("Foto publik jalur fiktif", exact=True)).to_be_visible()
            expect(portal.get_by_text("Foto internal jalur fiktif", exact=True)).to_have_count(0)
            assert customer.request.get(base+private_photo_url).status == 404
            assert customer.request.get(base+psb_photo_url).status == 404
            assert customer.request.get(base+public_photo_url).status == 200
            attach_photo(portal, "Foto pelanggan fiktif")
            portal.screenshot(path=str(evidence / "work-photos-customer-mobile.png"), full_page=True)
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
            # Only the disposable fixture is changed to represent a missed SLA.
            db_path = Path(directory) / "tagihnet.sqlite3"
            with sqlite3.connect(db_path) as db:
                db.execute("UPDATE core_ticket SET sla_due_at=? WHERE id=?",
                    ((datetime.now(ZoneInfo("UTC"))-timedelta(hours=1)).replace(tzinfo=None).isoformat(" "), int(hidden_ticket.rstrip("/").split("/")[-1])))
            page.goto(base + "/operations/reminders/")
            expect(page.get_by_role("heading", name="Pengingat billing & SLA")).to_be_visible()
            with sqlite3.connect(db_path) as db:
                assert db.execute("SELECT COUNT(*) FROM core_reminderpolicy").fetchone()[0] == 0
            page.get_by_role("link", name="Atur kebijakan simulasi").click()
            page.get_by_label("Aktifkan pengingat billing simulasi").check()
            page.get_by_label("Aktifkan pengingat SLA simulasi").check()
            page.get_by_label("Jam mulai pengingat billing").fill("0")
            page.get_by_label("Saya memahami ini hanya simulasi").check()
            page.get_by_role("button", name="Simpan", exact=True).click()
            page.wait_for_url(base + "/operations/reminders/")
            subprocess.run([sys.executable, "manage.py", "shell", "-c",
                "from core.models import ReminderPolicy; from core.reminders import plan_reminders; [plan_reminders(p.pk) for p in ReminderPolicy.objects.all()]"],
                cwd=root, env=env, check=True, stdout=subprocess.DEVNULL)
            with sqlite3.connect(db_path) as db:
                invoice_id, balance = db.execute("SELECT i.id,i.total-COALESCE((SELECT SUM(p.amount) FROM core_payment p LEFT JOIN core_paymentreversal v ON v.payment_id=p.id WHERE p.invoice_id=i.id AND v.id IS NULL),0) FROM core_invoice i JOIN core_reminder r ON r.invoice_id=i.id WHERE r.kind='billing' LIMIT 1").fetchone()
                count_before = db.execute("SELECT COUNT(*) FROM core_reminder").fetchone()[0]
            page.goto(base + f"/invoices/{invoice_id}/")
            page.get_by_label("Nominal pembayaran (rupiah)").fill(str(balance))
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Catat pembayaran simulasi", exact=True).click()
            expect(page.locator('.invoice-brand .pill')).to_have_text("Lunas")
            for repeat in range(2):
                subprocess.run([sys.executable, "manage.py", "run_reminder_worker", "--once"], cwd=root, env=env, check=True, stdout=subprocess.DEVNULL)
            with sqlite3.connect(db_path) as db:
                assert db.execute("SELECT COUNT(*) FROM core_reminder").fetchone()[0] == count_before
                assert db.execute("SELECT status,attempts FROM core_reminder WHERE invoice_id=?", (invoice_id,)).fetchone() == ("skipped", 0)
                sla_id, status, attempts, body = db.execute("SELECT id,status,attempts,body FROM core_reminder WHERE kind='sla'").fetchone()
                assert (status, attempts) == ("simulated", 1)
                assert "Catatan internal demo" not in body
            reminder_url = base + f"/operations/reminders/{sla_id}/"
            page.goto(reminder_url)
            expect(page.get_by_role("heading", name="Isi simulasi terakhir")).to_be_visible()
            expect(page.get_by_text("Tidak ada pesan nyata dikirim.", exact=False)).to_be_visible()
            page.screenshot(path=str(evidence / "reminder-detail-desktop.png"), full_page=True)
            assert technician.request.get(reminder_url).status == 403
            assert customer.request.get(reminder_url).status == 403
            # Monitoring uses another opt-in configuration; no real NAS is contacted.
            page.goto(base + "/network/monitoring/")
            expect(page.get_by_text("Belum ada data segar", exact=True)).to_have_count(2)
            page.get_by_role("link", name="＋ NAS simulasi", exact=True).click()
            page.get_by_label("Nama NAS simulasi").fill("NAS Bandung Fiktif")
            page.get_by_label("Lokasi (opsional)").fill("Laboratorium fiktif")
            page.get_by_label("Aktifkan pengambilan snapshot simulasi").check()
            page.get_by_label("Saya memahami NAS ini hanya simulasi").check()
            page.get_by_role("button", name="Simpan", exact=True).click()
            monitor_url = page.url
            page.get_by_role("button", name="Ambil snapshot simulasi").click()
            expect(page.locator('.page-heading .eyebrow')).to_contain_text("SEGAR")
            expect(page.get_by_text("demo-ppp-01", exact=True)).to_be_visible()
            page.get_by_role("button", name="Ambil snapshot simulasi").click()
            with sqlite3.connect(db_path) as db:
                assert db.execute("SELECT COUNT(*) FROM core_monitoringpoll").fetchone()[0] == 1
            page.screenshot(path=str(evidence / "monitoring-fresh-desktop.png"), full_page=True)
            for scenario, state in [("unavailable", "GAGAL DIAMBIL"), ("stale", "KEDALUWARSA"), ("empty", "SEGAR")]:
                page.get_by_role("link", name="Ubah konfigurasi", exact=True).click()
                page.get_by_label("Skenario data simulasi").select_option(scenario)
                page.get_by_label("Saya memahami NAS ini hanya simulasi").check()
                page.get_by_role("button", name="Simpan", exact=True).click()
                expect(page.locator('.page-heading .eyebrow')).to_contain_text("BELUM ADA SNAPSHOT VERSI INI")
                page.get_by_role("button", name="Ambil snapshot simulasi").click()
                expect(page.locator('.page-heading .eyebrow')).to_contain_text(state)
                if scenario == "unavailable":
                    expect(page.get_by_text("demo-ppp-01", exact=True)).to_be_visible()
                    page.screenshot(path=str(evidence / "monitoring-failed-history.png"), full_page=True)
                elif scenario == "stale":
                    page.screenshot(path=str(evidence / "monitoring-stale-desktop.png"), full_page=True)
                else:
                    expect(page.get_by_text("Snapshot berhasil: tidak ada sesi.", exact=False)).to_be_visible()
                page.goto(base + "/network/monitoring/")
                count = page.locator('.monitoring-summary .definition-list dd').first
                expect(count).to_have_text("0" if scenario == "empty" else "Belum ada data segar")
                page.goto(monitor_url)
            noc_context = browser.new_context(viewport={"width": 360, "height": 900})
            noc = new_page(noc_context)
            login(noc, "noc", enroll=True)
            noc.goto(monitor_url)
            expect(noc.get_by_role("link", name="Ubah konfigurasi", exact=True)).to_have_count(0)
            assert noc_context.request.get(monitor_url+"edit/").status == 403
            assert noc_context.request.get(base+"/network/monitoring/new/").status == 403
            assert "Rp" not in noc.inner_text("main")
            # Administrative NAS mapping and ledger-based due filters, in this disposable DB only.
            with sqlite3.connect(db_path) as db:
                owner_tenant = db.execute("SELECT tenant_id FROM core_membership m JOIN auth_user u ON m.user_id=u.id WHERE u.username='owner'").fetchone()[0]
                mapped_customer, mapped_name, mapped_code, due_invoice, due_amount = db.execute(
                    "SELECT c.id,c.name,c.code,i.id,i.total FROM core_customer c JOIN core_invoice i ON i.customer_id=c.id "
                    "WHERE c.tenant_id=? AND i.tenant_id=? AND i.due_date<? "
                    "AND NOT EXISTS (SELECT 1 FROM core_payment p WHERE p.invoice_id=i.id) ORDER BY i.id LIMIT 1",
                    (owner_tenant, owner_tenant, datetime.now(ZoneInfo('Asia/Jakarta')).date().isoformat())).fetchone()
                mapping_before = [db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0] for table in ['core_invoice', 'core_payment', 'core_networkjob', 'core_monitoringpoll']]
            mapped_nas = int(monitor_url.rstrip('/').split('/')[-1])
            mapped_detail = base + f"/customers/{mapped_customer}/"
            page.goto(mapped_detail + "edit/")
            page.get_by_label("NAS simulasi", exact=False).select_option(str(mapped_nas))
            page.get_by_role("button", name="Simpan", exact=True).click()
            page.wait_for_url(mapped_detail)
            expect(page.locator("dd").filter(has_text="NAS Bandung Fiktif")).to_be_visible()
            with sqlite3.connect(db_path) as db:
                assert db.execute("SELECT nas_id FROM core_customer WHERE id=?", (mapped_customer,)).fetchone()[0] == mapped_nas
                assert [db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0] for table in ['core_invoice', 'core_payment', 'core_networkjob', 'core_monitoringpoll']] == mapping_before
            filtered_customers = f"/customers/?nas={mapped_nas}&due=overdue&q={mapped_code}&sort=name&size=25"
            page.goto(base + filtered_customers)
            expect(page.locator('tbody tr')).to_have_count(1)
            expect(page.locator('tbody')).to_contain_text(mapped_name)
            expect(page.locator('tbody')).to_contain_text("Menunggak")
            csv_response = owner.request.get(base + filtered_customers.replace('/customers/', '/customers/export/'))
            assert csv_response.ok and mapped_code in csv_response.text()
            page.screenshot(path=str(evidence / "customer-filters-desktop.png"), full_page=True)
            page.locator("a.customer-cell").filter(has_text=mapped_name).click()
            page.wait_for_url(mapped_detail)
            page.get_by_role("link", name="Kembali ke pelanggan", exact=False).click()
            page.wait_for_url(base + filtered_customers)
            # A payment changes the next filtered request; reversal restores overdue membership.
            page.goto(base + f"/invoices/{due_invoice}/")
            page.get_by_label("Nominal pembayaran (rupiah)").fill(str(due_amount))
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Catat pembayaran simulasi", exact=True).click()
            expect(page.locator('.invoice-brand .pill')).to_have_text("Lunas")
            page.goto(base + filtered_customers)
            expect(page.get_by_role("heading", name="Tidak ada pelanggan ditemukan")).to_be_visible()
            page.goto(base + f"/invoices/{due_invoice}/")
            page.get_by_text("Koreksi pembayaran", exact=True).click()
            page.get_by_label("Alasan koreksi").fill("Pembayaran uji filter perlu dikoreksi kembali.")
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Buat reversal", exact=True).click()
            page.get_by_role("link", name="Lihat kuitansi", exact=True).click()
            reversed_receipt_url = page.url
            expect(page.locator('.receipt-reversal')).to_contain_text("Direversal · transaksi tidak berlaku")
            assert customer.request.get(reversed_receipt_url).status == 404
            reversed_pdf_url = base+page.get_by_role("link", name="Cetak / PDF A4").get_attribute('href')
            assert customer.request.get(reversed_pdf_url).status == 404
            assert noc_context.request.get(reversed_pdf_url).status == 403
            page.goto(base + filtered_customers)
            expect(page.locator('tbody')).to_contain_text(mapped_name)
            assert noc_context.request.get(base + filtered_customers).status == 403
            assert technician.request.get(base + filtered_customers).status == 403
            assert customer.request.get(base + filtered_customers).status == 403
            # All customer tabs are real server-rendered views, scoped to one customer.
            with sqlite3.connect(db_path) as db:
                ticket_customer = db.execute("SELECT customer_id FROM core_ticket WHERE id=?", (int(ticket_url.rstrip('/').split('/')[-1]),)).fetchone()[0]
                profile_before = [db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0] for table in ['core_customer', 'core_invoice', 'core_payment', 'core_networkjob', 'core_auditevent']]
            profile_url = base + f"/customers/{ticket_customer}/"
            profile_tabs = {"Ringkasan": "summary", "Layanan & Koneksi": "service", "Faktur": "invoices", "Pembayaran": "payments", "Tiket": "tickets", "Lokasi": "location", "Riwayat": "history"}
            page.goto(profile_url)
            for label, tab in profile_tabs.items():
                page.get_by_role("navigation", name="Tab pelanggan").get_by_role("link", name=label, exact=True).click()
                page.wait_for_url(profile_url+f"?tab={tab}&size=10")
                expect(page.locator('.customer-tabs [aria-current="page"]')).to_have_text(label)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), tab
                if tab == "tickets":
                    expect(page.get_by_text("Internet lokasi fiktif terputus", exact=True)).to_be_visible()
                    expect(page.get_by_text("Catatan internal demo: rencana pemeriksaan jalur.", exact=True)).to_have_count(0)
            page.goto(mapped_detail+"?tab=payments&size=25")
            expect(page.get_by_role("region", name="Pembayaran pelanggan")).to_contain_text("Direversal")
            page.screenshot(path=str(evidence / "customer-workspace-payments-desktop.png"), full_page=True)
            page.goto(profile_url+"?tab=tickets&size=10")
            page.screenshot(path=str(evidence / "customer-workspace-tickets-desktop.png"), full_page=True)
            # Native links also work when JavaScript is disabled; return-state enhancement is optional.
            no_js = browser.new_context(java_script_enabled=False, storage_state=owner.storage_state())
            static_page = no_js.new_page()
            static_page.goto(profile_url)
            static_page.get_by_role("navigation", name="Tab pelanggan").get_by_role("link", name="Faktur", exact=True).click()
            static_page.wait_for_url(profile_url+"?tab=invoices&size=10")
            expect(static_page.get_by_role("heading", name="Faktur pelanggan", exact=True)).to_be_visible()
            no_js.close()
            # Preserve exact filtered URL and vertical scroll when returning through a different tab.
            page.set_viewport_size({"width": 360, "height": 700})
            page.goto(base + filtered_customers)
            page.evaluate("window.scrollTo(0, 400)")
            page.wait_for_function("Math.abs(scrollY-400)<3")
            page.goto(mapped_detail+"?tab=payments&size=25")
            page.get_by_role("link", name="Kembali ke pelanggan", exact=False).click()
            page.wait_for_url(base + filtered_customers)
            page.wait_for_function("Math.abs(scrollY-400)<3")
            page.evaluate("window.scrollTo(0, 0)")
            page.set_viewport_size({"width": 1366, "height": 1000})
            for context in [technician, customer, noc_context]:
                assert context.request.get(profile_url+"?tab=payments").status == 403
            with sqlite3.connect(db_path) as db:
                assert [db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0] for table in ['core_customer', 'core_invoice', 'core_payment', 'core_networkjob', 'core_auditevent']] == profile_before
            # Reports use event dates and an independent source-SQL total for this temporary fixture.
            with sqlite3.connect(db_path) as db:
                report_issued = db.execute("SELECT COALESCE(SUM(total),0) FROM core_invoice WHERE tenant_id=?", (owner_tenant,)).fetchone()[0]
                report_gross = db.execute("SELECT COALESCE(SUM(amount),0) FROM core_payment WHERE tenant_id=?", (owner_tenant,)).fetchone()[0]
                report_reversed = db.execute("SELECT COALESCE(SUM(p.amount),0) FROM core_paymentreversal r JOIN core_payment p ON p.id=r.payment_id WHERE p.tenant_id=?", (owner_tenant,)).fetchone()[0]
                report_events = db.execute("SELECT COUNT(*) FROM core_payment WHERE tenant_id=?", (owner_tenant,)).fetchone()[0] + db.execute("SELECT COUNT(*) FROM core_paymentreversal r JOIN core_payment p ON p.id=r.payment_id WHERE p.tenant_id=?", (owner_tenant,)).fetchone()[0]
                report_audits = db.execute("SELECT COUNT(*) FROM core_auditevent WHERE action='report.exported'").fetchone()[0]
            def idr(amount): return "Rp"+f"{amount:,}".replace(',', '.')
            report_url = base + "/reports/billing/"
            page.goto(report_url)
            expect(page.get_by_role("heading", name="Laporan penagihan", exact=True)).to_be_visible()
            expect(page.get_by_text("Saldo cocok", exact=True)).to_be_visible()
            metrics = page.locator('.billing-report-metrics .metric')
            expect(metrics.filter(has_text="Penerimaan tercatat")).to_contain_text(idr(report_gross))
            expect(metrics.filter(has_text="Koreksi (reversal)")).to_contain_text(idr(report_reversed))
            expect(metrics.filter(has_text="Piutang akhir")).to_contain_text(idr(report_issued-report_gross+report_reversed))
            page.screenshot(path=str(evidence / "billing-report-cash-desktop.png"), full_page=True)
            export_url = page.get_by_role("link", name="Ekspor CSV", exact=False).get_attribute('href')
            exported = owner.request.get(base+export_url)
            assert exported.ok
            csv_rows = list(csv.reader(io.StringIO(exported.body().decode('utf-8-sig'))))
            assert len(csv_rows) == report_events+1
            assert sum(int(row[5]) for row in csv_rows[1:]) == report_gross-report_reversed
            for label in ["Faktur terbit", "Piutang akhir", "Penerimaan & koreksi"]:
                page.get_by_role("navigation", name="Jenis laporan").get_by_role("link", name=label, exact=True).click()
                expect(page.locator('[aria-label="Jenis laporan"] [aria-current="page"]')).to_have_text(label)
                expect(page.get_by_text("Saldo cocok", exact=True)).to_be_visible()
            assert owner.request.get(report_url+"?start=invalid&end=2026-10-31").status == 400
            for context in [technician, customer, noc_context]:
                assert context.request.get(report_url).status == 403
                assert context.request.get(report_url+"export.csv").status == 403
            with sqlite3.connect(db_path) as db:
                assert db.execute("SELECT COUNT(*) FROM core_auditevent WHERE action='report.exported'").fetchone()[0] == report_audits+1
            # CSV imports stay inside this disposable SQLite database.
            with sqlite3.connect(db_path) as db:
                import_plan = db.execute("SELECT p.id FROM core_plan p JOIN core_membership m ON p.tenant_id=m.tenant_id JOIN auth_user u ON m.user_id=u.id WHERE u.username='owner' ORDER BY p.id LIMIT 1").fetchone()[0]
                before_import = [db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0] for table in ["core_customer", "core_invoice", "core_networkjob", "core_membership"]]
            def upload_csv(payload):
                page.goto(base+"/customers/imports/new/")
                page.get_by_label("File pelanggan CSV UTF-8").set_input_files({"name": "pelanggan.csv", "mimeType": "text/csv", "buffer": payload.encode()})
                page.get_by_role("button", name="Buat pratinjau impor", exact=True).click()
                expect(page.get_by_role("heading", name="Tinjau penambahan pelanggan")).to_be_visible()
            header = "nama,email,telepon,username,paket_id,wilayah,alamat\n"
            upload_csv(header+f"Pelanggan CSV Salah,,bukan-telepon,csv-salah,{import_plan},Dago,Alamat fiktif\n")
            expect(page.get_by_role("button", name="Konfirmasi dan tambahkan pelanggan")).to_have_count(0)
            expect(page.get_by_text("Perlu diperbaiki", exact=True)).to_be_visible()
            upload_csv(header+f"Pelanggan CSV Fiktif,,+62 812-9999771,CSV-BROWSER-1,{import_plan},Dago,Alamat fiktif satu\n"+f"Pelanggan CSV Kedua,,08129999772,csv-browser-2,{import_plan},Dago,Alamat fiktif dua\n")
            import_url = page.url
            page.screenshot(path=str(evidence / "customer-import-preview.png"), full_page=True)
            with sqlite3.connect(db_path) as db:
                assert db.execute("SELECT COUNT(*) FROM core_customer").fetchone()[0] == before_import[0]
            page.get_by_label("Saya meninjau seluruh baris").check()
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Konfirmasi dan tambahkan pelanggan").click()
            page.wait_for_url(import_url)
            expect(page.locator('.page-heading .eyebrow')).to_contain_text("SELESAI")
            # The CSRF cookie is read by name; no factor or token is logged.
            csrf = next(cookie['value'] for cookie in owner.cookies() if cookie['name']=='csrftoken')
            assert owner.request.post(import_url+"confirm/", form={"reviewed": "on", "csrfmiddlewaretoken": csrf}, max_redirects=0).status == 302
            with sqlite3.connect(db_path) as db:
                after_import = [db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0] for table in ["core_customer", "core_invoice", "core_networkjob", "core_membership"]]
                assert after_import == [before_import[0]+2, *before_import[1:]]
                assert db.execute("SELECT service_state,phone FROM core_customer WHERE username='csv-browser-1'").fetchone() == ("pending", "08129999771")
            assert technician.request.get(base + "/plans/").status == 403
            assert customer.request.get(base + "/plans/").status == 403
            assert technician.request.get(import_url).status == 403
            assert customer.request.get(import_url).status == 403
            page.screenshot(path=str(evidence / "customer-import-completed.png"), full_page=True)
            for width in [360, 768, 1366, 1920]:
                page.set_viewport_size({"width": width, "height": 1000})
                for route in [ticket_photos.removeprefix(base), psb_photos.removeprefix(base), "/reports/billing/", "/reports/billing/?mode=invoices", "/reports/billing/?mode=receivables", filtered_customers, f"/customers/{mapped_customer}/edit/", "/plans/", "/plans/new/", package_edit, "/operations/tickets/", "/operations/installations/", "/operations/tickets/new/", "/operations/installations/new/", "/operations/sla/", "/operations/reminders/", "/operations/reminders/settings/", "/network/monitoring/", "/network/monitoring/new/", monitor_url.removeprefix(base)+"edit/", "/customers/imports/", "/customers/imports/new/"]:
                    page.goto(base + route)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, route)
                for tab in profile_tabs.values():
                    page.goto(profile_url+f"?tab={tab}&size=10")
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, tab)
                    assert page.locator('.customer-tabs a').first.bounding_box()["height"] >= 44
                    if width == 360 and tab == "invoices":
                        page.screenshot(path=str(evidence / "customer-workspace-invoices-mobile.png"), full_page=True)
                for url in [ticket_url, order_url, reminder_url, monitor_url, import_url, receipt_url, reversed_receipt_url]:
                    page.goto(url)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, url)
            page.set_viewport_size({"width": 360, "height": 1000})
            page.goto(report_url+"?mode=receivables")
            page.screenshot(path=str(evidence / "billing-report-receivables-mobile.png"), full_page=True)
            page.goto(base + filtered_customers)
            page.wait_for_function("document.querySelector('#sidebar').getBoundingClientRect().right <= 1")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert page.locator("tbody tr").first.bounding_box()["height"] < 170
            page.screenshot(path=str(evidence / "customer-filters-mobile.png"), full_page=True)
            page.goto(base + "/operations/reminders/")
            assert page.locator("tbody tr").first.bounding_box()["height"] < 170
            page.screenshot(path=str(evidence / "reminders-mobile.png"), full_page=True)
            page.goto(base + "/network/monitoring/")
            page.screenshot(path=str(evidence / "monitoring-mobile.png"), full_page=True)
            page.goto(import_url)
            page.screenshot(path=str(evidence / "customer-import-mobile.png"), full_page=True)
            subprocess.run([sys.executable, "scripts/verify_restore.py"], cwd=root, env=env, check=True, stdout=subprocess.DEVNULL)
            assert failures == [], failures
            browser.close()
        print("PASS: payment receipts/partial/words/PDF/private cache/portal owner scope/reversal/4 viewports; private normalized photos/customer scope/assigned scope/PSB/download headers/restore; billing reports/roll-forward/source SQL/CSV/scope/4 viewports; 7 customer tabs/permissions/ledger/scoped tickets/native links/scroll restoration; NAS mapping/due filters/export/return URL/payment/reversal; package create/review/confirm/replay/pricing/unchanged ledger/scope; owner/technician/customer ticket lifecycle, private-note isolation, assigned scope, PSB progression, signed activation/replay, single customer/invoice/job, simulated provisioning, PSB edit/reschedule/reset, opt-in reminders, dedupe, payment recheck, private SLA receipt, NAS opt-in/collection/dedupe/failure/stale/true-zero, NOC scope, CSV invalid/preview/atomic-confirm/replay/pending/permission, 4 viewports, restore state; no MFA factors in evidence.")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
