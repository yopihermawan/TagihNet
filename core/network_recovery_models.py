from django.conf import settings
from django.db import models


class NetworkJobRetry(models.Model):
    failed_job = models.OneToOneField('core.NetworkJob', on_delete=models.PROTECT, related_name='retry_replacement')
    job = models.OneToOneField('core.NetworkJob', on_delete=models.PROTECT, related_name='retry_request')
    trigger = models.ForeignKey('core.ScheduledIsolation', null=True, blank=True, on_delete=models.PROTECT)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    request_key = models.UUIDField(unique=True, editable=False)
    access_version = models.PositiveIntegerField()
    mfa_revision = models.UUIDField()
    reason = models.CharField(max_length=160)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=~models.Q(failed_job=models.F('job')),
                                              name='network_retry_has_distinct_job')]
