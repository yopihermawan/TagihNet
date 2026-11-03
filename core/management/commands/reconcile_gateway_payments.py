from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError
from django.utils import timezone
from core.gateway import refresh_order
from core.midtrans import GatewayUnavailable
from core.models import GatewayOrder


class Command(BaseCommand):
    help = "Verify unresolved Midtrans Sandbox orders without creating new checkouts."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 500:
            raise CommandError("limit harus 1–500")
        orders = GatewayOrder.objects.filter(status__in=["creating", "pending", "review"]).order_by("updated_at", "pk")[:options["limit"]]
        checked, failed = 0, 0
        for order in list(orders):
            try:
                refresh_order(order)
            except (ValidationError, GatewayUnavailable, IntegrityError, OperationalError):
                failed += 1
                # A failed merchant/order must not starve every later batch forever.
                GatewayOrder.objects.filter(pk=order.pk).update(updated_at=timezone.now())
            else:
                checked += 1
        self.stdout.write(f"Midtrans Sandbox: verified={checked}; unavailable={failed}. No new checkout created.")
