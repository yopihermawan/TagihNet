"""Real Chromium workflow against a LOCAL, seeded simulation instance only."""
import os
import uuid
from datetime import date
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect
from pypdf import PdfReader

base = os.getenv("TAGIHNET_TEST_URL", "http://127.0.0.1:8000")
if urlsplit(base).hostname not in {"localhost", "127.0.0.1"}:
    raise SystemExit("Smoke test hanya boleh dijalankan pada localhost.")
password = os.environ["TAGIHNET_DEMO_PASSWORD"]
evidence = Path(__file__).resolve().parent.parent / "docs" / "evidence"
evidence.mkdir(exist_ok=True)
with sync_playwright() as driver:
    browser = driver.chromium.launch(executable_path=os.getenv("CHROMIUM_PATH") or
        ("/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None), headless=True)
    context = browser.new_context(viewport={"width": 1366, "height": 1000}, reduced_motion="reduce")
    page = context.new_page()
    failures = []
    page.on("pageerror", lambda error: failures.append(str(error)))
    page.on("console", lambda message: failures.append(message.text) if message.type == "error" else None)
    page.goto(base + "/login/")
    page.screenshot(path=str(evidence / "login-desktop.png"), full_page=True)
    expect(page.get_by_text("LINGKUNGAN SIMULASI", exact=True)).to_be_visible()
    page.get_by_label("Username", exact=True).fill("owner")
    page.get_by_label("Kata sandi", exact=True).fill(password)
    page.get_by_role("button", name="Tampilkan", exact=True).click()
    expect(page.get_by_label("Kata sandi", exact=True)).to_have_attribute("type", "text")
    page.get_by_role("button", name="Sembunyikan", exact=True).click()
    page.get_by_role("button", name="Masuk ke workspace").click()
    page.wait_for_url(lambda url: url != base + "/login/")
    if "/mfa/verify/" in page.url:
        factor = os.environ.get("TAGIHNET_MFA_CODE")
        if not factor:
            raise RuntimeError("MFA aktif. Set TAGIHNET_MFA_CODE ke kode OTP baru atau kode pemulihan sekali pakai.")
        page.get_by_label("Kode authenticator atau kode pemulihan").fill(factor)
        page.get_by_role("button", name="Verifikasi dan masuk").click()
    elif "/mfa/setup/" in page.url:
        raise RuntimeError("Daftarkan MFA owner terlebih dahulu, atau jalankan scripts/security_smoke.py untuk uji terisolasi.")
    page.wait_for_url(base + "/")
    expect(page.get_by_text("Mode simulasi", exact=True)).to_be_visible()
    page.screenshot(path=str(evidence / "dashboard-desktop.png"), full_page=True)
    suffix = uuid.uuid4().hex[:8]
    name = f"Uji Browser {suffix}"
    page.goto(base + "/customers/new/")
    page.get_by_label("Nama pelanggan").fill(name)
    page.get_by_label("Telepon").fill("081" + str(int(suffix, 16)).zfill(10))
    page.get_by_label("Username layanan").fill("browser-" + suffix)
    page.get_by_label("Paket").select_option(index=1)
    page.get_by_label("Wilayah").fill("Bandung Uji")
    page.get_by_label("Alamat").fill("Jl. Fiktif Browser No. 1")
    page.get_by_role("button", name="Simpan", exact=True).click()
    expect(page.get_by_role("heading", name=name)).to_be_visible()
    page.reload()
    expect(page.get_by_role("heading", name=name)).to_be_visible()
    page.get_by_role("link", name="Buat faktur").click()
    page.get_by_label("Bulan layanan").fill(date.today().strftime("%Y-%m"))
    page.get_by_role("button", name="Simpan", exact=True).click()
    expect(page.get_by_text("DETAIL FAKTUR", exact=True)).to_be_visible()
    invoice_url = page.url
    page.get_by_label("Nominal pembayaran (rupiah)").fill("150000")
    page.on("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Catat pembayaran simulasi", exact=True).click()
    expect(page.locator('.invoice-brand .pill')).to_have_text("Lunas")
    page.screenshot(path=str(evidence / "invoice-desktop.png"), full_page=True)
    pdf_url = page.get_by_role("link", name="Cetak / PDF A4").get_attribute("href")
    response = context.request.get(base + pdf_url)
    assert response.status == 200
    reader = PdfReader(BytesIO(response.body()))
    pdf_text = "\n".join(item.extract_text() for item in reader.pages)
    assert name in pdf_text and "SIMULASI" in pdf_text and "Rp150.000" in pdf_text
    page.get_by_text("Koreksi pembayaran", exact=True).click()
    page.get_by_label("Alasan koreksi").fill("Koreksi pengujian browser lokal")
    page.get_by_role("button", name="Buat reversal", exact=True).click()
    expect(page.get_by_text("Dikoreksi dengan reversal", exact=True)).to_be_visible()
    page.goto(base + "/customers/?size=25&sort=name")
    assert len(page.locator("tbody tr").all()) == 25
    page.get_by_label("Cari pelanggan", exact=True).fill(name)
    page.get_by_role("button", name="Terapkan", exact=True).click()
    assert len(page.locator("tbody tr").all()) == 1
    page.get_by_role("link", name=f"Detail {name}").click()
    page.get_by_role("link", name="Kembali ke pelanggan").click()
    assert "q=Uji" in page.url and "size=25" in page.url
    page.goto(base + "/customers/?size=10")
    page.screenshot(path=str(evidence / "customers-desktop.png"), full_page=True)
    for width in [360, 768, 1366, 1920]:
        page.set_viewport_size({"width": width, "height": 920})
        for route in ["/", "/customers/", invoice_url.removeprefix(base), "/network/", "/settings/billing/"]:
            page.goto(base + route)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (width, route, "page overflows")
        page.goto(base + "/")
        if width == 360:
            page.screenshot(path=str(evidence / "dashboard-mobile.png"), full_page=True)
            toggle = page.get_by_role("button", name="Buka atau tutup navigasi")
            toggle.click()
            expect(toggle).to_have_attribute("aria-expanded", "true")
            page.keyboard.press("Escape")
            expect(toggle).to_have_attribute("aria-expanded", "false")
    page.set_viewport_size({"width": 1366, "height": 1000})
    page.get_by_role("button", name="Ganti mode terang atau gelap").click()
    expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
    page.screenshot(path=str(evidence / "dashboard-dark.png"), full_page=True)
    page.get_by_role("button", name="Ganti mode terang atau gelap").click()
    page.get_by_role("button", name="Keluar dari akun").click()
    page.wait_for_url(base + "/login/")
    page.set_viewport_size({"width": 360, "height": 800})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(evidence / "login-mobile.png"), full_page=True)
    assert failures == [], failures
    browser.close()
print("PASS: login, customer create/reload, invoice, payment, reversal, PDF, filters/return URL, 4 viewports, dark mode, mobile navigation, logout; 0 browser errors.")
