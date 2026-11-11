"""Local configuration diagnostics. Never performs provider or public URL requests."""
from ipaddress import ip_address
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import DatabaseError, connection
from django.db.migrations.exceptions import InconsistentMigrationHistory
from django.db.migrations.executor import MigrationExecutor
from django.http.request import validate_host
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from config.public_url import public_origin
from .midtrans import credentials
from .models import GatewayConfig, GatewayOrder, Tenant

LABELS = {"ready": "Sesuai", "blocked": "Belum sesuai", "unknown": "Belum dapat diperiksa", "local": "Lokal saja"}


def schema_pending_count():
    executor = MigrationExecutor(connection)
    executor.loader.check_consistent_history(connection)
    return len(executor.migration_plan(executor.loader.graph.leaf_nodes()))


def item(code, label, state, message, action=""):
    return {"code": code, "label": label, "state": state, "state_label": LABELS[state], "message": message, "action": action}


def is_loopback(host):
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ip_address(host).is_loopback or ip_address(host).is_unspecified
    except ValueError:
        return False


@sensitive_variables()
def readiness_report(tenant_id):
    if type(tenant_id) is not int or tenant_id < 1:
        raise ValidationError("ID ISP harus bilangan positif.")
    report = {"tenant_id": tenant_id, "checked_at": timezone.localtime(timezone.now(), ZoneInfo("Asia/Jakarta")),
        "configuration_ready": False, "checkout_configured": None, "public_urls_configured": None,
        "remote_verification": "not_performed", "checks": []}
    checks = report["checks"]
    try:
        pending = schema_pending_count()
        if pending:
            checks.append(item("schema_pending", "Skema database", "blocked", "Ada migrasi aplikasi yang belum diterapkan.",
                "Cadangkan database berisi data, lalu jalankan manage.py migrate --noinput."))
            return report
        checks.append(item("schema", "Skema database", "ready", "Migrasi aplikasi sudah diterapkan."))
        if not Tenant.objects.filter(pk=tenant_id).exists():
            checks.append(item("tenant_unknown", "ISP", "blocked", "ID ISP tidak ditemukan.", "Pilih ID ISP yang sudah terdaftar."))
            return report
        checks.append(item("simulation", "Mode sandbox", "ready" if settings.SIMULATION else "blocked",
            "Mode simulasi aktif." if settings.SIMULATION else "Mode simulasi tidak aktif.",
            "" if settings.SIMULATION else "Aktifkan TAGIHNET_SIMULATION=1 untuk Midtrans Sandbox."))
        merchant = None
        if not settings.SIMULATION:
            checks.append(item("credentials", "Kredensial ISP", "unknown", "Kredensial sandbox tidak diperiksa saat mode simulasi tidak aktif."))
        else:
            try:
                merchant = credentials(tenant_id)[0]
            except ValidationError:
                checks.append(item("credentials", "Kredensial ISP", "blocked", "Kredensial sandbox belum tersedia atau belum valid untuk ISP ini.",
                    "Administrator perlu memeriksa konfigurasi privat TAGIHNET_MIDTRANS_SANDBOX_ACCOUNT_KEYS; jangan memasukkan nilainya ke chat."))
            else:
                checks.append(item("credentials", "Kredensial ISP", "ready", "Format kredensial sandbox dan keunikan merchant sesuai; akses akun belum diuji."))
        config = GatewayConfig.objects.filter(tenant_id=tenant_id).first()
        if merchant is None:
            checks.append(item("merchant_binding", "Kecocokan merchant", "unknown", "Kecocokan merchant belum dapat diperiksa sebelum kredensial valid."))
        else:
            saved_mismatch = bool(config and config.merchant_id and config.merchant_id != merchant)
            historical_mismatch = GatewayOrder.objects.filter(tenant_id=tenant_id).exclude(merchant_id=merchant).exists()
            mismatch = saved_mismatch or historical_mismatch
            checks.append(item("merchant_binding", "Kecocokan merchant", "blocked" if mismatch else "ready",
                "Merchant konfigurasi tidak sesuai dengan konfigurasi tersimpan atau riwayat order." if mismatch else "Merchant sesuai dengan konfigurasi tersimpan dan riwayat order ISP ini.",
                ("Pulihkan konfigurasi akun merchant yang terkait; jangan mengganti merchant untuk melewati order lama." if historical_mismatch
                    else "Tinjau dan simpan ulang setelan merchant dengan MFA sebelum checkout baru.") if mismatch else ""))
        enabled = bool(config and config.enabled)
        checks.append(item("checkout", "Checkout baru", "ready" if enabled else "blocked",
            "Checkout sandbox sudah diaktifkan owner." if enabled else "Checkout sandbox belum diaktifkan.",
            "" if enabled else "Setelah kredensial sesuai, owner dapat mengaktifkan checkout dengan MFA pada Koneksi merchant."))
        origin = None
        try:
            origin = public_origin(settings.PUBLIC_ORIGIN, debug=settings.DEBUG)
        except ImproperlyConfigured:
            checks.append(item("origin", "Alamat aplikasi HTTPS", "blocked", "Format alamat aplikasi tidak valid.",
                "Administrator perlu memeriksa TAGIHNET_PUBLIC_ORIGIN; gunakan origin HTTPS tanpa path, kredensial, query atau fragment."))
        else:
            if not origin:
                checks.append(item("origin", "Alamat aplikasi HTTPS", "blocked", "Alamat aplikasi belum dikonfigurasi.",
                    "Administrator perlu mengatur TAGIHNET_PUBLIC_ORIGIN ke origin HTTPS aplikasi yang akan diuji."))
            else:
                parsed = urlsplit(origin)
                local = parsed.scheme != "https" or is_loopback(parsed.hostname)
                checks.append(item("origin", "Alamat aplikasi HTTPS", "local" if local else "ready",
                    "Alamat ini hanya untuk pengujian lokal." if local else "Origin HTTPS sudah dikonfigurasi; DNS dan akses publik belum diuji.",
                    "Gunakan origin HTTPS yang dapat dijangkau untuk pengujian notifikasi Midtrans." if local else ""))
        if origin:
            host = urlsplit(origin).hostname.lower().rstrip(".")
            host = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
            allowed = validate_host(host, settings.ALLOWED_HOSTS)
            checks.append(item("allowed_host", "Host aplikasi", "ready" if allowed else "blocked",
                "Hostname origin diterima oleh aplikasi." if allowed else "Hostname origin belum diizinkan oleh aplikasi.",
                "" if allowed else "Tambahkan hostname origin ke TAGIHNET_ALLOWED_HOSTS, lalu restart proses aplikasi."))
        else:
            checks.append(item("allowed_host", "Host aplikasi", "unknown", "Host belum dapat diperiksa sebelum origin valid tersedia."))
        states = {check["code"]: check["state"] for check in checks}
        report["checkout_configured"] = all(states[code] == "ready" for code in ["schema", "simulation", "credentials", "merchant_binding", "checkout"])
        report["public_urls_configured"] = all(states[code] == "ready" for code in ["origin", "allowed_host"])
        report["configuration_ready"] = report["checkout_configured"] and report["public_urls_configured"]
    except InconsistentMigrationHistory:
        checks[:] = [item("schema_inconsistent", "Skema database", "blocked", "Riwayat migrasi database perlu diperiksa administrator.")]
    except DatabaseError:
        checks[:] = [item("database_unavailable", "Database", "unknown", "Konfigurasi database belum dapat diperiksa.",
            "Periksa koneksi database melalui konfigurasi privat; detail koneksi tidak ditampilkan di sini.")]
    return report
