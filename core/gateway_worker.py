"""Opt-in sandbox status polling. Leases coordinate workers; HTTP never runs in a transaction."""
import time
import uuid
from datetime import timedelta
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import F, Q
from django.utils import timezone
from .access import owner_for_update
from .billing import audit, retry_locked
from .gateway import mark_unavailable, refresh_order
from .midtrans import GatewayUnavailable, credentials
from .models import GatewayOrder, GatewayReconciliationPolicy, Membership

LEASE_SECONDS = 60


@retry_locked
@transaction.atomic
def save_policy(actor, enabled, include_settled, interval, version, verified_at, access_version):
    member = owner_for_update(actor)
    if member.session_version != access_version or not 0 <= time.time()-verified_at <= 300:
        raise PermissionDenied("Verifikasi MFA terbaru diperlukan.")
    if type(interval) is not int or interval not in {60, 300, 900}:
        raise ValidationError("Interval pemeriksaan tidak valid.")
    policy = GatewayReconciliationPolicy.objects.select_for_update().filter(tenant=member.tenant).first()
    if (policy.version if policy else 0) != version:
        raise ValidationError("Kebijakan berubah. Muat ulang formulir.")
    merchant = policy.merchant_id if policy else ""
    if enabled:
        merchant = credentials(member.tenant_id)[0]
        if GatewayOrder.objects.filter(tenant=member.tenant).exclude(merchant_id=merchant).exists():
            raise ValidationError("Merchant harus sama dengan order yang sudah ada.")
    if not policy:
        policy = GatewayReconciliationPolicy(tenant=member.tenant)
    policy.enabled, policy.include_settled, policy.interval_seconds = bool(enabled), bool(include_settled), interval
    policy.merchant_id, policy.approved_by, policy.authorization_version = merchant, actor, member.session_version
    policy.version += 1
    policy.approved_at = policy.next_run_at = timezone.now()
    policy.lease_token = policy.lease_until = None
    policy.last_status, policy.failure_streak = "never", 0
    policy.last_verified = policy.last_unavailable = 0
    policy.save()
    audit(member.tenant, actor, "gateway.polling_policy", policy.pk,
        f"Sandbox; aktif={policy.enabled}; interval={interval}; termasuk_tercatat={policy.include_settled}")
    return policy


def consent_valid(policy):
    return settings.SIMULATION and Membership.objects.filter(user_id=policy.approved_by_id,
        tenant_id=policy.tenant_id, user__is_active=True, role="owner", session_version=policy.authorization_version).exists()


def block_policy(policy):
    policy.enabled, policy.last_status = False, "blocked"
    policy.lease_token = policy.lease_until = None
    policy.save(update_fields=["enabled", "last_status", "lease_token", "lease_until"])
    audit(policy.tenant, None, "gateway.polling_blocked", policy.pk, "Persetujuan atau mode simulasi tidak berlaku.")


@retry_locked
@transaction.atomic
def claim_policy(policy_id):
    policy = GatewayReconciliationPolicy.objects.select_for_update().get(pk=policy_id)
    now = timezone.now()
    if not policy.enabled or policy.next_run_at > now or (policy.lease_until and policy.lease_until > now):
        return None
    if not consent_valid(policy):
        block_policy(policy)
        return None
    policy.lease_token, policy.lease_until = uuid.uuid4(), now+timedelta(seconds=LEASE_SECONDS)
    policy.save(update_fields=["lease_token", "lease_until"])
    return policy


@retry_locked
@transaction.atomic
def renew_policy(policy_id, token, version):
    policy = GatewayReconciliationPolicy.objects.select_for_update().get(pk=policy_id)
    now = timezone.now()
    if (not policy.enabled or policy.version != version or policy.lease_token != token
            or not policy.lease_until or policy.lease_until <= now):
        return False
    if not consent_valid(policy):
        block_policy(policy)
        return False
    policy.lease_until = now+timedelta(seconds=LEASE_SECONDS)
    policy.save(update_fields=["lease_until"])
    return True


@retry_locked
@transaction.atomic
def finish_policy(policy_id, token, version, verified, unavailable):
    policy = GatewayReconciliationPolicy.objects.select_for_update().get(pk=policy_id)
    if policy.lease_token != token or policy.version != version or not policy.enabled:
        return policy
    if not consent_valid(policy):
        block_policy(policy)
        return policy
    previous = policy.last_status
    now = timezone.now()
    policy.last_run_at, policy.last_verified, policy.last_unavailable = now, verified, unavailable
    policy.last_status = "partial" if verified and unavailable else "failed" if unavailable else "completed" if verified else "idle"
    policy.failure_streak = min(policy.failure_streak+1, 32) if unavailable and not verified else 0
    delay = min(3600, policy.interval_seconds*2**min(max(policy.failure_streak-1, 0), 6))
    policy.next_run_at = now+timedelta(seconds=delay)
    if verified:
        policy.last_success_at = now
    policy.lease_token = policy.lease_until = None
    policy.save()
    if verified or unavailable or previous != "idle":
        audit(policy.tenant, None, "gateway.polling_run", policy.pk, f"terverifikasi={verified}; belum_terverifikasi={unavailable}")
    return policy


def process_policy(policy_id, limit=20):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValidationError("Batas pemeriksaan harus 1–100.")
    policy = claim_policy(policy_id)
    if not policy:
        return None
    token, version = policy.lease_token, policy.version
    verified = unavailable = 0
    try:
        states = ["creating", "pending", "review"] + (["settled","refunded"] if policy.include_settled else [])
        # Oldest checked first: both success and failure rotate an order to the back.
        orders = list(GatewayOrder.objects.filter(tenant_id=policy.tenant_id, status__in=states)
            .order_by(F("last_checked_at").asc(nulls_first=True), "created_at", "pk")[:limit])
        for order in orders:
            if not renew_policy(policy.pk, token, version):
                break
            try:
                credentials(policy.tenant_id, policy.merchant_id)
                refresh_order(order)
            except (GatewayUnavailable, ValidationError, IntegrityError, OperationalError):
                unavailable += 1
                mark_unavailable(order.pk)
            else:
                verified += 1
    except OperationalError:
        unavailable = max(1, unavailable)
        raise
    finally:
        result = finish_policy(policy.pk, token, version, verified, unavailable)
    return result


def run_cycle(limit=20):
    if not settings.SIMULATION:
        raise ValidationError("Worker gateway hanya tersedia dalam mode simulasi.")
    now = timezone.now()
    ids = list(GatewayReconciliationPolicy.objects.filter(enabled=True, next_run_at__lte=now)
        .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now)).order_by("next_run_at", "pk")
        .values_list("pk", flat=True)[:10])
    results = []
    for policy_id in ids:
        result = process_policy(policy_id, limit)
        if result:
            results.append(result)
    return results
