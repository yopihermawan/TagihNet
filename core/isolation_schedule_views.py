from datetime import date

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from . import isolation_schedule
from .models import IsolationSchedule
from .permissions import require
from .security_views import recent_factor


class ScheduleForm(forms.Form):
    enabled = forms.BooleanField(label='Aktifkan jadwal isolir simulasi', required=False)
    version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)


class ConfirmationForm(forms.Form):
    token = forms.CharField(max_length=4096, widget=forms.HiddenInput)
    reviewed = forms.BooleanField(label='Saya telah meninjau cakupan dan dampak jadwal ini.')


def render_review(request, payload, form, status=200):
    starts_on = date.fromisoformat(payload['starts_on']) if payload['enabled'] else None
    return render(request, 'network/isolation_schedule_review.html',
                  {'enabled': payload['enabled'], 'starts_on': starts_on, 'form': form}, status=status)


@require(isolation_schedule.CAPABILITY)
@require_http_methods(['GET', 'POST'])
def schedule_settings(request):
    member = isolation_schedule.manager(request.user)
    schedule = IsolationSchedule.objects.filter(tenant_id=member.tenant_id).select_related('authorized_by').first()
    form = ScheduleForm(request.POST if request.method == 'POST' else None,
                       initial={'enabled': bool(schedule and schedule.enabled), 'version': schedule.version if schedule else 0})
    if request.method == 'POST' and form.is_valid():
        try:
            token, payload = isolation_schedule.review_change(request.user, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return render_review(request, payload, ConfirmationForm(initial={'token': token}))
    approved = bool(schedule and isolation_schedule.consent_valid(schedule))
    return render(request, 'network/isolation_schedule.html', {'schedule': schedule, 'approved': approved, 'form': form},
                  status=400 if request.method == 'POST' else 200)


@require(isolation_schedule.CAPABILITY)
@sensitive_post_parameters('token')
@require_POST
def schedule_confirm(request):
    form = ConfirmationForm(request.POST)
    try:
        payload = isolation_schedule.read_review(request.user, request.POST.get('token', ''))
    except ValidationError as error:
        messages.error(request, error.messages[0])
        return redirect('isolation_schedule')
    if not form.is_valid():
        return render_review(request, payload, form, status=400)
    if not recent_factor(request, return_to=reverse('isolation_schedule')):
        return redirect('account_security')
    try:
        changed = isolation_schedule.confirm_change(request.user, form.cleaned_data['token'],
            request.session['mfa_verified_at'], request.session['access_version'], request.correlation_id)
    except ValidationError as error:
        form.add_error(None, error)
        return render_review(request, payload, form, status=409)
    except OperationalError:
        form.add_error(None, 'Jadwal belum tersimpan. Muat ulang dan tinjau kembali.')
        return render_review(request, payload, form, status=503)
    messages.success(request, 'Persetujuan jadwal tersimpan.' if changed else 'Jadwal sudah nonaktif.')
    return redirect('isolation_schedule')
