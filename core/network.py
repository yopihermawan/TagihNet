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
from .models import Customer, Invoice, NetworkJob, ScheduledIsolation, Tenant

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


@retry_locked
@transaction.atomic
def run_job(job_id):
    scheduled_tenant = ScheduledIsolation.objects.filter(job_id=job_id).values_list('job__tenant_id', flat=True).first()
    if scheduled_tenant is not None:
        # The same tenant lock serializes approvals, revocation and new payment writes.
        Tenant.objects.select_for_update(no_key=True).get(pk=scheduled_tenant)
    job = NetworkJob.objects.select_for_update().get(pk=job_id)
    if job.status in {"confirmed", "skipped"} or job.attempts >= 3 or job.run_after > timezone.now():
        return job
    customer = Customer.objects.select_for_update().get(tenant=job.tenant, pk=job.customer_id)
    today = timezone.localdate()
    if scheduled_tenant is not None:
        dispatch = ScheduledIsolation.objects.select_related('schedule__authorized_by', 'invoice__isolation_terms').get(job=job)
        skip_reason = scheduled_skip_reason(dispatch, job, customer, today)
        if skip_reason:
            job.status, job.result = 'skipped', skip_reason
            job.save(update_fields=['status', 'result', 'updated_at'])
            audit(job.tenant, None, 'network.skipped', customer.pk, skip_reason)
            return job
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
        job.result = "Adapter gagal; pembayaran tetap tercatat. Retry maksimal 3 kali."
        job.run_after = timezone.now() + timedelta(seconds=30 * 2 ** (job.attempts - 1))
    job.save()
    audit(job.tenant, None, "network." + job.status, customer.pk, job.result)
    return job
