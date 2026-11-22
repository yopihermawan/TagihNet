"""Read payments for a single customer and summarize reversals separately."""
from django.db.models import Q, Sum

from .models import Payment


def customer_payments(tenant_id, customer_id):
    return Payment.objects.filter(tenant_id=tenant_id, invoice__tenant_id=tenant_id,
                                  invoice__customer_id=customer_id).order_by("-created_at", "-pk")


def payment_totals(payments):
    values = payments.aggregate(gross=Sum("amount"), reversed=Sum("amount", filter=Q(reversal__isnull=False)),
                                valid=Sum("amount", filter=Q(reversal__isnull=True)))
    return {key: amount or 0 for key, amount in values.items()}
