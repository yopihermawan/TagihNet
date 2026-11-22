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
        const result = await axe.run({runOnly: {type: 'tag', values: tags}});
        return {
            violations: result.violations.map(rule => ({id: rule.id, impact: rule.impact,
                nodes: rule.nodes.map(node => ({target: node.target, summary: node.failureSummary}))})),
            manual_review: result.incomplete.map(rule => ({id: rule.id, targets: rule.nodes.map(node => node.target)})),
            automated_rules_passed: result.passes.length,
            page_has_horizontal_overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
        };
    }''', RULES)


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
        with closing(sqlite3.connect(Path(temporary) / 'tagihnet.sqlite3')) as db:
            customer = db.execute('SELECT id FROM core_customer ORDER BY id LIMIT 1').fetchone()[0]
            invoice = db.execute('SELECT id FROM core_invoice WHERE customer_id=? ORDER BY id LIMIT 1', (customer,)).fetchone()[0]
        screens = {'dashboard': '/', 'customers': '/customers/', 'customer_form': '/customers/new/',
                   'customer_detail': f'/customers/{customer}/', 'invoices': '/invoices/',
                   'invoice_detail': f'/invoices/{invoice}/', 'invoice_create': '/invoices/new/',
                   'network': '/network/'}
        server = subprocess.Popen([sys.executable, 'manage.py', 'runserver', f'127.0.0.1:{port}', '--noreload'],
                                  cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        scans, keyboard = [], []
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

                def scan(screen, width, theme):
                    result = audit_page(page, axe)
                    scans.append(dict(screen=screen, width=width, theme=theme, **result))

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
                        scan('login', width, theme)
                page.get_by_label('Username', exact=True).fill('owner')
                page.get_by_label('Kata sandi', exact=True).fill(password)
                page.get_by_role('button', name='Masuk ke workspace').click()
                page.wait_for_url(re.compile(re.escape(base) + r'/mfa/setup/(?:#main)?$'))
                for width in options.widths:
                    page.set_viewport_size({'width': width, 'height': 900})
                    for theme in ('light', 'dark'):
                        visit('/mfa/setup/', theme)
                        scan('mfa_setup', width, theme)
                secret = page.locator('.provisioning-secret').text_content().strip()
                page.get_by_label('Kode authenticator (6 digit)').fill(pyotp.TOTP(secret).now())
                page.get_by_role('button', name='Aktifkan MFA', exact=True).click()
                page.get_by_role('link', name='Saya sudah menyimpan kode').click()
                for width in options.widths:
                    page.set_viewport_size({'width': width, 'height': 900})
                    for theme in ('light', 'dark'):
                        for name, path in screens.items():
                            visit(path, theme)
                            scan(name, width, theme)
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
                        scan('customer_quick_view', width, theme)
                        page.keyboard.press('Escape')
                        keyboard.append({'label': 'drawer_returns_focus', 'width': width, 'theme': theme,
                                         'passed': link.evaluate('el => el === document.activeElement') and not dialog.is_visible()})
                chromium_version = browser.version
                axe_version = page.evaluate('axe.version')
                browser.close()
        finally:
            server.terminate()
            server.wait(timeout=10)
    summary = {
        'scans': len(scans),
        'violating_scans': sum(bool(scan['violations']) for scan in scans),
        'rules': Counter(rule['id'] for scan in scans for rule in scan['violations']),
        'manual_review': Counter(rule['id'] for scan in scans for rule in scan['manual_review']),
        'horizontal_overflow': [(scan['screen'], scan['width'], scan['theme'])
                                for scan in scans if scan['page_has_horizontal_overflow']],
        'keyboard_checks': len(keyboard),
        'keyboard_failed': [check for check in keyboard if not check['passed']],
    }
    failed = summary['violating_scans'] or summary['horizontal_overflow'] or summary['keyboard_failed']
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
