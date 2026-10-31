import time
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.management import call_command
from django.utils import timezone
from core.models import NetworkJob
from core.network import run_job

class Command(BaseCommand):
    help = "Proses rekonsiliasi simulasi; tanpa koneksi atau efek samping eksternal."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        if not settings.SIMULATION:
            raise CommandError("Worker simulasi ditolak di luar SIMULATION=1.")
        try:
            while True:
                jobs = list(NetworkJob.objects.filter(status__in=["pending", "failed"], attempts__lt=3,
                            run_after__lte=timezone.now()).values_list("id", flat=True)[:100])
                for job_id in jobs:
                    job = run_job(job_id)
                    self.stdout.write(f"job={job.pk} status={job.status} attempts={job.attempts}")
                try:
                    call_command("collect_monitoring", stdout=self.stdout, stderr=self.stderr)
                except CommandError as exc:
                    if options["once"]:
                        raise
                    self.stderr.write(str(exc))
                if options["once"]:
                    return
                time.sleep(5)
        except KeyboardInterrupt:
            self.stdout.write("Worker dihentikan.")
