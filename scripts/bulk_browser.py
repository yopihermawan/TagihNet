"""Bulk billing browser assertions for the private, temporary security smoke server."""
import sqlite3
import subprocess
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from playwright.sync_api import expect


def verify_bulk_billing(page, base, env, root, executable, evidence):
    database = Path(env["TAGIHNET_DATA_DIR"]) / "tagihnet.sqlite3"
    today = datetime.now(ZoneInfo("Asia/Jakarta")).date()

    def month_ahead(offset):
        month_index = today.year * 12 + today.month - 1 + offset
        return date(month_index // 12, month_index % 12 + 1, 1).strftime("%Y-%m")

    def invoice_summary(period):
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            return connection.execute("SELECT COUNT(*), COALESCE(SUM(total),0) FROM core_invoice WHERE period=?", (period + "-01",)).fetchone()

    def worker(limit):
        subprocess.run([executable, "manage.py", "run_billing_worker", "--once", "--limit", str(limit)],
                       cwd=root, env=env, check=True, stdout=subprocess.DEVNULL)

    def preview(period, discount=0):
        page.goto(base + "/invoices/batches/new/")
        page.get_by_label("Bulan layanan").fill(period)
        page.get_by_label("Diskon per faktur baru").fill(str(discount))
        page.get_by_role("button", name="Buat pratinjau", exact=True).click()
        expect(page.locator('.page-heading .eyebrow')).to_contain_text("PRATINJAU")
        return page.url

    def confirm():
        page.get_by_label("Saya telah meninjau pelanggan", exact=False).check()
        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="Konfirmasi dan antrekan penerbitan", exact=True).click()
        expect(page.locator('.page-heading .eyebrow')).to_contain_text("MENUNGGU WORKER")

    period = month_ahead(1)
    batch_url = preview(period, 5000)
    expect(page.locator('.metric').nth(0).locator('strong')).to_have_text("28")
    expect(page.locator('.metric').nth(1).locator('strong')).to_have_text("Rp6.310.000")
    assert invoice_summary(period) == (0, 0)
    page.screenshot(path=str(evidence / "bulk-preview-desktop.png"), full_page=True)
    for width in [360, 768, 1366, 1920]:
        page.set_viewport_size({"width": width, "height": 920})
        for route in [batch_url, base + "/invoices/batches/", base + "/invoices/batches/new/"]:
            page.goto(route)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, route)
        if width == 360:
            page.goto(batch_url)
            page.screenshot(path=str(evidence / "bulk-preview-mobile.png"), full_page=True)
    page.set_viewport_size({"width": 1366, "height": 1000})
    page.goto(batch_url)
    confirm()
    # Confirm replay is a real CSRF-protected HTTP request, never a direct domain bypass.
    csrf = page.locator('form.batch-cancel input[name="csrfmiddlewaretoken"]').get_attribute('value')
    replay = page.context.request.post(batch_url + "confirm/", form={"csrfmiddlewaretoken": csrf, "reviewed": "on", "total": "1"})
    assert replay.ok
    assert invoice_summary(period) == (0, 0)
    worker(1)
    assert invoice_summary(period)[0] == 1
    # Verify an actual in-progress queue survives SQLite backup/restore, using SQL only.
    subprocess.run([executable, "scripts/verify_restore.py"], cwd=root, env=env, check=True, stdout=subprocess.DEVNULL)
    page.reload()
    expect(page.locator('.page-heading .eyebrow')).to_contain_text("DIPROSES")
    worker(500)
    worker(500)
    assert invoice_summary(period) == (28, 6310000)
    page.reload()
    expect(page.locator('.page-heading .eyebrow')).to_contain_text("SELESAI")
    expect(page.locator('.metric').nth(2).locator('strong')).to_have_text("28")
    page.screenshot(path=str(evidence / "bulk-completed-desktop.png"), full_page=True)
    invoice_link = page.locator('.batch-result').first.get_attribute('href')
    assert page.context.request.get(base + invoice_link + "pdf/").ok
    preview(period)
    expect(page.locator('.metric').nth(0).locator('strong')).to_have_text("0")
    expect(page.locator('.metric').nth(3).locator('strong')).to_have_text("28")
    confirm()
    worker(500)
    assert invoice_summary(period) == (28, 6310000)
    next_period = month_ahead(2)
    partial_url = preview(next_period)
    confirm()
    worker(1)
    page.reload()
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Batalkan sisa pekerjaan", exact=True).click()
    expect(page.locator('.page-heading .eyebrow')).to_contain_text("DIBATALKAN")
    worker(500)
    assert invoice_summary(next_period)[0] == 1
    page.goto(base + "/invoices/")
    expect(page.get_by_role("link", name="Faktur massal", exact=True)).to_be_visible()
    print("PASS: bulk preview/confirmation, CSRF replay, worker resume/retry, 28 invoices with server totals, duplicates skipped, partial cancellation, PDF authorization, 4 viewports.")
