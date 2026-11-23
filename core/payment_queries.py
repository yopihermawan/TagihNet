"""Read payments for a single customer and summarize reversals separately."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db.models import Q, Sum

from .models import Payment

WIB = ZoneInfo("Asia/Jakarta")


def customer_payments(tenant_id, customer_id):
    return Payment.objects.filter(tenant_id=tenant_id, invoice__tenant_id=tenant_id,
                                  invoice__customer_id=customer_id).order_by("-created_at", "-pk")


def payment_totals(payments):
    values = payments.aggregate(gross=Sum("amount"), reversed=Sum("amount", filter=Q(reversal__isnull=False)),
                                valid=Sum("amount", filter=Q(reversal__isnull=True)))
    return {key: amount or 0 for key, amount in values.items()}


def payments_in_month(payments, month):
    # January of year 1 starts before the earliest representable UTC instant.
    if month > date.min:
        payments = payments.filter(created_at__gte=datetime.combine(month, time.min, WIB))
    if month == date.max.replace(day=1):
        return payments.filter(created_at__lte=datetime.max.replace(tzinfo=WIB))
    next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return payments.filter(created_at__lt=datetime.combine(next_month, time.min, WIB))
