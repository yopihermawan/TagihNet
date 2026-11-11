"""Bounded ledger activity from one SQL snapshot; never contacts the gateway."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Case, CharField, F, Value, When
from django.db.models.functions import Cast
from django.utils import timezone
from .models import GatewayOrder, Invoice
from .permissions import capabilities, membership

WIB = ZoneInfo("Asia/Jakarta")
MAX_ENTRIES = 5000
KIND_LABELS = {"payment": "Pembayaran tercatat", "refund": "Refund penuh tercatat", "reversal": "Koreksi lain · perlu ditinjau"}


def validate_period(start, end):
    if (type(start) is not date or type(end) is not date or end < start or (end-start).days >= 31 or end == date.max):
        raise ValidationError("Pilih periode tanggal yang valid dan berurutan, maksimal 31 hari.")
    return datetime.combine(start, time.min, WIB), datetime.combine(end+timedelta(days=1), time.min, WIB)


def ledger_report(actor, start, end):
    member = membership(actor)
    if not actor.is_active or not {"gateway_reports", "invoices"} <= capabilities(member):
        raise PermissionDenied
    lower, upper = validate_period(start, end)
    # Scope every ledger/invoice relation independently; a foreign or broken link
    # cannot expose another ISP's customer, amount or correction.
    orders = GatewayOrder.objects.filter(tenant=member.tenant, invoice__tenant=member.tenant,
        payment__tenant=member.tenant, payment__invoice_id=F("invoice_id"), payment__source="midtrans_sandbox").order_by()
    columns = ["id", "invoice_id", "invoice__period", "invoice__customer_name", "transaction_id", "status",
        "last_verified_at", "last_checked_at", "entry_pk", "occurred_at", "entry_kind", "ledger_amount"]
    payments = orders.filter(payment__created_at__gte=lower, payment__created_at__lt=upper).annotate(
        entry_pk=Cast("payment_id", CharField()), occurred_at=F("payment__created_at"),
        entry_kind=Value("payment", output_field=CharField()), ledger_amount=F("payment__amount")).values(*columns)
    corrections = orders.filter(payment__reversal__created_at__gte=lower, payment__reversal__created_at__lt=upper).annotate(
        entry_pk=Cast("payment__reversal__pk", CharField()), occurred_at=F("payment__reversal__created_at"),
        entry_kind=Case(When(refund_resolution__reversal_id=F("payment__reversal__pk"),
            refund_resolution__amount=F("payment__amount"), refund_resolution__transaction_id=F("transaction_id"), then=Value("refund")),
            default=Value("reversal"), output_field=CharField()), ledger_amount=F("payment__amount")).values(*columns)
    # Both entry types, timestamps and amounts come from the same database statement.
    # Each response uses a bounded complete snapshot for its rows and totals.
    # A later page or CSV request takes a fresh snapshot, with its own timestamp.
    rows = list(payments.union(corrections, all=True).order_by("-occurred_at", "entry_kind", "entry_pk")[:MAX_ENTRIES+1])
    if len(rows) > MAX_ENTRIES:
        raise ValidationError("Periode memuat lebih dari 5.000 entri ledger. Pilih periode lebih pendek; laporan tidak dipotong sebagian.")
    totals = {kind: 0 for kind in KIND_LABELS}
    counts = totals.copy()
    for row in rows:
        kind, amount = row["entry_kind"], row["ledger_amount"]
        row["reference"] = ("PAY-" if kind == "payment" else "REV-")+row["entry_pk"]
        row["signed_amount"] = amount if kind == "payment" else -amount
        row["kind_label"] = KIND_LABELS[kind]
        row["order_reference"] = "TN-SBX-"+row["id"].hex
        row["invoice_number"] = Invoice(pk=row["invoice_id"], period=row["invoice__period"]).number
        row["status_label"] = dict(GatewayOrder.STATES).get(row["status"], row["status"])
        row["verification_failed"] = bool(row["last_checked_at"] and (
            not row["last_verified_at"] or row["last_checked_at"] > row["last_verified_at"]))
        totals[kind] += amount
        counts[kind] += 1
    totals["net"] = totals["payment"]-totals["refund"]-totals["reversal"]
    return {"rows": rows, "totals": totals, "counts": counts, "start": start, "end": end, "generated_at": timezone.now()}
