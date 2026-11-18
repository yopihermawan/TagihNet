"""New assignments use active plans; existing assignments may retain their archived plan."""
from django.core.exceptions import ValidationError
from django.db.models import Q
from .models import Plan


def selectable_plans(tenant, current_plan_id=None):
    return Plan.objects.filter(tenant=tenant).filter(Q(is_active=True) | Q(pk=current_plan_id))


def assignment_plan(tenant, plan_id, current_plan_id=None, *, lock=False):
    query = Plan.objects.select_for_update() if lock else Plan.objects
    plan = query.get(tenant=tenant, pk=plan_id)
    if not plan.is_active and plan.pk != current_plan_id:
        raise ValidationError("Paket sudah diarsipkan. Pilih paket aktif atau pertahankan paket lama pada data yang sudah ada.")
    return plan
