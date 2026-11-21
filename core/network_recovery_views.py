from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_POST

from . import network_recovery as recovery
from .permissions import capabilities, require
from .security_views import recent_factor


class RetryForm(forms.Form):
    reason = forms.CharField(label='Alasan pemulihan', min_length=10, max_length=160,
                             widget=forms.TextInput(attrs={'maxlength': 160}))


class ConfirmationForm(forms.Form):
    token = forms.CharField(max_length=4096, widget=forms.HiddenInput)
    reviewed = forms.BooleanField(label='Saya telah meninjau job dan alasan pemulihan.')


def job_for(request, pk):
    jobs = recovery.scoped_jobs(request.member.tenant_id).select_related(
        'customer', 'scheduled_isolation', 'retry_request__trigger', 'retry_replacement__job')
    return get_object_or_404(jobs, pk=pk)


def detail_response(request, job, form=None, status=200):
    can_manage = recovery.RIGHTS <= capabilities(request.member)
    blocked = recovery.blocked_reason(job) if can_manage and job.status == 'failed' and job.attempts >= 3 else ''
    return render(request, 'network/job_detail.html', {'job': job, 'trigger': recovery.trigger_for(job),
        'can_retry': can_manage and not blocked and job.status == 'failed' and job.attempts >= 3,
        'blocked': blocked, 'form': form if form is not None else RetryForm()}, status=status)


@require('network')
@require_GET
def job_detail(request, pk):
    return detail_response(request, job_for(request, pk))


@require(recovery.CAPABILITY)
@sensitive_variables()
@sensitive_post_parameters('reason')
@require_POST
def retry_review(request, pk):
    job = job_for(request, pk)
    form = RetryForm(request.POST)
    if form.is_valid():
        try:
            token = recovery.review_retry(request.user, job.pk, form.cleaned_data['reason'])
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return render(request, 'network/job_retry_review.html', {'job': job, 'reason': form.cleaned_data['reason'],
                'form': ConfirmationForm(initial={'token': token})})
    return detail_response(request, job, form, status=400)


@require(recovery.CAPABILITY)
@sensitive_variables()
@sensitive_post_parameters('token')
@require_POST
def retry_confirm(request, pk):
    job = job_for(request, pk)
    form = ConfirmationForm(request.POST)
    try:
        payload = recovery.read_review(request.user, pk, request.POST.get('token', ''))
    except ValidationError as error:
        messages.error(request, error.messages[0])
        return redirect('network_job_detail', pk=pk)
    status = 400
    if form.is_valid():
        if not recent_factor(request, return_to=reverse('network_job_detail', args=[pk])):
            return redirect('account_security')
        try:
            replacement, created = recovery.confirm_retry(request.user, pk, form.cleaned_data['token'],
                request.session['mfa_verified_at'], request.session['access_version'], request.correlation_id)
        except ValidationError as error:
            form.add_error(None, error)
            status = 409
        except OperationalError:
            form.add_error(None, 'Job belum diantrekan. Coba konfirmasi kembali.')
            status = 503
        else:
            messages.success(request, 'Job pemulihan diantrekan.' if created else 'Job pemulihan sudah diantrekan sebelumnya.')
            return redirect('network_job_detail', pk=replacement.pk)
    return render(request, 'network/job_retry_review.html', {'job': job, 'reason': payload['reason'], 'form': form}, status=status)
