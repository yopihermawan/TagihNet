import csv
from datetime import date
from django import forms
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET
from .billing import audit
from .billing_reports import BillingReport, MAX_EXPORT_ROWS, MODES, WIB, report_snapshot, validate_range
from .permissions import require
from .views import paginate


class ReportDates(forms.Form):
    start = forms.DateField(label="Tanggal mulai", widget=forms.DateInput(attrs={"type": "date", "min": "2000-01-01"}))
    end = forms.DateField(label="Tanggal akhir", widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, today, **kwargs):
        super().__init__(*args, **kwargs)
        self.today = today
        for field in self.fields.values(): field.widget.attrs["max"] = today.isoformat()

    def clean(self):
        values = super().clean()
        if values.get("start") and values.get("end"):
            validate_range(values["start"], values["end"], self.today)
        return values


def filters(request):
    mode = request.GET.get("mode", "cash")
    if mode not in MODES:
        raise Http404
    today = timezone.localtime(timezone.now(), WIB).date()
    values = request.GET.copy()
    if "start" not in values and "end" not in values:
        values.update(start=today.replace(day=1).isoformat(), end=today.isoformat())
    return mode, ReportDates(values, today=today)


@require("billing_reports")
@require_GET
def billing_report(request):
    mode, form = filters(request)
    context = {"form": form, "mode": mode, "modes": MODES.items()}
    if form.is_valid():
        with report_snapshot():
            report = BillingReport(request.user, form.cleaned_data["start"], form.cleaned_data["end"])
            context.update(report=report, summary=report.summary(), page=paginate(request, report.rows(mode)), updated_at=timezone.now())
            return render(request, "reports/billing.html", context)
    return render(request, "reports/billing.html", context, status=400)


def safe_csv(value):
    if type(value) is int:  # Controlled signed rupiah remain numeric, including negative reversals.
        return value
    value = str(value)
    return "'"+value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value


@require("export_billing_reports")
@require_GET
def billing_report_export(request):
    mode, form = filters(request)
    if not form.is_valid():
        return HttpResponse("Tanggal laporan tidak valid. Perbaiki filter sebelum mengekspor.", status=400)
    with report_snapshot():
        report = BillingReport(request.user, form.cleaned_data["start"], form.cleaned_data["end"], exporting=True)
        rows = list(report.rows(mode)[:MAX_EXPORT_ROWS+1])
    if len(rows) > MAX_EXPORT_ROWS:
        return HttpResponse("Maksimal 10.000 baris per ekspor. Gunakan daftar berpaginasi atau persempit rentang transaksi.", status=400)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="tagihnet-{mode}-{report.start}-{report.end}.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    if mode == "cash":
        writer.writerow(["Waktu WIB", "Jenis", "ID peristiwa", "Faktur", "Pelanggan saat terbit", "Rupiah bertanda", "Catatan", "Pencatat"])
        values = ([row["entry_at"].astimezone(WIB).isoformat(), "Pembayaran" if row["entry_type"] == "payment" else "Reversal",
            ("P-" if row["entry_type"] == "payment" else "R-")+str(row["entry_id"]),
            f'TN-{row["invoice_period"]:%Y%m}-{row["invoice_pk"]:05d}', row["customer_label"], row["signed_amount"],
            row["entry_note"], row["actor_label"]] for row in rows)
    else:
        writer.writerow(["Faktur", "Terbit WIB", "Periode layanan", "Jatuh tempo", "Pelanggan saat terbit", "Total rupiah", "Dibayar sampai tanggal akhir", "Sisa sampai tanggal akhir"])
        values = ([row.number, row.issued_at.astimezone(WIB).isoformat(), row.period.isoformat(), row.due_date.isoformat(),
                   row.customer_name, row.total, row.paid_at_end, row.remaining] for row in rows)
    for row in values: writer.writerow([safe_csv(value) for value in row])
    audit(report.tenant, request.user, "report.exported", mode, f"{report.start}–{report.end}; {len(rows)} baris.", request.correlation_id)
    return response
