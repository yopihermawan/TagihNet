"""Metadata for authenticated webhook deliveries; the synchronous status check is unchanged."""
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Count, Max, Q, Sum
from django.utils import timezone
from .billing import retry_locked
from .gateway import refresh_order
from .midtrans import GatewayUnavailable
from .models import GatewayNotificationState, GatewayOrder


@retry_locked
@transaction.atomic
def begin_delivery(order_id):
    GatewayOrder.objects.select_for_update().get(pk=order_id)
    state, created = GatewayNotificationState.objects.get_or_create(order_id=order_id)
    if not created:
        state.delivery_count += 1
        state.last_received_at = timezone.now()
        state.last_outcome, state.last_completed_at = "received", None
        state.save()
    return state.delivery_count


@retry_locked
@transaction.atomic
def finish_delivery(order_id, sequence, outcome):
    if type(sequence) is not int or sequence < 1 or outcome not in {"verified", "unavailable"}:
        raise ValidationError("Hasil pengiriman tidak valid.")
    GatewayOrder.objects.select_for_update().get(pk=order_id)
    state = GatewayNotificationState.objects.select_for_update().get(order_id=order_id)
    if state.delivery_count == sequence and state.last_outcome == "received":
        state.last_outcome, state.last_completed_at = outcome, timezone.now()
        state.save(update_fields=["last_outcome", "last_completed_at"])
    # An older request finishing late, or repeated completion, cannot change the
    # result of the latest received delivery. Ledger application is independent.
    return state


def process_notification(order):
    # The public view must authenticate the signature before entering here.
    sequence = begin_delivery(order.pk)
    try:
        refresh_order(order)
    except (GatewayUnavailable, ValidationError, IntegrityError, OperationalError):
        finish_delivery(order.pk, sequence, "unavailable")
        raise
    finish_delivery(order.pk, sequence, "verified")


def notification_overview(tenant):
    return GatewayNotificationState.objects.filter(order__tenant=tenant, order__invoice__tenant=tenant).aggregate(
        orders=Count("order"), deliveries=Sum("delivery_count"), last_received_at=Max("last_received_at"),
        verified=Count("order", filter=Q(last_outcome="verified")),
        unavailable=Count("order", filter=Q(last_outcome="unavailable")),
        unfinished=Count("order", filter=Q(last_outcome="received")))
