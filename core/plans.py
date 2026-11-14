"""Tenant-scoped package changes with signed reviews and optimistic locking."""
import time
import uuid
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from .billing import audit, retry_locked
from .models import Customer, Plan, Tenant
from .operations import member_for
from .permissions import capabilities

FIELDS = ("name", "speed_mbps", "price")
SALT = "tagihnet.plan-review.v1"
REVIEW_SECONDS = 900


def manager(actor):
    member = member_for(actor)
    if not {"customers", "manage_plans"} <= capabilities(member):
        raise PermissionDenied("Izin mengelola paket tidak tersedia.")
    return member


def values_for(plan):
    return {field: getattr(plan, field) for field in FIELDS}


def validate_values(values):
    if not isinstance(values, dict) or set(values) != set(FIELDS):
        raise ValidationError("Field paket tidak valid.")
    if (not isinstance(values["name"], str) or not 1 <= len(values["name"].strip()) <= 80
            or any(ord(char) < 32 or ord(char) == 127 for char in values["name"])):
        raise ValidationError("Nama paket harus 1–80 karakter tanpa karakter kontrol.")
    if type(values["speed_mbps"]) is not int or not 1 <= values["speed_mbps"] <= 100000:
        raise ValidationError("Kecepatan harus integer 1–100.000 Mbps.")
    if type(values["price"]) is not int or not 1 <= values["price"] <= 100000000:
        raise ValidationError("Harga harus integer 1–100.000.000 rupiah.")
    return {**values, "name": values["name"].strip()}


def check_name(tenant, values, plan_id):
    if Plan.objects.filter(tenant=tenant, name__iexact=values["name"]).exclude(pk=plan_id).exists():
        raise ValidationError("Nama paket sudah digunakan dalam ISP ini.")


def review_change(actor, values, version, plan_id=None):
    member = manager(actor)
    values = validate_values(values)
    plan = Plan.objects.get(tenant=member.tenant, pk=plan_id) if plan_id else None
    if type(version) is not int or version != (plan.version if plan else 1):
        raise ValidationError("Paket telah berubah. Muat ulang formulir dan tinjau kembali.")
    check_name(member.tenant, values, plan_id)
    payload = {"tenant": member.tenant_id, "actor": actor.pk, "access_version": member.session_version,
        "plan": plan_id, "version": version, "before": values_for(plan) if plan else None,
        "after": values, "creation_key": str(uuid.uuid4())}
    return signing.dumps(payload, salt=SALT, compress=True), payload


def read_review(actor, token):
    member = manager(actor)
    try:
        payload = signing.loads(token, salt=SALT, max_age=REVIEW_SECONDS)
    except (signing.BadSignature, ValueError, TypeError):
        raise ValidationError("Pratinjau tidak valid atau kedaluwarsa. Buat pratinjau baru.") from None
    if (payload["actor"] != actor.pk or payload["tenant"] != member.tenant_id
            or payload["access_version"] != member.session_version):
        raise PermissionDenied("Pratinjau bukan milik akun/sesi akses ini.")
    return payload


def impact(tenant, plan_id):
    # Counts describe the time of review, not a frozen future recipient set.
    customers = Customer.objects.filter(tenant=tenant, plan_id=plan_id)
    return {"total": customers.count(), **{state: customers.filter(service_state=state).count()
        for state in ("active", "isolated", "pending")}}


@retry_locked
@transaction.atomic
def confirm_change(actor, token, verified_at, access_version):
    member = manager(actor)
    Tenant.objects.select_for_update(no_key=True).get(pk=member.tenant_id)
    member = manager(actor)
    payload = read_review(actor, token)
    if (member.session_version != access_version or not isinstance(verified_at, (int, float))
            or not 0 <= time.time() - verified_at <= 300):
        raise PermissionDenied("MFA terbaru diperlukan untuk menyimpan paket.")
    values = validate_values(payload["after"])
    if payload["plan"] is None:
        existing = Plan.objects.filter(tenant=member.tenant, creation_key=payload["creation_key"]).first()
        if existing:
            return existing
        plan = Plan(tenant=member.tenant, creation_key=payload["creation_key"])
    else:
        plan = Plan.objects.select_for_update().get(tenant=member.tenant, pk=payload["plan"])
        if plan.version != payload["version"] or values_for(plan) != payload["before"]:
            raise ValidationError("Paket telah berubah sejak pratinjau. Muat ulang dan tinjau kembali.")
    check_name(member.tenant, values, plan.pk)
    if plan.pk and values_for(plan) == values:
        return plan
    for field, value in values.items():
        setattr(plan, field, value)
    if plan.pk:
        plan.version += 1
    plan.full_clean()
    plan.save()
    correlation = uuid.uuid4()
    def record(action, state, version):
        audit(member.tenant, actor, action, plan.pk,
            f"Nama: {state['name']} · {state['speed_mbps']} Mbps · Rp{state['price']} · versi {version}", correlation)
    if payload["before"]:
        record("plan.before_update", payload["before"], payload["version"])
    record("plan.updated" if payload["plan"] else "plan.created", values, plan.version)
    return plan
