"""Gateway domain. Network calls happen outside transactions; locked writers are retryable."""
import hashlib
import json
import re
import time
import uuid
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone
from .access import owner_for_update
from .billing import audit, retry_locked
from .midtrans import GatewayUnavailable, SandboxClient, checkout_url, credentials, rupiah
from .models import Customer, GatewayConfig, GatewayEvent, GatewayOrder, Invoice, Membership, NetworkJob, Payment
from .permissions import capabilities, membership

OPEN_STATES = ["creating", "pending", "review"]
DISPUTE_STATES = {"refund", "partial_refund", "chargeback", "partial_chargeback"}


def can_checkout(member):
    return member.user.is_active and (member.role == "customer" or "pay" in capabilities(member))


@retry_locked
@transaction.atomic
def save_config(actor, enabled, version, verified_at, access_version):
    member = owner_for_update(actor)
    if member.session_version != access_version or not 0 <= time.time() - verified_at <= 300:
        raise PermissionDenied("Verifikasi MFA terbaru diperlukan.")
    config = GatewayConfig.objects.select_for_update().filter(tenant=member.tenant).first()
    if (config.version if config else 0) != version:
        raise ValidationError("Setelan berubah. Muat ulang sebelum menyimpan.")
    if enabled:
        merchant = credentials(member.tenant_id)[0]
        if GatewayOrder.objects.filter(tenant=member.tenant).exclude(merchant_id=merchant).exists():
            raise ValidationError("Merchant tidak boleh diganti setelah order dibuat. Rekonsiliasi akun lama terlebih dahulu.")
    else:
        merchant = config.merchant_id if config else ""
    if not config:
        config = GatewayConfig(tenant=member.tenant)
    config.merchant_id, config.enabled = merchant, bool(enabled)
    config.version += 1
    config.updated_at = timezone.now()
    config.save()
    audit(member.tenant, actor, "gateway.configured", config.pk, f"Midtrans Sandbox; aktif={config.enabled}")
    return config


@retry_locked
@transaction.atomic
def reserve_order(actor, invoice_id):
    member = membership(actor)
    if not can_checkout(member):
        raise PermissionDenied
    invoice = Invoice.objects.select_for_update().get(pk=invoice_id, tenant=member.tenant)
    Customer.objects.select_for_update().get(pk=invoice.customer_id, tenant=member.tenant)
    # Re-read access inside the transaction. The browser's tenant/amount/order fields are unused.
    member = membership(Membership.objects.select_related("user").get(pk=member.pk).user)
    if not can_checkout(member) or member.tenant_id != invoice.tenant_id:
        raise PermissionDenied
    if member.role == "customer" and member.customer_id != invoice.customer_id:
        raise PermissionDenied
    config = GatewayConfig.objects.select_for_update().filter(tenant=member.tenant, enabled=True).first()
    if not config:
        raise ValidationError("Payment gateway sandbox belum diaktifkan oleh pemilik ISP.")
    credentials(member.tenant_id, config.merchant_id)
    existing = GatewayOrder.objects.filter(invoice=invoice, status__in=OPEN_STATES, payment__isnull=True).first()
    if existing:
        return existing
    if invoice.balance <= 0:
        raise ValidationError("Faktur sudah lunas.")
    order = GatewayOrder.objects.create(tenant=member.tenant, invoice=invoice, created_by=actor,
        merchant_id=config.merchant_id, amount=invoice.balance)
    audit(member.tenant, actor, "gateway.reserved", order.pk, f"{invoice.number}; Rp{order.amount}; sandbox")
    return order


@retry_locked
@transaction.atomic
def store_checkout(order_id, url):
    order = GatewayOrder.objects.select_for_update().get(pk=order_id)
    if order.status in {"creating", "pending"}:
        order.checkout_url = checkout_url(url)
        order.status, order.reason, order.updated_at = "pending", "", timezone.now()
        order.save(update_fields=["checkout_url", "status", "reason", "updated_at"])
    return order


def start_checkout(actor, invoice_id):
    order = reserve_order(actor, invoice_id)
    if order.status == "review":
        raise ValidationError("Order perlu rekonsiliasi. Periksa status di Midtrans.")
    if not order.checkout_url:
        # A retry always reuses the persisted merchant order ID. A timeout never releases it.
        order = store_checkout(order.pk, SandboxClient().create(order))
    return order


def verified_status(order, payload):
    if not isinstance(payload, dict):
        raise ValidationError("Status gateway tidak valid.")
    if (payload.get("order_id") != order.order_id or payload.get("merchant_id") != order.merchant_id
            or payload.get("currency") != "IDR" or rupiah(payload.get("gross_amount")) != order.amount):
        raise ValidationError("Identitas atau nominal status gateway tidak sesuai.")
    txn, status = payload.get("transaction_id"), payload.get("transaction_status")
    if not isinstance(txn, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", txn):
        raise ValidationError("Identitas transaksi gateway tidak valid.")
    if not isinstance(status, str) or status not in {"pending", "capture", "settlement", "deny", "cancel", "expire", "refund", "partial_refund", "chargeback", "partial_chargeback", "failure"}:
        raise ValidationError("Status transaksi gateway tidak dikenali.")
    paid = (status == "settlement" and payload.get("fraud_status", "accept") == "accept"
        or status == "capture" and payload.get("fraud_status") == "accept") and payload.get("status_code") == "200"
    normalized = {key: payload.get(key, "") for key in ["transaction_id", "transaction_status", "status_code", "fraud_status", "gross_amount"]}
    fingerprint = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
    return txn, status, paid, fingerprint


@retry_locked
@transaction.atomic
def apply_status(order_id, payload):
    initial = GatewayOrder.objects.get(pk=order_id)
    invoice = Invoice.objects.select_for_update().get(pk=initial.invoice_id, tenant_id=initial.tenant_id)
    Customer.objects.select_for_update().get(pk=invoice.customer_id, tenant_id=initial.tenant_id)
    order = GatewayOrder.objects.select_for_update().get(pk=order_id)
    txn, status, paid, fingerprint = verified_status(order, payload)
    if order.transaction_id and order.transaction_id != txn:
        raise ValidationError("Identitas transaksi gateway berubah.")
    order.last_checked_at = order.last_verified_at = timezone.now()
    existing_event = GatewayEvent.objects.filter(order=order, fingerprint=fingerprint).first()
    if existing_event and (order.payment_id or not paid):
        # Rotate reconciled pending orders behind unexamined work without duplicating events.
        order.updated_at = timezone.now()
        order.save(update_fields=["updated_at", "last_checked_at", "last_verified_at"])
        return order
    order.transaction_id = txn
    before_status = order.status
    if status in DISPUTE_STATES:
        order.status, order.reason = "review", "Refund/chargeback perlu rekonsiliasi. Ledger asli tidak diubah otomatis."
    elif order.payment_id:
        # Notifications arriving late cannot undo a recorded payment.
        pass
    elif paid and order.events.filter(gateway_status__in=DISPUTE_STATES).exists():
        # An earlier API request can finish after a refund has already been applied.
        # Its stale success must not create a payment for a disputed transaction.
        order.status, order.reason = "review", "Refund/chargeback pernah terverifikasi. Rekonsiliasi diperlukan sebelum mengubah ledger."
    elif paid:
        if order.amount > invoice.balance:
            order.status, order.reason = "review", "Nominal telah dibayar di gateway tetapi melebihi saldo faktur. Rekonsiliasi diperlukan."
        else:
            payment = Payment.objects.create(tenant_id=order.tenant_id, invoice=invoice, amount=order.amount,
                # The locked order/payment link provides idempotency. Keep ledger UUIDs private
                # so a public checkout ID cannot be reused to reserve a manual payment key.
                event_key=uuid.uuid4(), recorded_by_id=order.created_by_id, source="midtrans_sandbox",
                note=f"Midtrans Sandbox · {order.order_id}")
            order.payment, order.status, order.reason = payment, "settled", "Pembayaran sandbox terverifikasi dan tercatat."
            NetworkJob.objects.create(tenant_id=order.tenant_id, customer=invoice.customer)
            audit(order.tenant, None, "payment.midtrans_sandbox", payment.pk, f"{order.order_id}; Rp{order.amount}")
    elif status in {"deny", "cancel", "expire"} and order.status != "review":
        order.status, order.reason = status, "Status dikonfirmasi langsung oleh Midtrans Sandbox."
    elif status in {"failure", "capture", "settlement"}:
        order.status, order.reason = "review", "Status/fraud gateway memerlukan rekonsiliasi; pembayaran belum dicatat."
    elif order.status == "creating":
        order.status = "pending"
    order.updated_at = timezone.now()
    order.save()
    _, created = GatewayEvent.objects.get_or_create(order=order, fingerprint=fingerprint,
        defaults={"gateway_status": status, "outcome": order.status})
    # Keep the first event outcome as historical evidence. A fresh status check may
    # recover an unpaid settlement after a ledger correction, with a separate audit.
    if created or before_status != order.status:
        audit(order.tenant, None, "gateway.status", order.pk, f"{status} → {order.status}")
    return order


def refresh_order(order):
    try:
        return apply_status(order.pk, SandboxClient().status(order))
    except (GatewayUnavailable, ValidationError, IntegrityError, OperationalError):
        mark_unavailable(order.pk)
        raise


@retry_locked
def mark_unavailable(order_id):
    # Attempt time is distinct from evidence of a verified status. Never refresh
    # the verification timestamp when the remote response could not be checked.
    GatewayOrder.objects.filter(pk=order_id).update(last_checked_at=timezone.now())
