from django.conf import settings
from django.db import models


class IsolationSchedule(models.Model):
    tenant = models.OneToOneField('core.Tenant', on_delete=models.PROTECT, related_name='isolation_schedule')
    enabled = models.BooleanField(default=False)
    starts_on = models.DateField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    authorized_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    authorization_version = models.PositiveIntegerField(default=0)
    mfa_revision = models.UUIDField(null=True, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=(models.Q(enabled=False) | models.Q(
            starts_on__isnull=False, authorized_by__isnull=False, authorization_version__gt=0, mfa_revision__isnull=False,
        )), name='enabled_isolation_requires_approval')]


class ScheduledIsolation(models.Model):
    schedule = models.ForeignKey(IsolationSchedule, on_delete=models.PROTECT)
    schedule_version = models.PositiveIntegerField()
    invoice = models.ForeignKey('core.Invoice', on_delete=models.PROTECT)
    job = models.OneToOneField('core.NetworkJob', on_delete=models.PROTECT, related_name='scheduled_isolation')
    eligible_on = models.DateField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['invoice', 'schedule_version'], name='one_isolation_per_invoice_approval')]
