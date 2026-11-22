"""Tenant-scoped, current ledger amounts for read-only lists and dashboard aggregates."""
from django.db.models import BigIntegerField,F,OuterRef,Subquery,Sum,Value
from django.db.models.functions import Coalesce
from .models import Payment


INVOICE_STATUS_LABELS = {"issued": "Terbit", "partial": "Dibayar sebagian", "overdue": "Menunggak", "paid": "Lunas"}


def with_balances(invoices,tenant):
    valid=Payment.objects.filter(tenant=tenant,invoice_id=OuterRef('pk'),reversal__isnull=True).order_by()
    paid=valid.values('invoice_id').annotate(amount=Sum('amount')).values('amount')
    return invoices.filter(tenant=tenant).annotate(
        ledger_paid=Coalesce(Subquery(paid,output_field=BigIntegerField()),Value(0),output_field=BigIntegerField())
    ).annotate(remaining=F('total')-F('ledger_paid'))


def filter_invoice_status(invoices, state, today):
    if state == "paid":
        return invoices.filter(remaining=0)
    if state == "overdue":
        return invoices.exclude(remaining=0).filter(due_date__lt=today)
    if state in {"issued", "partial"}:
        unpaid = invoices.exclude(remaining=0).filter(due_date__gte=today)
        return unpaid.filter(ledger_paid=0) if state == "issued" else unpaid.exclude(ledger_paid=0)
    return invoices
