import json
from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from core.gateway_readiness import readiness_report


class Command(BaseCommand):
    help = "Periksa konfigurasi lokal Midtrans Sandbox tanpa request jaringan atau perubahan data."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--tenant", type=int, required=True, help="ID ISP yang akan diperiksa.")
        parser.add_argument("--json", action="store_true", dest="as_json", help="Keluarkan laporan JSON tanpa nilai kredensial.")

    def handle(self, *args, **options):
        if type(options["tenant"]) is not int or options["tenant"] < 1:
            raise CommandError("--tenant harus ID ISP positif.")
        report = readiness_report(options["tenant"])
        if options["as_json"]:
            self.stdout.write(json.dumps(report, cls=DjangoJSONEncoder, ensure_ascii=False))
        else:
            self.stdout.write(f'ISP {report["tenant_id"]}: konfigurasi lokal '+("sesuai" if report["configuration_ready"] else "belum lengkap"))
            for check in report["checks"]:
                self.stdout.write(f'{check["label"]}: {check["state_label"]}. {check["message"]}')
                if check["action"]:
                    self.stdout.write(check["action"])
            self.stdout.write("Tidak ada request ke Midtrans atau pengujian akses HTTPS publik; tidak ada perubahan data.")
        if not report["configuration_ready"]:
            raise CommandError("Konfigurasi lokal belum lengkap; lihat hasil pemeriksaan. Tidak ada request ke Midtrans.")
