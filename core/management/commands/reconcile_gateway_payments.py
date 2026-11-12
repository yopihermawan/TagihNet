import json
from zoneinfo import ZoneInfo
from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, OperationalError
from django.db.models import F
from django.utils import timezone
from core.gateway import refresh_order
from core.midtrans import GatewayUnavailable
from core.models import GatewayOrder, Tenant


class Command(BaseCommand):
    help = "Verify existing Midtrans Sandbox orders; --dry-run only previews the local selection."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--tenant", type=int, help="Batasi pemeriksaan ke ID ISP tertentu.")
        parser.add_argument("--include-settled", action="store_true", help="Periksa juga pembayaran tercatat untuk refund/chargeback yang notifikasinya terlewat.")
        parser.add_argument("--dry-run", action="store_true", help="Pratinjau order tersimpan tanpa request Midtrans atau perubahan data.")
        parser.add_argument("--json", action="store_true", dest="as_json", help="Laporan JSON tanpa kredensial atau data pelanggan.")

    def handle(self, *args, **options):
        if type(options["limit"]) is not int or not 1 <= options["limit"] <= 500:
            raise CommandError("limit harus 1–500")
        if options["tenant"] is not None and (type(options["tenant"]) is not int or options["tenant"] < 1):
            raise CommandError("tenant harus ID ISP positif")
        report = {"schema_version": 1, "dry_run": options["dry_run"], "tenant_id": options["tenant"],
            "include_settled": options["include_settled"], "limit": options["limit"], "selected": None,
            "attempted": 0, "verified": 0, "unavailable": 0, "outcome": "database_unavailable", "results": []}
        states = ["creating", "pending", "review"] + (["settled","refunded"] if options["include_settled"] else [])
        orders = []
        try:
            if options["tenant"] is not None and not Tenant.objects.filter(pk=options["tenant"]).exists():
                report["outcome"] = "unknown_tenant"
            else:
                query = GatewayOrder.objects.filter(status__in=states)
                if options["tenant"] is not None:
                    query = query.filter(tenant_id=options["tenant"])
                # Freeze this bounded batch before any provider request or ledger write.
                orders = list(query.order_by(F("last_checked_at").asc(nulls_first=True), "created_at", "pk")[:options["limit"]])
                report["selected"] = len(orders)
                report["outcome"] = "planned" if options["dry_run"] else "empty" if not orders else "completed"
        except DatabaseError:
            # Selection failure is unknown work, never a successful empty batch.
            pass
        for order in orders:
            result = {"order_id": order.order_id, "tenant_id": order.tenant_id, "outcome": "planned", "local_status": order.status}
            if not options["dry_run"]:
                report["attempted"] += 1
                try:
                    verified = refresh_order(order)
                except (ValidationError, GatewayUnavailable, IntegrityError, OperationalError):
                    # refresh_order records the attempt (when the DB permits it),
                    # rotates failed work and never updates successful proof time.
                    report["unavailable"] += 1
                    result.update(outcome="unavailable", local_status=None)
                else:
                    report["verified"] += 1
                    result.update(outcome="verified", local_status=verified.status)
            report["results"].append(result)
        if report["unavailable"]:
            report["outcome"] = "partial" if report["verified"] else "failed"
        report["generated_at"] = timezone.localtime(timezone.now(), ZoneInfo("Asia/Jakarta"))
        if options["as_json"]:
            self.stdout.write(json.dumps(report, cls=DjangoJSONEncoder, ensure_ascii=False))
        elif options["dry_run"]:
            self.stdout.write(f'Midtrans Sandbox: dry-run; outcome={report["outcome"]}; selected={report["selected"]}. Tidak ada request Midtrans atau perubahan data.')
        else:
            self.stdout.write(f'Midtrans Sandbox: verified={report["verified"]}; unavailable={report["unavailable"]}; outcome={report["outcome"]}; selected={report["selected"]}. No new checkout created.')
        if report["outcome"] in {"failed", "partial", "unknown_tenant", "database_unavailable"}:
            raise CommandError("Rekonsiliasi belum lengkap; lihat laporan. Checkout baru tidak dibuat.")
