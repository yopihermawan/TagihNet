import csv
import re
from datetime import timedelta
from urllib.parse import urlencode
from django import forms
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET
from .billing import audit
from .gateway_reports import WIB, ledger_report, validate_period
from .permissions import require
from .views import paginate


class PeriodForm(forms.Form):
    start = forms.DateField(label="Tanggal awal (WIB)", input_formats=["%Y-%m-%d"], widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}))
    end = forms.DateField(label="Tanggal akhir (WIB)", input_formats=["%Y-%m-%d"], widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}))

    def clean(self):
        cleaned = super().clean()
        if "start" in cleaned and "end" in cleaned:
            validate_period(cleaned["start"], cleaned["end"])
        return cleaned


def csv_cell(value):
    if isinstance(value, int):
        return value
    value = re.sub(r"[\x00-\x1f\x7f]", " ", "" if value is None else str(value))
    return "'"+value if re.match(r"^[\s\ufeff]*[=+\-@]", value) else value


def timestamp(value):
    return timezone.localtime(value, WIB).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def report_response(request, *, export=False):
    today = timezone.localdate(timezone=WIB)
    form = PeriodForm({"start": request.GET.get("start", (today-timedelta(days=6)).isoformat()), "end": request.GET.get("end", today.isoformat())})
    report = None
    if form.is_valid():
        try:
            report = ledger_report(request.user, form.cleaned_data["start"], form.cleaned_data["end"])
        except ValidationError as exc:
            form.add_error(None, exc)
    if report is None:
        return render(request, "gateway/report.html", {"form": form}, status=400)
    if not export:
        query = urlencode({"start": report["start"].isoformat(), "end": report["end"].isoformat()})
        return render(request, "gateway/report.html", {"form": form, "report": report,
            "page": paginate(request, report["rows"]), "export_url": reverse("gateway_report_export")+"?"+query})
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="tagihnet-ledger-gateway-{report["start"]}_{report["end"]}.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(["referensi_ledger", "waktu_ledger_wib", "jenis", "faktur", "pelanggan", "order_gateway", "transaction_id",
        "pembayaran_rupiah", "refund_penuh_rupiah", "koreksi_lain_rupiah", "perubahan_bersih_rupiah", "status_order_saat_laporan",
        "verifikasi_status_terakhir_wib", "percobaan_status_terakhir_wib", "periode_awal_wib", "periode_akhir_wib", "dibuat_laporan_wib", "sumber"])
    for row in report["rows"]:
        writer.writerow([csv_cell(value) for value in [row["reference"], timestamp(row["occurred_at"]), row["kind_label"],
            row["invoice_number"], row["invoice__customer_name"], row["order_reference"], row["transaction_id"],
            row["ledger_amount"] if row["entry_kind"] == "payment" else 0,
            row["ledger_amount"] if row["entry_kind"] == "refund" else 0,
            row["ledger_amount"] if row["entry_kind"] == "reversal" else 0, row["signed_amount"], row["status_label"],
            timestamp(row["last_verified_at"]), timestamp(row["last_checked_at"]), report["start"], report["end"],
            timestamp(report["generated_at"]), "Midtrans Sandbox · ledger TagihNet"]])
    audit(request.member.tenant, request.user, "gateway.report_exported", "csv",
        f'{report["start"]}–{report["end"]} WIB; {len(report["rows"])} entri', request.correlation_id)
    return response


@require("gateway_reports")
@require_GET
def report(request):
    return report_response(request)


@require("gateway_reports")
@require_GET
def report_export(request):
    return report_response(request, export=True)
