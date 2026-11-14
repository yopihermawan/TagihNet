"""Customer filters share one SQL ledger calculation across list and export."""
from datetime import timedelta
from django.db.models import BigIntegerField, Exists, F, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from .models import Invoice, Payment

DUE_CHOICES = (
    ("overdue", "Menunggak"), ("today", "Jatuh tempo hari ini"),
    ("next7", "Jatuh tempo 7 hari mendatang"), ("unpaid", "Ada sisa tagihan"),
    ("clear", "Tanpa sisa tagihan"),
)


def positive_id(value):
    if value.isascii() and value.isdecimal() and len(value) <= 19 and 0 < int(value) <= 9223372036854775807:
        return int(value)
    return None


def receivables(tenant):
    payments = Payment.objects.filter(tenant=tenant, invoice_id=OuterRef("pk"), reversal__isnull=True).order_by()
    paid = payments.values("invoice_id").annotate(amount=Sum("amount")).values("amount")
    return Invoice.objects.filter(tenant=tenant).annotate(
        remaining=F("total")-Coalesce(Subquery(paid, output_field=BigIntegerField()), Value(0),
                                    output_field=BigIntegerField())).filter(remaining__gt=0)


def with_financial_summary(customers, tenant):
    pending = receivables(tenant).filter(customer_id=OuterRef("pk")).order_by()
    amount = pending.values("customer_id").annotate(amount=Sum("remaining")).values("amount")
    return customers.annotate(
        receivable_amount=Coalesce(Subquery(amount, output_field=BigIntegerField()), Value(0), output_field=BigIntegerField()),
        oldest_due=Subquery(pending.order_by("due_date", "pk").values("due_date")[:1]))


def apply_service_filters(customers, tenant, params, today=None):
    nas = params.get("nas", "")
    if nas == "unassigned":
        customers = customers.filter(nas__isnull=True)
    elif nas:
        nas_id = positive_id(nas)
        if nas_id is None:
            return customers.none()
        customers = customers.filter(nas_id=nas_id, nas__tenant=tenant)
    due = params.get("due", "")
    if due:
        if due not in dict(DUE_CHOICES):
            return customers.none()
        today = today or timezone.localdate()
        pending = receivables(tenant).filter(customer_id=OuterRef("pk"))
        if due == "overdue":
            pending = pending.filter(due_date__lt=today)
        elif due == "today":
            pending = pending.filter(due_date=today)
        elif due == "next7":
            pending = pending.filter(due_date__gt=today, due_date__lte=today+timedelta(days=7))
        customers = customers.filter(~Exists(pending) if due == "clear" else Exists(pending))
    return customers
