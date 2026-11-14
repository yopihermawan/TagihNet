"""WIB event-date billing reports and historical receivables. No ledger mutations."""
from datetime import date, datetime, time, timedelta
from contextlib import contextmanager
from zoneinfo import ZoneInfo
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import BigIntegerField, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.db import connection, transaction
from django.utils import timezone
from .models import Invoice, Payment, PaymentReversal
from .operations import member_for
from .permissions import capabilities

WIB = ZoneInfo("Asia/Jakarta")
MODES = {"cash": "Penerimaan & koreksi", "invoices": "Faktur terbit", "receivables": "Piutang akhir"}
MAX_EXPORT_ROWS = 10000


@contextmanager
def report_snapshot():
    if connection.in_atomic_block:
        raise ValidationError("Laporan memerlukan transaksi baca tersendiri.")
    with transaction.atomic():
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        yield


def validate_range(start, end, today=None):
    today = today or timezone.localtime(timezone.now(), WIB).date()
    if type(start) is not date or type(end) is not date:
        raise ValidationError("Tanggal laporan tidak valid.")
    if start < date(2000, 1, 1) or end > today or start > end:
        raise ValidationError("Gunakan tanggal mulai sejak 1 Januari 2000 sampai tanggal akhir maksimal hari ini.")
    if (end-start).days > 365:
        raise ValidationError("Maksimal 366 hari kalender per laporan.")


def sum_amount(query, field):
    return query.aggregate(value=Sum(field))["value"] or 0


class BillingReport:
    def __init__(self, actor, start, end, exporting=False):
        self.member = member_for(actor)
        required = {"billing_reports", "invoices"} | ({"export_billing_reports"} if exporting else set())
        if not required <= capabilities(self.member):
            raise PermissionDenied("Izin laporan penagihan tidak tersedia.")
        validate_range(start, end)
        self.tenant, self.start, self.end = self.member.tenant, start, end
        self.lower = datetime.combine(start, time.min, WIB)
        self.upper = datetime.combine(end+timedelta(days=1), time.min, WIB)

    def payments(self):
        return Payment.objects.filter(tenant=self.tenant, invoice__tenant=self.tenant)

    def reversals(self):
        return PaymentReversal.objects.filter(payment__tenant=self.tenant, payment__invoice__tenant=self.tenant)

    def as_of_invoices(self):
        paid = self.payments().filter(invoice_id=OuterRef("pk"), created_at__lt=self.upper).filter(
            Q(reversal__isnull=True) | Q(reversal__created_at__gte=self.upper)).order_by()
        paid = paid.values("invoice_id").annotate(amount=Sum("amount")).values("amount")
        return Invoice.objects.filter(tenant=self.tenant, issued_at__lt=self.upper).annotate(
            paid_at_end=Coalesce(Subquery(paid, output_field=BigIntegerField()), Value(0), output_field=BigIntegerField())
        ).annotate(remaining=F("total")-F("paid_at_end"))

    def summary(self):
        invoices = Invoice.objects.filter(tenant=self.tenant)
        payments, reversals = self.payments(), self.reversals()
        opening = (sum_amount(invoices.filter(issued_at__lt=self.lower), "total")
            -sum_amount(payments.filter(created_at__lt=self.lower), "amount")
            +sum_amount(reversals.filter(created_at__lt=self.lower), "payment__amount"))
        issued = sum_amount(invoices.filter(issued_at__gte=self.lower, issued_at__lt=self.upper), "total")
        received = sum_amount(payments.filter(created_at__gte=self.lower, created_at__lt=self.upper), "amount")
        reversed_amount = sum_amount(reversals.filter(created_at__gte=self.lower, created_at__lt=self.upper), "payment__amount")
        closing = opening+issued-received+reversed_amount
        actual = sum_amount(self.as_of_invoices(), "remaining")
        overdue = sum_amount(self.as_of_invoices().filter(remaining__gt=0, due_date__lt=self.end), "remaining")
        return {"opening": opening, "issued": issued, "received": received, "reversed": reversed_amount,
            "net": received-reversed_amount, "closing": closing, "actual": actual,
            "overdue": overdue, "reconciles": closing == actual}

    def cash_entries(self):
        fields = ("entry_at", "entry_type", "entry_id", "invoice_pk", "invoice_period", "customer_label",
                  "signed_amount", "entry_note", "actor_label")
        payments = self.payments().filter(created_at__gte=self.lower, created_at__lt=self.upper).order_by().annotate(
            entry_at=F("created_at"), entry_type=Value("payment"), entry_id=F("pk"), invoice_pk=F("invoice_id"),
            invoice_period=F("invoice__period"), customer_label=F("invoice__customer_name"), signed_amount=F("amount"),
            entry_note=F("note"), actor_label=F("recorded_by__username")).values(*fields)
        reversals = self.reversals().filter(created_at__gte=self.lower, created_at__lt=self.upper).order_by().annotate(
            entry_at=F("created_at"), entry_type=Value("reversal"), entry_id=F("pk"), invoice_pk=F("payment__invoice_id"),
            invoice_period=F("payment__invoice__period"), customer_label=F("payment__invoice__customer_name"),
            signed_amount=-F("payment__amount"), entry_note=F("reason"), actor_label=F("recorded_by__username")).values(*fields)
        return payments.union(reversals, all=True).order_by("-entry_at", "entry_type", "-entry_id")

    def rows(self, mode):
        if mode == "cash":
            return self.cash_entries()
        if mode == "invoices":
            return self.as_of_invoices().filter(issued_at__gte=self.lower).order_by("-issued_at", "-pk")
        if mode == "receivables":
            return self.as_of_invoices().filter(remaining__gt=0).order_by("due_date", "pk")
        raise ValidationError("Jenis laporan tidak tersedia.")
