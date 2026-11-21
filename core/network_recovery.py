"""Review and enqueue replacement jobs without rewriting failed attempts."""
import time
import uuid

from django.conf import settings
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .billing import audit, retry_locked
from .models import Customer, Membership, MFADevice, NetworkJob, NetworkJobRetry, Tenant
from .operations import member_for
from .permissions import capabilities

CAPABILITY = 'manage_network_retries'
RIGHTS = {CAPABILITY, 'network'}
SALT = 'tagihnet.network-job-retry.v1'


def manager(actor):
    member = member_for(actor)
    if not RIGHTS <= capabilities(member):
        raise PermissionDenied('Izin mengulang job jaringan diperlukan.')
    return member


def scoped_jobs(tenant_id):
    return NetworkJob.objects.filter(tenant_id=tenant_id, customer__tenant_id=tenant_id)


def attention_jobs(tenant_id):
    jobs = scoped_jobs(tenant_id)
    newer = jobs.filter(customer_id=OuterRef('customer_id'), pk__gt=OuterRef('pk'))
    return jobs.filter(Q(status='pending') | Q(status='failed', attempts__lt=3)
                       | (Q(status='failed', attempts__gte=3) & ~Exists(newer)))


def trigger_for(job):
    retry = getattr(job, 'retry_request', None)
    return retry.trigger if retry else getattr(job, 'scheduled_isolation', None)


def blocked_reason(job, customer=None):
    if not settings.SIMULATION:
        return 'Pemulihan job tersedia dalam mode simulasi.'
    if job.status != 'failed' or job.attempts < 3:
        return 'Pemulihan tersedia untuk job gagal yang telah mencapai tiga percobaan.'
    if scoped_jobs(job.tenant_id).filter(customer_id=job.customer_id, pk__gt=job.pk).exists():
        return 'Pelanggan sudah memiliki job yang lebih baru.'
    trigger = trigger_for(job)
    if trigger is not None:
        from .network import scheduled_skip_reason
        return scheduled_skip_reason(trigger, job, customer or job.customer, timezone.localdate())
    return ''


def clean_reason(reason):
    if not isinstance(reason, str):
        raise ValidationError('Isi alasan yang valid.')
    reason = reason.strip()
    if not 10 <= len(reason) <= 160 or not reason.isprintable():
        raise ValidationError('Alasan harus 10–160 karakter teks satu baris.')
    return reason


def snapshot(job):
    return [job.status, job.attempts, job.updated_at.isoformat()]


@sensitive_variables()
def review_retry(actor, job_id, reason):
    member = manager(actor)
    job = scoped_jobs(member.tenant_id).select_related('customer').get(pk=job_id)
    reason = clean_reason(reason)
    error = blocked_reason(job)
    if error:
        raise ValidationError(error)
    device = MFADevice.objects.filter(user=actor).first()
    if device is None:
        raise PermissionDenied('MFA operator diperlukan.')
    payload = {'actor': actor.pk, 'tenant': member.tenant_id, 'access': member.session_version,
               'mfa': str(device.revision), 'job': job.pk, 'state': snapshot(job), 'reason': reason,
               'key': str(uuid.uuid4())}
    return signing.dumps(payload, salt=SALT)


@sensitive_variables()
def read_review(actor, job_id, token):
    member = manager(actor)
    try:
        if not isinstance(token, str) or len(token) > 4096:
            raise ValueError
        payload = signing.loads(token, salt=SALT, max_age=900)
        if (not isinstance(payload, dict) or set(payload) != {'actor', 'tenant', 'access', 'mfa', 'job', 'state', 'reason', 'key'}
                or payload['reason'] != clean_reason(payload['reason']) or str(uuid.UUID(payload['key'])) != payload['key']):
            raise ValueError
    except (signing.BadSignature, ValueError, TypeError, KeyError, AttributeError, ValidationError):
        raise ValidationError('Tinjauan pemulihan tidak valid atau kedaluwarsa.') from None
    device = MFADevice.objects.filter(user=actor).first()
    if (payload['actor'] != actor.pk or payload['tenant'] != member.tenant_id or payload['job'] != job_id
            or payload['access'] != member.session_version or not device or payload['mfa'] != str(device.revision)):
        raise PermissionDenied('Tinjauan memerlukan job, akses, dan perangkat MFA yang sama.')
    return payload


def retry_authorized(retry, job):
    if not settings.SIMULATION:
        return False
    try:
        member = manager(retry.requested_by)
    except (Membership.DoesNotExist, PermissionDenied):
        return False
    return (member.tenant_id == job.tenant_id and member.session_version == retry.access_version
            and MFADevice.objects.filter(user_id=member.user_id, revision=retry.mfa_revision).exists())


@retry_locked
@transaction.atomic
@sensitive_variables()
def confirm_retry(actor, job_id, token, verified_at, access_version, correlation_id=None):
    member = manager(actor)
    tenant = Tenant.objects.select_for_update(no_key=True).get(pk=member.tenant_id)
    Membership.objects.select_for_update().get(user=actor, tenant=tenant)
    member = manager(actor)
    if member.session_version != access_version or type(verified_at) not in (int, float) or not 0 <= time.time() - verified_at <= 300:
        raise PermissionDenied('Verifikasi ulang MFA sebelum mengulang job.')
    payload = read_review(actor, job_id, token)
    original = scoped_jobs(tenant.pk).select_for_update(of=('self',)).get(pk=job_id)
    existing = NetworkJobRetry.objects.filter(failed_job=original).select_related('job').first()
    if existing and str(existing.request_key) == payload['key'] and existing.requested_by_id == actor.pk:
        return existing.job, False
    if snapshot(original) != payload['state']:
        raise ValidationError('Job sudah berubah. Muat ulang dan tinjau kembali.')
    customer = Customer.objects.select_for_update().get(tenant=tenant, pk=original.customer_id)
    error = blocked_reason(original, customer)
    if error:
        raise ValidationError(error)
    replacement = NetworkJob.objects.create(tenant=tenant, customer=customer)
    NetworkJobRetry.objects.create(failed_job=original, job=replacement, trigger=trigger_for(original),
        requested_by=actor, request_key=payload['key'], reason=payload['reason'], access_version=member.session_version,
        mfa_revision=payload['mfa'])
    audit(tenant, actor, 'network.retry_queued', original.pk, f'Job pemulihan #{replacement.pk} diantrekan.', correlation_id)
    return replacement, True
