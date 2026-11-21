"""Read-only audit searches scoped to the operator's ISP."""
from datetime import date, datetime, time

from django import forms
from django.db.models import Q
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_safe

from .models import AuditEvent
from .permissions import require
from .views import paginate


class AuditFilters(forms.Form):
    action = forms.RegexField(label="Aksi atau kelompok aksi", required=False, max_length=80,
        regex=r"\A[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\Z", widget=forms.TextInput(attrs={"placeholder": "network"}))
    actor = forms.CharField(label="Username pelaku", required=False, max_length=150)
    target = forms.CharField(label="Target", required=False, max_length=80)
    start = forms.DateField(label="Dari tanggal (WIB)", required=False, input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}))
    end = forms.DateField(label="Sampai tanggal (WIB)", required=False, input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}))
    correlation_id = forms.UUIDField(label="ID korelasi", required=False, widget=forms.TextInput(attrs={"spellcheck": "false"}))

    def clean(self):
        data = super().clean()
        start, end = data.get("start"), data.get("end")
        if start and end and end < start:
            self.add_error("end", "Tanggal akhir harus sama dengan atau setelah tanggal awal.")
        return data

    def apply(self, events):
        data = self.cleaned_data
        if data["action"]:
            events = events.filter(Q(action__iexact=data["action"]) | Q(action__istartswith=data["action"] + "."))
        if data["actor"]:
            events = events.filter(actor__username__iexact=data["actor"])
        if data["target"]:
            events = events.filter(target=data["target"])
        if data["correlation_id"]:
            events = events.filter(correlation_id=data["correlation_id"])
        wib = timezone.get_default_timezone()
        if data["start"] and data["start"] != date.min:
            events = events.filter(created_at__gte=datetime.combine(data["start"], time.min, tzinfo=wib))
        if data["end"] and data["end"] != date.max:
            events = events.filter(created_at__lte=datetime.combine(data["end"], time.max, tzinfo=wib))
        return events


@require("audit")
@require_safe
def audit_log(request):
    form = AuditFilters(request.GET)
    if not form.is_valid():
        return render(request, "core/audit.html", {"form": form}, status=400)
    events = AuditEvent.objects.filter(tenant_id=request.member.tenant_id).select_related("actor").order_by("-created_at", "-pk")
    return render(request, "core/audit.html", {"form": form, "page": paginate(request, form.apply(events))})
