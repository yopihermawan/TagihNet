"""Exercise keyboard and WCAG rules on core pages using fictional data and Chromium."""
import argparse
from collections import Counter
from contextlib import closing
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright
import pyotp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.backup_archive import atomic_output

RULES = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']


def audit_page(page, axe):
    page.evaluate(axe)
    return page.evaluate('''async tags => {
        function luminance(color) {
            const channels = /^rgb[(]([0-9]+), ([0-9]+), ([0-9]+)[)]$/.exec(color);
            if (!channels) return null;
            const linear = channels.slice(1).map(value => {
                const channel = Number(value) / 255;
                return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4;
            });
            return .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2];
        }
        function flatBackground(element) {
            let background = null;
            for (let current = element; current; current = current.parentElement) {
                const style = getComputedStyle(current);
                if (style.opacity !== '1' || style.filter !== 'none' || style.mixBlendMode !== 'normal') return null;
                if (background !== null) continue;
                if (style.backgroundImage !== 'none') return null;
                if (style.backgroundColor === 'rgba(0, 0, 0, 0)') continue;
                background = luminance(style.backgroundColor);
                if (background === null) return null;
            }
            return background;
        }
        function ratio(foreground, background) {
            if (foreground === null || background === null) return null;
            return (Math.max(foreground, background) + .05) / (Math.min(foreground, background) + .05);
        }
        function measurement(target, contrast, minimum) {
            const status = contrast === null ? 'unresolved' : contrast >= minimum ? 'passed' : 'failed';
            return {target, ratio: contrast === null ? null : Number(contrast.toFixed(2)), minimum, status};
        }

        const result = await axe.run({runOnly: {type: 'tag', values: tags}});
        const ambiguous = result.incomplete.find(rule => rule.id === 'color-contrast')?.nodes || [];
        const flagged = ambiguous.map(({target}) => {
            const matches = target.length === 1 ? document.querySelectorAll(target[0]) : [];
            const element = matches.length === 1 ? matches[0] : null;
            const contrast = element ? ratio(luminance(getComputedStyle(element).color), flatBackground(element)) : null;
            return measurement(target, contrast, 4.5);
        });
        const fields = document.querySelectorAll('#id_username, #id_password, #id_name, #id_plan, #id_address, #id_amount');
        const boundaries = [...fields].filter(field => field.getClientRects().length && !field.matches(':disabled')).map(field => {
            const style = getComputedStyle(field);
            const border = style.borderTopStyle === 'solid' && parseFloat(style.borderTopWidth) > 0
                ? luminance(style.borderTopColor) : null;
            const inside = ratio(border, flatBackground(field));
            const outside = ratio(border, flatBackground(field.parentElement));
            const minimumContrast = inside === null || outside === null ? null : Math.min(inside, outside);
            return measurement('#' + field.id, minimumContrast, 3);
        });
        return {
            violations: result.violations.map(rule => ({id: rule.id, impact: rule.impact,
                nodes: rule.nodes.map(node => ({target: node.target, summary: node.failureSummary}))})),
            manual_review: result.incomplete.map(rule => ({id: rule.id, targets: rule.nodes.map(node => node.target)})),
            contrast: {flagged, boundaries},
            automated_rules_passed: result.passes.length,
            page_has_horizontal_overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
        };
    }''', RULES)


def ax_node(page, selector):
    session = page.context.new_cdp_session(page)
    try:
        root = session.send('DOM.getDocument')['root']['nodeId']
        node_id = session.send('DOM.querySelector', {'nodeId': root, 'selector': selector})['nodeId']
        assert node_id, selector
        node = session.send('Accessibility.getPartialAXTree', {'nodeId': node_id, 'fetchRelatives': False})['nodes'][0]
        assert not node['ignored'], selector
        return (node.get('role', {}).get('value'), node.get('name', {}).get('value'),
                node.get('description', {}).get('value', ''),
                {item['name']: item['value'].get('value') for item in node.get('properties', [])})
    finally:
        session.detach()


def check_error_focus(page, field=None):
    summary = page.locator('[data-form-error-summary]')
    expect(summary).to_be_focused()
    role, name, description, properties = ax_node(page, '[data-form-error-summary]')
    assert role == 'group' and name == 'Periksa isian formulir' and description and properties.get('focused') is True
    if field:
        summary.locator(f'a[href="#{field}"]').click()
        expect(page.locator(f'#{field}')).to_be_focused()
        _, _, description, properties = ax_node(page, f'#{field}')
        inline_error = page.locator(f'#{field}_error').inner_text().strip()
        assert inline_error and inline_error in description and properties.get('invalid') in (True, 'true'), field


def submit_login(page, password, username='owner'):
    page.get_by_label('Username', exact=True).fill(username)
    page.get_by_label('Kata sandi', exact=True).fill(password)
    page.get_by_role('button', name='Masuk ke workspace').click()


def invalid_totp(secret):
    totp = pyotp.TOTP(secret)
    now = int(time.time())
    nearby = {totp.at(now + step * totp.interval) for step in range(-2, 3)}
    return next(f'{number:06d}' for number in range(6) if f'{number:06d}' not in nearby)


def check_menu_announcements(page, base, width):
    mobile = width <= 850
    page.set_viewport_size({'width': width, 'height': 900})
    page.goto(base + '/')
    if mobile:
        page.get_by_role('button', name='Buka atau tutup navigasi', exact=True).click()
    name = 'Cari menu navigasi mobile' if mobile else 'Cari menu navigasi'
    search = page.get_by_role('searchbox', name=name, exact=True)
    selector = f'{".sidebar-search" if mobile else ".menu-search"} [data-menu-search-status]'
    status = page.locator(selector)
    search.fill('payroll')
    expect(status).to_have_text(re.compile(r'^[1-9][0-9]* menu ditemukan\.$'))
    search.fill('menu-fiktif-yang-tidak-tersedia')
    expect(status).to_have_text('0 menu ditemukan.')
    role, _, _, properties = ax_node(page, selector)
    assert role == 'status' and properties.get('live') == 'polite'
    search.fill('')
    if mobile:
        page.keyboard.press('Escape')


def check_theme_state(page, base):
    page.goto(base + '/')
    page.evaluate('localStorage.setItem("tagihnet-theme", "light")')
    page.reload()
    selector = '#theme-toggle'
    _, name, _, properties = ax_node(page, selector)
    assert name == 'Mode gelap' and properties.get('pressed') in ('false', False)
    page.get_by_role('button', name='Mode gelap', exact=True).click()
    page.reload()
    _, name, _, properties = ax_node(page, selector)
    assert name == 'Mode gelap' and properties.get('pressed') in ('true', True)


def run(options):
    axe = options.axe.read_text()
    if os.path.lexists(options.output):
        raise ValueError("Gunakan jalur laporan aksesibilitas yang baru.")
    with tempfile.TemporaryDirectory(prefix='tagihnet-accessibility-') as temporary:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        password = secrets.token_urlsafe(32)
        env = {name: value for name, value in os.environ.items() if not name.startswith('TAGIHNET_')}
        env.update(DJANGO_SETTINGS_MODULE='config.settings', TAGIHNET_DB_ENGINE='sqlite',
                   TAGIHNET_DATA_DIR=temporary, TAGIHNET_DEBUG='1',
                   TAGIHNET_SIMULATION='1', TAGIHNET_DEMO_PASSWORD=password)
        for args in (('migrate', '--noinput'), ('seed_demo',)):
            subprocess.run([sys.executable, 'manage.py', *args], cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
        portal_history = '''
from datetime import date, datetime, timedelta, timezone as utc_timezone
from uuid import uuid4
from django.utils import timezone
from core.billing import record_payment, reverse_payment
from core.models import Invoice, Membership, Payment

customer = Membership.objects.select_related('customer__tenant').get(user__username='pelanggan').customer
today = timezone.localdate()
due = today - timedelta(days=1)
Invoice.objects.bulk_create([
    Invoice(tenant=customer.tenant, customer=customer, period=date(today.year - 2, month, 1), due_date=due,
            customer_name=customer.name, customer_address='Alamat fiktif', subtotal=150000, total=150000)
    for month in range(1, 13)
])
Invoice.objects.create(tenant=customer.tenant, customer=customer, period=date(today.year - 1, 1, 1),
    due_date=today, customer_name=customer.name, customer_address='Alamat fiktif', subtotal=150000, total=150000)
invoice = Invoice.objects.get(customer=customer, period=date(today.year - 2, 12, 1))
operator = Membership.objects.get(tenant=customer.tenant, user__username='owner').user
posted = [record_payment(customer.tenant, invoice.pk, 1000, uuid4(), operator)[0].pk for _ in range(12)]
corrected, _ = record_payment(customer.tenant, invoice.pk, 2000, uuid4(), operator)
reverse_payment(customer.tenant, corrected.pk, 'Koreksi pembayaran fiktif', operator)
Payment.objects.filter(pk__in=[*posted, corrected.pk]).update(created_at=datetime(2024, 2, 29, 16, 30, tzinfo=utc_timezone.utc))
'''
        subprocess.run([sys.executable, 'manage.py', 'shell', '-c', portal_history], cwd=ROOT, env=env,
                       check=True, stdout=subprocess.DEVNULL)
        with closing(sqlite3.connect(Path(temporary) / 'tagihnet.sqlite3')) as db:
            customer = db.execute('SELECT id FROM core_customer ORDER BY id LIMIT 1').fetchone()[0]
            invoice = db.execute('SELECT id FROM core_invoice WHERE customer_id=? ORDER BY id LIMIT 1', (customer,)).fetchone()[0]
            portal_period = db.execute('''SELECT i.period FROM core_invoice i
                JOIN core_membership m ON m.customer_id=i.customer_id JOIN auth_user u ON u.id=m.user_id
                WHERE u.username=? ORDER BY i.period LIMIT 1''', ('pelanggan',)).fetchone()[0][:7]
        screens = {'dashboard': '/', 'customers': '/customers/', 'customer_form': '/customers/new/',
                   'customer_detail': f'/customers/{customer}/', 'invoices': '/invoices/',
                   'invoice_detail': f'/invoices/{invoice}/', 'invoice_create': '/invoices/new/',
                   'network': '/network/'}
        server = subprocess.Popen([sys.executable, 'manage.py', 'runserver', f'127.0.0.1:{port}', '--noreload'],
                                  cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        scans, keyboard, accessibility_tree = [], [], []
        try:
            for _ in range(100):
                try:
                    with urlopen(base + '/health/', timeout=.5) as reply:
                        if reply.status == 200:
                            break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError('Temporary application failed to start')
            with sync_playwright() as driver:
                chromium = os.getenv('CHROMIUM_PATH') or ('/usr/bin/chromium' if Path('/usr/bin/chromium').exists() else None)
                browser = driver.chromium.launch(executable_path=chromium, headless=True)
                context = browser.new_context(viewport={'width': 1366, 'height': 900}, reduced_motion='reduce')
                page = context.new_page()

                def scan(screen, tab=page):
                    width = tab.viewport_size['width']
                    theme = tab.locator('html').get_attribute('data-theme')
                    scans.append(dict(screen=screen, width=width, theme=theme, **audit_page(tab, axe)))

                def visit(path, theme):
                    page.evaluate('theme => localStorage.setItem("tagihnet-theme", theme)', theme)
                    response = page.goto(base + path)
                    assert response.status == 200 and page.url == base + path, path
                    expect(page.locator('html')).to_have_attribute('data-theme', theme)

                def check_skip_link(width, theme):
                    page.keyboard.press('Tab')
                    skip = page.get_by_role('link', name='Lewati navigasi')
                    focused = skip.evaluate('el => el === document.activeElement')
                    page.keyboard.press('Enter')
                    main = page.locator('main').evaluate('el => el === document.activeElement')
                    keyboard.append(dict(label='skip_link_reaches_main', width=width, theme=theme, passed=focused and main))

                page.goto(base + '/login/')
                for width in options.widths:
                    page.set_viewport_size({'width': width, 'height': 900})
                    for theme in ('light', 'dark'):
                        visit('/login/', theme)
                        check_skip_link(width, theme)
                        scan('login')
                submit_login(page, 'Sandi-uji-yang-sengaja-salah!')
                check_error_focus(page)
                accessibility_tree.append('login_error')
                scan('login_error')
                submit_login(page, password)
                page.wait_for_url(re.compile(re.escape(base) + r'/mfa/setup/(?:#main)?$'))
                for width in options.widths:
                    page.set_viewport_size({'width': width, 'height': 900})
                    for theme in ('light', 'dark'):
                        visit('/mfa/setup/', theme)
                        scan('mfa_setup')
                secret = page.locator('.provisioning-secret').text_content().strip()
                page.get_by_label('Kode authenticator (6 digit)').fill(invalid_totp(secret))
                page.get_by_role('button', name='Aktifkan MFA', exact=True).click()
                check_error_focus(page, field='id_code')
                accessibility_tree.append('mfa_enrollment_error')
                scan('mfa_enrollment_error')
                page.get_by_label('Kode authenticator (6 digit)').fill(pyotp.TOTP(secret).now())
                page.get_by_role('button', name='Aktifkan MFA', exact=True).click()
                page.get_by_role('link', name='Saya sudah menyimpan kode').click()
                for width in options.widths:
                    page.set_viewport_size({'width': width, 'height': 900})
                    for theme in ('light', 'dark'):
                        for name, path in screens.items():
                            visit(path, theme)
                            scan(name)
                            if name == 'network':
                                job_link = page.locator('.network-job-link').first
                                expect(job_link).to_have_css('text-decoration-line', 'underline')
                        if width <= 850:
                            visit('/', theme)
                            toggle = page.get_by_role('button', name='Buka atau tutup navigasi')
                            toggle.focus()
                            page.keyboard.press('Enter')
                            close = page.get_by_role('button', name='Tutup navigasi', exact=True)
                            expect(close).to_be_focused()
                            page.keyboard.press('Escape')
                            keyboard.append(dict(label='navigation_returns_focus', width=width, theme=theme,
                                                 passed=toggle.evaluate('el => el === document.activeElement')
                                                 and toggle.get_attribute('aria-expanded') == 'false'))
                        visit('/customers/', theme)
                        link = page.get_by_role('link', name=re.compile('^Ringkasan ')).first
                        link.focus()
                        page.keyboard.press('Enter')
                        dialog = page.get_by_role('dialog', name='Ringkasan cepat pelanggan')
                        expect(dialog).to_be_visible()
                        scan('customer_quick_view')
                        page.keyboard.press('Escape')
                        keyboard.append({'label': 'drawer_returns_focus', 'width': width, 'theme': theme,
                                         'passed': link.evaluate('el => el === document.activeElement') and not dialog.is_visible()})
                chromium_version = browser.version
                axe_version = page.evaluate('axe.version')
                visit('/customers/new/', 'light')
                page.locator('form.stack-form').evaluate('form => form.noValidate = true')
                page.get_by_role('button', name='Simpan', exact=True).click()
                check_error_focus(page, field='id_name')
                accessibility_tree.append('customer_error')
                scan('customer_error')
                for width, layout in ((1366, 'desktop'), (360, 'mobile')):
                    check_menu_announcements(page, base, width)
                    accessibility_tree.append(f'{layout}_menu_status')
                check_theme_state(page, base)
                accessibility_tree.append('theme_toggle_state')
                challenge = browser.new_context().new_page()
                challenge.goto(base + '/login/')
                submit_login(challenge, password)
                challenge.wait_for_url(base + '/mfa/verify/')
                challenge.locator('#id_code').fill(invalid_totp(secret))
                challenge.get_by_role('button', name='Verifikasi dan masuk', exact=True).click()
                check_error_focus(challenge, field='id_code')
                accessibility_tree.append('mfa_challenge_error')
                scan('mfa_challenge_error', challenge)

                portal = browser.new_context(viewport={'width': 360, 'height': 900}, reduced_motion='reduce').new_page()
                portal.goto(base + '/login/')
                submit_login(portal, password, username='pelanggan')
                portal.wait_for_url(base + '/portal/')
                table = portal.get_by_role('region', name='Tagihan saya', exact=True)
                billing = portal.get_by_role('region', name='Ringkasan tagihan', exact=True)
                all_periods = billing.locator('dl').inner_text()
                portal.get_by_label('Bulan layanan', exact=True).fill(portal_period)
                portal.get_by_label('Status tagihan', exact=True).select_option('overdue')
                portal.get_by_role('button', name='Terapkan', exact=True).click()
                expect(table.get_by_role('row')).to_have_count(2)
                expect(portal.get_by_label('Bulan layanan', exact=True)).to_have_value(portal_period)
                expect(billing.locator('dl')).to_have_text(all_periods, use_inner_text=True)
                scan('portal_invoices_month', portal)
                invalid = portal.goto(base + '/portal/?month=2024-13')
                assert invalid.status == 400
                check_error_focus(portal, field='id_month')
                scan('portal_invoices_invalid_month', portal)
                portal.get_by_role('link', name='Reset filter', exact=True).click()
                portal.wait_for_url(base + '/portal/?size=10')
                portal.get_by_label('Status tagihan', exact=True).select_option('overdue')
                portal.get_by_role('button', name='Terapkan', exact=True).click()
                portal.wait_for_url(re.compile(re.escape(base) + r'/portal/\?.*state=overdue'))
                expect(table.get_by_role('row')).to_have_count(11)
                expect(billing.get_by_text('Total sisa tagihan', exact=True)).to_be_visible()
                expect(billing.get_by_text('Tunggakan', exact=True)).to_be_visible()
                next_invoice = billing.get_by_role('link', name='Lihat faktur berikutnya', exact=True)
                destination = next_invoice.get_attribute('href')
                scan('portal_invoices_filtered', portal)
                next_invoice.click()
                portal.wait_for_url(base + destination)
                portal.go_back()
                expect(portal.get_by_label('Status tagihan', exact=True)).to_have_value('overdue')
                portal.get_by_role('link', name='Halaman berikutnya', exact=True).click()
                portal.wait_for_url(re.compile(re.escape(base) + r'/portal/\?.*state=overdue.*page=2'))
                expect(portal.get_by_label('Status tagihan', exact=True)).to_have_value('overdue')
                expect(portal.get_by_text('Halaman 2 / 2', exact=True)).to_be_visible()
                portal.set_viewport_size({'width': 1366, 'height': 900})
                portal.get_by_role('button', name='Mode gelap', exact=True).click()
                expect(portal.locator('html')).to_have_attribute('data-theme', 'dark')
                scan('portal_invoices_page_two', portal)
                table.get_by_role('link', name=re.compile('^Detail TN-')).first.click()
                portal.wait_for_url(re.compile(re.escape(base) + r'/invoices/[0-9]+/$'))
                portal.go_back()
                portal.get_by_role('link', name='Reset filter', exact=True).click()
                portal.wait_for_url(base + '/portal/?size=10')
                expect(portal.get_by_label('Status tagihan', exact=True)).to_have_value('')

                portal.get_by_role('link', name='Riwayat pembayaran', exact=True).click()
                expect(portal.get_by_role('heading', name='Riwayat pembayaran', exact=True)).to_be_visible()
                portal.set_viewport_size({'width': 360, 'height': 900})
                portal.get_by_role('button', name='Mode gelap', exact=True).click()
                expect(portal.locator('html')).to_have_attribute('data-theme', 'light')
                payment_status = portal.get_by_label('Status pembayaran', exact=True)
                payment_month = portal.get_by_label('Bulan pembayaran (WIB)', exact=True)
                payment_month.fill('2024-02')
                payment_status.select_option('recorded')
                portal.get_by_role('button', name='Terapkan', exact=True).click()
                expect(portal.get_by_role('heading', name='Ringkasan pembayaran Februari 2024')).to_be_visible()
                payments = portal.get_by_role('region', name='Riwayat pembayaran', exact=True)
                expect(payments.get_by_role('row')).to_have_count(11)
                scan('portal_payments_recorded', portal)
                portal.get_by_role('link', name='Halaman berikutnya', exact=True).click()
                expect(portal.get_by_text('Halaman 2 / 2', exact=True)).to_be_visible()
                expect(payment_status).to_have_value('recorded')
                expect(payment_month).to_have_value('2024-02')
                payment_status.select_option('reversed')
                portal.get_by_role('button', name='Terapkan', exact=True).click()
                expect(payments.get_by_text('Direversal', exact=True)).to_be_visible()
                expect(payments.get_by_role('row')).to_have_count(2)
                portal.set_viewport_size({'width': 1366, 'height': 900})
                portal.get_by_role('button', name='Mode gelap', exact=True).click()
                expect(portal.locator('html')).to_have_attribute('data-theme', 'dark')
                scan('portal_payments_reversed', portal)
                payments.get_by_role('link', name=re.compile('^Lihat kuitansi pembayaran ')).click()
                expect(portal.get_by_role('heading', name='Direversal · transaksi tidak berlaku')).to_be_visible()
                invalid = portal.goto(base + '/portal/payments/?month=2024-13')
                assert invalid.status == 400
                check_error_focus(portal, field='id_month')
                scan('portal_payments_invalid_month', portal)
                portal.goto(base + '/portal/')
                portal.get_by_role('link', name='Ajukan upgrade paket', exact=True).click()
                for width, theme in ((1366, 'dark'), (360, 'light')):
                    portal.set_viewport_size({'width': width, 'height': 900})
                    portal.evaluate('theme => localStorage.setItem("tagihnet-theme", theme)', theme)
                    portal.goto(base + '/portal/upgrade/')
                    expect(portal.locator('html')).to_have_attribute('data-theme', theme)
                    scan('portal_upgrade_options', portal)
                    portal.get_by_role('link', name='Tinjau Rumah Nyaman', exact=True).click()
                    expect(portal.get_by_role('heading', name='Tinjau upgrade paket')).to_be_visible()
                    expect(portal.get_by_text(re.compile(r'225\.000 per bulan'))).to_be_visible()
                    scan('portal_upgrade_review', portal)
                portal.get_by_role('button', name='Kirim pengajuan', exact=True).click()
                expect(portal.get_by_role('heading', name='Pengajuan upgrade paket', exact=True)).to_be_visible()
                portal.goto(base + '/portal/')
                expect(portal.get_by_role('heading', name='Rumah Hemat · 15 Mbps')).to_be_visible()
                page.goto(base + '/operations/tickets/')
                expect(page.get_by_text('Pengajuan upgrade paket', exact=True)).to_be_visible()
                browser.close()
        finally:
            server.terminate()
            server.wait(timeout=10)
    contrast = {group: Counter(measurement['status'] for scan in scans for measurement in scan['contrast'][group])
                for group in ('flagged', 'boundaries')}
    summary = {
        'scans': len(scans),
        'violating_scans': sum(bool(scan['violations']) for scan in scans),
        'rules': Counter(rule['id'] for scan in scans for rule in scan['violations']),
        'manual_review': Counter(rule['id'] for scan in scans for rule in scan['manual_review']),
        'contrast': contrast,
        'horizontal_overflow': [(scan['screen'], scan['width'], scan['theme'])
                                for scan in scans if scan['page_has_horizontal_overflow']],
        'keyboard_checks': len(keyboard),
        'keyboard_failed': [check for check in keyboard if not check['passed']],
        'accessibility_tree_checks': accessibility_tree,
        'portal_upgrade': 'passed',
        'portal_invoices': 'passed',
        'portal_payments': 'passed',
        'portal_payment_month': 'passed',
        'portal_invoice_month': 'passed',
        'portal_billing_summary': 'passed',
    }
    failed = (summary['violating_scans'] or summary['horizontal_overflow'] or summary['keyboard_failed']
              or contrast['flagged']['failed'] or contrast['boundaries']['failed'] or contrast['boundaries']['unresolved'])
    data = {'status': 'failed' if failed else 'passed', 'summary': summary, 'scans': scans, 'keyboard': keyboard,
            'axe_version': axe_version, 'chromium_version': chromium_version,
            'checkout_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'checkout_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT).strip())}
    with atomic_output(options.output) as stream:
        stream.write(json.dumps(data, indent=2, ensure_ascii=False).encode())
    print(json.dumps(summary, ensure_ascii=False))
    return int(bool(failed))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--axe', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--widths', type=int, nargs='+', default=[360, 768, 1366, 1920])
    raise SystemExit(run(parser.parse_args()))
