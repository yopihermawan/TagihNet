import time
import pyotp
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods
from .access import create_member, save_member, save_role
from .billing import audit
from .models import AccessRole, MFADevice, Membership
from .permissions import membership, require
from .security import (complete_login, confirm_enrollment, decrypt_secret, encrypt_secret,
    pending_user, provisioning, regenerate_codes, remember_password, throttle, verify_factor)
from .security_forms import (EnrollmentForm, FactorForm, MemberForm, NewMemberForm, PasswordProofForm,
    ReauthenticationForm, RoleForm)
from .views import landing


def limited(request, form, template="security/factor.html", **context):
    response = render(request, template, {"form": form, "limited": True, **context}, status=429)
    response["Retry-After"] = "900"
    return response


@sensitive_post_parameters("code")
@require_http_methods(["GET", "POST"])
def challenge(request):
    if request.user.is_authenticated:
        return redirect(landing(membership(request.user)))
    user = pending_user(request)
    if not user:
        request.session.flush()
        return redirect("login")
    if not MFADevice.objects.filter(user=user).exists():
        return redirect("mfa_setup")
    form = FactorForm(request.POST or None)
    if request.method == "POST":
        if throttle(request, "mfa", user.pk):
            return limited(request, form)
        if form.is_valid():
            if verify_factor(user, form.cleaned_data["code"]):
                complete_login(request, user)
                return redirect(landing(membership(user)))
            form.add_error("code", "Kode tidak valid, telah dipakai, atau kedaluwarsa.")
            audit(user.membership.tenant, user, "auth.factor_failed", user.pk, correlation_id=request.correlation_id)
    return render(request, "security/factor.html", {"form": form})


@sensitive_variables()
@sensitive_post_parameters("code")
@require_http_methods(["GET", "POST"])
def setup(request):
    if request.user.is_authenticated:
        return redirect("account_security")
    user = pending_user(request)
    if not user:
        request.session.flush()
        return redirect("login")
    if MFADevice.objects.filter(user=user).exists():
        return redirect("mfa_challenge")
    setup_data = request.session.get("mfa_setup")
    if not setup_data:
        setup_data = {"encrypted": encrypt_secret(pyotp.random_base32()), "uid": user.pk}
        request.session["mfa_setup"] = setup_data
    if setup_data["uid"] != user.pk:
        request.session.flush()
        return redirect("login")
    secret = decrypt_secret(setup_data["encrypted"])
    context = {"secret": secret, "qr": provisioning(secret, user)}
    form = EnrollmentForm(request.POST or None)
    if request.method == "POST":
        if throttle(request, "mfa", user.pk):
            return limited(request, form, "security/setup.html", **context)
        if form.is_valid():
            try:
                codes = confirm_enrollment(user, setup_data["encrypted"], form.cleaned_data["code"], request.correlation_id)
            except ValidationError as exc:
                form.add_error("code", exc)
            else:
                complete_login(request, user, allow_version_change=True)
                return render(request, "security/recovery_codes.html", {"codes": codes})
    return render(request, "security/setup.html", {"form": form, **context})


@login_required
@sensitive_variables("codes")
@sensitive_post_parameters("password", "code")
@require_http_methods(["GET", "POST"])
def account_security(request):
    device = MFADevice.objects.filter(user=request.user).first()
    form = ReauthenticationForm(request.POST or None) if device else PasswordProofForm(request.POST or None)
    if request.method == "POST":
        if throttle(request, "stepup", request.user.pk):
            return limited(request, form, "security/account.html", device=device)
        if form.is_valid():
            if not request.user.check_password(form.cleaned_data["password"]):
                form.add_error(None, "Verifikasi tidak berhasil. Periksa kata sandi dan kode.")
            elif not device:
                remember_password(request, request.user)
                return redirect("mfa_setup")
            elif not verify_factor(request.user, form.cleaned_data["code"]):
                form.add_error(None, "Verifikasi tidak berhasil. Periksa kata sandi dan kode.")
            elif request.POST.get("action") == "regenerate":
                codes = regenerate_codes(request.user, request.correlation_id)
                complete_login(request, request.user)
                return render(request, "security/recovery_codes.html", {"codes": codes})
            else:
                request.session.cycle_key()
                request.session["mfa_verified_at"] = time.time()
                audit(request.user.membership.tenant, request.user, "auth.stepup", request.user.pk, correlation_id=request.correlation_id)
                messages.success(request, "Verifikasi ulang berhasil. Aksi sensitif tersedia selama 5 menit.")
                return redirect("access_index" if membership(request.user).role == "owner" else landing(membership(request.user)))
    return render(request, "security/account.html", {"form": form, "device": device,
                  "remaining": len(device.recovery_hashes) if device else 0})


def recent_factor(request):
    verified = request.session.get("mfa_verified_at", 0)
    if not 0 <= time.time() - verified <= 300:
        messages.info(request, "Verifikasi ulang diperlukan sebelum mengubah akses. Formulir belum disimpan.")
        return False
    return True


@require("manage_access")
def access_index(request):
    return render(request, "security/access.html", {
        "members": Membership.objects.filter(tenant=request.member.tenant).select_related("user", "access_role").order_by("user__username"),
        "roles": AccessRole.objects.filter(tenant=request.member.tenant).order_by("name"),
    })


@require("manage_access")
@require_http_methods(["GET", "POST"])
def role_form(request, pk=None):
    role = get_object_or_404(AccessRole, pk=pk, tenant=request.member.tenant) if pk else None
    form = RoleForm(request.POST or None, initial={"name": role.name, "capabilities": role.capabilities} if role else None)
    if request.method == "POST":
        if not recent_factor(request):
            return redirect("account_security")
        if form.is_valid():
            try:
                save_role(request.user, form.cleaned_data["name"], form.cleaned_data["capabilities"], pk, request.correlation_id)
            except ValidationError as exc:
                form.add_error(None, exc)
            except IntegrityError:
                form.add_error(None, "Nama peran sudah digunakan.")
            else:
                messages.success(request, "Peran disimpan. Sesi akun yang izinnya berubah dicabut.")
                return redirect("access_index")
    return render(request, "core/form.html", {"form": form, "title": "Ubah peran" if role else "Buat peran",
        "description": "Izin hanya berlaku pada ISP Anda. Kelola akses tidak dapat diberikan lewat peran kustom. Owner dan pelanggan memakai preset khusus.", "back": "/settings/access/"})


@require("manage_access")
@sensitive_post_parameters("password")
@require_http_methods(["GET", "POST"])
def member_form(request, pk=None):
    target = get_object_or_404(Membership.objects.select_related("user"), pk=pk, tenant=request.member.tenant) if pk else None
    initial = {"role": target.role, "access_role": target.access_role_id, "customer": target.customer_id, "active": target.user.is_active} if target else None
    form_class = MemberForm if target else NewMemberForm
    form = form_class(request.POST or None, initial=initial, tenant=request.member.tenant)
    if request.method == "POST":
        if not recent_factor(request):
            return redirect("account_security")
        if form.is_valid():
            data = form.cleaned_data
            try:
                if target:
                    save_member(request.user, target.pk, data["role"], data["access_role"].pk if data["access_role"] else None,
                        data["customer"].pk if data["customer"] else None, data["active"], request.correlation_id)
                else:
                    create_member(request.user, data["username"], data["email"], data["password"], data["role"],
                        data["access_role"].pk if data["access_role"] else None, data["customer"].pk if data["customer"] else None,
                        request.correlation_id)
            except ValidationError as exc:
                form.add_error(None, exc)
            except IntegrityError:
                form.add_error(None, "Username tidak tersedia. Pilih username lain.")
            else:
                messages.success(request, "Akses akun disimpan. Sesi lama akun tersebut dicabut.")
                return redirect("access_index")
    return render(request, "core/form.html", {"form": form, "title": f"Ubah akses · {target.user.username}" if target else "Tambah akun",
        "description": "Pemilik aktif terakhir dilindungi. Akun operator wajib mendaftarkan MFA saat login. Perubahan berlaku segera dan dicatat di audit.", "back": "/settings/access/"})
