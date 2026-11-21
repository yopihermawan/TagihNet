"""Review changes to the router availability used by the simulation worker."""
import time

from django.conf import settings
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Subquery
from django.views.decorators.debug import sensitive_variables

from .billing import audit, retry_locked
from .models import AuditEvent, Customer, Membership, MFADevice, Tenant
from .network_recovery import clean_reason, scoped_jobs
from .operations import member_for
from .permissions import capabilities

CAPABILITY = 'manage_router_simulation'
RIGHTS = {'network', CAPABILITY}
SALT = 'tagihnet.router-simulation.v1'
ACTION = 'network.router_simulation_changed'
LABELS = {True: 'Dapat dijangkau', False: 'Tidak dapat dijangkau'}


def manager(actor):
    member = member_for(actor)
    if not settings.SIMULATION or not RIGHTS <= capabilities(member):
        raise PermissionDenied('Izin mengubah router simulasi diperlukan.')
    return member


def scenario_customer(tenant_id, job_id):
    customer_id = scoped_jobs(tenant_id).values_list('customer_id', flat=True).get(pk=job_id)
    # The audit ID also detects changes that returned the flag to its previous value.
    revision = AuditEvent.objects.filter(tenant_id=tenant_id, action=ACTION, target=str(customer_id))
    return Customer.objects.filter(tenant_id=tenant_id, pk=customer_id).annotate(
        scenario_revision=Subquery(revision.order_by('-pk').values('pk')[:1]))


@sensitive_variables()
def review_scenario(actor, job_id, available, reason):
    member = manager(actor)
    customer = scenario_customer(member.tenant_id, job_id).get()
    reason = clean_reason(reason)
    if type(available) is not bool or available == customer.router_available:
        raise ValidationError('Pilih kondisi router yang berbeda dari kondisi saat ini.')
    revision = MFADevice.objects.filter(user=actor).values_list('revision', flat=True).first()
    if revision is None:
        raise PermissionDenied('MFA operator diperlukan.')
    payload = {'actor': actor.pk, 'tenant': member.tenant_id, 'access': member.session_version,
               'mfa': str(revision), 'job': job_id, 'customer': customer.pk,
               'before': customer.router_available, 'revision': customer.scenario_revision,
               'after': available, 'reason': reason}
    return payload, signing.dumps(payload, salt=SALT)


@sensitive_variables()
def read_review(actor, job_id, token):
    member = manager(actor)
    try:
        if not isinstance(token, str) or len(token) > 4096:
            raise signing.BadSignature
        payload = signing.loads(token, salt=SALT, max_age=900)
    except signing.BadSignature:
        raise ValidationError('Tinjauan skenario tidak valid atau kedaluwarsa.') from None
    if (payload['actor'] != actor.pk or payload['tenant'] != member.tenant_id or payload['job'] != job_id
            or payload['access'] != member.session_version
            or not MFADevice.objects.filter(user=actor, revision=payload['mfa']).exists()):
        raise PermissionDenied('Tinjauan memerlukan job, akses, dan perangkat MFA yang sama.')
    return payload


@retry_locked
@transaction.atomic
@sensitive_variables()
def confirm_scenario(actor, job_id, token, verified_at, access_version, correlation_id=None):
    member = manager(actor)
    tenant = Tenant.objects.select_for_update(no_key=True).get(pk=member.tenant_id)
    Membership.objects.select_for_update().get(user=actor, tenant=tenant)
    member = manager(actor)
    if (member.tenant_id != tenant.pk or member.session_version != access_version
            or type(verified_at) not in (int, float) or not 0 <= time.time() - verified_at <= 300):
        raise PermissionDenied('Verifikasi ulang MFA sebelum mengubah skenario router.')
    payload = read_review(actor, job_id, token)
    customer = scenario_customer(tenant.pk, job_id).select_for_update(of=('self',)).get()
    if (customer.pk != payload['customer'] or customer.router_available != payload['before']
            or customer.scenario_revision != payload['revision']):
        raise ValidationError('Skenario router sudah berubah. Muat ulang dan tinjau kembali.')
    customer.router_available = payload['after']
    customer.save(update_fields=['router_available'])
    audit(tenant, actor, ACTION, customer.pk,
          f"{LABELS[payload['before']]} → {LABELS[payload['after']]}. {payload['reason']}", correlation_id)
