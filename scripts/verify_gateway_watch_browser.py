"""Chromium with a virtual clock and intercepted local fixtures; no remote service."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://billing.example.test"
URL = BASE+"/payments/orders/00000000-0000-0000-0000-000000000001/"
HTML = '''<!doctype html><html lang="id"><head><meta charset="utf-8">
<script src="/gateway-watch.js" defer></script></head><body>
<textarea aria-label="Alasan">Draft belum dikirim</textarea>
<section id="gateway-watch" data-url="snapshot/" data-revision="%s" hidden>
<p role="status" data-watch-message></p><a data-watch-reload href="%s" hidden>Tampilkan pembaruan</a>
<button data-watch-button type="button">Hentikan pemantauan</button></section></body></html>'''


def fixture(browser):
    context = browser.new_context()
    state = {"data": {"revision": "a"*64, "status": "pending", "label": "Menunggu pembayaran", "watch": True},
        "status": 200, "content_type": "application/json", "hold": False}
    requests, errors = [], []
    def route(request):
        if request.request.url.endswith("snapshot/"):
            requests.append(request.request.method)
            if state["hold"]:
                state["held_route"] = request
            else:
                request.fulfill(status=state["status"], content_type=state["content_type"], body=json.dumps(state["data"]))
        elif request.request.url.endswith("gateway-watch.js"):
            request.fulfill(content_type="text/javascript", body=(ROOT/"static/gateway-watch.js").read_text())
        else:
            request.fulfill(content_type="text/html", body=HTML % ("a"*64, URL))
    context.route(BASE+"/**", route)
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.clock.install()
    page.goto(URL)
    expect(page.locator('[data-watch-message]')).to_contain_text("Pemantauan halaman aktif")
    return context, page, state, requests, errors


def poll(page):
    with page.expect_response("**/snapshot/"):
        page.clock.run_for(5000)


with sync_playwright() as driver:
    browser = driver.chromium.launch(executable_path=os.getenv("CHROMIUM_PATH") or "/usr/bin/chromium", headless=True)
    context, page, state, requests, errors = fixture(browser)
    assert requests == []
    poll(page)
    assert requests == ["GET"]
    page.get_by_role("button", name="Hentikan pemantauan").click()
    page.clock.run_for(15000)
    assert len(requests) == 1
    page.get_by_role("button", name="Pantau lagi").click()
    poll(page)
    state["data"] = {"revision": "b"*64, "status": "settled", "label": "Pembayaran tercatat <img src=x>", "watch": False}
    poll(page)
    expect(page.locator('[data-watch-message]')).to_contain_text("Pembayaran tercatat <img src=x>")
    assert page.locator("img").count() == 0 and page.url == URL
    assert page.get_by_label("Alasan").input_value() == "Draft belum dikirim"
    expect(page.get_by_role("link", name="Tampilkan pembaruan")).to_be_visible()
    page.clock.run_for(60000)
    assert len(requests) == 3 and not errors
    context.close()
    print("PASS: stop/restart; changed snapshot uses text, preserves form/navigation, stops GETs and offers explicit reload.")

    context, page, state, requests, errors = fixture(browser)
    for _ in range(23):
        poll(page)
    page.clock.run_for(5000)
    expect(page.locator('[data-watch-message]')).to_contain_text("Pemantauan dua menit selesai")
    page.clock.run_for(60000)
    assert len(requests) == 23 and not errors
    context.close()
    print("PASS: two-minute wall-clock budget; 23 bounded reads at five-second intervals, then no further requests.")

    context, page, state, requests, errors = fixture(browser)
    page.evaluate("Object.defineProperty(document, 'hidden', {configurable:true, get:()=>true}); document.dispatchEvent(new Event('visibilitychange'))")
    page.clock.run_for(15000)
    assert requests == []
    page.evaluate("Object.defineProperty(document, 'hidden', {configurable:true, get:()=>false}); document.dispatchEvent(new Event('visibilitychange'))")
    poll(page)
    page.evaluate("Object.defineProperty(navigator, 'onLine', {configurable:true, get:()=>false})")
    page.clock.run_for(10000)
    assert len(requests) == 1
    page.evaluate("Object.defineProperty(navigator, 'onLine', {configurable:true, get:()=>true})")
    poll(page)
    page.evaluate("Object.defineProperty(document, 'hidden', {configurable:true, get:()=>true}); document.dispatchEvent(new Event('visibilitychange'))")
    page.clock.run_for(120000)
    expect(page.locator('[data-watch-message]')).to_contain_text("Pemantauan dua menit selesai")
    assert len(requests) == 2 and not errors
    context.close()
    print("PASS: controlled hidden/offline signals skip requests; hidden time does not extend the deadline.")

    variants = [{"status": code} for code in [401, 403, 404, 503]] + [
        {"content_type": "text/html"}, {"data": {"revision": "bad"}}, {"data": []}]
    for variant in variants:
        context, page, state, requests, errors = fixture(browser)
        state.update(variant)
        poll(page)
        expect(page.locator('[data-watch-message]')).to_contain_text("Pemantauan berhenti karena koneksi atau akses")
        page.clock.run_for(15000)
        assert len(requests) == 1 and page.url == URL and not errors
        context.close()
    print("PASS: authorization/errors/malformed responses stop monitoring without a retry loop, redirect or success claim.")

    context, page, state, requests, errors = fixture(browser)
    state["hold"] = True
    with page.expect_request("**/snapshot/"):
        page.clock.run_for(5000)
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.clock.run_for(5000)
    assert len(requests) == 1
    page.clock.run_for(3001)
    expect(page.locator('[data-watch-message]')).to_contain_text("Pemantauan berhenti karena koneksi atau akses")
    page.clock.run_for(15000)
    assert len(requests) == 1 and not errors
    state["held_route"].abort()
    context.close()
    print("PASS: eight-second request timeout aborts a stalled read; no overlapping request or automatic retry.")
    browser.close()
