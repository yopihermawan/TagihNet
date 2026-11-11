"""Real Chromium/HTTP flow on disposable data. Midtrans transport is stubbed; no external payment."""
import hashlib
import csv
import io
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
        "TAGIHNET_PUBLIC_ORIGIN": base,
        "TAGIHNET_ALLOWED_HOSTS": "localhost,127.0.0.1,testserver", "TAGIHNET_MIDTRANS_SANDBOX_ACCOUNT_KEYS":
            json.dumps({"1": {"merchant_id": "G-BROWSER", "server_key": KEY}})}
    subprocess.run([sys.executable, "manage.py", "migrate", "--noinput"], cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
    fixture = '''
import django
django.setup()
from datetime import date
from core.tests.test_workflow import fixtures
from core.billing import issue_invoice
from core.models import MFADevice, Membership
from django.contrib.auth import get_user_model
from core.security import encrypt_secret
tenants, owners, customers = fixtures()
issue_invoice(tenants[0], customers[0].pk, date(2026, 11, 1), owners[0])
issue_invoice(tenants[0], customers[0].pk, date(2026, 12, 1), owners[0])
MFADevice.objects.create(user=owners[0], encrypted_secret=encrypt_secret("JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"))
portal=get_user_model().objects.create_user(username="refund-customer",password="Local-test-secret-2026!")
Membership.objects.create(user=portal,tenant=tenants[0],role="customer",customer=customers[0])
'''
    subprocess.run([sys.executable, "-c", fixture], cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
    (temporary / "remote-status").write_text("pending")
    server_code = '''
import django, os
django.setup()
from pathlib import Path
from unittest.mock import patch
from django.core.management import call_command
from core.midtrans import GatewayUnavailable
def status(order):
    current=(Path(os.environ["TAGIHNET_DATA_DIR"])/"remote-status").read_text()
    if current=="unavailable":
        raise GatewayUnavailable
    result={"order_id":order.order_id,"merchant_id":"G-BROWSER","gross_amount":f"{order.amount}.00",
        "currency":"IDR","transaction_id":f"txn-{order.pk.hex}","transaction_status":
        current,"fraud_status":"accept","status_code":"200"}
    if result["transaction_status"]=="refund":
        result["refund_amount"]=f"{order.amount}.00"
    return result
def expire(order):
    (Path(os.environ["TAGIHNET_DATA_DIR"])/"remote-status").write_text("expire")
    return {"status_code":"407"}
def create(order):
    with (Path(os.environ["TAGIHNET_DATA_DIR"])/"checkout-posts").open("a") as stream:
        stream.write(order.order_id+"\\n")
    return "https://app.sandbox.midtrans.com/snap/v4/redirection/browser-fixture"
with patch("core.gateway.SandboxClient.create",side_effect=create), patch("core.gateway.SandboxClient.status",side_effect=status), patch("core.gateway_expiry.SandboxClient.expire",side_effect=expire):
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
            readiness_section=page.locator("section").filter(has=page.get_by_role("heading",name="Kesiapan konfigurasi",exact=True))
            expect(readiness_section).to_contain_text("Lokal saja")
            expect(readiness_section).to_contain_text("Checkout sandbox sudah diaktifkan owner.")
            cli=subprocess.run([sys.executable,"manage.py","check_payment_gateway","--tenant","1","--json"],cwd=ROOT,env=env,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            assert cli.returncode==1
            readiness=json.loads(cli.stdout)
            assert readiness["checkout_configured"] and not readiness["public_urls_configured"]
            assert readiness["remote_verification"]=="not_performed" and KEY not in cli.stdout
            evidence=ROOT/"docs/evidence"
            for width in [360,768,1366,1920]:
                page.set_viewport_size({"width":width,"height":920})
                page.evaluate("document.activeElement.blur(); window.scrollTo(0, 0)")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"),width
                if width in [360,1366]:
                    readiness_section.screenshot(path=str(evidence/f"gateway-readiness-{'mobile' if width==360 else 'desktop'}.png"),animations="disabled")
            creation_fixture='from django.contrib.auth import get_user_model; from core.gateway import prepare_checkout; prepare_checkout(get_user_model().objects.get(username="owner0"),1)'
            subprocess.run([sys.executable,"manage.py","shell","-c",creation_fixture],cwd=ROOT,env=env,check=True,stdout=subprocess.DEVNULL)
            page.goto(base + "/invoices/1/")
            page.get_by_role("button", name="Buka checkout sandbox").click()
            page.wait_for_url("**/payments/orders/*/")
            order_url = page.url
            order_id = "TN-SBX-" + order_url.rstrip("/").split("/")[-1].replace("-", "")
            evidence = ROOT / "docs/evidence"
            expect(page.get_by_role("heading",name="Menyiapkan checkout",exact=True)).to_be_visible()
            assert not (temporary/"checkout-posts").exists()
            for width in [360,768,1366,1920]:
                page.set_viewport_size({"width":width,"height":920})
                expect(page.get_by_role("link",name="Muat ulang halaman checkout",exact=True)).to_be_visible()
                assert page.get_by_role("button",name="Lanjutkan checkout yang sama",exact=True).count()==0
                page.evaluate("document.activeElement.blur(); window.scrollTo(0, 0)")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"),width
                if width in [360,1366]:
                    page.screenshot(path=str(evidence/f"gateway-creation-{'mobile' if width==360 else 'desktop'}.png"),full_page=True,animations="disabled")
            expire_creation='from datetime import timedelta; from django.utils import timezone; from core.models import GatewayOrder; GatewayOrder.objects.filter(invoice_id=1).update(checkout_lease_until=timezone.now()-timedelta(seconds=1))'
            subprocess.run([sys.executable,"manage.py","shell","-c",expire_creation],cwd=ROOT,env=env,check=True,stdout=subprocess.DEVNULL)
            page.reload()
            page.get_by_role("button",name="Lanjutkan checkout yang sama",exact=True).click()
            page.wait_for_url(order_url)
            assert (temporary/"checkout-posts").read_text().splitlines()==[order_id]
            expect(page.get_by_role("heading", name="Menunggu pembayaran", exact=True)).to_be_visible()
            evidence = ROOT / "docs/evidence"
            for width in [360,768,1366,1920]:
                page.set_viewport_size({"width":width,"height":920})
                page.goto(order_url+"return/?transaction_status=settlement&order_id=forged&next=https://evil.example.test")
                page.wait_for_url(order_url)
                expect(page.get_by_text("Anda telah kembali dari checkout.",exact=False)).to_be_visible()
                expect(page.get_by_role("heading",name="Menunggu pembayaran",exact=True)).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"),width
                if width in [360,1366]:
                    page.screenshot(path=str(evidence/f"gateway-return-{'mobile' if width==360 else 'desktop'}.png"),full_page=True,animations="disabled")
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
                    page.screenshot(path=str(evidence / f"gateway-{'mobile' if width == 360 else 'desktop'}.png"), full_page=True, animations="disabled")
                    page.goto(base + "/payments/orders/")
                    page.screenshot(path=str(evidence / f"gateway-history-{'mobile' if width == 360 else 'desktop'}.png"), full_page=True, animations="disabled")
            payload = {"order_id": order_id, "status_code": "200", "gross_amount": "249750.00"}
            payload["signature_key"] = "0" * 128
            assert context.request.post(base + "/payments/midtrans/notifications/", data=payload).status == 403
            payload["signature_key"] = hashlib.sha512((order_id + "200" + "249750.00" + KEY).encode()).hexdigest()
            (temporary / "remote-status").write_text("unavailable")
            assert context.request.post(base+"/payments/midtrans/notifications/",data=payload).status==503
            page.goto(order_url)
            expect(page.get_by_role("heading",name="Notifikasi Midtrans",exact=True)).to_be_visible()
            expect(page.get_by_text("Verifikasi pengiriman terakhir belum berhasil",exact=True)).to_be_visible()
            for width in [360,768,1366,1920]:
                page.set_viewport_size({"width":width,"height":920})
                page.evaluate("document.activeElement.blur(); window.scrollTo(0, 0)")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"),width
                if width in [360,1366]:
                    page.screenshot(path=str(evidence/f"gateway-notification-{'mobile' if width==360 else 'desktop'}.png"),full_page=True,animations="disabled")
            (temporary / "remote-status").write_text("settlement")
            for _ in range(2):
                assert context.request.post(base + "/payments/midtrans/notifications/", data=payload).status == 200
            page.goto(order_url)
            expect(page.get_by_role("heading", name="Pembayaran tercatat", exact=True)).to_be_visible()
            expect(page.get_by_text("Status pengiriman terakhir terverifikasi",exact=True)).to_be_visible()
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
            (temporary / "remote-status").write_text("pending")
            page.goto(base + "/invoices/2/")
            page.get_by_role("button", name="Buka checkout sandbox").click()
            page.wait_for_url("**/payments/orders/*/")
            expiring_url = page.url
            expiry_key = page.locator('input[name="request_key"]').input_value()
            reason = "Pelanggan meminta checkout sandbox baru untuk pengujian"
            page.get_by_label("Alasan mengakhiri pembayaran").fill(reason)
            page.get_by_label("Saya meminta pengakhiran di gateway", exact=False).check()
            for width in [360, 768, 1366, 1920]:
                page.set_viewport_size({"width": width, "height": 920})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
                if width in [360, 1366]:
                    page.screenshot(path=str(evidence / f"gateway-expiry-{'mobile' if width==360 else 'desktop'}.png"), full_page=True, animations="disabled")
            page.get_by_role("button", name="Minta pengakhiran pembayaran", exact=True).click()
            expect(page.get_by_role("heading", name="Kedaluwarsa di gateway", exact=True)).to_be_visible()
            expect(page.get_by_text("Dikonfirmasi berakhir", exact=True)).to_be_visible()
            assert page.get_by_role("button", name="Minta pengakhiran pembayaran", exact=True).count()==0
            csrf = next(cookie["value"] for cookie in context.cookies(base) if cookie["name"]=="csrftoken")
            replay = context.request.post(expiring_url + "expire/", form={"request_key":expiry_key,"reason":reason,
                "reviewed":"on","csrfmiddlewaretoken":csrf})
            assert replay.status==200
            (temporary / "remote-status").write_text("pending")
            page.goto(base + "/invoices/2/")
            page.get_by_role("button", name="Buka checkout sandbox").click()
            page.wait_for_url("**/payments/orders/*/")
            assert page.url!=expiring_url
            expect(page.get_by_role("heading", name="Menunggu pembayaran", exact=True)).to_be_visible()
            page.goto(base + "/settings/payment-gateway/")
            page.get_by_label("Aktifkan pemeriksaan status otomatis").check()
            page.get_by_label("Jeda antar pemeriksaan").select_option("60")
            page.get_by_label("Saya menyetujui pemeriksaan berkala", exact=False).check()
            page.get_by_role("button", name="Simpan pemeriksaan otomatis dengan MFA", exact=True).click()
            expect(page.get_by_text("Kebijakan pemeriksaan otomatis disimpan.", exact=False)).to_be_visible()
            worker_code = '''
import django
django.setup()
from unittest.mock import patch
from django.core.management import call_command
def status(order):
    result={"order_id":order.order_id,"merchant_id":"G-BROWSER","gross_amount":f"{order.amount}.00",
        "currency":"IDR","transaction_id":f"txn-{order.pk.hex}","transaction_status":
        "refund" if order.payment_id else "settlement","fraud_status":"accept","status_code":"200"}
    if result["transaction_status"]=="refund":
        result["refund_amount"]=f"{order.amount}.00"
    return result
with patch("core.gateway.SandboxClient.status",side_effect=status) as remote:
    call_command("run_gateway_worker",once=True)
    assert remote.call_count==2
with patch("core.gateway.SandboxClient.status") as remote:
    call_command("run_gateway_worker",once=True)
    remote.assert_not_called()
'''
            subprocess.run([sys.executable,"-c",worker_code],cwd=ROOT,env=env,check=True)
            page.goto(base + "/settings/payment-gateway/")
            expect(page.get_by_text("Pemeriksaan selesai", exact=False)).to_be_visible()
            expect(page.locator("section").filter(has=page.get_by_role("heading",name="Pemeriksaan status otomatis",exact=True)).locator(".definition-list")).to_contain_text("2 terverifikasi")
            for width in [360, 768, 1366, 1920]:
                page.set_viewport_size({"width":width,"height":920})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
                if width in [360,1366]:
                    page.screenshot(path=str(evidence / f"gateway-worker-{'mobile' if width==360 else 'desktop'}.png"),full_page=True,animations="disabled")
            (temporary / "remote-status").write_text("refund")
            page.goto(order_url)
            refund_reason="Catatan internal owner untuk pencatatan refund sandbox"
            refund_key=page.locator('input[name="request_key"]').input_value()
            page.get_by_label("Alasan pencatatan refund").fill(refund_reason)
            page.get_by_label("Saya memahami refund terverifikasi",exact=False).check()
            for width in [360,768,1366,1920]:
                page.set_viewport_size({"width":width,"height":920})
                page.evaluate("document.activeElement.blur(); window.scrollTo(0, 0)")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
                if width in [360,1366]:
                    page.screenshot(path=str(evidence/f"gateway-refund-{'mobile' if width==360 else 'desktop'}.png"),full_page=True,animations="disabled")
            page.get_by_role("button",name="Catat refund penuh yang sudah terjadi",exact=True).click()
            expect(page.get_by_role("heading",name="Refund penuh tercatat",exact=True)).to_be_visible()
            expect(page.get_by_role("heading",name="Refund penuh telah dicatat",exact=True)).to_be_visible()
            csrf=next(cookie["value"] for cookie in context.cookies(base) if cookie["name"]=="csrftoken")
            assert context.request.post(order_url+"record-refund/",form={"request_key":refund_key,"reason":refund_reason,"reviewed":"on","csrfmiddlewaretoken":csrf}).status==200
            page.get_by_role("link",name="Ledger gateway",exact=True).click()
            expect(page.get_by_role("heading",name="Laporan ledger gateway",exact=True)).to_be_visible()
            assert page.locator("tbody tr").count()==3
            report_export=page.get_by_role("link",name="Unduh CSV").get_attribute("href")
            download=context.request.get(base+report_export)
            assert download.status==200
            ledger=list(csv.DictReader(io.StringIO(download.body().decode("utf-8-sig"))))
            assert len(ledger)==3 and sum(int(row["pembayaran_rupiah"]) for row in ledger)==499500
            assert sum(int(row["refund_penuh_rupiah"]) for row in ledger)==249750
            assert sum(int(row["perubahan_bersih_rupiah"]) for row in ledger)==249750
            assert refund_reason not in download.text() and "SB-Mid-server-" not in download.text()
            for width in [360,768,1366,1920]:
                page.set_viewport_size({"width":width,"height":920})
                page.evaluate("document.activeElement.blur(); window.scrollTo(0, 0)")
                assert page.locator(".gateway-ledger-summary .amount").evaluate_all('''nodes => nodes.length === 4 && nodes.every(node => {
                    const saved = node.textContent;
                    node.textContent = "Rp-4.999.999.999.995.000";
                    const fits = node.scrollWidth <= node.clientWidth;
                    node.textContent = saved;
                    return fits;
                })'''),width
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"),width
                if width in [360,1366]:
                    page.screenshot(path=str(evidence/f"gateway-ledger-{'mobile' if width==360 else 'desktop'}.png"),full_page=True,animations="disabled")
            portal=browser.new_context()
            portal_page=portal.new_page()
            portal_page.goto(order_url+"return/?transaction_status=settlement")
            portal_page.wait_for_url(base+"/login/")
            portal_page.get_by_label("Username",exact=True).fill("refund-customer")
            portal_page.get_by_label("Kata sandi",exact=True).fill(PASSWORD)
            portal_page.get_by_role("button",name="Masuk ke workspace").click()
            portal_page.wait_for_url(order_url)
            expect(portal_page.get_by_role("heading",name="Refund penuh telah dicatat",exact=True)).to_be_visible()
            assert portal_page.get_by_text(refund_reason,exact=True).count()==0
            assert portal_page.get_by_role("heading",name="Notifikasi Midtrans",exact=True).count()==0
            assert portal.request.get(base+"/payments/reports/").status==403
            assert portal.request.get(base+report_export).status==403
            assert portal_page.get_by_role("button",name="Catat refund penuh yang sudah terjadi",exact=True).count()==0
            portal_page.goto(base+"/invoices/1/")
            expect(portal_page.get_by_text("Refund penuh Midtrans Sandbox terverifikasi. Sisa tagihan dibuka kembali.",exact=True)).to_be_visible()
            assert portal_page.get_by_text(refund_reason,exact=True).count()==0
            assert not errors, errors
            browser.close()
        verify = '''
import django
django.setup()
from core.models import Payment,PaymentReversal,GatewayRefundResolution,GatewayEvent,GatewayExpiry,GatewayOrder,GatewayReconciliationPolicy,GatewayNotificationState,NetworkJob,Invoice
assert Payment.objects.count()==2 and NetworkJob.objects.count()==3
assert GatewayEvent.objects.count()==6
assert set(GatewayEvent.objects.values_list("gateway_status",flat=True))=={"pending","settlement","refund","expire"}
assert [invoice.balance for invoice in Invoice.objects.order_by("pk")]==[249750,0]
assert Payment.objects.filter(gatewayorder__status="refunded").count()==1
assert PaymentReversal.objects.count()==GatewayRefundResolution.objects.count()==1
assert GatewayExpiry.objects.count()==1 and GatewayExpiry.objects.get().status=="confirmed"
assert GatewayOrder.objects.filter(invoice_id=2).count()==2
assert GatewayReconciliationPolicy.objects.get().last_status=="completed"
assert GatewayReconciliationPolicy.objects.get().last_verified==2
notification=GatewayNotificationState.objects.get()
assert notification.delivery_count==3 and notification.last_outcome=="verified" and notification.last_completed_at is not None
'''
        subprocess.run([sys.executable, "-c", verify], cwd=ROOT, env=env, check=True)
        creation_posts=(temporary/"checkout-posts").read_text().splitlines()
        assert len(creation_posts)==len(set(creation_posts))==3
        print("PASS: Chromium local-readiness/CLI redaction, busy checkout/resume, notification retry, MFA/return, expiry/polling/refund and report/CSV; privacy; 4 viewports; 0 browser errors. Midtrans transport stubbed.")
    finally:
        process.terminate()
        process.wait(timeout=10)
