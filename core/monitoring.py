"""Read-only simulation snapshots. No router address, credential or network I/O."""
import time
from datetime import datetime, timedelta
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import OuterRef, Subquery
from django.utils import timezone
from .billing import audit, retry_locked
from .models import Membership, MonitoringDevice, MonitoringPoll, Tenant
from .operations import check_version, member_for
from .permissions import capabilities

POLL_SECONDS = 60
FRESH_SECONDS = 120


def manager(actor):
    member = member_for(actor)
    if not {"network", "manage_monitoring"} <= capabilities(member):
        raise PermissionDenied("Izin mengatur monitoring tidak tersedia.")
    return member


def consent_valid(device):
    if not device.authorized_by_id:
        return False
    try:
        member = manager(device.authorized_by)
    except (PermissionDenied, Membership.DoesNotExist):
        return False
    return member.tenant_id == device.tenant_id and member.session_version == device.authorization_version


@retry_locked
@transaction.atomic
def save_device(actor, values, version, verified_at, access_version, device_id=None):
    member = manager(actor)
    Tenant.objects.select_for_update(no_key=True).get(pk=member.tenant_id)
    member = manager(actor)
    if member.session_version != access_version or not 0 <= time.time()-verified_at <= 300:
        raise PermissionDenied("MFA terbaru diperlukan untuk menyimpan monitoring.")
    if set(values) != {"name", "location", "enabled", "scenario"} or type(values["enabled"]) is not bool:
        raise ValidationError("Field monitoring tidak valid.")
    if values["enabled"] and not settings.SIMULATION:
        raise ValidationError("Monitoring nyata belum tersedia.")
    if device_id is None and MonitoringDevice.objects.filter(tenant=member.tenant).count() >= 50:
        raise ValidationError("Maksimal 50 NAS simulasi per ISP pada checkpoint ini.")
    device = (MonitoringDevice.objects.select_for_update().get(tenant=member.tenant, pk=device_id)
        if device_id else MonitoringDevice(tenant=member.tenant))
    check_version(device, version)
    for field, value in values.items():
        setattr(device, field, value.strip() if isinstance(value, str) else value)
    device.authorized_by, device.authorization_version = actor, member.session_version
    device.version += 1
    device.updated_at = timezone.now()
    device.next_poll_at = device.updated_at if device.enabled else None
    device.full_clean()
    device.save()
    audit(member.tenant, actor, "monitoring.configured", device.pk,
          f"Versi {device.version}; aktif={device.enabled}; hanya simulasi.")
    return device


class SimulationMonitor:
    def read(self, device):
        if not settings.SIMULATION:
            raise ValidationError("Adapter monitoring nyata belum tersedia.")
        if device.scenario == "unavailable":
            raise ConnectionError("Skenario router tidak tersedia")
        observed = timezone.now() - (timedelta(minutes=10) if device.scenario == "stale" else timedelta())
        sessions = [] if device.scenario == "empty" else [
            {"username": "demo-ppp-01", "state": "online", "uptime_seconds": 3600},
            {"username": "demo-ppp-02", "state": "online", "uptime_seconds": 120},
            {"username": "demo-ppp-03", "state": "offline", "uptime_seconds": 0},
        ]
        return observed, sessions


def validate_snapshot(observed, sessions, now):
    if (not isinstance(observed, datetime) or timezone.is_naive(observed) or observed > now
            or observed < now-timedelta(days=1) or not isinstance(sessions, list) or len(sessions) > 100):
        raise ValidationError("Snapshot monitoring tidak valid.")
    names = set()
    for item in sessions:
        if (not isinstance(item, dict) or set(item) != {"username", "state", "uptime_seconds"}
                or not isinstance(item["username"], str) or not 1 <= len(item["username"]) <= 64
                or item["username"] in names or item["state"] not in {"online", "offline"}
                or type(item["uptime_seconds"]) is not int or not 0 <= item["uptime_seconds"] <= 31536000
                or (item["state"] == "offline" and item["uptime_seconds"] != 0)):
            raise ValidationError("Data sesi monitoring tidak valid.")
        names.add(item["username"])


@retry_locked
@transaction.atomic
def collect_poll(device_id, actor=None, expected_version=None):
    if not settings.SIMULATION:
        raise ValidationError("Pengambilan monitoring hanya tersedia dalam simulasi.")
    query = MonitoringDevice.objects.select_for_update()
    if actor is not None:
        member = member_for(actor)
        if "network" not in capabilities(member):
            raise PermissionDenied
        query = query.filter(tenant=member.tenant)
    device = query.get(pk=device_id)
    if expected_version is not None:
        check_version(device, expected_version)
    if not device.enabled:
        raise ValidationError("Monitoring NAS ini nonaktif.")
    now = timezone.now()
    if device.next_poll_at is None or device.next_poll_at > now:
        return device.polls.first()
    key = {"device": device, "device_version": device.version, "bucket": int(now.timestamp()) // POLL_SECONDS}
    existing = MonitoringPoll.objects.filter(**key).first()
    if existing:
        return existing
    observed, sessions = None, []
    status, reason = "blocked", "Izin persetujuan berubah. Simpan ulang konfigurasi dengan MFA terbaru."
    if consent_valid(device):
        try:
            observed, sessions = SimulationMonitor().read(device)
            # The adapter is called after now was captured; compare with its completion time.
            validate_snapshot(observed, sessions, timezone.now())
        except (ConnectionError, TimeoutError, ValidationError, TypeError, ValueError):
            observed, sessions = None, []
            status, reason = "failed", "Pengambilan simulasi gagal atau snapshot tidak valid. Snapshot lama tetap tersimpan."
        else:
            status, reason = "success", "Snapshot simulasi tersimpan; tidak terhubung ke router nyata."
    poll = MonitoringPoll.objects.create(**key, tenant=device.tenant, scenario=device.scenario, status=status,
        observed_at=observed, sessions=sessions, reason=reason, created_at=now)
    device.next_poll_at = None if status == "blocked" else now + timedelta(seconds=POLL_SECONDS)
    device.save(update_fields=["next_poll_at"])
    audit(device.tenant, actor, "monitoring."+status, device.pk, reason)
    return poll


def snapshots(devices):
    """Bounded query count for paginated devices and whole-tenant aggregate alike."""
    last = MonitoringPoll.objects.filter(device_id=OuterRef("pk"), tenant_id=OuterRef("tenant_id"))
    devices = list(devices.annotate(last_id=Subquery(last.values("pk")[:1]),
        snapshot_id=Subquery(last.filter(status="success").values("pk")[:1])).select_related("authorized_by"))
    ids = {key for device in devices for key in [device.last_id, device.snapshot_id] if key}
    polls = MonitoringPoll.objects.in_bulk(ids)
    now = timezone.now()
    rows = []
    consent = {}
    for device in devices:
        consent_key = (device.tenant_id, device.authorized_by_id, device.authorization_version)
        if consent_key not in consent:
            consent[consent_key] = consent_valid(device)
        latest, snapshot = polls.get(device.last_id), polls.get(device.snapshot_id)
        current = snapshot and snapshot.device_version == device.version
        age = (now-snapshot.observed_at).total_seconds() if snapshot else None
        state, label = "pending", "Belum ada snapshot versi ini"
        if not device.enabled:
            state, label = "disabled", "Nonaktif"
        elif not consent[consent_key]:
            state, label = "blocked", "Persetujuan tertahan"
        elif latest and latest.device_version == device.version and latest.status != "success":
            state, label = "failed", "Gagal diambil"
        elif current:
            state, label = ("fresh", "Segar") if 0 <= age <= FRESH_SECONDS else ("stale", "Kedaluwarsa")
        online = sum(item["state"] == "online" for item in snapshot.sessions) if snapshot else None
        rows.append({"device": device, "latest": latest, "snapshot": snapshot, "state": state, "label": label,
            "online": online, "offline": len(snapshot.sessions)-online if snapshot else None, "age_seconds": age})
    return rows


def overview(tenant):
    rows = snapshots(MonitoringDevice.objects.filter(tenant=tenant))
    fresh = [row for row in rows if row["state"] == "fresh"]
    return {"total": len(rows), "fresh": len(fresh), "excluded": len(rows)-len(fresh),
        "online": sum(row["online"] for row in fresh) if fresh else None,
        "offline": sum(row["offline"] for row in fresh) if fresh else None,
        "oldest_observed": min((row["snapshot"].observed_at for row in fresh), default=None),
        "updated_at": timezone.now()}
