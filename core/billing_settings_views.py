from datetime import date

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import OperationalError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods, require_POST

from .billing import totals
from .billing_settings import (FIELDS, MAX_TOKEN, SettingsConflict, confirm_change, form_token,
    manager, read_token, review_change, values_for)
from .billing_timeline import calendar_dates, rules_for
from .forms import BillingSettingsForm
from .isolation import GRACE_FIELD
from .models import Customer, Invoice
from .permissions import require
from .security_views import recent_factor


class ConfirmationForm(forms.Form):
    token = forms.CharField(max_length=MAX_TOKEN, widget=forms.HiddenInput)
    reviewed = forms.BooleanField(label="Saya menyetujui rincian dan dampak perubahan setelan billing serta identitas penerbit.")


class ExampleMonthForm(forms.Form):
    period = forms.DateField(label="Bulan contoh", input_formats=["%Y-%m"],
        validators=[MinValueValidator(date(2000, 1, 1)), MaxValueValidator(date(2099, 12, 1))],
        widget=forms.DateInput(format="%Y-%m", attrs={"type": "month", "min": "2000-01", "max": "2099-12"}))


def example_month(request):
    current = timezone.localdate().replace(day=1)
    data = request.GET if "period" in request.GET else None
    form = ExampleMonthForm(data, initial={"period": current})
    valid = form.is_valid()
    period = form.cleaned_data["period"] if valid else current
    month_query = f"?period={period:%Y-%m}" if valid else ""
    return period, form, month_query


@sensitive_variables()
def review_response(request, token, proposal, form=None, status=200):
    period, _, month_query = example_month(request)
    tenant = request.member.tenant
    timeline_rules = rules_for(tenant)
    examples = {}
    for label in ("before","after"):
        values = proposal[label]
        total, tax = totals(100000, 0, values["tax_bp"])
        examples[label] = {**calendar_dates(period, values["due_day"], timeline_rules, values[GRACE_FIELD]),
                           "tax":tax, "total":total}
    return render(request,"billing/settings_review.html",{"proposal":proposal,"period":period,"examples":examples,
        "timeline_rules":timeline_rules,"month_query":month_query,
        "impact":{"customers":Customer.objects.filter(tenant=tenant).count(),"invoices":Invoice.objects.filter(tenant=tenant).count()},
        "form":form if form is not None else ConfirmationForm(initial={"token":token})},status=status)


@require("settings")
@sensitive_variables()
@sensitive_post_parameters("token","name","address","invoice_footer")
@require_http_methods(["GET","POST"])
def billing_settings(request):
    member = manager(request.user)
    initial = values_for(member.tenant)
    if request.method == "GET":
        initial["token"] = form_token(request.user)
    form = BillingSettingsForm(request.POST if request.method == "POST" else None, initial=initial)
    status = 200
    if request.method == "POST":
        status = 400
        if form.is_valid():
            try:
                token, proposal = review_change(request.user,form.cleaned_data["token"],
                    {key:form.cleaned_data[key] for key in FIELDS})
            except SettingsConflict as error:
                form.add_error(None,error); status = 409
            except ValidationError as error:
                form.add_error(None,error)
            else:
                return review_response(request,token,proposal)
    period, month_form, _ = example_month(request)
    timeline_rules = rules_for(member.tenant)
    return render(request,"billing/settings.html",{"form":form,"title":"Setelan billing","back":"/",
        "submit_label":"Tinjau perubahan","description":"Atur identitas penerbit, jatuh tempo, tenggang isolir, pajak dan footer faktur. "
            "Pajak 0 bp berarti tarif nol. Tinjau perubahan sebelum konfirmasi MFA.",
        "period":period,"month_form":month_form,"timeline_rules":timeline_rules,
        "examples":{"before":calendar_dates(period, initial["due_day"], timeline_rules, initial[GRACE_FIELD])}},status=status)


@require("settings")
@sensitive_variables()
@sensitive_post_parameters("token")
@require_POST
def billing_settings_confirm(request):
    token = request.POST.get("token","")[:MAX_TOKEN+1]
    try:
        proposal = read_token(request.user,token,"review")
    except ValidationError:
        messages.error(request,"Tinjauan setelan tidak valid atau kedaluwarsa. Muat ulang dan tinjau lagi.")
        return redirect("billing_settings")
    form = ConfirmationForm(request.POST)
    status = 400
    if form.is_valid():
        _, _, month_query = example_month(request)
        return_to = reverse("billing_settings") + month_query
        if not recent_factor(request,return_to=return_to):
            return redirect("account_security")
        try:
            changed = confirm_change(request.user,form.cleaned_data["token"],request.session["mfa_verified_at"],
                request.session["access_version"],request.correlation_id)
        except SettingsConflict as error:
            form.add_error(None,error); status = 409
        except ValidationError as error:
            form.add_error(None,error)
        except OperationalError:
            form.add_error(None,"Setelan belum disimpan. Muat ulang dan tinjau kembali sebelum mencoba lagi."); status = 503
        else:
            messages.success(request,"Setelan billing disimpan. Snapshot tagihan dan batch yang sudah disetujui tetap." if changed else "Setelan billing tidak berubah.")
            return redirect(return_to)
    return review_response(request,token,proposal,form,status=status)
