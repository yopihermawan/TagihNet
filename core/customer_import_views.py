import csv
import uuid
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.db.models import Count
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from .customer_import import COLUMNS, MAX_BYTES, cancel_import, confirm_import, manager, preview_import
from .models import CustomerImportBatch
from .permissions import require
from .security_views import recent_factor
from .views import paginate


class UploadForm(forms.Form):
    request_key = forms.UUIDField(widget=forms.HiddenInput)
    csv_file = forms.FileField(label="File pelanggan CSV UTF-8", help_text="Maksimal 128 KiB dan 200 baris data. Gunakan template di bawah.")

    def clean_csv_file(self):
        file = self.cleaned_data["csv_file"]
        if not file.name.lower().endswith(".csv") or not 0 < file.size <= MAX_BYTES:
            raise ValidationError("Pilih file .csv berukuran 1–131072 byte.")
        return file


class ConfirmationForm(forms.Form):
    reviewed = forms.BooleanField(label="Saya meninjau seluruh baris dan menyetujui penambahan pelanggan baru tanpa faktur, akun portal atau perintah router.")


def batches_for(request):
    member = manager(request.user)
    return CustomerImportBatch.objects.filter(tenant=member.tenant, created_by=request.user)


@require("import_customers")
@require_GET
def import_list(request):
    batches = batches_for(request).annotate(row_count=Count("rows")).order_by("-created_at", "-pk")
    return render(request, "imports/list.html", {"page": paginate(request, batches)})


@require("import_customers")
@require_http_methods(["GET", "POST"])
def import_upload(request):
    manager(request.user)
    form = UploadForm(request.POST if request.method == "POST" else None, request.FILES if request.method == "POST" else None,
        initial={"request_key": uuid.uuid4()})
    if request.method == "POST" and form.is_valid():
        try:
            batch = preview_import(request.user, form.cleaned_data["csv_file"].read(MAX_BYTES+1), form.cleaned_data["request_key"])
        except ValidationError as exc:
            form.add_error(None, exc)
        except OperationalError:
            form.add_error(None, "Database sibuk. Ulangi unggahan dengan formulir yang sama; pratinjau tidak digandakan.")
        else:
            return redirect("customer_import_detail", pk=batch.pk)
    return render(request, "imports/upload.html", {"form": form, "plans": request.member.tenant.plan_set.order_by("pk")})


def detail_response(request, batch, form=None, status=200):
    counts = dict(batch.rows.order_by().values_list("status").annotate(count=Count("pk")))
    summary = {"total": sum(counts.values()), "valid": counts.get("valid", 0), "invalid": counts.get("invalid", 0),
        "created": counts.get("created", 0)}
    return render(request, "imports/detail.html", {"batch": batch, "summary": summary,
        "page": paginate(request, batch.rows.select_related("customer")), "confirmation_form": form or ConfirmationForm(),
        "can_confirm": batch.status == "preview" and batch.expires_at >= timezone.now() and not summary["invalid"] and summary["valid"] > 0}, status=status)


@require("import_customers")
@require_GET
def import_detail(request, pk):
    return detail_response(request, get_object_or_404(batches_for(request), pk=pk))


@require("import_customers")
@require_POST
def import_confirm(request, pk):
    batch = get_object_or_404(batches_for(request), pk=pk)
    if not recent_factor(request, return_to=reverse("customer_import_detail", args=[pk])):
        return redirect("account_security")
    form = ConfirmationForm(request.POST)
    if form.is_valid():
        try:
            batch = confirm_import(request.user, pk, request.session["mfa_verified_at"], request.session["access_version"])
        except CustomerImportBatch.DoesNotExist:
            raise Http404 from None
        except ValidationError as exc:
            form.add_error(None, exc)
        except OperationalError:
            form.add_error(None, "Database sibuk. Muat ulang status sebelum mengirim ulang konfirmasi.")
        else:
            if batch.status == "completed":
                messages.success(request, batch.reason)
            else:
                messages.info(request, batch.reason)
            return redirect("customer_import_detail", pk=pk)
    return detail_response(request, batch, form, status=400)


@require("import_customers")
@require_POST
def import_cancel(request, pk):
    get_object_or_404(batches_for(request), pk=pk)
    try:
        cancel_import(request.user, pk)
    except CustomerImportBatch.DoesNotExist:
        raise Http404 from None
    except OperationalError:
        messages.error(request, "Database sibuk. Muat ulang status sebelum mencoba lagi.")
    return redirect("customer_import_detail", pk=pk)


def csv_response(filename):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")
    return response, csv.writer(response)


def csv_safe(value):
    value = str(value)
    return "'"+value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value


@require("import_customers")
@require_GET
def import_template(request):
    member = manager(request.user)
    response, writer = csv_response("tagihnet-template-pelanggan.csv")
    writer.writerow(COLUMNS)
    plan = member.tenant.plan_set.order_by("pk").first()
    writer.writerow(["Pelanggan Contoh", "contoh@example.invalid", "081299988877", "contoh-pelanggan",
        plan.pk if plan else "", "Wilayah Contoh", "Jl. Fiktif No. 1"])
    return response


@require("import_customers")
@require_GET
def import_report(request, pk):
    batch = get_object_or_404(batches_for(request), pk=pk)
    response, writer = csv_response("tagihnet-hasil-impor.csv")
    writer.writerow(["baris", "status", *COLUMNS, "paket_saat_pratinjau", "kesalahan"])
    for row in batch.rows.all():
        writer.writerow([csv_safe(value) for value in [row.row_number, row.get_status_display(),
            *[row.data.get(field, "") for field in COLUMNS.values()], row.plan_snapshot.get("name", ""), row.error]])
    return response
