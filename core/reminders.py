"""Opt-in, idempotent reminder outbox. This adapter never sends external messages."""
import time
from datetime import timedelta
from zoneinfo import ZoneInfo
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import BigIntegerField, Exists, F, OuterRef, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from .billing import audit, retry_locked
from .models import Invoice, Membership, Reminder, ReminderPolicy, Tenant, Ticket
from .operations import check_version, member_for
from .permissions import capabilities

WIB = ZoneInfo("Asia/Jakarta")


def manager(actor):
    member = member_for(actor)
    if not {"reminders", "tickets", "invoices", "customers"} <= capabilities(member):
        raise PermissionDenied("Izin pengingat billing dan SLA tidak tersedia.")
    return member


def policy_authorized(policy):
    if not policy.authorized_by_id:
        return False
    try:
        member = manager(policy.authorized_by)
    except (PermissionDenied, Membership.DoesNotExist):
        return False
    return member.tenant_id == policy.tenant_id and member.session_version == policy.authorization_version


@retry_locked
@transaction.atomic
def save_policy(actor, values, version, verified_at, access_version):
    member = manager(actor)
    Tenant.objects.select_for_update(no_key=True).get(pk=member.tenant_id)
    member = manager(actor)
    if member.session_version != access_version or not 0 <= time.time() - verified_at <= 300:
        raise PermissionDenied("Verifikasi MFA terbaru diperlukan untuk kebijakan pengingat.")
    if set(values) != {"billing_enabled", "sla_enabled", "lead_days", "billing_hour", "scenario"}:
        raise ValidationError("Field kebijakan pengingat tidak valid.")
    if any(type(values[field]) is not bool for field in ["billing_enabled", "sla_enabled"]):
        raise ValidationError("Status aktivasi pengingat harus boolean.")
    if not settings.SIMULATION and (values["billing_enabled"] or values["sla_enabled"]):
        raise ValidationError("Pengingat nyata belum tersedia. Fitur ini hanya untuk simulasi.")
    policy, _ = ReminderPolicy.objects.get_or_create(tenant=member.tenant)
    check_version(policy, version)
    for field, value in values.items():
        setattr(policy, field, value)
    policy.authorized_by, policy.authorization_version = actor, member.session_version
    policy.version += 1
    policy.updated_at = timezone.now()
    policy.full_clean()
    policy.save()
    # Fresh approval can resume permission-blocked rows; conditions are still rechecked by the worker.
    Reminder.objects.filter(tenant=member.tenant, status="blocked").update(status="pending", next_attempt_at=timezone.now(),
        reason="Kebijakan disetujui ulang; menunggu pemeriksaan kondisi terbaru.", processed_at=None)
    audit(member.tenant, actor, "reminders.policy_saved", policy.pk,
          f"Billing={policy.billing_enabled}; SLA={policy.sla_enabled}; simulasi saja.")
    return policy


def check_limit(limit):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("Batas pemrosesan harus 1–500.")


@retry_locked
@transaction.atomic
def plan_reminders(policy_id, limit=100):
    check_limit(limit)
    if not settings.SIMULATION:
        raise ValidationError("Planner pengingat hanya tersedia dalam mode simulasi.")
    policy = ReminderPolicy.objects.get(pk=policy_id)
    now = timezone.now()
    policy.last_checked_at = now
    policy.save(update_fields=["last_checked_at"])
    if not policy_authorized(policy):
        return 0
    local = timezone.localtime(now, WIB)
    today = local.date()
    candidates = []
    if policy.sla_enabled:
        existing = Reminder.objects.filter(tenant=policy.tenant, kind="sla", ticket_id=OuterRef("pk"),
            sla_cycle=OuterRef("sla_cycle"), deadline_snapshot=OuterRef("sla_due_at"))
        tickets = Ticket.objects.filter(tenant=policy.tenant, sla_due_at__lt=now, resolved_at__isnull=True)
        tickets = tickets.exclude(status__in=["resolved", "closed"]).annotate(already_planned=Exists(existing)).filter(already_planned=False)
        for ticket in tickets.order_by("sla_due_at", "pk")[:limit]:
            candidates.append((0, ticket.pk, "sla", ticket))
    if policy.billing_enabled and local.hour >= policy.billing_hour:
        existing = Reminder.objects.filter(tenant=policy.tenant, kind="billing", invoice_id=OuterRef("pk"), business_date=today)
        invoices = Invoice.objects.filter(tenant=policy.tenant, due_date__lte=today + timedelta(days=policy.lead_days))
        invoices = invoices.annotate(allocated=Coalesce(Sum("payments__amount", filter=Q(payments__reversal__isnull=True)),
            Value(0), output_field=BigIntegerField()), already_planned=Exists(existing))
        invoices = invoices.filter(total__gt=F("allocated"), already_planned=False)
        for invoice in invoices.order_by("due_date", "pk")[:limit]:
            candidates.append((1, invoice.pk, "billing", invoice))
    created = 0
    for _, _, kind, target in sorted(candidates, key=lambda item: item[:2])[:limit]:
        common = {"tenant": policy.tenant, "kind": kind}
        if kind == "billing":
            key = {**common, "invoice": target, "business_date": today}
        else:
            key = {**common, "ticket": target, "sla_cycle": target.sla_cycle, "deadline_snapshot": target.sla_due_at}
        _, is_new = Reminder.objects.get_or_create(**key, defaults={"policy_version_planned": policy.version,
            "created_at": now, "next_attempt_at": now,
            **({"business_date": today} if kind == "sla" else {})})
        created += int(is_new)
    policy.last_checked_at = now
    policy.save(update_fields=["last_checked_at"])
    return created


class SandboxNotifier:
    def record(self, recipient, body, scenario):
        if not settings.SIMULATION:
            raise ValidationError("Pengiriman nyata tidak tersedia.")
        if scenario == "unavailable":
            raise ConnectionError("Skenario gangguan simulasi")
        # No HTTP, SMTP, filesystem delivery or messaging SDK is used.
        return True


def finish(reminder, policy, state, reason, now):
    reminder.status, reminder.reason, reminder.processed_at = state, reason, now
    reminder.policy_version_processed = policy.version if policy else None
    if state in {"skipped", "blocked"}:
        reminder.body = reminder.recipient = ""
    reminder.save()
    audit(reminder.tenant, None, "reminder." + state, reminder.pk, reason)
    return reminder


@retry_locked
@transaction.atomic
def process_reminder(reminder_id):
    if not settings.SIMULATION:
        raise ValidationError("Worker pengingat hanya tersedia dalam simulasi.")
    reminder = Reminder.objects.select_for_update().get(pk=reminder_id)
    now = timezone.now()
    if reminder.status not in {"pending", "failed"} or reminder.attempts >= 3 or reminder.next_attempt_at > now:
        return reminder
    policy = ReminderPolicy.objects.filter(tenant=reminder.tenant).first()
    if not policy or not policy_authorized(policy):
        return finish(reminder, policy, "blocked", "Izin persetujuan berubah. Simpan ulang kebijakan dengan MFA terbaru.", now)
    local = timezone.localtime(now, WIB)
    if reminder.kind == "billing":
        if not policy.billing_enabled or reminder.business_date != local.date():
            return finish(reminder, policy, "skipped", "Pengingat billing dinonaktifkan atau tanggal bisnis telah berganti.", now)
        if local.hour < policy.billing_hour:
            reminder.next_attempt_at = local.replace(hour=policy.billing_hour, minute=0, second=0, microsecond=0)
            reminder.save(update_fields=["next_attempt_at"])
            return reminder
        invoice = Invoice.objects.select_for_update(of=("self",)).select_related("customer").filter(pk=reminder.invoice_id, tenant=reminder.tenant).first()
        if not invoice:
            return finish(reminder, policy, "skipped", "Objek faktur tidak tersedia dalam ISP ini.", now)
        if invoice.customer.tenant_id != reminder.tenant_id:
            return finish(reminder, policy, "skipped", "Cakupan pelanggan tidak sesuai.", now)
        if invoice.balance <= 0 or invoice.due_date > local.date() + timedelta(days=policy.lead_days):
            return finish(reminder, policy, "skipped", "Faktur sudah lunas atau di luar jendela jatuh tempo.", now)
        reminder.recipient = f"Simulasi pelanggan {invoice.customer.code}"
        amount = "Rp" + f"{invoice.balance:,}".replace(",", ".")
        reminder.body = (f"{reminder.tenant.name}: pengingat simulasi {invoice.number} untuk {invoice.customer_name}. "
            f"Sisa tagihan {amount}; jatuh tempo {invoice.due_date:%d/%m/%Y}. "
            f"Data diperiksa {local:%d/%m/%Y %H:%M} WIB. Tidak ada pesan nyata dikirim.")
    else:
        ticket = Ticket.objects.select_for_update().filter(pk=reminder.ticket_id, tenant=reminder.tenant).first()
        if not ticket:
            return finish(reminder, policy, "skipped", "Objek tiket tidak tersedia dalam ISP ini.", now)
        if (not policy.sla_enabled or ticket.status in {"resolved", "closed"} or ticket.resolved_at
                or ticket.sla_due_at >= now or ticket.sla_due_at != reminder.deadline_snapshot or ticket.sla_cycle != reminder.sla_cycle):
            return finish(reminder, policy, "skipped", "SLA telah selesai, dinonaktifkan, atau deadline/siklus telah berubah.", now)
        reminder.recipient = f"Tim operasional {reminder.tenant.name}"
        deadline = timezone.localtime(ticket.sla_due_at, WIB)
        reminder.body = (f"Pengingat SLA simulasi {ticket.number}: {ticket.title}. Target {deadline:%d/%m/%Y %H:%M} WIB "
            f"telah lewat. Data diperiksa {local:%d/%m/%Y %H:%M} WIB. Tidak ada pesan nyata dikirim.")
    reminder.attempts += 1
    try:
        SandboxNotifier().record(reminder.recipient, reminder.body, policy.scenario)
    except (ConnectionError, TimeoutError):
        reminder.next_attempt_at = now + timedelta(seconds=30 * 2 ** (reminder.attempts - 1))
        return finish(reminder, policy, "failed", "Gangguan simulasi; maksimal 3 percobaan dengan backoff.", now)
    return finish(reminder, policy, "simulated", "Hasil simulasi tersimpan. Tidak ada pesan dikirim ke luar aplikasi.", now)
