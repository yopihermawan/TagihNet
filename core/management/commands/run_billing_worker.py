import time
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError
from core.bulk_billing import process_batch
from core.models import BillingBatch


class Command(BaseCommand):
    help = "Proses hanya batch faktur yang telah dikonfirmasi. Aman untuk retry/restart; tanpa efek eksternal."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--limit", type=int, default=100, help="Maksimal 1–500 baris per batch per putaran.")

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 500:
            raise CommandError("--limit harus 1–500.")
        try:
            while True:
                ids = list(BillingBatch.objects.filter(status__in=["queued", "processing"])
                           .order_by("created_at").values_list("pk", flat=True)[:10])
                failed = False
                for batch_id in ids:
                    try:
                        count = process_batch(batch_id, limit=options["limit"])
                        batch = BillingBatch.objects.get(pk=batch_id)
                        self.stdout.write(f"batch={batch_id} status={batch.status} processed={count}")
                    except OperationalError:
                        failed = True
                        self.stderr.write(f"batch={batch_id} database_busy; progress retained, retry later")
                if options["once"]:
                    if failed:
                        raise CommandError("Sebagian batch belum selesai karena database sibuk. Progres tersimpan; ulangi nanti.")
                    return
                time.sleep(5)
        except KeyboardInterrupt:
            self.stdout.write("Worker billing dihentikan.")
