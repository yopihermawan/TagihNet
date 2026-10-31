from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods
from .models import Reminder, ReminderPolicy
from .permissions import require
from .reminders import manager, policy_authorized, save_policy
from .security_views import recent_factor
from .views import paginate


class ReminderPolicyForm(forms.ModelForm):
    version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    reviewed = forms.BooleanField(label="Saya memahami ini hanya simulasi dan kebijakan baru dapat melanjutkan antrean yang tertahan.")

    class Meta:
        model = ReminderPolicy
        fields = ["billing_enabled", "sla_enabled", "lead_days", "billing_hour", "scenario"]
        labels = {"billing_enabled": "Aktifkan pengingat billing simulasi", "sla_enabled": "Aktifkan pengingat SLA simulasi",
            "lead_days": "Mulai pengingat sebelum jatuh tempo (0–14 hari)", "billing_hour": "Jam mulai pengingat billing (0–23 WIB)",
            "scenario": "Skenario simulasi"}


@require("reminders")
@require_GET
def reminder_list(request):
    member = manager(request.user)
    query = Reminder.objects.filter(tenant=member.tenant).select_related("invoice", "ticket")
    if request.GET.get("kind") in dict(Reminder.KINDS):
        query = query.filter(kind=request.GET["kind"])
    if request.GET.get("status") in dict(Reminder.STATES):
        query = query.filter(status=request.GET["status"])
    policy = ReminderPolicy.objects.filter(tenant=member.tenant).first()
    return render(request, "reminders/list.html", {"page": paginate(request, query), "policy": policy,
        "policy_authorized": policy_authorized(policy) if policy else False, "states": Reminder.STATES, "kinds": Reminder.KINDS})


@require("reminders")
@require_GET
def reminder_detail(request, pk):
    member = manager(request.user)
    reminder = get_object_or_404(Reminder.objects.select_related("invoice", "ticket"), pk=pk, tenant=member.tenant)
    return render(request, "reminders/detail.html", {"reminder": reminder})


@require("reminders")
@require_http_methods(["GET", "POST"])
def reminder_settings(request):
    member = manager(request.user)
    policy = ReminderPolicy.objects.filter(tenant=member.tenant).first() or ReminderPolicy(tenant=member.tenant)
    form = ReminderPolicyForm(request.POST or None, instance=policy, initial={"version": policy.version})
    if request.method == "POST":
        if not recent_factor(request, return_to=reverse("reminder_settings")):
            return redirect("account_security")
        if form.is_valid():
            data = {field: form.cleaned_data[field] for field in ReminderPolicyForm.Meta.fields}
            try:
                save_policy(request.user, data, form.cleaned_data["version"], request.session["mfa_verified_at"], request.session["access_version"])
            except ValidationError as exc:
                form.add_error(None, exc)
            except OperationalError:
                form.add_error(None, "Database sedang sibuk. Muat ulang kebijakan sebelum mencoba lagi.")
            else:
                messages.success(request, "Kebijakan pengingat simulasi tersimpan. Tidak ada pesan nyata dikirim.")
                return redirect("reminder_list")
    return render(request, "core/form.html", {"form": form, "title": "Kebijakan pengingat simulasi", "back": reverse("reminder_list"),
        "description": "Nonaktif secara bawaan. Billing dibatasi satu pengingat per faktur/hari WIB; SLA satu per deadline dan siklus penanganan. Worker memeriksa ulang pelunasan, penyelesaian tiket, dan izin persetujuan."})
