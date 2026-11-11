"""Real Chromium/HTTP flow on disposable data. Midtrans transport is stubbed; no external payment."""
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import pyotp
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
KEY = "SB-Mid-server-browser-fixture-only"
SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
PASSWORD = "Local-test-secret-2026!"


with tempfile.TemporaryDirectory(prefix="tagihnet-gateway-browser-") as temporary:
    temporary = Path(temporary)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings", "TAGIHNET_DATA_DIR": str(temporary),
        "TAGIHNET_DB_ENGINE": "sqlite", "TAGIHNET_DEBUG": "1", "TAGIHNET_SIMULATION": "1",
        "TAGIHNET_ALLOWED_HOSTS": "localhost,127.0.0.1,testserver", "TAGIHNET_MIDTRANS_SANDBOX_ACCOUNT_KEYS":
            json.dumps({"1": {"merchant_id": "G-BROWSER", "server_key": KEY}})}
    subprocess.run([sys.executable, "manage.py", "migrate", "--noinput"], cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
    fixture = '''
import django
django.setup()
from datetime import date
from core.tests.test_workflow import fixtures
from core.billing import issue_invoice
from core.models import MFADevice
from core.security import encrypt_secret
tenants, owners, customers = fixtures()
issue_invoice(tenants[0], customers[0].pk, date(2026, 11, 1), owners[0])
MFADevice.objects.create(user=owners[0], encrypted_secret=encrypt_secret("JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"))
'''
    subprocess.run([sys.executable, "-c", fixture], cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
    (temporary / "remote-status").write_text("pending")
    server_code = '''
import django, os
django.setup()
from pathlib import Path
from unittest.mock import patch
from django.core.management import call_command
def status(order):
    return {"order_id":order.order_id,"merchant_id":"G-BROWSER","gross_amount":f"{order.amount}.00",
        "currency":"IDR","transaction_id":f"txn-{order.pk.hex}","transaction_status":
        (Path(os.environ["TAGIHNET_DATA_DIR"])/"remote-status").read_text(),"fraud_status":"accept","status_code":"200"}
with patch("core.gateway.SandboxClient.create",return_value="https://app.sandbox.midtrans.com/snap/v4/redirection/browser-fixture"), patch("core.gateway.SandboxClient.status",side_effect=status):
    call_command("runserver",os.environ["TAGIHNET_BROWSER_ADDRESS"],use_reloader=False)
'''
    process = subprocess.Popen([sys.executable, "-c", server_code], cwd=ROOT,
        env={**env, "TAGIHNET_BROWSER_ADDRESS": f"127.0.0.1:{port}"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=os.getenv("CHROMIUM_PATH") or "/usr/bin/chromium", headless=True)
            context = browser.new_context(viewport={"width": 1366, "height": 1000})
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            for _ in range(100):
                try:
                    if context.request.get(base + "/health/").ok:
                        break
                except Exception:
                    time.sleep(.1)
            else:
                raise RuntimeError("Temporary server unavailable")
            page.goto(base + "/login/")
            page.get_by_label("Username", exact=True).fill("owner0")
            page.get_by_label("Kata sandi", exact=True).fill(PASSWORD)
            page.get_by_role("button", name="Masuk ke workspace").click()
            page.get_by_label("Kode authenticator atau kode pemulihan").fill(pyotp.TOTP(SECRET).now())
            page.get_by_role("button", name="Verifikasi dan masuk").click()
            page.wait_for_url(base + "/")
            page.goto(base + "/settings/payment-gateway/")
            page.get_by_label("Aktifkan checkout Midtrans Sandbox").check()
            page.get_by_label("Saya memahami ini pengujian sandbox").check()
            page.get_by_role("button", name="Simpan dengan verifikasi MFA").click()
            expect(page.get_by_text("Setelan Midtrans Sandbox disimpan.", exact=False)).to_be_visible()
            page.goto(base + "/invoices/1/")
            page.get_by_role("button", name="Buka checkout sandbox").click()
            page.wait_for_url("**/payments/orders/*/")
            order_url = page.url
            order_id = "TN-SBX-" + order_url.rstrip("/").split("/")[-1].replace("-", "")
            expect(page.get_by_role("heading", name="Menunggu pembayaran", exact=True)).to_be_visible()
            assert page.get_by_role("link", name="Buka pembayaran Midtrans Sandbox").get_attribute("href").startswith("https://app.sandbox.midtrans.com/")
            page.get_by_role("button", name="Periksa status pembayaran").click()
            expect(page.get_by_role("heading", name="Menunggu pembayaran", exact=True)).to_be_visible()
            page.goto(base + "/payments/orders/")
            page.get_by_label("Cari pelanggan atau ID order").fill(order_id)
            page.get_by_label("Status", exact=True).select_option("pending")
            page.get_by_role("button", name="Terapkan filter").click()
            expect(page.get_by_role("link", name=f"Lihat order {order_id}")).to_be_visible()
            page.get_by_label("Status", exact=True).select_option("review")
            page.get_by_role("button", name="Terapkan filter").click()
            expect(page.get_by_role("heading", name="Tidak ada order yang cocok")).to_be_visible()
            evidence = ROOT / "docs/evidence"
            for width in [360, 768, 1366, 1920]:
                page.set_viewport_size({"width": width, "height": 920})
                for route in [order_url, base + "/settings/payment-gateway/", base + "/invoices/1/", base + "/payments/orders/"]:
                    page.goto(route)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, route)
                if width in [360, 1366]:
                    page.goto(order_url)
                    page.screenshot(path=str(evidence / f"gateway-{'mobile' if width == 360 else 'desktop'}.png"), full_page=True)
                    page.goto(base + "/payments/orders/")
                    page.screenshot(path=str(evidence / f"gateway-history-{'mobile' if width == 360 else 'desktop'}.png"), full_page=True)
            payload = {"order_id": order_id, "status_code": "200", "gross_amount": "249750.00"}
            payload["signature_key"] = "0" * 128
            assert context.request.post(base + "/payments/midtrans/notifications/", data=payload).status == 403
            (temporary / "remote-status").write_text("settlement")
            payload["signature_key"] = hashlib.sha512((order_id + "200" + "249750.00" + KEY).encode()).hexdigest()
            for _ in range(2):
                assert context.request.post(base + "/payments/midtrans/notifications/", data=payload).status == 200
            page.goto(order_url)
            expect(page.get_by_role("heading", name="Pembayaran tercatat", exact=True)).to_be_visible()
            page.goto(base + "/invoices/1/")
            expect(page.get_by_text("Lunas", exact=True)).to_be_visible()
            expect(page.get_by_text("Midtrans Sandbox", exact=True)).to_be_visible()
            assert page.get_by_role("button", name="Buat reversal", exact=True).count() == 0
            (temporary / "remote-status").write_text("refund")
            page.goto(order_url)
            page.get_by_role("button", name="Periksa status pembayaran").click()
            expect(page.get_by_role("heading", name="Perlu rekonsiliasi", exact=True)).to_be_visible()
            expect(page.get_by_role("heading", name="Riwayat verifikasi", exact=True)).to_be_visible()
            (temporary / "remote-status").write_text("settlement")
            page.get_by_role("button", name="Periksa status pembayaran").click()
            expect(page.get_by_role("heading", name="Perlu rekonsiliasi", exact=True)).to_be_visible()
            page.goto(base + "/payments/orders/?status=review")
            expect(page.get_by_role("link", name=f"Lihat order {order_id}")).to_be_visible()
            assert not errors, errors
            browser.close()
        verify = '''
import django
django.setup()
from core.models import Payment,GatewayEvent,NetworkJob,Invoice
assert Payment.objects.count()==NetworkJob.objects.count()==1
assert GatewayEvent.objects.count()==3
assert set(GatewayEvent.objects.values_list("gateway_status",flat=True))=={"pending","settlement","refund"}
assert Invoice.objects.get().balance==0
assert Payment.objects.get().gatewayorder.status=="review"
'''
        subprocess.run([sys.executable, "-c", verify], cwd=ROOT, env=env, check=True)
        print("PASS: Chromium login/MFA, gateway opt-in, checkout, history/filter, signed callback/replay, settled ledger and refund/late-success review; 4 viewports; 0 browser errors. Midtrans transport stubbed.")
    finally:
        process.terminate()
        process.wait(timeout=10)
