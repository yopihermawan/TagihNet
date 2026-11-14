"""Read-only customer workspace; tab permissions precede each scoped query."""
from django.core.exceptions import PermissionDenied
from django.db.models import BigIntegerField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import Http404
from .models import AuditEvent, Invoice, NetworkJob, Payment
from .operations import visible_tickets
from .permissions import capabilities

TABS = (
    ("summary", "Ringkasan", None), ("service", "Layanan & Koneksi", None),
    ("invoices", "Faktur", "invoices"), ("payments", "Pembayaran", "invoices"),
    ("tickets", "Tiket", "tickets"), ("location", "Lokasi", None), ("history", "Riwayat", "audit"),
)


def profile_context(member, customer, selected):
    if customer.tenant_id != member.tenant_id:
        raise PermissionDenied
    rights = capabilities(member)
    required = next((permission for key, _, permission in TABS if key == selected), False)
    if required is False:
        raise Http404("Tab pelanggan tidak tersedia.")
    if required and required not in rights:
        raise PermissionDenied("Anda tidak memiliki izin membaca tab pelanggan ini.")
    context = {"customer": customer, "tab": selected,
        "tabs": [(key, label) for key, label, permission in TABS if not permission or permission in rights],
        "tab_template": f"customers/{selected}.html", "balance": customer.receivable_amount}
    tenant = member.tenant
    rows = None
    if selected == "service":
        rows = NetworkJob.objects.filter(tenant=tenant, customer=customer).order_by("-updated_at", "-pk")
    elif selected == "invoices":
        rows = Invoice.objects.filter(tenant=tenant, customer=customer).annotate(
            ledger_paid=Coalesce(Sum("payments__amount", filter=Q(payments__tenant=tenant,
                payments__reversal__isnull=True)), Value(0), output_field=BigIntegerField())).order_by("-period", "-pk")
    elif selected == "payments":
        rows = Payment.objects.filter(tenant=tenant, invoice__tenant=tenant, invoice__customer=customer).select_related(
            "invoice", "recorded_by", "reversal__recorded_by").order_by("-created_at", "-pk")
        totals = rows.aggregate(gross=Sum("amount"), reversed=Sum("amount", filter=Q(reversal__isnull=False)),
            valid=Sum("amount", filter=Q(reversal__isnull=True)))
        context["payment_totals"] = {key: amount or 0 for key, amount in totals.items()}
    elif selected == "tickets":
        rows = visible_tickets(member).filter(customer=customer).select_related("assigned_to__user")
    elif selected == "history":
        rows = AuditEvent.objects.filter(tenant=tenant, target=str(customer.pk),
            action__in=["customer.created", "customer.updated"]).select_related("actor").order_by("-created_at", "-pk")
    return context, rows


def invoice_rows(invoices, today):
    rows = []
    labels = {"paid": "Lunas", "overdue": "Menunggak", "partial": "Dibayar sebagian", "issued": "Terbit"}
    for invoice in invoices:
        balance = invoice.total-invoice.ledger_paid
        status = "paid" if balance == 0 else "overdue" if invoice.due_date < today else "partial" if invoice.ledger_paid else "issued"
        rows.append({"invoice": invoice, "paid": invoice.ledger_paid, "balance": balance,
            "status": status, "label": labels[status]})
    return rows
