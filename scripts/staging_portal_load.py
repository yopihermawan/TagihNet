"""Synthetic fixtures and real HTTPS sessions for the Compose portal load profile."""
import http.cookiejar
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, HTTPCookieProcessor, ProxyHandler, Request, build_opener


FIXTURE_COUNTS = {
    'tenants': 2, 'customers': 500, 'initial_invoices': 2000,
    'payments': 8500, 'reversals': 500, 'portal_accounts': 2,
}

FIXTURE = '''
import json
import secrets
import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.urls import reverse
from django.utils import timezone

from core.billing import invoice_snapshot
from core.models import Customer, Invoice, InvoiceLine, Membership, Payment, PaymentReversal, Plan, Tenant

users = get_user_model().objects
owner = users.get(username='staging-owner')
assert settings.SIMULATION and Tenant.objects.count() == 1 and not Customer.objects.exists()
assert not Invoice.objects.exists() and not Payment.objects.exists()
period = timezone.localdate().replace(day=1)
periods = []
for _ in range(4):
    period = (period - timedelta(days=1)).replace(day=1)
    periods.append(period)
payment_month = periods[0]
payment_at = datetime.combine(payment_month, time.min, tzinfo=ZoneInfo('Asia/Jakarta'))
accounts = []

def references(invoice, payment):
    return {
        'number': invoice.number,
        'invoice': reverse('invoice_detail', args=[invoice.pk]),
        'invoice_pdf': reverse('invoice_pdf', args=[invoice.pk]),
        'receipt': reverse('payment_receipt', args=[invoice.pk, payment.pk]),
        'receipt_pdf': reverse('payment_receipt_pdf', args=[invoice.pk, payment.pk]),
    }

with transaction.atomic():
    other = Tenant.objects.create(name='ISP Portal Kedua')
    other_owner = users.create_user(username='owner-portal-fixture')
    Membership.objects.create(user=other_owner, tenant=other, role='owner')
    for number, actor in enumerate((owner, other_owner), 1):
        tenant = actor.membership.tenant
        plan = Plan.objects.create(tenant=tenant, name='Paket Portal Fiktif', speed_mbps=30, price=150000)
        customers = Customer.objects.bulk_create([
            Customer(tenant=tenant, plan=plan, code=f'PORTAL-{number}-{index:03d}',
                     name=f'Pelanggan Portal {number}-{index:03d}', username=f'portal-{number}-{index:03d}',
                     phone=f'081299{number}{index:05d}', address='Alamat fiktif', area='Portal')
            for index in range(250)
        ])
        invoices = Invoice.objects.bulk_create([
            Invoice(tenant=tenant, customer=customer, period=period,
                    **invoice_snapshot(tenant, customer, period)['invoice'])
            for customer in customers for period in periods
        ])
        InvoiceLine.objects.bulk_create([
            InvoiceLine(invoice=invoice, description='Layanan portal fiktif', quantity=1, unit_price=invoice.subtotal)
            for invoice in invoices
        ])
        payments = Payment.objects.bulk_create([
            Payment(tenant=tenant, invoice=invoices[4 * index + part % 4], amount=1000,
                    event_key=uuid.uuid4(), recorded_by=actor, created_at=payment_at)
            for index in range(250) for part in range(17)
        ])
        PaymentReversal.objects.bulk_create([
            PaymentReversal(payment=payment, recorded_by=actor, created_at=payment_at + timedelta(days=1),
                            reason='Koreksi pembayaran fiktif')
            for payment in payments[::17]
        ])
        password = secrets.token_urlsafe(36)
        user = users.create_user(username=f'customer-load-{number}', password=password)
        Membership.objects.create(user=user, tenant=tenant, role='customer', customer=customers[0])
        accounts.append({'username': user.username, 'password': password,
                         'own': references(invoices[0], payments[0]), 'peer': references(invoices[4], payments[17])})

with connection.cursor() as cursor:
    for model in (Customer, Invoice, InvoiceLine, Payment, PaymentReversal):
        cursor.execute('ANALYZE ' + connection.ops.quote_name(model._meta.db_table))
print('LOAD:' + json.dumps({'accounts': accounts, 'month': payment_month.strftime('%Y-%m')}))
'''

SNAPSHOT = '''
import hashlib
import json

from django.core.serializers.json import DjangoJSONEncoder
from core.models import Customer, Invoice, InvoiceLine, Payment, PaymentReversal

records = {}
for model in (Customer, Invoice, InvoiceLine, Payment, PaymentReversal):
    digest = hashlib.sha256()
    count = 0
    for row in model.objects.order_by('pk').values_list().iterator():
        digest.update(json.dumps(row, cls=DjangoJSONEncoder).encode() + b'\\n')
        count += 1
    records[model._meta.model_name] = [count, digest.hexdigest()]
print('LOAD:' + json.dumps(records))
'''


def login(port, context, account):
    base = f'https://localhost:{port}'
    url = base + '/login/'
    jar = http.cookiejar.CookieJar()
    client = build_opener(ProxyHandler({}), HTTPSHandler(context=context), HTTPCookieProcessor(jar))
    with client.open(url, timeout=10) as response:
        if response.status != 200 or response.url != url:
            raise RuntimeError('Portal login form unavailable')
    csrf = next(cookie.value for cookie in jar if cookie.name == 'csrftoken')
    data = urlencode({'username': account['username'], 'password': account['password'],
                      'csrfmiddlewaretoken': csrf}).encode()
    request = Request(url, data=data, headers={'Origin': base, 'Referer': url})
    with client.open(request, timeout=10) as response:
        if response.status != 200 or response.url != base + '/portal/':
            raise RuntimeError('Portal login did not reach the customer page')
    cookies = {cookie.name: cookie.value for cookie in jar}
    if 'sessionid' not in cookies:
        raise RuntimeError('Portal session unavailable')
    return cookies


def routes(account, month):
    own = account['own']
    number = own['number'].encode()
    return [
        ('invoice_list', '/portal/', number),
        ('invoice_month_status', f'/portal/?month={month}&state=overdue', (number, b'Menunggak')),
        ('payment_month_page_two', f'/portal/payments/?month={month}&size=10&page=2', own['receipt'].encode()),
        ('reversed_receipt', own['receipt'], (number, 'Direversal · transaksi tidak berlaku'.encode())),
    ]
