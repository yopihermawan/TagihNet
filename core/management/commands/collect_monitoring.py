from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError
from django.utils import timezone
from core.models import MonitoringDevice
from core.monitoring import collect_poll


class Command(BaseCommand):
    help = "Ambil satu putaran snapshot monitoring simulasi yang jatuh tempo; tanpa I/O jaringan."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if not settings.SIMULATION:
            raise CommandError("Monitoring nyata belum tersedia.")
        if not 1 <= options["limit"] <= 500:
            raise CommandError("--limit harus 1–500.")
        ids = list(MonitoringDevice.objects.filter(enabled=True, next_poll_at__lte=timezone.now())
            .order_by("next_poll_at", "pk").values_list("pk", flat=True)[:options["limit"]])
        failed = False
        for pk in ids:
            try:
                poll = collect_poll(pk)
            except OperationalError:
                failed = True
                self.stderr.write(f"nas={pk} database_busy")
            else:
                if poll:
                    self.stdout.write(f"nas={pk} poll={poll.pk} status={poll.status}")
                    failed |= poll.status != "success"
        if failed:
            raise CommandError("Sebagian snapshot gagal atau tertahan; periksa status monitoring.")
