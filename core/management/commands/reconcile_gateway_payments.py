from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError
from django.db.models import F
from core.gateway import mark_unavailable, refresh_order
from core.midtrans import GatewayUnavailable
from core.models import GatewayOrder


class Command(BaseCommand):
    help = "Verify unresolved Midtrans Sandbox orders without creating new checkouts."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--tenant", type=int, help="Batasi pemeriksaan ke ID ISP tertentu.")
        parser.add_argument("--include-settled", action="store_true", help="Periksa juga pembayaran tercatat untuk refund/chargeback yang notifikasinya terlewat.")

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 500:
            raise CommandError("limit harus 1–500")
        if options["tenant"] is not None and options["tenant"] < 1:
            raise CommandError("tenant harus ID ISP positif")
        states = ["creating", "pending", "review"] + (["settled","refunded"] if options["include_settled"] else [])
        orders = GatewayOrder.objects.filter(status__in=states)
        if options["tenant"] is not None:
            orders = orders.filter(tenant_id=options["tenant"])
        orders = orders.order_by(F("last_checked_at").asc(nulls_first=True), "created_at", "pk")[:options["limit"]]
        checked, failed = 0, 0
        for order in list(orders):
            try:
                refresh_order(order)
            except (ValidationError, GatewayUnavailable, IntegrityError, OperationalError):
                failed += 1
                # A failed merchant/order must not starve every later batch forever.
                mark_unavailable(order.pk)
            else:
                checked += 1
        self.stdout.write(f"Midtrans Sandbox: verified={checked}; unavailable={failed}. No new checkout created.")
