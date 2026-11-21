"""Opt-in date planner for the existing simulated network queue."""
from datetime import date, timedelta
import time

from django.conf import settings
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q, Subquery
from django.utils import timezone

from .billing import audit, retry_locked
from .invoice_queries import with_balances
from .isolation import invoice_grace_days, isolation_date
from .isolation_models import MAX_GRACE_DAYS
from .models import Customer, Invoice, IsolationSchedule, Membership, MFADevice, NetworkJob, ScheduledIsolation, Tenant
from .operations import member_for
from .permissions import capabilities

CAPABILITY = 'manage_isolation_schedule'
RIGHTS = {CAPABILITY, 'network'}
SALT = 'tagihnet.isolation-schedule.v1'
CHECK_INTERVAL = timedelta(minutes=1)
JOBS_PER_ISP = 25


def manager(actor):
    member = member_for(actor)
    if not RIGHTS <= capabilities(member):
        raise PermissionDenied('Izin mengatur jadwal isolir diperlukan.')
    return member


def consent_valid(schedule):
    if not (settings.SIMULATION and schedule.enabled and schedule.authorized_by_id and schedule.starts_on):
        return False
    try:
        member = manager(schedule.authorized_by)
    except (Membership.DoesNotExist, PermissionDenied):
        return False
    return (member.tenant_id == schedule.tenant_id and member.session_version == schedule.authorization_version
            and MFADevice.objects.filter(user_id=member.user_id, revision=schedule.mfa_revision).exists())


def review_change(actor, enabled, version):
    member = manager(actor)
    if type(enabled) is not bool or type(version) is not int or version < 0:
        raise ValidationError('Setelan jadwal tidak valid.')
    if enabled and not settings.SIMULATION:
        raise ValidationError('Jadwal isolir tersedia dalam mode simulasi.')
    schedule = IsolationSchedule.objects.filter(tenant_id=member.tenant_id).first()
    if version != (schedule.version if schedule else 0):
        raise ValidationError('Jadwal telah berubah. Muat ulang dan tinjau kembali.')
    device = MFADevice.objects.filter(user=actor).first()
    if device is None:
        raise PermissionDenied('MFA operator diperlukan.')
    payload = {'actor': actor.pk, 'tenant': member.tenant_id, 'access': member.session_version,
               'mfa': str(device.revision), 'version': version, 'enabled': enabled,
               'starts_on': timezone.localdate().isoformat() if enabled else None}
    return signing.dumps(payload, salt=SALT), payload


def read_review(actor, token):
    member = manager(actor)
    try:
        if not isinstance(token, str) or len(token) > 4096:
            raise ValueError
        payload = signing.loads(token, salt=SALT, max_age=900)
        if (not isinstance(payload, dict) or set(payload) != {'actor', 'tenant', 'access', 'mfa', 'version', 'enabled', 'starts_on'}
                or type(payload['enabled']) is not bool or type(payload['version']) is not int or payload['version'] < 0):
            raise ValueError
        if payload['enabled']:
            date.fromisoformat(payload['starts_on'])
        elif payload['starts_on'] is not None:
            raise ValueError
    except (signing.BadSignature, TypeError, ValueError, KeyError):
        raise ValidationError('Tinjauan jadwal tidak valid atau kedaluwarsa.') from None
    device = MFADevice.objects.filter(user=actor).first()
    if (payload['actor'] != actor.pk or payload['tenant'] != member.tenant_id
            or payload['access'] != member.session_version or not device or payload['mfa'] != str(device.revision)):
        raise PermissionDenied('Tinjauan ini memerlukan akses dan perangkat MFA yang sama.')
    return payload


@retry_locked
@transaction.atomic
def confirm_change(actor, token, verified_at, access_version, correlation_id=None):
    member = manager(actor)
    tenant = Tenant.objects.select_for_update(no_key=True).get(pk=member.tenant_id)
    Membership.objects.select_for_update().get(user_id=actor.pk, tenant=tenant)
    member = manager(actor)
    if member.session_version != access_version or type(verified_at) not in (int, float) or not 0 <= time.time() - verified_at <= 300:
        raise PermissionDenied('Verifikasi ulang MFA sebelum mengubah jadwal.')
    payload = read_review(actor, token)
    schedule = IsolationSchedule.objects.select_for_update().filter(tenant=tenant).first()
    if payload['version'] != (schedule.version if schedule else 0):
        raise ValidationError('Jadwal telah berubah. Tinjau kembali.')
    enabled = payload['enabled']
    starts_on = date.fromisoformat(payload['starts_on']) if enabled else None
    if enabled and (not settings.SIMULATION or starts_on != timezone.localdate()):
        raise ValidationError('Tanggal aktivasi atau mode simulasi berubah. Tinjau kembali.')
    if not enabled and (schedule is None or not schedule.enabled):
        return False
    schedule = schedule or IsolationSchedule(tenant=tenant, version=0)
    schedule.enabled = enabled
    schedule.starts_on = starts_on
    schedule.version += 1
    schedule.authorized_by = actor if enabled else None
    schedule.authorization_version = member.session_version if enabled else 0
    schedule.mfa_revision = payload['mfa'] if enabled else None
    schedule.checked_at = None
    schedule.save()
    detail = f'Jadwal mulai {starts_on.isoformat()} WIB.' if enabled else 'Jadwal dinonaktifkan.'
    audit(tenant, actor, 'network.isolation_schedule_configured', schedule.pk, detail, correlation_id)
    return True


def eligible_invoices(schedule, today):
    # DateField comparisons stay identical on SQLite and PostgreSQL. At most 31 windows.
    eligible = Q(pk__in=[])
    for days in range(MAX_GRACE_DAYS + 1):
        shift = timedelta(days=days + 1)
        terms = Q(isolation_terms__grace_days=days) if days else Q(isolation_terms__isnull=True)
        eligible |= terms & Q(due_date__range=(schedule.starts_on - shift, today - shift))
    already_queued = ScheduledIsolation.objects.filter(invoice_id=OuterRef('pk'), schedule_version=schedule.version)
    return with_balances(Invoice.objects.filter(eligible), schedule.tenant_id).filter(
        remaining__gt=0).filter(~Exists(already_queued))


def candidate_customers(schedule, today):
    pending = NetworkJob.objects.filter(tenant_id=schedule.tenant_id, customer_id=OuterRef('pk'),
        status__in=['pending', 'failed'], attempts__lt=3)
    exhausted = NetworkJob.objects.filter(tenant_id=schedule.tenant_id, customer_id=OuterRef('pk'),
        status='failed', attempts__gte=3, retry_replacement__isnull=True).filter(
            Q(scheduled_isolation__schedule=schedule, scheduled_isolation__schedule_version=schedule.version)
            | Q(retry_request__trigger__schedule=schedule, retry_request__trigger__schedule_version=schedule.version))
    invoices = eligible_invoices(schedule, today).filter(customer_id=OuterRef('pk')).order_by('due_date', 'pk')
    return Customer.objects.filter(tenant_id=schedule.tenant_id, service_state='active').filter(
        ~Exists(pending), ~Exists(exhausted)).annotate(trigger_id=Subquery(invoices.values('pk')[:1])).filter(trigger_id__isnull=False)


@retry_locked
@transaction.atomic
def plan_schedule(schedule_id, now=None):
    if not settings.SIMULATION:
        return 0
    current = timezone.now() if now is None else now
    today = timezone.localdate(current)
    tenant_id = IsolationSchedule.objects.values_list('tenant_id', flat=True).get(pk=schedule_id)
    tenant = Tenant.objects.select_for_update(no_key=True).get(pk=tenant_id)
    schedule = IsolationSchedule.objects.select_for_update().get(pk=schedule_id, tenant=tenant)
    if not schedule.enabled or (schedule.checked_at and schedule.checked_at > current - CHECK_INTERVAL):
        return 0
    schedule.checked_at = current
    schedule.save(update_fields=['checked_at'])
    if not consent_valid(schedule) or schedule.starts_on > today:
        return 0
    candidates = candidate_customers(schedule, today).order_by('pk')
    selected = list(candidates.values_list('pk', flat=True)[:JOBS_PER_ISP])
    created = 0
    for pk in selected:
        # Payment and normal reconciliation serialize through this row. Recheck after the lock.
        Customer.objects.select_for_update().get(tenant=tenant, pk=pk)
        trigger = candidates.filter(pk=pk).values_list('trigger_id', flat=True).first()
        if trigger is None:
            continue
        invoice = Invoice.objects.select_related('isolation_terms').get(pk=trigger, customer_id=pk, tenant=tenant)
        job = NetworkJob.objects.create(tenant=tenant, customer_id=pk, run_after=current)
        ScheduledIsolation.objects.create(schedule=schedule, schedule_version=schedule.version,
            invoice=invoice, job=job, eligible_on=isolation_date(invoice.due_date, invoice_grace_days(invoice)))
        audit(tenant, None, 'network.isolation_queued', job.pk, f'Faktur {invoice.number} memenuhi jadwal isolir simulasi.')
        created += 1
    return created


def plan_schedules():
    if not settings.SIMULATION:
        return 0
    current = timezone.now()
    due = IsolationSchedule.objects.filter(enabled=True).filter(
        Q(checked_at__isnull=True) | Q(checked_at__lte=current - CHECK_INTERVAL))
    ids = list(due.order_by(F('checked_at').asc(nulls_first=True), 'pk').values_list('pk', flat=True)[:4])
    return sum(plan_schedule(pk, current) for pk in ids)
