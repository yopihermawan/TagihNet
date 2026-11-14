"""Opt-in calendar-month billing. Snapshot once, then use the existing resumable worker."""
import hashlib
import json
import time
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .billing import audit, retry_locked
from .bulk_billing import preview_data
from .models import BillingBatch, BillingBatchItem, BillingSchedule, BillingScheduleRun, MFADevice, Membership, Plan, Tenant, month_due
from .operations import member_for
from .permissions import capabilities

SALT = "tagihnet.billing-schedule.v1"
RIGHTS = {"manage_billing_schedule", "bulk_issue", "issue", "invoices", "customers"}
FIELDS = ("enabled", "day", "state", "plan", "area")
WIB = ZoneInfo("Asia/Jakarta")


def manager(actor):
    member = member_for(actor)
    if not RIGHTS <= capabilities(member):
        raise PermissionDenied("Izin mengelola jadwal faktur tidak tersedia.")
    return member


def values_for(schedule):
    return {"enabled": schedule.enabled, "day": schedule.day, "state": schedule.state,
            "plan": schedule.plan_id, "area": schedule.area} if schedule else {
                "enabled": False, "day": 1, "state": "active", "plan": None, "area": ""}


def validate_values(tenant, values):
    if not isinstance(values, dict) or set(values) != set(FIELDS):
        raise ValidationError("Field jadwal tidak valid.")
    if type(values["enabled"]) is not bool or type(values["day"]) is not int or not 1 <= values["day"] <= 31:
        raise ValidationError("Tanggal jadwal harus integer 1–31.")
    if not isinstance(values["state"], str) or values["state"] not in {"active", "isolated", "all"}:
        raise ValidationError("Filter layanan tidak valid.")
    if not isinstance(values["area"], str) or len(values["area"]) > 80 or any(ord(c)<32 or ord(c)==127 for c in values["area"]):
        raise ValidationError("Wilayah maksimal 80 karakter tanpa karakter kontrol.")
    if values["plan"] is not None and (type(values["plan"]) is not int or not Plan.objects.filter(tenant=tenant, pk=values["plan"]).exists()):
        raise ValidationError("Paket harus berasal dari ISP ini.")
    if values["enabled"] and not settings.SIMULATION:
        raise ValidationError("Jadwal pada checkpoint ini hanya tersedia dalam simulasi.")
    return {**values, "area": values["area"].strip()}


def next_month(period):
    return (period.replace(day=28)+timedelta(days=4)).replace(day=1)


def example(tenant, values, period):
    filters = {key: values[key] for key in ("state", "plan", "area")}
    # An empty cohort is a valid no-op cycle, while invalid prices/oversize cohorts are not.
    from .models import Customer
    query = Customer.objects.filter(tenant=tenant)
    if values["state"] != "all":
        query = query.filter(service_state=values["state"])
    if values["plan"]:
        query = query.filter(plan_id=values["plan"])
    if values["area"]:
        query = query.filter(area=values["area"])
    if not query.exists():
        return [], hashlib.sha256(b"empty-cohort-v1").hexdigest()
    return preview_data(tenant, period, filters, 0)


def review_change(actor, values, version):
    member = manager(actor)
    values = validate_values(member.tenant, values)
    schedule = BillingSchedule.objects.filter(tenant=member.tenant).first()
    if type(version) is not int or version != (schedule.version if schedule else 0):
        raise ValidationError("Jadwal berubah. Muat ulang formulir.")
    period = timezone.now().astimezone(WIB).date().replace(day=1)
    if schedule and schedule.runs.filter(period=period).exists():
        period = next_month(period)
    rows, fingerprint = example(member.tenant, values, period) if values["enabled"] else ([], "disabled")
    payload = {"tenant": member.tenant_id, "actor": actor.pk, "access_version": member.session_version,
        "version": version, "after": values, "key": str(uuid.uuid4()), "period": period.isoformat(),
        "fingerprint": fingerprint, "selected": len(rows), "new": sum(not row["existing_id"] for row in rows),
        "total": sum(row["snapshot"]["invoice"]["total"] for row in rows if not row["existing_id"])}
    return signing.dumps(payload, salt=SALT, compress=True), payload


def read_review(actor, token):
    member = manager(actor)
    try:
        payload = signing.loads(token, salt=SALT, max_age=900)
        if not isinstance(payload, dict):
            raise ValueError
        if (payload["actor"] != actor.pk or payload["tenant"] != member.tenant_id
                or payload["access_version"] != member.session_version):
            raise PermissionDenied("Pratinjau bukan milik akun/sesi akses ini.")
        return payload
    except (signing.BadSignature, ValueError, TypeError, KeyError):
        raise ValidationError("Pratinjau tidak valid atau kedaluwarsa. Tinjau ulang jadwal.") from None


@retry_locked
@transaction.atomic
def confirm_change(actor, token, verified_at, access_version):
    member = manager(actor)
    Tenant.objects.select_for_update(no_key=True).get(pk=member.tenant_id)
    member = manager(actor)
    payload = read_review(actor, token)
    if (member.session_version != access_version or type(verified_at) not in (int, float)
            or not 0 <= time.time()-verified_at <= 300):
        raise PermissionDenied("MFA terbaru diperlukan untuk menyetujui jadwal.")
    device = MFADevice.objects.filter(user=actor).first()
    if not device:
        raise PermissionDenied("MFA operator belum tersedia.")
    schedule = BillingSchedule.objects.select_for_update().filter(tenant=member.tenant).first()
    if schedule and str(schedule.confirmation_key) == payload["key"]:
        return schedule
    if payload["version"] != (schedule.version if schedule else 0):
        raise ValidationError("Jadwal berubah sejak pratinjau. Tinjau ulang.")
    values = validate_values(member.tenant, payload["after"])
    period = timezone.now().astimezone(WIB).date().replace(day=1)
    if schedule and schedule.runs.filter(period=period).exists():
        period = next_month(period)
    if payload["period"] != period.isoformat():
        raise ValidationError("Periode contoh berubah. Tinjau ulang jadwal.")
    _, fingerprint = example(member.tenant, values, period) if values["enabled"] else ([], "disabled")
    if fingerprint != payload["fingerprint"]:
        raise ValidationError("Pelanggan, harga, setelan atau faktur berubah. Tinjau ulang jadwal.")
    schedule = schedule or BillingSchedule(tenant=member.tenant, version=0)
    before = values_for(schedule)
    for key, value in values.items():
        setattr(schedule, "plan_id" if key=="plan" else key, value)
    schedule.version += 1
    schedule.authorized_by, schedule.authorization_version = actor, member.session_version
    schedule.mfa_revision, schedule.confirmation_key = device.revision, payload["key"]
    schedule.updated_at, schedule.reason, schedule.checked_at = timezone.now(), "", None
    schedule.full_clean()
    schedule.save()
    # A concurrent row can finish before this transaction commits; no later rows resume.
    cancelled = BillingBatch.objects.filter(schedule_run__schedule=schedule,
        status__in=["queued", "processing", "blocked"]).update(status="cancelled", finished_at=timezone.now(),
        reason="Jadwal diubah/dinonaktifkan; sisa pekerjaan dibatalkan. Faktur terbit tetap tersimpan.")
    audit(member.tenant, actor, "billing.schedule_configured", schedule.pk,
        json.dumps({"before": before, "after": values, "version": schedule.version, "cancelled_batches": cancelled}, sort_keys=True))
    return schedule


def consent_valid(schedule):
    try:
        member = manager(schedule.authorized_by) if schedule.authorized_by_id else None
    except (PermissionDenied, Membership.DoesNotExist):
        return False
    return bool(member and member.tenant_id == schedule.tenant_id
        and member.session_version == schedule.authorization_version
        and MFADevice.objects.filter(user=schedule.authorized_by, revision=schedule.mfa_revision).exists())


def batch_allowed(batch):
    run = BillingScheduleRun.objects.filter(batch=batch).select_related("schedule__authorized_by").first()
    return not run or (settings.SIMULATION and run.schedule.enabled
        and run.schedule.tenant_id == batch.tenant_id and run.period == batch.period
        and run.schedule.authorized_by_id == batch.created_by_id
        and run.schedule.authorization_version == batch.authorization_version
        and run.schedule.version == run.schedule_version and consent_valid(run.schedule))


@retry_locked
@transaction.atomic
def run_schedule(schedule_id, now=None):
    if not settings.SIMULATION:
        return None
    tenant_id = BillingSchedule.objects.values_list("tenant_id", flat=True).get(pk=schedule_id)
    tenant = Tenant.objects.select_for_update(no_key=True).get(pk=tenant_id)
    schedule = BillingSchedule.objects.select_for_update(of=("self",)).select_related("authorized_by").get(pk=schedule_id)
    current = now if now is not None else timezone.now()
    if timezone.is_naive(current):
        raise ValueError("Worker memerlukan waktu dengan timezone.")
    today = current.astimezone(WIB).date()
    period = today.replace(day=1)
    schedule.checked_at = current
    schedule.save(update_fields=["checked_at"])
    if not schedule.enabled:
        return None
    reason = ""
    if not consent_valid(schedule):
        reason = "Persetujuan akses/MFA berubah. Tinjau dan setujui ulang jadwal."
    if not reason and today >= month_due(period, schedule.day) and not schedule.runs.filter(period=period).exists():
        try:
            values = validate_values(tenant, values_for(schedule))
            rows, fingerprint = example(tenant, values, period)
        except ValidationError:
            reason = "Cakupan melebihi 500 pelanggan atau data harga/filter tidak valid. Perbaiki lalu tinjau jadwal."
        else:
            batch = None
            total = sum(row["snapshot"]["invoice"]["total"] for row in rows if not row["existing_id"])
            if rows:
                batch = BillingBatch.objects.create(tenant=tenant, created_by=schedule.authorized_by, period=period,
                    filters={key:values[key] for key in ("state", "plan", "area")}, discount=0,
                    fingerprint=fingerprint, status="queued", authorization_version=schedule.authorization_version,
                    expires_at=current, confirmed_at=current)
                BillingBatchItem.objects.bulk_create([BillingBatchItem(batch=batch, customer_id=row["customer_id"],
                    snapshot=row["snapshot"], invoice_id=row["existing_id"], status="skipped" if row["existing_id"] else "pending") for row in rows])
            run = BillingScheduleRun.objects.create(schedule=schedule, period=period, schedule_version=schedule.version,
                batch=batch, selected=len(rows), total=total)
            schedule.reason = ""
            schedule.save(update_fields=["reason"])
            audit(tenant, schedule.authorized_by, "billing.schedule_queued" if rows else "billing.schedule_empty",
                schedule.pk, f"Periode {period:%m/%Y}; {len(rows)} pelanggan; nominal baru Rp{total}.")
            return run
    if reason != schedule.reason:
        schedule.reason = reason
        schedule.save(update_fields=["reason"])
        if reason:
            audit(tenant, None, "billing.schedule_blocked", schedule.pk, reason)
    return None


def run_schedules(limit=10):
    if not settings.SIMULATION:
        return 0
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Batas jadwal harus 1–100.")
    ids = list(BillingSchedule.objects.filter(enabled=True).order_by(F("checked_at").asc(nulls_first=True), "pk")
               .values_list("pk", flat=True)[:limit])
    return sum(run_schedule(pk) is not None for pk in ids)
