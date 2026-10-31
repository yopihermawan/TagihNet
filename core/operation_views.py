import uuid
from functools import wraps
from datetime import date
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from .billing import audit
from .models import Customer, Installation, Membership, Plan, Ticket
from .operation_forms import (ActivationConfirmationForm, ActivationForm, CommentForm, InstallationAssignmentForm,
    InstallationCreationForm, InstallationEditForm, InstallationRescheduleForm, InstallationTransitionForm,
    SLASettingsForm, TicketCreationForm, TicketManageForm, TicketTransitionForm)
from .operations import (INSTALLATION_TRANSITIONS, TICKET_TRANSITIONS, activate_installation, activation_quote, add_comment,
    assign_installation, create_installation, create_ticket, edit_installation, manage_ticket, member_for,
    reschedule_installation, transition_installation,
    transition_ticket, visible_installations, visible_tickets)
from .permissions import capabilities, require
from .security_views import recent_factor
from .views import paginate

QUOTE_SALT = "tagihnet.psb.activation.v1"


def scope_stability(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except (Customer.DoesNotExist, Installation.DoesNotExist, Membership.DoesNotExist, Plan.DoesNotExist, Ticket.DoesNotExist):
            raise Http404("Data tidak tersedia pada cakupan akses akun ini.") from None
    return wrapped


def feedback(form, exc):
    if isinstance(exc, ValidationError):
        form.add_error(None, exc)
    else:
        form.add_error(None, "Data sedang diproses atau bertabrakan. Muat ulang dan coba lagi dengan permintaan yang sama.")


@login_required
@scope_stability
@require_GET
def ticket_list(request):
    member = member_for(request.user)
    query = visible_tickets(member).select_related("customer", "assigned_to__user")
    search = request.GET.get("q", "").strip()[:100]
    if search:
        query = query.filter(Q(title__icontains=search) | Q(customer__name__icontains=search) | Q(customer__code__icontains=search))
    status = request.GET.get("status", "")
    if status in dict(Ticket.STATES):
        query = query.filter(status=status)
    priority = request.GET.get("priority", "")
    if priority in dict(Ticket.PRIORITIES):
        query = query.filter(priority=priority)
    return render(request, "operations/tickets.html", {"page": paginate(request, query), "states": Ticket.STATES,
        "priorities": Ticket.PRIORITIES, "can_create": member.role == "customer" or "tickets" in capabilities(member)})


@login_required
@scope_stability
@require_http_methods(["GET", "POST"])
def ticket_create(request):
    member = member_for(request.user)
    if member.role != "customer" and "tickets" not in capabilities(member):
        raise PermissionDenied
    form = TicketCreationForm(request.POST or None, member=member, initial={"priority": "normal", "request_key": uuid.uuid4()})
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            ticket = create_ticket(request.user, member.customer_id if member.role == "customer" else data["customer"].pk,
                data["title"], data["description"], data["category"], data.get("priority", "normal"),
                data["assigned_to"].pk if data.get("assigned_to") else None, data["request_key"])
        except (ValidationError, IntegrityError, OperationalError) as exc:
            feedback(form, exc)
        else:
            messages.success(request, "Tiket tersimpan. Target SLA menggunakan kebijakan saat tiket dibuat.")
            return redirect("ticket_detail", pk=ticket.pk)
    return render(request, "core/form.html", {"form": form, "title": "Buat tiket gangguan", "submit_label": "Buat tiket",
        "back": "/operations/tickets/", "description": "Jelaskan kendala dan layanan yang terdampak. Lampiran belum tersedia pada tahap ini."})


def ticket_response(request, ticket, comment_form=None, status_form=None, status=200):
    member = member_for(request.user)
    customer = member.role == "customer"
    events = ticket.events.select_related("actor").order_by("-created_at", "-pk")
    if customer:
        events = events.filter(internal=False)
    choices = [choice for choice in Ticket.STATES if choice[0] in TICKET_TRANSITIONS[ticket.status]
               and not (member.role == "technician" and choice[0] == "closed")]
    status_form = status_form or TicketTransitionForm(initial={"request_key": uuid.uuid4(), "version": ticket.version})
    status_form.fields["status"].choices = choices
    return render(request, "operations/ticket_detail.html", {"ticket": ticket, "page": paginate(request, events),
        "comment_form": comment_form or CommentForm(customer=customer, initial={"request_key": uuid.uuid4()}),
        "status_form": status_form, "can_transition": bool(choices) and not customer,
        "can_manage": "tickets" in capabilities(member) and ticket.status != "closed"}, status=status)


@login_required
@scope_stability
@require_GET
def ticket_detail(request, pk):
    ticket = get_object_or_404(visible_tickets(member_for(request.user)).select_related("customer", "assigned_to__user"), pk=pk)
    return ticket_response(request, ticket)


@login_required
@scope_stability
@require_POST
def ticket_comment(request, pk):
    member = member_for(request.user)
    ticket = get_object_or_404(visible_tickets(member), pk=pk)
    if member.role == "customer" and "internal" in request.POST:
        raise PermissionDenied
    form = CommentForm(request.POST, customer=member.role == "customer")
    if form.is_valid():
        try:
            add_comment(request.user, pk, form.cleaned_data["body"], form.cleaned_data.get("internal", False), form.cleaned_data["request_key"])
        except (ValidationError, IntegrityError, OperationalError) as exc:
            feedback(form, exc)
        else:
            return redirect("ticket_detail", pk=pk)
    return ticket_response(request, ticket, comment_form=form, status=400)


@login_required
@scope_stability
@require_POST
def ticket_status(request, pk):
    member = member_for(request.user)
    if member.role == "customer":
        raise PermissionDenied
    ticket = get_object_or_404(visible_tickets(member), pk=pk)
    form = TicketTransitionForm(request.POST)
    if form.is_valid():
        data = form.cleaned_data
        try:
            transition_ticket(request.user, pk, data["status"], data["note"], data["version"], data["request_key"])
        except (ValidationError, IntegrityError, OperationalError) as exc:
            feedback(form, exc)
        else:
            return redirect("ticket_detail", pk=pk)
    ticket.refresh_from_db()
    return ticket_response(request, ticket, status_form=form, status=400)


@require("tickets")
@scope_stability
@require_http_methods(["GET", "POST"])
def ticket_manage(request, pk):
    ticket = get_object_or_404(visible_tickets(member_for(request.user)), pk=pk)
    form = TicketManageForm(request.POST or None, tenant=request.member.tenant,
        initial={"priority": ticket.priority, "category": ticket.category, "assigned_to": ticket.assigned_to_id,
                 "version": ticket.version, "request_key": uuid.uuid4()})
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            manage_ticket(request.user, pk, data["priority"], data["category"], data["assigned_to"].pk if data["assigned_to"] else None,
                          data["version"], data["request_key"])
        except (ValidationError, IntegrityError, OperationalError) as exc:
            feedback(form, exc)
        else:
            return redirect("ticket_detail", pk=pk)
    return render(request, "core/form.html", {"form": form, "title": "Triase & penugasan tiket", "back": reverse("ticket_detail", args=[pk]),
        "description": "Perubahan prioritas menghitung SLA dari waktu pembuatan tiket dengan snapshot kebijakan awal. Catatan triase hanya terlihat internal."})


@login_required
@scope_stability
@require_GET
def installation_list(request):
    member = member_for(request.user)
    query = visible_installations(member).select_related("plan", "assigned_to__user")
    search = request.GET.get("q", "").strip()[:100]
    if search:
        query = query.filter(Q(name__icontains=search) | Q(phone__icontains=search) | Q(username__icontains=search))
    if request.GET.get("status") in dict(Installation.STATES):
        query = query.filter(status=request.GET["status"])
    return render(request, "operations/installations.html", {"page": paginate(request, query), "states": Installation.STATES,
        "can_create": "installations" in capabilities(member)})


@require("installations")
@scope_stability
@require_http_methods(["GET", "POST"])
def installation_create(request):
    form = InstallationCreationForm(request.POST or None, tenant=request.member.tenant, initial={"request_key": uuid.uuid4()})
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        key, plan, assigned = data.pop("request_key"), data.pop("plan"), data.pop("assigned_to")
        data.update(plan_id=plan.pk, assigned_id=assigned.pk if assigned else None)
        try:
            order = create_installation(request.user, data, key)
        except (ValidationError, IntegrityError, OperationalError) as exc:
            feedback(form, exc)
        else:
            return redirect("installation_detail", pk=order.pk)
    return render(request, "core/form.html", {"form": form, "title": "Pengajuan pasang baru", "submit_label": "Buat PSB",
        "back": "/operations/installations/", "description": "Prospek belum menjadi pelanggan. Pemasangan dan faktur awal dibuat setelah tahapan serta persetujuan aktivasi selesai."})


def installation_response(request, order, transition_form=None, comment_form=None, status=200):
    member = member_for(request.user)
    choices = [choice for choice in Installation.STATES if choice[0] in INSTALLATION_TRANSITIONS.get(order.status, set())
               and not (member.role == "technician" and choice[0] == "cancelled")]
    transition_form = transition_form or InstallationTransitionForm(initial={"request_key": uuid.uuid4(), "version": order.version})
    transition_form.fields["status"].choices = choices
    return render(request, "operations/installation_detail.html", {"order": order, "status_form": transition_form,
        "comment_form": comment_form or CommentForm(customer=True, initial={"request_key": uuid.uuid4()}),
        "can_transition": bool(choices), "can_assign": "installations" in capabilities(member) and order.status not in {"activated", "cancelled"},
        "can_edit": "installations" in capabilities(member) and order.status in {"prospect", "survey", "scheduled"},
        "can_reschedule": "installations" in capabilities(member) and order.status == "scheduled",
        "can_activate": "activate_installations" in capabilities(member) and order.status == "installed",
        "page": paginate(request, order.events.select_related("actor").order_by("-created_at", "-pk"))}, status=status)


@login_required
@scope_stability
@require_GET
def installation_detail(request, pk):
    order = get_object_or_404(visible_installations(member_for(request.user)).select_related("plan", "assigned_to__user", "customer"), pk=pk)
    return installation_response(request, order)


@login_required
@scope_stability
@require_POST
def installation_status(request, pk):
    order = get_object_or_404(visible_installations(member_for(request.user)), pk=pk)
    form = InstallationTransitionForm(request.POST)
    if form.is_valid():
        data = form.cleaned_data
        try:
            transition_installation(request.user, pk, data["status"], data["note"], data["scheduled_for"], data["version"], data["request_key"])
        except (ValidationError, IntegrityError, OperationalError) as exc:
            feedback(form, exc)
        else:
            return redirect("installation_detail", pk=pk)
    order.refresh_from_db()
    return installation_response(request, order, transition_form=form, status=400)


@login_required
@scope_stability
@require_POST
def installation_comment(request, pk):
    order = get_object_or_404(visible_installations(member_for(request.user)), pk=pk)
    form = CommentForm(request.POST, customer=True)
    if form.is_valid():
        try:
            add_comment(request.user, pk, form.cleaned_data["body"], True, form.cleaned_data["request_key"], installation=True)
        except (ValidationError, IntegrityError, OperationalError) as exc:
            feedback(form, exc)
        else:
            return redirect("installation_detail", pk=pk)
    return installation_response(request, order, comment_form=form, status=400)


@require("installations")
@scope_stability
@require_http_methods(["GET", "POST"])
def installation_assign(request, pk):
    order = get_object_or_404(visible_installations(member_for(request.user)), pk=pk)
    form = InstallationAssignmentForm(request.POST or None, tenant=request.member.tenant,
        initial={"assigned_to": order.assigned_to_id, "version": order.version, "request_key": uuid.uuid4()})
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            assign_installation(request.user, pk, data["assigned_to"].pk if data["assigned_to"] else None, data["version"], data["request_key"])
        except (ValidationError, IntegrityError, OperationalError) as exc:
            feedback(form, exc)
        else:
            return redirect("installation_detail", pk=pk)
    return render(request, "core/form.html", {"form": form, "title": "Penugasan teknisi PSB", "back": reverse("installation_detail", args=[pk]),
        "description": "Teknisi hanya dapat mengakses pekerjaan yang ditugaskan. Buat akun preset Teknisi melalui Akun & peran jika daftar kosong."})


@require("installations")
@scope_stability
@require_http_methods(["GET", "POST"])
def installation_edit(request, pk):
    order = get_object_or_404(visible_installations(member_for(request.user)), pk=pk)
    initial = {field: getattr(order, field) for field in ["name", "email", "phone", "username", "address", "area"]}
    initial.update(plan=order.plan_id, version=order.version, request_key=uuid.uuid4())
    form = InstallationEditForm(request.POST or None, tenant=request.member.tenant, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        key, version, reason = data.pop("request_key"), data.pop("version"), data.pop("reason")
        data["plan_id"] = data.pop("plan").pk
        try:
            edit_installation(request.user, pk, data, reason, version, key)
        except (ValidationError, IntegrityError, OperationalError) as exc:
            feedback(form, exc)
        else:
            messages.success(request, "Data PSB disimpan. Periksa kembali tahap dan jadwal pemasangannya.")
            return redirect("installation_detail", pk=pk)
    return render(request, "core/form.html", {"form": form, "title": "Ubah data PSB", "back": reverse("installation_detail", args=[pk]),
        "description": "Hanya sebelum Pengerjaan. Jika PSB sudah Terjadwal, perubahan identitas/paket mengembalikannya ke Survei dan membatalkan jadwal lama. Riwayat sebelum/sesudah tersimpan internal."})


@require("installations")
@scope_stability
@require_http_methods(["GET", "POST"])
def installation_reschedule(request, pk):
    order = get_object_or_404(visible_installations(member_for(request.user)), pk=pk)
    form = InstallationRescheduleForm(request.POST or None, initial={"scheduled_for": order.scheduled_for,
        "version": order.version, "request_key": uuid.uuid4()})
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            reschedule_installation(request.user, pk, data["scheduled_for"], data["reason"], data["version"], data["request_key"])
        except (ValidationError, IntegrityError, OperationalError) as exc:
            feedback(form, exc)
        else:
            messages.success(request, "Jadwal baru disimpan. Teknisi melihat penugasan yang telah diperbarui.")
            return redirect("installation_detail", pk=pk)
    return render(request, "core/form.html", {"form": form, "title": "Jadwalkan ulang PSB", "back": reverse("installation_detail", args=[pk]),
        "description": "Hanya pada tahap Terjadwal dengan teknisi aktif. Pilih waktu mendatang; alasan dan jadwal lama/baru dicatat tanpa mengubah pelanggan atau faktur."})


@require("activate_installations")
@scope_stability
@require_http_methods(["GET", "POST"])
def installation_activate(request, pk):
    order = get_object_or_404(visible_installations(member_for(request.user)).select_related("tenant", "plan"), pk=pk)
    period_form = ActivationForm(request.GET or None, initial={"period": timezone.localdate().replace(day=1)})
    form = ActivationConfirmationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            payload = signing.loads(form.cleaned_data["quote"], salt=QUOTE_SALT, max_age=900)
            if (payload["order"] != pk or payload["actor"] != request.user.pk or payload["tenant"] != request.member.tenant_id):
                raise PermissionDenied
            period = date.fromisoformat(payload["period"])
            if not recent_factor(request, return_to=reverse("installation_activate", args=[pk]) + f"?period={period:%Y-%m}"):
                return redirect("account_security")
            activate_installation(request.user, pk, period, payload["fingerprint"], request.session["mfa_verified_at"], request.session["access_version"])
        except (signing.BadSignature, KeyError, TypeError, ValueError):
            form.add_error(None, "Pratinjau aktivasi tidak valid atau kedaluwarsa. Muat ulang halaman.")
        except (ValidationError, IntegrityError, OperationalError) as exc:
            feedback(form, exc)
        else:
            messages.success(request, "Aktivasi administrasi selesai. Pelanggan/faktur tersimpan; provisioning masih proses terpisah.")
            return redirect("installation_detail", pk=pk)
    period = period_form.cleaned_data["period"] if period_form.is_valid() else timezone.localdate().replace(day=1)
    snapshot, fingerprint = activation_quote(order, period)
    token = signing.dumps({"order": pk, "actor": request.user.pk, "tenant": request.member.tenant_id,
        "period": period.isoformat(), "fingerprint": fingerprint}, salt=QUOTE_SALT, compress=True)
    if request.method != "POST":
        form = ActivationConfirmationForm(initial={"quote": token})
    return render(request, "operations/activation.html", {"order": order, "period_form": period_form,
        "form": form, "snapshot": snapshot, "period": period, "can_activate": order.status == "installed"})


@require("settings")
@scope_stability
@require_http_methods(["GET", "POST"])
def sla_settings(request):
    form = SLASettingsForm(request.POST or None, instance=request.member.tenant)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            form.save()
            audit(request.member.tenant, request.user, "operations.sla_changed", request.member.tenant.pk,
                  "SLA baru hanya berlaku untuk tiket yang dibuat berikutnya.", request.correlation_id)
        messages.success(request, "Target SLA disimpan. Snapshot tiket yang sudah ada tidak berubah.")
        return redirect("sla_settings")
    return render(request, "core/form.html", {"form": form, "title": "Target SLA tiket", "back": "/operations/tickets/",
        "description": "Rentang 1–720 jam. Target berjalan sejak tiket dibuat, tidak berhenti saat Menunggu pelanggan. Perubahan berlaku untuk tiket baru; belum ada notifikasi SLA otomatis."})
