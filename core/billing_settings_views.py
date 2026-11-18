from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods, require_POST

from .billing import totals
from .billing_settings import (FIELDS, MAX_TOKEN, SettingsConflict, confirm_change, form_token,
    manager, read_token, review_change, values_for)
from .forms import BillingSettingsForm
from .models import Customer, Invoice, month_due
from .permissions import require
from .security_views import recent_factor


class ConfirmationForm(forms.Form):
    token = forms.CharField(max_length=MAX_TOKEN, widget=forms.HiddenInput)
    reviewed = forms.BooleanField(label="Saya menyetujui rincian dan dampak perubahan setelan billing serta identitas penerbit.")


@sensitive_variables()
def review_response(request, token, proposal, form=None, status=200):
    period = timezone.localdate().replace(day=1)
    examples = {}
    for label in ("before","after"):
        values = proposal[label]
        total, tax = totals(100000, 0, values["tax_bp"])
        examples[label] = {"due":month_due(period,values["due_day"]), "tax":tax, "total":total}
    tenant = request.member.tenant
    return render(request,"billing/settings_review.html",{"proposal":proposal,"period":period,"examples":examples,
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
    return render(request,"core/form.html",{"form":form,"title":"Setelan billing","back":"/",
        "submit_label":"Tinjau perubahan","description":"Prabayar bulanan penuh, tanpa prorata. Tinjau perubahan sebelum konfirmasi MFA. "
            "Pajak 0 bp berarti nol. Nama/alamat penerbit pada tampilan dan cetakan memakai identitas ISP terkini; "
            "nominal, jatuh tempo dan footer faktur lama mempertahankan snapshot."},status=status)


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
        if not recent_factor(request,return_to=reverse("billing_settings")):
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
            return redirect("billing_settings")
    return review_response(request,token,proposal,form,status=status)
