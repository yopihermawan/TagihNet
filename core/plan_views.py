from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from .models import Plan
from .permissions import require
from .plans import FIELDS, confirm_change, impact, manager, read_review, review_change, values_for
from .plan_availability import availability_impact, confirm_availability, read_availability, review_availability
from .security_views import recent_factor
from .views import paginate
from django.views.decorators.debug import sensitive_post_parameters


class PlanForm(forms.Form):
    name = forms.CharField(label="Nama paket", max_length=80)
    speed_mbps = forms.IntegerField(label="Kecepatan (Mbps)", min_value=1, max_value=100000)
    price = forms.IntegerField(label="Harga bulanan sebelum pajak (rupiah)", min_value=1, max_value=100000000)
    version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class ConfirmationForm(forms.Form):
    token = forms.CharField(max_length=8192, widget=forms.HiddenInput)
    reviewed = forms.BooleanField(label="Saya menyetujui rincian paket dan dampaknya pada tagihan berikutnya.")


class AvailabilityConfirmationForm(forms.Form):
    token = forms.CharField(max_length=4096, widget=forms.HiddenInput)
    reviewed = forms.BooleanField(label="Saya menyetujui perubahan ketersediaan katalog ini. Pelanggan, PSB dan tagihan yang sudah ada tetap dilayani.")


@require("customers")
@require_GET
def plan_list(request):
    query = Plan.objects.filter(tenant=request.member.tenant).annotate(
        subscribers=Count("customer", filter=Q(customer__tenant=request.member.tenant))).order_by("name", "pk")
    search = request.GET.get("q", "").strip()[:80]
    if search:
        query = query.filter(name__icontains=search)
    availability = request.GET.get("availability", "active")
    if availability not in {"active", "archived", "all"}:availability = "active"
    if availability != "all":query = query.filter(is_active=availability == "active")
    return render(request, "plans/list.html", {"page": paginate(request, query), "availability": availability})


def availability_response(request, proposal, token, form=None, status=200):
    return render(request, "plans/availability.html", {"proposal": proposal,
        "impact": availability_impact(request.member.tenant, proposal["plan"]),
        "form": form if form is not None else AvailabilityConfirmationForm(initial={"token": token})}, status=status)


@require("manage_plans")
@require_GET
def plan_availability_review(request, pk, is_active):
    manager(request.user)
    plan = get_object_or_404(Plan, tenant=request.member.tenant, pk=pk)
    token, proposal = review_availability(request.user, plan.pk, plan.version, is_active)
    return availability_response(request, proposal, token)


@require("manage_plans")
@sensitive_post_parameters("token")
@require_POST
def plan_availability_confirm(request):
    form = AvailabilityConfirmationForm(request.POST)
    try:
        proposal = read_availability(request.user, request.POST.get("token", "")[:4097])
    except ValidationError:
        messages.error(request, "Tinjauan ketersediaan paket tidak valid atau kedaluwarsa. Muat ulang paket.")
        return redirect("plan_list")
    if form.is_valid():
        back = reverse("plan_reactivate" if proposal["after"] else "plan_archive", args=[proposal["plan"]])
        if not recent_factor(request, return_to=back):return redirect("account_security")
        try:
            plan = confirm_availability(request.user, form.cleaned_data["token"], request.session["mfa_verified_at"], request.session["access_version"])
        except ValidationError as error:form.add_error(None, error)
        except OperationalError:form.add_error(None, "Status paket belum disimpan. Muat ulang paket dan tinjau lagi.")
        else:
            messages.success(request, "Paket aktif kembali dalam katalog." if plan.is_active else "Paket diarsipkan. Pelanggan dan PSB yang sudah ada tetap dilayani.")
            return redirect(reverse("plan_list") + ("?availability=active" if plan.is_active else "?availability=archived"))
    return availability_response(request, proposal, request.POST.get("token", "")[:4096], form, status=400)


@require("manage_plans")
@require_http_methods(["GET", "POST"])
def plan_form(request, pk=None):
    member = manager(request.user)
    plan = get_object_or_404(Plan, tenant=member.tenant, pk=pk) if pk else None
    initial = {**values_for(plan), "version": plan.version} if plan else {"version": 1}
    form = PlanForm(request.POST if request.method == "POST" else None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            token, payload = review_change(request.user, {key: form.cleaned_data[key] for key in FIELDS},
                form.cleaned_data["version"], pk)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            return review_response(request, token, payload)
    return render(request, "core/form.html", {"form": form, "title": "Ubah paket" if pk else "Tambah paket",
        "back": reverse("plan_list"), "submit_label": "Tinjau perubahan",
        "description": "Harga per bulan penuh sebelum pajak dan diskon. Langkah berikutnya menampilkan dampak "
            "untuk ditinjau sebelum konfirmasi MFA. Kecepatan adalah informasi paket; pengaturan router belum tersedia."})


def review_response(request, token, payload, form=None, status=200):
    return render(request, "plans/review.html", {"proposal": payload,
        "form": form if form is not None else ConfirmationForm(initial={"token": token}),
        "impact": impact(request.member.tenant, payload["plan"]),
        "edit_url": reverse("plan_edit", args=[payload["plan"]]) if payload["plan"] else reverse("plan_new")}, status=status)


@require("manage_plans")
@require_POST
def plan_confirm(request):
    form = ConfirmationForm(request.POST)
    token = request.POST.get("token", "")[:8193]
    try:
        payload = read_review(request.user, token)
    except ValidationError:
        messages.error(request, "Pratinjau tidak valid atau kedaluwarsa. Buat pratinjau baru.")
        return redirect("plan_list")
    if form.is_valid():
        return_to = reverse("plan_edit", args=[payload["plan"]]) if payload["plan"] else reverse("plan_new")
        if not recent_factor(request, return_to=return_to):
            return redirect("account_security")
        try:
            plan = confirm_change(request.user, form.cleaned_data["token"], request.session["mfa_verified_at"],
                request.session["access_version"])
        except ValidationError as exc:
            form.add_error(None, exc)
        except (IntegrityError, OperationalError):
            form.add_error(None, "Paket belum disimpan. Muat ulang dan buat pratinjau baru sebelum mencoba lagi.")
        else:
            messages.success(request, f"Paket {plan.name} tersimpan. Faktur yang sudah terbit tetap memakai snapshot semula.")
            return redirect("plan_list")
    return review_response(request, token, payload, form, status=400)
