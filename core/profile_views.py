from django import forms
from django.contrib import messages
from django.contrib.auth import HASH_SESSION_KEY
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.shortcuts import redirect, render
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from .account_profile import ProfileConflict, clean_name, review_token, save_profile
from .permissions import membership


class ProfileForm(forms.Form):
    first_name = forms.CharField(label="Nama depan atau panggilan", max_length=150, required=False, strip=False,
        widget=forms.TextInput(attrs={"autocomplete": "given-name"}))
    last_name = forms.CharField(label="Nama belakang", max_length=150, required=False, strip=False,
        widget=forms.TextInput(attrs={"autocomplete": "family-name"}))
    token = forms.CharField(max_length=2048, widget=forms.HiddenInput)

    def clean_first_name(self):return clean_name(self.cleaned_data["first_name"])
    def clean_last_name(self):return clean_name(self.cleaned_data["last_name"])


@login_required
@sensitive_variables()
@sensitive_post_parameters("token", "first_name", "last_name")
@require_http_methods(["GET", "POST"])
def account_profile(request):
    member = membership(request.user)
    initial = {"first_name": request.user.first_name, "last_name": request.user.last_name}
    if request.method == "GET":initial["token"] = review_token(request.user, member, request.session.session_key)
    form = ProfileForm(request.POST if request.method == "POST" else None, initial=initial)
    status = 200
    if request.method == "POST":
        status = 400
        if form.is_valid():
            try:
                changed = save_profile(request.user, form.cleaned_data["token"], form.cleaned_data["first_name"], form.cleaned_data["last_name"],
                    session_key=request.session.session_key, access_version=request.session.get("access_version"),
                    mfa_revision=request.session.get("mfa_revision"), auth_hash=request.session.get(HASH_SESSION_KEY),
                    correlation_id=request.correlation_id)
            except ProfileConflict as error:
                form.add_error(None, error); status = 409
            except ValidationError as error:
                form.add_error(None, error)
            except OperationalError:
                form.add_error(None, "Profil belum disimpan. Muat ulang profil dan coba lagi."); status = 503
            else:
                messages.success(request, "Nama tampilan disimpan." if changed else "Nama tampilan tidak berubah.")
                return redirect("account_profile")
    return render(request, "security/profile.html", {"form": form, "profile_member": member}, status=status)
