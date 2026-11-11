"""Record an already completed full sandbox refund. Never calls the provider refund API."""
import hashlib
import json
import time
import uuid
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone
from .access import owner_for_update
from .billing import audit, retry_locked
from .gateway import apply_status, mark_unavailable, verified_status
from .midtrans import GatewayUnavailable, SandboxClient, credentials, rupiah
from .models import Customer, GatewayOrder, GatewayRefundResolution, Invoice, NetworkJob, Payment, PaymentReversal


def validate_request(reason, request_key):
    reason = reason.strip()
    if not 10 <= len(reason) <= 250 or any(ord(char)<32 and char not in "\n\t" for char in reason):
        raise ValidationError("Alasan harus 10–250 karakter tanpa karakter kontrol.")
    try:
        request_key = uuid.UUID(str(request_key))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Kunci permintaan tidak valid.") from None
    return reason, request_key


def approved_owner(actor, verified_at, access_version):
    owner = owner_for_update(actor)
    if owner.session_version != access_version or not 0 <= time.time()-verified_at <= 300:
        raise PermissionDenied("Verifikasi MFA terbaru diperlukan.")
    return owner


def existing_resolution(order, request_key, reason):
    result = GatewayRefundResolution.objects.filter(order=order).first()
    if result and (result.request_key != request_key or result.reason != reason):
        raise ValidationError("Refund sudah dicatat. Muat ulang detail order sebelum membuat permintaan lain.")
    return result


@retry_locked
@transaction.atomic
def prepare_refund(actor, order_id, request_key, reason, verified_at, access_version):
    owner = approved_owner(actor, verified_at, access_version)
    reason, request_key = validate_request(reason, request_key)
    order = GatewayOrder.objects.select_for_update(of=("self",)).get(pk=order_id, tenant=owner.tenant, invoice__tenant=owner.tenant)
    existing = existing_resolution(order, request_key, reason)
    if not existing and not order.payment_id:
        raise ValidationError("Tidak ada pembayaran gateway tercatat untuk dikoreksi.")
    if not existing:
        credentials(order.tenant_id, order.merchant_id)
    return order, existing, reason, request_key


@retry_locked
@transaction.atomic
def record_refund(actor, order_id, request_key, reason, verified_at, access_version, payload):
    owner = approved_owner(actor, verified_at, access_version)
    reason, request_key = validate_request(reason, request_key)
    initial = GatewayOrder.objects.get(pk=order_id, tenant=owner.tenant)
    invoice = Invoice.objects.select_for_update().get(pk=initial.invoice_id, tenant=owner.tenant)
    Customer.objects.select_for_update().get(pk=invoice.customer_id, tenant=owner.tenant)
    order = GatewayOrder.objects.select_for_update().get(pk=order_id, tenant=owner.tenant)
    existing = existing_resolution(order, request_key, reason)
    if existing:
        return existing
    credentials(order.tenant_id, order.merchant_id)
    transaction_id, status, _, _ = verified_status(order, payload)
    if status != "refund" or payload.get("status_code") != "200" or rupiah(payload.get("refund_amount")) != order.amount:
        raise ValidationError("Refund penuh beserta nominalnya belum terverifikasi. Refund sebagian dan sengketa tetap perlu ditinjau.")
    if order.transaction_id != transaction_id or order.events.filter(gateway_status__in=["chargeback","partial_chargeback"]).exists():
        raise ValidationError("Identitas transaksi atau riwayat sengketa perlu ditinjau terlebih dahulu.")
    if not order.payment_id:
        raise ValidationError("Pembayaran sumber tidak ditemukan.")
    payment = Payment.objects.select_for_update().get(pk=order.payment_id, tenant=owner.tenant, invoice=invoice)
    if payment.source != "midtrans_sandbox" or payment.amount != order.amount or PaymentReversal.objects.filter(payment=payment).exists():
        raise ValidationError("Pembayaran sumber tidak sesuai atau sudah memiliki koreksi. Periksa ledger terlebih dahulu.")
    reversal = PaymentReversal.objects.create(payment=payment, recorded_by=actor,
        reason="Refund penuh Midtrans Sandbox terverifikasi. Sisa tagihan dibuka kembali.")
    proof = {"schema":1,"order_id":order.order_id,"merchant_id":order.merchant_id,"transaction_id":transaction_id,
        "gross_amount":order.amount,"refund_amount":order.amount,"currency":"IDR","transaction_status":"refund","status_code":"200"}
    result = GatewayRefundResolution.objects.create(order=order, reversal=reversal, request_key=request_key,
        recorded_by=actor, reason=reason, amount=order.amount, transaction_id=transaction_id,
        proof_digest=hashlib.sha256(json.dumps(proof,sort_keys=True).encode()).hexdigest(), verified_at=timezone.now())
    order.status, order.reason, order.updated_at = "refunded", "Refund penuh terverifikasi telah dicatat. Sisa tagihan dibuka kembali.", timezone.now()
    order.save(update_fields=["status","reason","updated_at"])
    NetworkJob.objects.create(tenant=owner.tenant, customer=invoice.customer)
    audit(owner.tenant, actor, "gateway.refund_recorded", result.pk, f"{order.order_id}; Rp{result.amount}; {reason}")
    return result


def reconcile_refund(actor, order_id, request_key, reason, verified_at, access_version):
    order, existing, reason, request_key = prepare_refund(actor, order_id, request_key, reason, verified_at, access_version)
    if existing:
        return existing
    try:
        # Only a fresh authenticated GET is proof. Browser input, webhook bodies and
        # stored event status alone cannot authorize the ledger reversal.
        payload = SandboxClient().status(order)
        apply_status(order.pk, payload)
    except (GatewayUnavailable, ValidationError, IntegrityError, OperationalError):
        mark_unavailable(order.pk)
        raise
    return record_refund(actor, order.pk, request_key, reason, verified_at, access_version, payload)
