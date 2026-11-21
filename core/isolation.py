"""Grace affects simulated isolation eligibility. Overdue accounting uses the original due date."""
from datetime import timedelta

from django.core.exceptions import ValidationError

from .isolation_models import MAX_GRACE_DAYS

GRACE_FIELD = 'isolation_grace_days'


def validate_grace_days(value):
    if type(value) is not int or not 0 <= value <= MAX_GRACE_DAYS:
        raise ValidationError(f'Gunakan bilangan bulat 0–{MAX_GRACE_DAYS} untuk tenggang isolir.')
    return value


def configured_grace_days(tenant):
    policy = getattr(tenant, 'isolation_policy', None)
    return policy.grace_days if policy else 0


def invoice_grace_days(invoice):
    terms = getattr(invoice, 'isolation_terms', None)
    return terms.grace_days if terms else 0


def isolation_date(due_date, grace_days):
    return due_date + timedelta(days=grace_days + 1)
