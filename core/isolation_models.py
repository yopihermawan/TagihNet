"""Current ISP grace policy and the grace terms of an issued invoice."""
from django.core.validators import MaxValueValidator
from django.db import models

MAX_GRACE_DAYS = 30


class TenantIsolationPolicy(models.Model):
    tenant = models.OneToOneField('core.Tenant', on_delete=models.PROTECT, related_name='isolation_policy')
    grace_days = models.PositiveSmallIntegerField(default=0, validators=[MaxValueValidator(MAX_GRACE_DAYS)])

    class Meta:
        constraints = [models.CheckConstraint(
            condition=models.Q(grace_days__lte=MAX_GRACE_DAYS), name='tenant_isolation_grace_limit')]


class InvoiceIsolationTerms(models.Model):
    invoice = models.OneToOneField('core.Invoice', on_delete=models.PROTECT, related_name='isolation_terms')
    grace_days = models.PositiveSmallIntegerField(validators=[MaxValueValidator(MAX_GRACE_DAYS)])

    class Meta:
        constraints = [models.CheckConstraint(
            condition=models.Q(grace_days__gte=1, grace_days__lte=MAX_GRACE_DAYS), name='invoice_isolation_grace_limit')]
