"""A separate, deliberately non-networked adapter for simulation."""
from datetime import timedelta
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from .billing import audit, retry_locked
from .invoice_queries import with_balances
from .isolation import invoice_grace_days, isolation_date
from .isolation_schedule import consent_valid
from .models import Customer, Invoice, NetworkJob, NetworkJobRetry, ScheduledIsolation, Tenant
from .network_recovery import retry_authorized

class SimulationAdapter:
    def reconcile(self, customer, desired):
        if not settings.SIMULATION:
            raise ValidationError("Adapter produksi belum tersedia; tidak ada perintah perangkat dikirim.")
        if not customer.router_available:
            raise ConnectionError("Router simulasi tidak dapat dijangkau")
        return desired


def scheduled_skip_reason(dispatch, job, customer, today):
    schedule = dispatch.schedule
    if (schedule.tenant_id != job.tenant_id or schedule.version != dispatch.schedule_version
            or not consent_valid(schedule)):
        return 'Persetujuan jadwal berubah atau dicabut.'
    invoice = dispatch.invoice
    if customer.service_state != 'active' or invoice.tenant_id != job.tenant_id or invoice.customer_id != job.customer_id:
        return 'Layanan atau faktur tidak lagi sesuai cakupan jadwal.'
    eligible_date = isolation_date(invoice.due_date, invoice_grace_days(invoice))
    unpaid = with_balances(Invoice.objects.filter(pk=invoice.pk), job.tenant_id).filter(remaining__gt=0)
    if not schedule.starts_on <= eligible_date <= today or not unpaid.exists():
        return 'Faktur pemicu sudah lunas atau tidak memenuhi tanggal isolir.'
    return ''


def skip_job(job, reason):
    job.status, job.result = 'skipped', reason
    job.save(update_fields=['status', 'result', 'updated_at'])
    audit(job.tenant, None, 'network.skipped', job.customer_id, reason)
    return job


@retry_locked
@transaction.atomic
def run_job(job_id):
    tenant_id, trigger_id, retry_id = NetworkJob.objects.values_list(
        'tenant_id', 'scheduled_isolation__pk', 'retry_request__pk').get(pk=job_id)
    if trigger_id is not None or retry_id is not None:
        # The same tenant lock serializes approvals, revocation and new payment writes.
        Tenant.objects.select_for_update(no_key=True).get(pk=tenant_id)
    job = NetworkJob.objects.select_for_update().get(pk=job_id)
    if job.status in {"confirmed", "skipped"} or job.attempts >= 3 or job.run_after > timezone.now():
        return job
    customer = Customer.objects.select_for_update().get(tenant=job.tenant, pk=job.customer_id)
    today = timezone.localdate()
    if retry_id is not None:
        retry = NetworkJobRetry.objects.select_related('requested_by').get(pk=retry_id, job=job)
        if not retry_authorized(retry, job):
            return skip_job(job, 'Persetujuan pemulihan berubah atau dicabut.')
        trigger_id = retry.trigger_id
    if trigger_id is not None:
        dispatch = ScheduledIsolation.objects.select_related('schedule__authorized_by', 'invoice__isolation_terms').get(pk=trigger_id)
        skip_reason = scheduled_skip_reason(dispatch, job, customer, today)
        if skip_reason:
            return skip_job(job, skip_reason)
        desired = 'isolated'
    else:
        # Payment, reversal and provisioning jobs reconcile all current debt.
        overdue = customer.invoice_set.filter(tenant=job.tenant, due_date__lt=today).select_related('isolation_terms')
        eligible_debt = any((today - invoice.due_date).days > invoice_grace_days(invoice) and invoice.balance > 0
                            for invoice in overdue)
        desired = 'isolated' if eligible_debt else 'active'
    job.attempts += 1
    try:
        customer.service_state = SimulationAdapter().reconcile(customer, desired)
        customer.save(update_fields=["service_state"])
        job.status = "confirmed"
        job.result = f"Simulasi terkonfirmasi: {customer.get_service_state_display()}. Tidak ada router nyata diubah."
    except (ConnectionError, ValidationError):
        job.status = "failed"
        job.result = "Router simulasi tidak dapat dijangkau." if not customer.router_available else "Adapter simulasi tidak tersedia."
        job.run_after = timezone.now() + timedelta(seconds=30 * 2 ** (job.attempts - 1))
    job.save()
    audit(job.tenant, None, "network." + job.status, customer.pk, job.result)
    return job
