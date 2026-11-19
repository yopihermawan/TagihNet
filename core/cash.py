"""Cash flow in integer rupiah. Original events and later corrections keep their dates."""
import calendar
from datetime import date
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.utils import timezone
from .models import CashClose, CashCorrection, CashMovement, Fund, Payment, PaymentReversal
from .operations import digest

OUTGOING = {'expense', 'transfer_out', 'payroll', 'commission'}
MANUAL = {'funding', 'income', 'expense'}
MAX_AMOUNT = 2_000_000_000


def total(query, field='amount'):
    return query.aggregate(value=Sum(field))['value'] or 0


def cash_summary(tenant, end=None, start=None):
    """A bounded number of aggregate queries, independent of transaction count."""
    funds = list(Fund.objects.filter(tenant=tenant))
    rows = {f.pk: {'id':f.pk, 'name':f.name, 'active':f.active, 'opening':0, 'in':0, 'out':0} for f in funds}
    rows[None] = {'id':None, 'name':'Belum dipetakan', 'active':True, 'opening':0, 'in':0, 'out':0}
    payments = Payment.objects.filter(tenant=tenant)
    reversals = PaymentReversal.objects.filter(payment__tenant=tenant)
    movements = CashMovement.objects.filter(tenant=tenant)
    corrections = CashCorrection.objects.filter(tenant=tenant)
    streams = [(payments, 'created_at__date', 'funding__fund_id', 'amount', False),
        (reversals, 'created_at__date', 'payment__funding__fund_id', 'payment__amount', True)]
    for query, when, account, amount, outgoing in streams:
        if end: query=query.filter(**{when+'__lte':end})
        if start:
            for item in query.filter(**{when+'__lt':start}).values(account).annotate(value=Sum(amount)):
                rows[item[account]]['opening'] += (-1 if outgoing else 1)*item['value']
            query=query.filter(**{when+'__gte':start})
        for item in query.values(account).annotate(value=Sum(amount)):
            rows[item[account]]['out' if outgoing else 'in'] += item['value']
    for query, when, account, amount, kind, invert in [
        (movements,'business_date','fund_id','amount','kind',False),
        (corrections,'created_at__date','movement__fund_id','movement__amount','movement__kind',True)]:
        if end:query=query.filter(**{when+'__lte':end})
        if start:
            for item in query.filter(**{when+'__lt':start}).values(account,kind).annotate(value=Sum(amount)):
                negative=(item[kind] in OUTGOING) != invert
                rows[item[account]]['opening'] += (-1 if negative else 1)*item['value']
            query=query.filter(**{when+'__gte':start})
        for item in query.values(account,kind).annotate(value=Sum(amount)):
            negative=(item[kind] in OUTGOING) != invert
            rows[item[account]]['out' if negative else 'in'] += item['value']
    for row in rows.values():row['closing']=row['opening']+row['in']-row['out']
    return list(rows.values())


def balance(tenant, fund_id):
    return next(row['closing'] for row in cash_summary(tenant) if row['id']==fund_id)


def open_date(tenant, value):
    if not isinstance(value,date) or value>timezone.localdate():
        raise ValidationError('Tanggal kas harus hari ini atau sebelumnya.')
    latest=CashClose.objects.filter(tenant=tenant).order_by('-period').first()
    if latest and value<=latest.period.replace(day=calendar.monthrange(latest.period.year,latest.period.month)[1]):
        raise ValidationError('Periode kas sudah ditutup. Catat koreksi pada periode terbuka.')


def post(member, fund, kind, amount, when, note, key, *, leg=1, source=''):
    if type(amount) is not int or not 1<=amount<=MAX_AMOUNT or kind not in dict(CashMovement.KINDS):
        raise ValidationError('Jenis atau nominal kas tidak valid.')
    if fund.tenant_id!=member.tenant_id or not fund.active:
        raise ValidationError('Pilih dana aktif ISP ini.')
    open_date(member.tenant,when)
    if kind in OUTGOING and balance(member.tenant,fund.pk)<amount:
        raise ValidationError('Saldo dana tidak mencukupi. Tidak ada pencatatan diterapkan.')
    return CashMovement.objects.create(tenant=member.tenant,fund=fund,kind=kind,amount=amount,
        business_date=when,note=note,actor=member.user,request_key=key,leg=leg,source_key=source,
        fingerprint=digest([kind,amount,str(when),fund.pk,note,source]))


def correct(member, movement, reason):
    open_date(member.tenant,timezone.localdate())
    if not 10<=len(reason.strip())<=250:raise ValidationError('Alasan koreksi harus 10–250 karakter.')
    # Corrections restore the truth even when spending has made an incoming balance insufficient.
    correction,_=CashCorrection.objects.get_or_create(tenant=member.tenant,movement=movement,
        defaults={'actor':member.user,'reason':reason.strip()})
    return correction


def close_snapshot(tenant, period):
    if period.day!=1 or period>=timezone.localdate().replace(day=1):
        raise ValidationError('Tutup buku hanya untuk bulan yang sudah selesai.')
    end=period.replace(day=calendar.monthrange(period.year,period.month)[1])
    return cash_summary(tenant,end,period)
