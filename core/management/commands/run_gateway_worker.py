import time
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError
from core.gateway_worker import run_cycle


class Command(BaseCommand):
    help = "Periksa status Midtrans Sandbox secara berkala sesuai kebijakan opt-in pemilik ISP."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 100:
            raise CommandError("--limit harus 1–100.")
        try:
            while True:
                try:
                    results = run_cycle(options["limit"])
                except (OperationalError, ValidationError):
                    if options["once"]:
                        raise CommandError("Worker belum dapat dijalankan. Periksa database dan mode simulasi.") from None
                    self.stderr.write("Worker belum dapat dijalankan; periksa database dan mode simulasi.")
                else:
                    for result in results:
                        if result.last_status == "blocked":
                            self.stdout.write(f"policy={result.pk} status=blocked Perlu persetujuan ulang owner dengan MFA.")
                        else:
                            self.stdout.write(f"policy={result.pk} status={result.last_status} verified={result.last_verified} unavailable={result.last_unavailable}")
                    if options["once"]:
                        if any(result.last_status == "blocked" for result in results):
                            raise CommandError("Ada kebijakan yang memerlukan persetujuan ulang owner dengan MFA. Lihat hasil per kebijakan; pembayaran yang sah tetap tersimpan.")
                        if any(result.last_status in {"failed", "partial"} for result in results):
                            raise CommandError("Sebagian status belum terverifikasi. Jadwal ulang tersimpan pada kebijakan.")
                        self.stdout.write(f"Gateway worker: {len(results)} kebijakan diproses.")
                        return
                time.sleep(5)
        except KeyboardInterrupt:
            self.stdout.write("Worker gateway dihentikan.")
