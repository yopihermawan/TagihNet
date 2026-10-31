"""Persistent preview -> MFA confirmation -> bounded, resumable invoice issuance."""
import hashlib
import json
import time
from datetime import timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from .billing import audit, invoice_snapshot, retry_locked, write_snapshot_invoice
from .models import BillingBatch, BillingBatchItem, Customer, Invoice, Membership, Tenant
from .permissions import capabilities

MAX_CUSTOMERS = 500


def authorized(actor, tenant_id=None, version=None):
    member = Membership.objects.select_related("tenant", "user", "access_role").get(user=actor)
    if (not member.user.is_active or "bulk_issue" not in capabilities(member)
            or (tenant_id is not None and member.tenant_id != tenant_id)
            or (version is not None and member.session_version != version)):
        raise PermissionDenied("Izin penerbitan massal tidak tersedia atau telah berubah.")
    return member


def preview_data(tenant, period, filters, discount):
    if period.day != 1:
        raise ValidationError("Periode harus tanggal pertama bulan layanan.")
    if type(discount) is not int or not 0 <= discount <= 100000000:
        raise ValidationError("Diskon harus integer rupiah dalam rentang 0–100.000.000.")
    if set(filters) - {"state", "plan", "area"} or filters.get("state", "active") not in {"active", "isolated", "all"}:
        raise ValidationError("Filter tidak valid.")
    customers = Customer.objects.filter(tenant=tenant).select_related("plan").order_by("pk")
    if filters.get("state", "active") != "all":
        customers = customers.filter(service_state=filters.get("state", "active"))
    if filters.get("plan"):
        if not tenant.plan_set.filter(pk=filters["plan"]).exists():
            raise ValidationError("Paket di luar ISP ini.")
        customers = customers.filter(plan_id=filters["plan"])
    if filters.get("area"):
        customers = customers.filter(area=filters["area"])
    customers = list(customers[:MAX_CUSTOMERS + 1])
    if not customers:
        raise ValidationError("Tidak ada pelanggan yang sesuai. Ubah filter sebelum membuat pratinjau.")
    if len(customers) > MAX_CUSTOMERS:
        raise ValidationError(f"Maksimal {MAX_CUSTOMERS} pelanggan per batch. Persempit filter paket/wilayah.")
    existing = {invoice.customer_id: invoice for invoice in Invoice.objects.filter(
        tenant=tenant, customer_id__in=[item.pk for item in customers], period=period)}
    rows = []
    for customer in customers:
        prior = existing.get(customer.pk)
        snapshot = invoice_snapshot(tenant, customer, period, 0 if prior else discount)
        if prior:
            # Uniform discounts apply only to NEW invoices, never to already-issued rows.
            snapshot["invoice"] = {key: getattr(prior, key) for key in ["customer_name", "customer_address", "subtotal",
                "discount", "tax_bp", "tax_amount", "total", "footer"]}
            snapshot["invoice"]["due_date"] = prior.due_date.isoformat()
            snapshot["line"] = {"description": "Faktur yang sudah terbit · tidak diubah", "quantity": 1, "unit_price": prior.subtotal}
        rows.append({"customer_id": customer.pk, "snapshot": snapshot, "existing_id": prior.pk if prior else None})
    canonical = json.dumps({"period": period.isoformat(), "filters": filters, "discount": discount,
                            "rows": rows}, sort_keys=True, separators=(",", ":"))
    return rows, hashlib.sha256(canonical.encode()).hexdigest()


@retry_locked
@transaction.atomic
def create_preview(actor, period, filters, discount=0, correlation_id=None):
    member = authorized(actor)
    rows, fingerprint = preview_data(member.tenant, period, filters, discount)
    batch = BillingBatch.objects.create(tenant=member.tenant, created_by=actor, period=period, filters=filters,
        discount=discount, fingerprint=fingerprint, authorization_version=member.session_version,
        expires_at=timezone.now() + timedelta(minutes=15), **({"correlation_id": correlation_id} if correlation_id else {}))
    BillingBatchItem.objects.bulk_create([BillingBatchItem(batch=batch, customer_id=row["customer_id"], snapshot=row["snapshot"],
        invoice_id=row["existing_id"], status="skipped" if row["existing_id"] else "pending") for row in rows])
    audit(member.tenant, actor, "billing.preview_created", batch.pk, f"{len(rows)} pelanggan; periode {period:%m/%Y}", batch.correlation_id)
    return batch


def own_batch(actor, batch_id, lock=False):
    member = authorized(actor)
    query = BillingBatch.objects.select_for_update() if lock else BillingBatch.objects
    return query.get(pk=batch_id, tenant=member.tenant, created_by=actor)


@retry_locked
@transaction.atomic
def confirm_batch(actor, batch_id, verified_at, session_version):
    batch = own_batch(actor, batch_id, lock=True)
    member = authorized(actor, batch.tenant_id, session_version)
    if not 0 <= time.time() - verified_at <= 300:
        raise PermissionDenied("Verifikasi MFA terbaru diperlukan.")
    if batch.status != "preview":
        return batch  # a replay never starts a second run or revives a terminal batch
    if timezone.now() > batch.expires_at:
        batch.status, batch.reason = "expired", "Pratinjau kedaluwarsa. Buat pratinjau baru."
    else:
        try:
            _, fingerprint = preview_data(member.tenant, batch.period, batch.filters, batch.discount)
        except ValidationError:
            fingerprint = None
        if fingerprint != batch.fingerprint:
            batch.status, batch.reason = "stale", "Pelanggan, paket, setelan, atau faktur berubah. Buat pratinjau baru."
        else:
            batch.status = "queued"
            batch.confirmed_at = timezone.now()
            batch.authorization_version = member.session_version
    batch.save()
    audit(batch.tenant, actor, "billing." + batch.status, batch.pk, batch.reason or "Snapshot disetujui; menunggu worker.", batch.correlation_id)
    return batch


@retry_locked
@transaction.atomic
def cancel_batch(actor, batch_id):
    batch = own_batch(actor, batch_id, lock=True)
    if batch.status in {"preview", "queued", "processing", "blocked"}:
        batch.status = "cancelled"
        batch.reason = "Sisa pekerjaan dibatalkan. Faktur yang sudah terbit tetap tersimpan."
        batch.finished_at = timezone.now()
        batch.save()
        audit(batch.tenant, actor, "billing.cancelled", batch.pk, batch.reason, batch.correlation_id)
    return batch


@retry_locked
@transaction.atomic
def process_next(batch_id):
    # One row per transaction. Crash/restart cannot lose completed progress.
    # Lock the job, not its joined actor/tenant; their FK locks can deadlock invoice writers.
    batch = BillingBatch.objects.select_for_update(of=("self",)).select_related("created_by", "tenant").get(pk=batch_id)
    if batch.status not in {"queued", "processing"}:
        return False
    try:
        authorized(batch.created_by, batch.tenant_id, batch.authorization_version)
    except (PermissionDenied, Membership.DoesNotExist):
        batch.status, batch.reason = "blocked", "Akses pembuat berubah atau dicabut. Tinjau sisa pekerjaan melalui pratinjau baru."
        batch.save(update_fields=["status", "reason"])
        audit(batch.tenant, None, "billing.blocked", batch.pk, batch.reason, batch.correlation_id)
        return False
    item = batch.items.select_for_update().filter(status="pending").first()
    if item:
        try:
            # The savepoint rolls back both invoice and audit if a snapshot is invalid.
            with transaction.atomic():
                customer = Customer.objects.select_for_update().get(pk=item.customer_id, tenant=batch.tenant)
                invoice = Invoice.objects.filter(tenant=batch.tenant, customer=customer, period=batch.period).first()
                if invoice:
                    item.status = "skipped"
                else:
                    invoice = write_snapshot_invoice(batch.tenant, customer, batch.period, item.snapshot, batch.created_by, batch.correlation_id)
                    item.status = "created"
                item.invoice = invoice
                item.processed_at = timezone.now()
                item.save(update_fields=["invoice", "status", "processed_at"])
        except (Customer.DoesNotExist, ValidationError, KeyError, TypeError, ValueError):
            batch.status, batch.reason = "blocked", "Data snapshot/pelanggan tidak valid. Tidak ada faktur baru untuk baris ini. Tinjau pratinjau baru."
            batch.save(update_fields=["status", "reason"])
            audit(batch.tenant, None, "billing.blocked", batch.pk, batch.reason, batch.correlation_id)
            return False
    batch.status = "processing"
    if not batch.items.filter(status="pending").exists():
        batch.status, batch.finished_at = "completed", timezone.now()
        audit(batch.tenant, None, "billing.completed", batch.pk, "Penerbitan selesai; duplikat dilewati.", batch.correlation_id)
    batch.save(update_fields=["status", "finished_at"])
    return bool(item)


def process_batch(batch_id, limit=100):
    if type(limit) is not int or not 1 <= limit <= MAX_CUSTOMERS:
        raise ValueError("Ukuran batch worker harus 1–500.")
    processed = 0
    while processed < limit and process_next(batch_id):
        processed += 1
    return processed
