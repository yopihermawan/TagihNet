from datetime import date

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .billing_scheduler import FIELDS, WIB, confirm_change, manager, next_month, read_review, review_change, values_for
from .models import BillingSchedule, Plan, month_due
from .permissions import require
from .security_views import recent_factor
from .views import paginate


class ScheduleForm(forms.Form):
    enabled = forms.BooleanField(label="Aktifkan penerbitan otomatis bulanan (simulasi)", required=False)
    day = forms.IntegerField(label="Tanggal penerbitan bulanan (WIB)", min_value=1, max_value=31)
    state = forms.ChoiceField(label="Status layanan yang ditagih", choices=[("active", "Aktif"), ("isolated", "Diisolir"), ("all", "Semua layanan, termasuk pending")])
    plan = forms.ModelChoiceField(label="Paket", queryset=Plan.objects.none(), required=False, empty_label="Semua paket")
    area = forms.CharField(label="Wilayah persis (opsional)", max_length=80, required=False)
    version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def __init__(self, *args, tenant, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan"].queryset = Plan.objects.filter(tenant=tenant).order_by("name", "pk")


class ConfirmationForm(forms.Form):
    token = forms.CharField(max_length=8192, widget=forms.HiddenInput)
    reviewed = forms.BooleanField(label="Saya menyetujui jadwal otomatis, cakupan dinamis dan aturan pembatalan ini.")


def review_response(request, token, payload, form=None, status=200):
    period = date.fromisoformat(payload["period"])
    example_period = period
    dates = []
    for _ in range(3):
        dates.append({"issue": month_due(period, payload["after"]["day"]),
                      "due": month_due(period, request.member.tenant.due_day)})
        period = next_month(period)
    plan_name = (Plan.objects.filter(tenant=request.member.tenant, pk=payload["after"]["plan"])
                 .values_list("name", flat=True).first()) if payload["after"]["plan"] else "Semua paket"
    return render(request, "billing/schedule_review.html", {"proposal":payload, "dates":dates,
        "example_period":example_period, "state_label":dict(ScheduleForm.base_fields["state"].choices)[payload["after"]["state"]],
        "plan_name":plan_name or "Paket tidak tersedia",
        "form":form if form is not None else ConfirmationForm(initial={"token":token})}, status=status)


@require("manage_billing_schedule")
@require_http_methods(["GET", "POST"])
def schedule_form(request):
    member = manager(request.user)
    schedule = BillingSchedule.objects.filter(tenant=member.tenant).first()
    form = ScheduleForm(request.POST if request.method == "POST" else None, tenant=member.tenant,
        initial={**values_for(schedule), "version":schedule.version if schedule else 0})
    if request.method == "POST" and form.is_valid():
        values = {key:form.cleaned_data[key] for key in FIELDS}
        values["plan"] = values["plan"].pk if values["plan"] else None
        try:
            token, payload = review_change(request.user, values, form.cleaned_data["version"])
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            return review_response(request, token, payload)
    runs = schedule.runs.select_related("batch") if schedule else []
    return render(request, "billing/schedule.html", {"form":form, "schedule":schedule,
        "page":paginate(request, runs), "today":timezone.now().astimezone(WIB).date()})


@require("manage_billing_schedule")
@require_POST
def schedule_confirm(request):
    manager(request.user)
    form = ConfirmationForm(request.POST)
    token = request.POST.get("token", "")[:8193]
    try:
        payload = read_review(request.user, token)
    except ValidationError:
        messages.error(request, "Pratinjau tidak valid atau kedaluwarsa. Tinjau ulang jadwal.")
        return redirect("billing_schedule")
    if form.is_valid():
        if not recent_factor(request, return_to=reverse("billing_schedule")):
            return redirect("account_security")
        try:
            confirm_change(request.user, token, request.session["mfa_verified_at"], request.session["access_version"])
        except ValidationError as exc:
            form.add_error(None, exc)
        except (IntegrityError, OperationalError):
            form.add_error(None, "Jadwal belum tersimpan. Muat ulang dan tinjau kembali sebelum mencoba lagi.")
        else:
            messages.success(request, "Jadwal disimpan. Worker memeriksa jadwal setiap putaran; faktur lama tetap tersimpan.")
            return redirect("billing_schedule")
    return review_response(request, token, payload, form, status=400)
