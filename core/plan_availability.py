"""Reviewed, reversible catalog archival without changing existing service/billing assignments."""
import time
import uuid
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from .billing import audit, retry_locked
from .models import Installation, MFADevice, Membership, Plan, Tenant
from .plans import impact, manager, values_for

SALT = "tagihnet.plan-availability.v1"
SECONDS = 900


def device_revision(actor):
    revision = MFADevice.objects.filter(user_id=actor.pk).values_list("revision", flat=True).first()
    if revision is None:raise PermissionDenied("MFA diperlukan untuk mengubah ketersediaan paket.")
    return str(revision)


def review_availability(actor, plan_id, version, is_active):
    member = manager(actor)
    plan = Plan.objects.get(tenant=member.tenant, pk=plan_id)
    if type(version) is not int or version != plan.version or type(is_active) is not bool:
        raise ValidationError("Status atau versi paket tidak valid. Muat ulang dan tinjau kembali.")
    payload = {"v": 1, "actor": actor.pk, "tenant": member.tenant_id, "access": member.session_version,
        "mfa": device_revision(actor), "plan": plan.pk, "version": plan.version,
        "before": {**values_for(plan), "is_active": plan.is_active}, "after": is_active}
    return signing.dumps(payload, salt=SALT), payload


def read_availability(actor, token):
    member = manager(actor)
    try:
        if not isinstance(token, str) or len(token) > 4096:raise ValueError
        value = signing.loads(token, salt=SALT, max_age=SECONDS)
        if (not isinstance(value, dict) or set(value) != {"v", "actor", "tenant", "access", "mfa", "plan", "version", "before", "after"}
                or type(value["v"]) is not int or value["v"] != 1
                or any(type(value[key]) is not int or value[key] < 1 for key in ("actor", "tenant", "access", "plan", "version"))
                or type(value["after"]) is not bool or not isinstance(value["before"], dict)
                or set(value["before"]) != {"name", "speed_mbps", "price", "is_active"}
                or type(value["before"]["is_active"]) is not bool
                or not isinstance(value["mfa"], str) or str(uuid.UUID(value["mfa"])) != value["mfa"]):
            raise ValueError
    except (signing.BadSignature, ValueError, TypeError, KeyError, AttributeError):
        raise ValidationError("Tinjauan ketersediaan paket tidak valid atau kedaluwarsa. Muat ulang paket.") from None
    if value["actor"] != actor.pk or value["tenant"] != member.tenant_id or value["access"] != member.session_version or value["mfa"] != device_revision(actor):
        raise PermissionDenied("Tinjauan bukan milik akun atau versi akses/MFA saat ini.")
    return value


def availability_impact(tenant, plan_id):
    return {**impact(tenant, plan_id), "installations": Installation.objects.filter(tenant=tenant, plan_id=plan_id)
        .exclude(status__in=["activated", "cancelled"]).count()}


@retry_locked
@transaction.atomic
def confirm_availability(actor, token, verified_at, access_version):
    member = manager(actor)
    Tenant.objects.select_for_update(no_key=True).get(pk=member.tenant_id)
    Membership.objects.select_for_update().get(user_id=actor.pk, tenant_id=member.tenant_id)
    member = manager(actor)
    payload = read_availability(actor, token)
    if (type(access_version) is not int or member.session_version != access_version
            or type(verified_at) not in (int, float) or not 0 <= time.time() - verified_at <= 300):
        raise PermissionDenied("Verifikasi MFA terbaru diperlukan sebelum mengubah ketersediaan paket.")
    plan = Plan.objects.select_for_update().get(tenant=member.tenant, pk=payload["plan"])
    if plan.version != payload["version"] or {**values_for(plan), "is_active": plan.is_active} != payload["before"]:
        raise ValidationError("Paket telah berubah sejak tinjauan. Muat ulang dan tinjau status terbaru.")
    if plan.is_active == payload["after"]:return plan
    plan.is_active = payload["after"]; plan.version += 1
    plan.save(update_fields=["is_active", "version"])
    audit(member.tenant, actor, "plan.reactivated" if plan.is_active else "plan.archived", plan.pk,
        f"Ketersediaan katalog diperbarui; versi {payload['version']} → {plan.version}. Penugasan pelanggan/PSB dan ledger tetap.")
    return plan
