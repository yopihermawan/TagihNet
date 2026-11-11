"""Owner-approved sandbox expiry with persisted intent and status verification.

No network I/O occurs inside a retried transaction. A successful POST response
alone cannot change an invoice, payment, or reservation.
"""
import time
import uuid
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone
from .access import owner_for_update
from .billing import audit, retry_locked
from .gateway import refresh_order
from .midtrans import GatewayUnavailable, SandboxClient, credentials
from .models import GatewayExpiry, GatewayOrder, Membership


@retry_locked
def authorized(actor, tenant_id, access_version, verified_at):
    return (0 <= time.time() - verified_at <= 300 and Membership.objects.filter(user=actor,
        tenant_id=tenant_id, user__is_active=True, role="owner", session_version=access_version).exists())


@retry_locked
@transaction.atomic
def prepare_expiry(actor, order_id, request_key, reason, verified_at, access_version):
    member = owner_for_update(actor)
    if member.session_version != access_version or not 0 <= time.time() - verified_at <= 300:
        raise PermissionDenied("Verifikasi MFA terbaru diperlukan.")
    reason = reason.strip()
    if not 10 <= len(reason) <= 250 or any(ord(char) < 32 and char not in "\n\t" for char in reason):
        raise ValidationError("Alasan harus 10–250 karakter tanpa karakter kontrol.")
    try:
        request_key = uuid.UUID(str(request_key))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Kunci permintaan tidak valid.") from None
    order = GatewayOrder.objects.select_for_update(of=("self",)).get(pk=order_id, tenant=member.tenant, invoice__tenant=member.tenant)
    existing = GatewayExpiry.objects.select_for_update().filter(order=order, request_key=request_key).first()
    if existing:
        if existing.reason != reason:
            raise ValidationError("Kunci permintaan telah digunakan dengan alasan berbeda.")
        existing.order = order
        if existing.status not in {"confirmed", "blocked"}:
            audit(order.tenant, actor, "gateway.expiry_attempt", existing.pk, order.order_id)
        return existing
    if order.payment_id or order.status not in {"creating", "pending"}:
        raise ValidationError("Hanya checkout yang masih menunggu yang dapat diminta berakhir.")
    credentials(order.tenant_id, order.merchant_id)
    expiry = GatewayExpiry.objects.create(order=order, request_key=request_key, requested_by=actor, reason=reason)
    audit(order.tenant, actor, "gateway.expiry_requested", expiry.pk, f"{order.order_id}; {reason}")
    audit(order.tenant, actor, "gateway.expiry_attempt", expiry.pk, order.order_id)
    return expiry


@retry_locked
@transaction.atomic
def finish_expiry(expiry_id, actor, revoked=False):
    initial = GatewayExpiry.objects.get(pk=expiry_id)
    order = GatewayOrder.objects.select_for_update().get(pk=initial.order_id)
    expiry = GatewayExpiry.objects.select_for_update().get(pk=expiry_id)
    if expiry.status in {"confirmed", "blocked"}:
        return expiry
    if order.payment_id or order.status == "review":
        state, result = "blocked", "Pembayaran tercatat atau memerlukan rekonsiliasi. Tidak ada refund atau reversal otomatis."
    elif order.status in {"expire", "cancel", "deny"}:
        state, result = "confirmed", "Status terminal dikonfirmasi oleh gateway. Reservasi checkout ini telah dilepas."
    elif revoked:
        state, result = "blocked", "Izin atau verifikasi MFA berubah sebelum permintaan dikirim. Buat permintaan baru setelah verifikasi."
    else:
        state, result = "unavailable", "Status terminal belum terverifikasi. Reservasi tetap ada; periksa atau coba ulang permintaan yang sama."
    changed = expiry.status != state or expiry.result != result
    expiry.status, expiry.result, expiry.updated_at = state, result, timezone.now()
    expiry.save(update_fields=["status", "result", "updated_at"])
    if changed:
        audit(order.tenant, actor, "gateway.expiry_" + state, expiry.pk, order.order_id)
    return expiry


def expire_order(actor, order_id, request_key, reason, verified_at, access_version):
    expiry = prepare_expiry(actor, order_id, request_key, reason, verified_at, access_version)
    if expiry.status in {"confirmed", "blocked"}:
        return expiry
    failures = (GatewayUnavailable, ValidationError, IntegrityError, OperationalError)
    try:
        order = refresh_order(expiry.order)
    except failures:
        return finish_expiry(expiry.pk, actor)
    if order.payment_id or order.status != "pending":
        return finish_expiry(expiry.pk, actor)
    if not authorized(actor, order.tenant_id, access_version, verified_at):
        return finish_expiry(expiry.pk, actor, revoked=True)
    try:
        SandboxClient().expire(order)
    except failures:
        # A timeout or provider rejection may mean the write succeeded, or a payment
        # won the race. Verify the authoritative status in both cases.
        pass
    try:
        refresh_order(order)
    except failures:
        pass
    return finish_expiry(expiry.pk, actor)
