import time
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.management import call_command
from django.utils import timezone
from core.models import NetworkJob
from core.isolation_schedule import plan_schedules
from core.network import run_job
from core.worker_heartbeat import heartbeat

class Command(BaseCommand):
    help = "Proses rekonsiliasi simulasi; tanpa koneksi atau efek samping eksternal."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        if not settings.SIMULATION:
            raise CommandError("Worker simulasi ditolak di luar SIMULATION=1.")
        try:
            while True:
                heartbeat('network','busy')
                failed=False
                queued = plan_schedules()
                if queued:
                    self.stdout.write(f"isolation_jobs_queued={queued}")
                jobs = list(NetworkJob.objects.filter(status__in=["pending", "failed"], attempts__lt=3,
                            run_after__lte=timezone.now()).order_by('run_after', 'pk').values_list("id", flat=True)[:100])
                for job_id in jobs:
                    heartbeat('network','busy')
                    job = run_job(job_id)
                    failed |= job.status=='failed'
                    self.stdout.write(f"job={job.pk} status={job.status} attempts={job.attempts}")
                try:
                    call_command("collect_monitoring", stdout=self.stdout, stderr=self.stderr)
                except CommandError as exc:
                    failed=True
                    heartbeat('network','degraded')
                    if options["once"]:
                        raise
                    self.stderr.write(str(exc))
                heartbeat('network','degraded' if failed else 'ok')
                if options["once"]:
                    return
                time.sleep(5)
        except KeyboardInterrupt:
            heartbeat('network','stopping')
            self.stdout.write("Worker dihentikan.")
