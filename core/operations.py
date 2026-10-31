"""Scoped support and installation workflows; no external network effects."""
import hashlib
import json
import time
import uuid
from datetime import timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .billing import audit, invoice_snapshot, retry_locked, write_snapshot_invoice
from .models import Customer, Installation, Membership, NetworkJob, Ticket, WorkEvent
from .permissions import CAPABILITIES, capabilities
from .validation import normalize_phone

TICKET_TRANSITIONS = {"open": {"in_progress", "resolved"}, "in_progress": {"waiting_customer", "resolved"},
    "waiting_customer": {"in_progress", "resolved"}, "resolved": {"in_progress", "closed"}, "closed": set()}
INSTALLATION_TRANSITIONS = {"prospect": {"survey", "cancelled"}, "survey": {"scheduled", "cancelled"},
    "scheduled": {"installing", "cancelled"}, "installing": {"installed", "cancelled"}, "installed": {"cancelled"}}


def member_for(actor):
    member = Membership.objects.select_related("user", "tenant", "access_role", "customer").get(user_id=actor.pk)
    if not member.user.is_active or member.role not in CAPABILITIES:
        raise PermissionDenied
    if member.role == "customer" and (not member.customer_id or member.customer.tenant_id != member.tenant_id):
        raise PermissionDenied
    if member.access_role_id and (member.access_role.tenant_id != member.tenant_id or member.role in {"owner", "customer", "technician"}):
        raise PermissionDenied
    return member


def visible_tickets(member):
    query = Ticket.objects.filter(tenant=member.tenant)
    if member.role == "customer":
        return query.filter(customer_id=member.customer_id)
    if member.role == "technician":
        return query.filter(assigned_to=member)
    if "tickets" not in capabilities(member):
        raise PermissionDenied
    return query


def visible_installations(member):
    query = Installation.objects.filter(tenant=member.tenant)
    if member.role == "technician":
        return query.filter(assigned_to=member)
    if "installations" not in capabilities(member):
        raise PermissionDenied
    return query


def assignee(tenant, assigned_id):
    if assigned_id is None:
        return None
    try:
        return Membership.objects.get(pk=assigned_id, tenant=tenant, role="technician", user__is_active=True)
    except Membership.DoesNotExist:
        raise ValidationError("Pilih teknisi aktif pada ISP yang sama.") from None


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def checked_text(text, minimum=1, maximum=4000):
    text = text.strip()
    if not minimum <= len(text) <= maximum:
        raise ValidationError(f"Teks wajib {minimum}–{maximum} karakter.")
    return text


def replay(member, target, key, kind, data):
    previous = WorkEvent.objects.filter(tenant=member.tenant, request_key=key).first()
    if previous:
        signature = digest({"kind": kind, "data": data, "target": target.pk, "type": target._meta.model_name})
        target_matches = previous.ticket_id == target.pk if isinstance(target, Ticket) else previous.installation_id == target.pk
        if not target_matches or previous.actor_id != member.user_id or previous.signature != signature or previous.kind != kind:
            raise ValidationError("Kunci permintaan telah digunakan untuk tindakan berbeda.")
        return True
    return False


def event(member, target, kind, body, key=None, data=None, internal=True):
    values = {"ticket": target} if isinstance(target, Ticket) else {"installation": target}
    signature = digest({"kind": kind, "data": data, "target": target.pk, "type": target._meta.model_name}) if key else ""
    WorkEvent.objects.create(tenant=member.tenant, actor=member.user, kind=kind, body=body, internal=internal,
        request_key=key, signature=signature, **values)
    audit(member.tenant, member.user, f"{target._meta.model_name}.{kind}", target.pk,
          "Komentar internal" if kind == "comment" and internal else "Aktivitas operasional dicatat.")


def check_version(target, version):
    if target.version != version:
        raise ValidationError("Data sudah berubah. Muat ulang sebelum menyimpan; perubahan Anda belum diterapkan.")


@retry_locked
@transaction.atomic
def create_ticket(actor, customer_id, title, description, category, priority="normal", assigned_id=None, request_key=None):
    member = member_for(actor)
    if member.role == "customer":
        if customer_id != member.customer_id:
            raise PermissionDenied
        priority, assigned_id = "normal", None
    elif "tickets" not in capabilities(member):
        raise PermissionDenied
    customer = Customer.objects.get(pk=customer_id, tenant=member.tenant)
    assigned = assignee(member.tenant, assigned_id)
    data = {"customer": customer.pk, "title": checked_text(title, maximum=140), "description": checked_text(description),
            "category": category, "priority": priority, "assigned": assigned_id}
    key = request_key or uuid.uuid4()
    existing = Ticket.objects.filter(tenant=member.tenant, request_key=key).first()
    if existing:
        if existing.created_by_id != actor.pk or existing.fingerprint != digest(data):
            raise ValidationError("Kunci permintaan tiket telah dipakai untuk data berbeda.")
        return existing
    policy = {priority: getattr(member.tenant, f"sla_{priority}_hours") for priority, _ in Ticket.PRIORITIES}
    if priority not in policy:
        raise ValidationError("Prioritas tidak valid.")
    now = timezone.now()
    ticket = Ticket(tenant=member.tenant, created_by=actor, customer=customer, assigned_to=assigned,
        title=data["title"], description=data["description"], category=category, priority=priority,
        request_key=key, fingerprint=digest(data), sla_policy=policy,
        sla_due_at=now + timedelta(hours=policy[priority]), created_at=now, updated_at=now)
    ticket.full_clean()
    ticket.save()
    event(member, ticket, "created", "Tiket dibuat.", internal=False)
    return ticket


@retry_locked
@transaction.atomic
def manage_ticket(actor, ticket_id, priority, category, assigned_id, version, key):
    member = member_for(actor)
    if "tickets" not in capabilities(member):
        raise PermissionDenied
    ticket = visible_tickets(member).select_for_update().get(pk=ticket_id)
    data = {"priority": priority, "category": category, "assigned": assigned_id, "version": version}
    if replay(member, ticket, key, "managed", data):
        return ticket
    check_version(ticket, version)
    if ticket.status == "closed":
        raise ValidationError("Tiket ditutup tidak dapat diubah.")
    ticket.assigned_to = assignee(member.tenant, assigned_id)
    if priority not in ticket.sla_policy:
        raise ValidationError("Prioritas tidak valid.")
    ticket.priority, ticket.category = priority, category
    ticket.sla_due_at = ticket.created_at + timedelta(hours=ticket.sla_policy[priority])
    ticket.version += 1
    ticket.updated_at = timezone.now()
    ticket.full_clean()
    ticket.save()
    event(member, ticket, "managed", "Prioritas, kategori, atau penugasan diperbarui; SLA memakai kebijakan saat tiket dibuat.", key, data)
    return ticket


@retry_locked
@transaction.atomic
def transition_ticket(actor, ticket_id, status, note, version, key):
    member = member_for(actor)
    if member.role == "customer":
        raise PermissionDenied
    ticket = visible_tickets(member).select_for_update().get(pk=ticket_id)
    data = {"status": status, "note": note, "version": version}
    if replay(member, ticket, key, "status", data):
        return ticket
    check_version(ticket, version)
    if status not in TICKET_TRANSITIONS[ticket.status] or (member.role == "technician" and status == "closed"):
        raise ValidationError("Perubahan status tidak diizinkan.")
    note = checked_text(note, minimum=10, maximum=2000)
    before = ticket.get_status_display()
    ticket.status = status
    if status == "resolved":
        ticket.resolved_at = timezone.now()
    elif status == "in_progress":
        ticket.resolved_at = None
    ticket.version += 1
    ticket.updated_at = timezone.now()
    ticket.save()
    event(member, ticket, "status", f"{before} → {ticket.get_status_display()}. {note}", key, data, internal=False)
    return ticket


@retry_locked
@transaction.atomic
def add_comment(actor, target_id, body, internal, key, installation=False):
    member = member_for(actor)
    if member.role == "customer" and internal:
        raise PermissionDenied
    query = visible_installations(member) if installation else visible_tickets(member)
    target = query.select_for_update().get(pk=target_id)
    body = checked_text(body)
    data = {"body": body, "internal": bool(internal)}
    if replay(member, target, key, "comment", data):
        return target
    event(member, target, "comment", body, key, data, internal=bool(internal) if not installation else True)
    target.updated_at = timezone.now()
    target.save(update_fields=["updated_at"])
    return target


@retry_locked
@transaction.atomic
def create_installation(actor, data, request_key=None):
    member = member_for(actor)
    if "installations" not in capabilities(member):
        raise PermissionDenied
    allowed = {"name", "email", "phone", "username", "address", "area", "plan_id", "assigned_id"}
    if set(data) - allowed:
        raise ValidationError("Field PSB tidak diizinkan.")
    values = dict(data)
    values["phone"] = normalize_phone(values["phone"])
    plan = member.tenant.plan_set.get(pk=values["plan_id"])
    assigned = assignee(member.tenant, values.pop("assigned_id", None))
    values["assigned_id"] = assigned.pk if assigned else None
    signature = digest(values)
    key = request_key or uuid.uuid4()
    existing = Installation.objects.filter(tenant=member.tenant, request_key=key).first()
    if existing:
        if existing.created_by_id != actor.pk or existing.fingerprint != signature:
            raise ValidationError("Kunci permintaan PSB telah digunakan untuk data berbeda.")
        return existing
    if Customer.objects.filter(tenant=member.tenant).filter(Q(phone=values["phone"]) | Q(username__iexact=values["username"])).exists():
        raise ValidationError("Telepon atau username sudah dipakai pelanggan. Tinjau data sebelum membuat PSB.")
    values.pop("assigned_id")
    order = Installation(tenant=member.tenant, created_by=actor, request_key=key, fingerprint=signature,
                         assigned_to=assigned, **values)
    order.full_clean()
    order.save()
    event(member, order, "created", "Prospek PSB dibuat. Belum ada pelanggan, faktur, atau provisioning.")
    return order


@retry_locked
@transaction.atomic
def assign_installation(actor, order_id, assigned_id, version, key):
    member = member_for(actor)
    if "installations" not in capabilities(member):
        raise PermissionDenied
    order = visible_installations(member).select_for_update().get(pk=order_id)
    data = {"assigned": assigned_id, "version": version}
    if replay(member, order, key, "assigned", data):
        return order
    check_version(order, version)
    if order.status in {"activated", "cancelled"}:
        raise ValidationError("PSB sudah selesai dan tidak dapat ditugaskan ulang.")
    assigned = assignee(member.tenant, assigned_id)
    if not assigned and order.status in {"scheduled", "installing", "installed"}:
        raise ValidationError("Tahap ini memerlukan teknisi aktif.")
    order.assigned_to, order.updated_at = assigned, timezone.now()
    order.version += 1
    order.save()
    event(member, order, "assigned", "Penugasan teknisi diperbarui.", key, data)
    return order


@retry_locked
@transaction.atomic
def transition_installation(actor, order_id, status, note, scheduled_for, version, key):
    member = member_for(actor)
    order = visible_installations(member).select_for_update().get(pk=order_id)
    data = {"status": status, "note": note, "scheduled": scheduled_for, "version": version}
    if replay(member, order, key, "status", data):
        return order
    check_version(order, version)
    if status not in INSTALLATION_TRANSITIONS.get(order.status, set()) or (member.role == "technician" and status == "cancelled"):
        raise ValidationError("Perubahan status PSB tidak diizinkan.")
    note = checked_text(note, minimum=10, maximum=2000)
    if status == "scheduled":
        if not order.assigned_to_id:
            raise ValidationError("Tentukan teknisi sebelum menjadwalkan pemasangan.")
        assignee(member.tenant, order.assigned_to_id)
        if not scheduled_for or timezone.is_naive(scheduled_for) or scheduled_for <= timezone.now():
            raise ValidationError("Jadwal pemasangan harus waktu mendatang dan bertimezone.")
        order.scheduled_for = scheduled_for
    before = order.get_status_display()
    order.status, order.updated_at = status, timezone.now()
    order.version += 1
    order.save()
    event(member, order, "status", f"{before} → {order.get_status_display()}. {note}", key, data)
    return order


def activation_quote(order, period):
    customer = Customer(tenant=order.tenant, plan=order.plan, name=order.name, email=order.email,
        phone=order.phone, username=order.username, address=order.address, area=order.area)
    snapshot = invoice_snapshot(order.tenant, customer, period)
    identity = {field: getattr(order, field) for field in ["name", "email", "phone", "username", "address", "area", "plan_id"]}
    return snapshot, digest({"snapshot": snapshot, "identity": identity, "version": order.version, "status": order.status})


@retry_locked
@transaction.atomic
def activate_installation(actor, order_id, period, fingerprint, verified_at, access_version):
    member = member_for(actor)
    if "activate_installations" not in capabilities(member) or member.session_version != access_version or not 0 <= time.time() - verified_at <= 300:
        raise PermissionDenied("Izin aktivasi dan MFA terbaru diperlukan.")
    order = visible_installations(member).select_for_update().get(pk=order_id)
    if order.status == "activated":
        if order.invoice.period != period:
            raise ValidationError("PSB telah diaktivasi untuk periode berbeda.")
        return order
    if order.status != "installed":
        raise ValidationError("Pemasangan harus berstatus Terpasang sebelum aktivasi.")
    _, current = activation_quote(order, period)
    if current != fingerprint:
        raise ValidationError("Harga, setelan atau PSB berubah. Muat ulang pratinjau aktivasi.")
    if Customer.objects.filter(tenant=member.tenant).filter(Q(phone=order.phone) | Q(username__iexact=order.username)).exists():
        raise ValidationError("Telepon atau username sudah dipakai pelanggan. Tidak ada pelanggan/faktur baru dibuat.")
    customer = Customer(tenant=member.tenant, plan=order.plan, code="TN-" + uuid.uuid4().hex[:10].upper(),
        name=order.name, email=order.email, phone=order.phone, username=order.username, address=order.address,
        area=order.area, service_state="pending")
    customer.full_clean()
    customer.save()
    snapshot = invoice_snapshot(order.tenant, customer, period)
    invoice = write_snapshot_invoice(member.tenant, customer, period, snapshot, actor)
    NetworkJob.objects.create(tenant=member.tenant, customer=customer)
    order.customer, order.invoice = customer, invoice
    order.status, order.updated_at = "activated", timezone.now()
    order.version += 1
    order.save()
    event(member, order, "activated", "Aktivasi administrasi selesai: pelanggan dan faktur awal dibuat. Provisioning jaringan masuk antrean terpisah.")
    return order
