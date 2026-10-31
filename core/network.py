"""A separate, deliberately non-networked adapter for simulation."""
from datetime import timedelta
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from .billing import audit, retry_locked
from .models import Customer, NetworkJob

class SimulationAdapter:
    def reconcile(self, customer, desired):
        if not settings.SIMULATION:
            raise ValidationError("Adapter produksi belum tersedia; tidak ada perintah perangkat dikirim.")
        if not customer.router_available:
            raise ConnectionError("Router simulasi tidak dapat dijangkau")
        return desired

@retry_locked
@transaction.atomic
def run_job(job_id):
    job = NetworkJob.objects.select_for_update().get(pk=job_id)
    if job.status == "confirmed" or job.attempts >= 3 or job.run_after > timezone.now():
        return job
    customer = Customer.objects.select_for_update().get(tenant=job.tenant, pk=job.customer_id)
    # Recheck debt at execution time, never reuse the state when the job was queued.
    overdue = any(invoice.balance > 0 for invoice in customer.invoice_set.filter(tenant=job.tenant, due_date__lt=timezone.localdate()))
    desired = "isolated" if overdue else "active"
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
