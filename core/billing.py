"""Money and ledger mutations. All amounts are integer rupiah; no network I/O."""
import calendar
import time
import uuid
from datetime import date
from functools import wraps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import OperationalError, transaction
from django.utils import timezone
from .models import AuditEvent, Customer, Invoice, InvoiceLine, NetworkJob, Payment, PaymentReversal, Tenant, month_due
from .document_identity import capture_invoice_issuer, capture_payment_issuer
from .isolation import GRACE_FIELD, configured_grace_days, validate_grace_days
from .isolation_models import InvoiceIsolationTerms


def retry_locked(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        for attempt in range(5):
            try:
                return function(*args, **kwargs)
            except OperationalError as exc:
                cause = exc.__cause__
                state = getattr(cause, "sqlstate", None)
                sqlite_code = getattr(cause, "sqlite_errorcode", 0)
                retryable = (state in {"40001", "40P01", "55P03"} or (sqlite_code & 255) in {5, 6}
                    or (state is None and str(exc).lower().startswith(
                        ("database is locked", "database table is locked", "database schema is locked"))))
                if not retryable or attempt == 4:
                    raise
                time.sleep(0.04 * (2 ** attempt))
    return wrapped


def audit(tenant, actor, action, target, detail="", correlation_id=None):
    return AuditEvent.objects.create(tenant=tenant, actor=actor, action=action, target=str(target),
                                     detail=detail, correlation_id=correlation_id or uuid.uuid4())


def totals(price, discount, tax_bp):
    if not all(type(value) is int for value in (price, discount, tax_bp)):
        raise ValidationError("Nominal harus berupa integer rupiah.")
    if not 1 <= price <= 100000000 or not 0 <= discount < price or not 0 <= tax_bp <= 10000:
        raise ValidationError("Harga, diskon, atau pajak di luar batas.")
    net = price - discount
    tax = (net * tax_bp + 5000) // 10000
    return net + tax, tax


def prorated_days(period, service_start):
    if type(service_start) is not date or service_start.replace(day=1) != period:
        raise ValidationError("Tanggal awal prorata harus berada dalam bulan layanan yang dipilih.")
    if service_start > timezone.localdate():
        raise ValidationError("Tanggal awal prorata tidak boleh setelah hari ini (WIB).")
    days_in_month = calendar.monthrange(period.year, period.month)[1]
    return days_in_month - service_start.day + 1, days_in_month


def invoice_snapshot(tenant, customer, period, discount=0, *, service_start=None, grace_days=None):
    if customer.tenant_id != tenant.pk or customer.plan.tenant_id != tenant.pk:
        raise ValidationError("Paket dan pelanggan harus berada pada ISP yang sama.")
    if period.day != 1:
        raise ValidationError("Periode harus tanggal pertama bulan layanan.")
    monthly_price = subtotal = customer.plan.price
    due_date = month_due(period, tenant.due_day)
    description = f"{customer.plan.name} · {customer.plan.speed_mbps} Mbps · {period:%m/%Y}"
    proration = None
    if service_start is not None:
        days, days_in_month = prorated_days(period, service_start)
        end = period.replace(day=days_in_month)
        # Round the exact fraction half up before tax. Positive invoices have a Rp1 minimum.
        subtotal = max(1, (2 * monthly_price * days + days_in_month) // (2 * days_in_month))
        due_date = max(due_date, service_start)
        rate = f"Rp{monthly_price:,}".replace(",", ".")
        description += f" · Prorata {service_start:%d/%m/%Y}–{end:%d/%m/%Y} ({days}/{days_in_month} hari) · {rate}/bulan"
        proration = {"start": service_start.isoformat(), "end": end.isoformat(), "days": days,
                     "days_in_month": days_in_month, "monthly_price": monthly_price}
    total, tax = totals(subtotal, discount, tenant.tax_bp)
    snapshot = {"schema": 1, "tenant_id": tenant.pk, "customer_id": customer.pk,
        "plan_id": customer.plan_id, "service_state": customer.service_state, "period": period.isoformat(),
        "invoice": {"due_date": due_date.isoformat(), "customer_name": customer.name,
            "customer_address": customer.address, "subtotal": subtotal, "discount": discount,
            "tax_bp": tenant.tax_bp, "tax_amount": tax, "total": total, "footer": tenant.invoice_footer},
        "line": {"description": description, "quantity": 1, "unit_price": subtotal}}
    if proration is not None:
        snapshot["proration"] = proration
    grace_days = validate_grace_days(configured_grace_days(tenant) if grace_days is None else grace_days)
    # Omission represents zero, preserving existing approved snapshots and their fingerprints.
    if grace_days:
        snapshot[GRACE_FIELD] = grace_days
    return snapshot


def write_snapshot_invoice(tenant, customer, period, snapshot, actor, correlation_id=None):
    """Internal writer; caller holds transaction. Snapshot must originate from server-side preview."""
    if (snapshot["schema"] != 1 or snapshot["tenant_id"] != tenant.pk or customer.tenant_id != tenant.pk
            or snapshot["customer_id"] != customer.pk or snapshot["period"] != period.isoformat()):
        raise ValidationError("Snapshot faktur tidak sesuai cakupan.")
    values = snapshot["invoice"].copy()
    line = snapshot["line"].copy()
    total, tax = totals(values["subtotal"], values["discount"], values["tax_bp"])
    if values["total"] != total or values["tax_amount"] != tax or line["quantity"] != 1 or line["unit_price"] != values["subtotal"]:
        raise ValidationError("Snapshot nominal tidak konsisten.")
    values["due_date"] = date.fromisoformat(values["due_date"])
    grace_days = validate_grace_days(snapshot.get(GRACE_FIELD, 0))
    invoice = Invoice.objects.create(tenant=tenant, customer=customer, period=period, **values)
    capture_invoice_issuer(invoice)
    if grace_days:
        InvoiceIsolationTerms.objects.create(invoice=invoice, grace_days=grace_days)
    InvoiceLine.objects.create(invoice=invoice, **line)
    audit(tenant, actor, "invoice.issued", invoice.pk, invoice.number, correlation_id)
    return invoice


@retry_locked
@transaction.atomic
def issue_invoice(tenant, customer_id, period, actor, discount=0, correlation_id=None):
    customer = Customer.objects.select_for_update(of=("self",)).select_related("plan").get(tenant=tenant, pk=customer_id)
    if customer.plan.tenant_id != tenant.pk:
        raise ValidationError("Paket tidak berada pada ISP yang sama.")
    if period.day != 1:
        raise ValidationError("Periode harus tanggal pertama bulan layanan.")
    existing = Invoice.objects.filter(tenant=tenant, customer=customer, period=period).first()
    if existing:
        if existing.discount != discount:
            raise ValidationError("Faktur periode ini sudah terbit dengan diskon berbeda.")
        return existing, False
    tenant = Tenant.objects.select_related('isolation_policy').get(pk=tenant.pk)
    invoice = write_snapshot_invoice(tenant, customer, period, invoice_snapshot(tenant, customer, period, discount), actor, correlation_id)
    return invoice, True


@retry_locked
@transaction.atomic
def record_payment(tenant, invoice_id, amount, event_key, actor, note="", correlation_id=None, fund_id=None):
    if not settings.SIMULATION:
        raise ValidationError("Adapter pembayaran produksi belum tersedia. Pencatatan ini hanya untuk simulasi.")
    from .models import Fund, PaymentFunding
    tenant = Tenant.objects.select_for_update(no_key=True).get(pk=tenant.pk)
    invoice = Invoice.objects.select_for_update().get(tenant=tenant, pk=invoice_id)
    # Serialize all billing and worker actions through this customer's row.
    Customer.objects.select_for_update().get(pk=invoice.customer_id, tenant=tenant)
    existing = Payment.objects.filter(tenant=tenant, event_key=event_key).first()
    if existing:
        allocated=PaymentFunding.objects.filter(payment=existing).values_list('fund_id',flat=True).first()
        if existing.invoice_id != invoice.pk or existing.amount != amount or allocated != fund_id:
            raise ValidationError("Kunci idempotensi telah digunakan untuk pembayaran berbeda.")
        return existing, False
    if type(amount) is not int or amount <= 0 or amount > invoice.balance:
        raise ValidationError("Nominal harus positif dan tidak melebihi sisa tagihan.")
    fund=None
    if fund_id is not None:
        if type(fund_id) is not int:raise ValidationError('Dana tidak valid.')
        fund=Fund.objects.filter(tenant=tenant,pk=fund_id,active=True).first()
        if fund is None:raise ValidationError('Pilih dana aktif pada ISP yang sama.')
    # A shared event key can race across different customers, whose row locks are independent.
    # get_or_create contains the unique-key insert in a savepoint and returns the winner.
    payment, created = Payment.objects.get_or_create(tenant=tenant, event_key=event_key,
        defaults={"invoice": invoice, "amount": amount, "recorded_by": actor, "note": note})
    if not created:
        allocated=PaymentFunding.objects.filter(payment=payment).values_list('fund_id',flat=True).first()
        if payment.invoice_id != invoice.pk or payment.amount != amount or allocated != fund_id:
            raise ValidationError("Kunci idempotensi telah digunakan untuk pembayaran berbeda.")
        return payment, False
    capture_payment_issuer(payment, tenant)
    if fund:PaymentFunding.objects.create(tenant=tenant,payment=payment,fund=fund)
    NetworkJob.objects.create(tenant=tenant, customer=invoice.customer)
    audit(tenant, actor, "payment.simulated", payment.pk, f"{invoice.number}; Rp{amount}", correlation_id)
    return payment, True


@retry_locked
@transaction.atomic
def reverse_payment(tenant, payment_id, reason, actor, correlation_id=None):
    if not settings.SIMULATION:
        raise ValidationError("Reversal pada fondasi ini hanya tersedia dalam simulasi.")
    Tenant.objects.select_for_update(no_key=True).get(pk=tenant.pk)
    payment = Payment.objects.select_for_update(of=("self",)).select_related("invoice").get(tenant=tenant, pk=payment_id)
    Customer.objects.select_for_update().get(tenant=tenant, pk=payment.invoice.customer_id)
    if len(reason.strip()) < 10 or len(reason) > 250:
        raise ValidationError("Alasan koreksi harus 10–250 karakter.")
    reversal, created = PaymentReversal.objects.get_or_create(payment=payment, defaults={"reason": reason, "recorded_by": actor})
    if created:
        NetworkJob.objects.create(tenant=tenant, customer=payment.invoice.customer)
        audit(tenant, actor, "payment.reversed", payment.pk, reason, correlation_id)
    return reversal
