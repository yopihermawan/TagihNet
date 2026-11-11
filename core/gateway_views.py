import uuid
from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from .gateway import can_checkout, refresh_order, save_config, start_checkout
from .gateway_expiry import expire_order
from .gateway_refunds import reconcile_refund
from .gateway_worker import save_policy as save_reconciliation_policy
from .midtrans import GatewayUnavailable, credentials, json_object, valid_signature
from .models import GatewayConfig, GatewayOrder, GatewayReconciliationPolicy, GatewayRefundResolution
from .permissions import capabilities, membership, require
from .security import throttle
from .security_views import recent_factor
from .views import allowed_invoice, paginate


class ConfigForm(forms.Form):
    enabled = forms.BooleanField(required=False, label="Aktifkan checkout Midtrans Sandbox untuk ISP ini")
    version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    reviewed = forms.BooleanField(label="Saya memahami ini pengujian sandbox dan tidak menerima uang nyata.")


class ExpiryForm(forms.Form):
    request_key = forms.UUIDField(widget=forms.HiddenInput)
    reason = forms.CharField(min_length=10, max_length=250, widget=forms.Textarea(attrs={"rows": 3}), label="Alasan mengakhiri pembayaran")
    reviewed = forms.BooleanField(label="Saya meminta pengakhiran di gateway dan memahami tagihan tetap belum lunas bila belum dibayar.")


class ReconciliationForm(forms.Form):
    enabled = forms.BooleanField(required=False, label="Aktifkan pemeriksaan status otomatis")
    include_settled = forms.BooleanField(required=False, label="Periksa juga pembayaran tercatat untuk mendeteksi refund atau sengketa")
    interval = forms.TypedChoiceField(coerce=int, choices=[(60, "Setiap 1 menit"), (300, "Setiap 5 menit"), (900, "Setiap 15 menit")], label="Jeda antar pemeriksaan")
    version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    reviewed = forms.BooleanField(label="Saya menyetujui pemeriksaan berkala dan pencatatan otomatis pembayaran sandbox yang terverifikasi.")


class RefundForm(forms.Form):
    request_key = forms.UUIDField(widget=forms.HiddenInput)
    reason = forms.CharField(min_length=10,max_length=250,widget=forms.Textarea(attrs={"rows":3}),label="Alasan pencatatan refund")
    reviewed = forms.BooleanField(label="Saya memahami refund terverifikasi akan membatalkan pembayaran ini pada ledger dan membuka kembali sisa tagihan.")


def refund_context(request, order, form=None):
    owner = membership(request.user).role=="owner"
    resolution = GatewayRefundResolution.objects.filter(order=order).first()
    if not owner or not order.payment_id or resolution:
        form = None
    elif form is None:
        form = RefundForm(auto_id="refund_%s", initial={"request_key":uuid.uuid4()})
    return {"refund_resolution":resolution,"refund_form":form}


def settings_response(request, form=None, policy_form=None, status=200):
    config = GatewayConfig.objects.filter(tenant=request.member.tenant).first()
    policy = GatewayReconciliationPolicy.objects.filter(tenant=request.member.tenant).first()
    if form is None:
        form = ConfigForm(initial={"enabled": bool(config and config.enabled), "version": config.version if config else 0})
    if policy_form is None:
        policy_form = ReconciliationForm(auto_id="reconciliation_%s", initial={"enabled": bool(policy and policy.enabled),
            "include_settled": bool(policy and policy.include_settled), "interval": policy.interval_seconds if policy else 60,
            "version": policy.version if policy else 0})
    try:
        merchant = credentials(request.member.tenant_id)[0]
        ready = True
    except ValidationError:
        merchant, ready = "", False
    return render(request, "gateway/settings.html", {"form": form, "ready": ready, "merchant_id": merchant,
        "policy": policy, "policy_form": policy_form}, status=status)


def expiry_form(order, data=None):
    previous = order.expiry_requests.filter(status__in=["requested", "unavailable"]).first()
    form = ExpiryForm(data, initial={"request_key": previous.request_key if previous else uuid.uuid4(),
        "reason": previous.reason if previous else ""})
    if previous:
        form.fields["reason"].disabled = True
        form.fields["reason"].help_text = "Coba ulang permintaan yang sama; alasan awal tetap disimpan."
    return form


@require("manage_access")
@require_http_methods(["GET", "POST"])
def gateway_settings(request):
    if request.member.role != "owner":
        raise PermissionDenied
    config = GatewayConfig.objects.filter(tenant=request.member.tenant).first()
    form = ConfigForm(request.POST or None, initial={"enabled": bool(config and config.enabled), "version": config.version if config else 0})
    if request.method == "POST":
        if not recent_factor(request, return_to=request.path):
            return redirect("account_security")
        if form.is_valid():
            try:
                save_config(request.user, form.cleaned_data["enabled"], form.cleaned_data["version"],
                    request.session["mfa_verified_at"], request.session["access_version"])
            except (ValidationError, IntegrityError, OperationalError):
                form.add_error(None, "Setelan belum disimpan. Periksa konfigurasi kredensial dan muat ulang formulir bila data berubah.")
            else:
                messages.success(request, "Setelan Midtrans Sandbox disimpan. Order lama tetap dapat direkonsiliasi.")
                return redirect("gateway_settings")
    return settings_response(request, form=form)


@require("manage_access")
@require_POST
def reconciliation_settings(request):
    if request.member.role != "owner":
        raise PermissionDenied
    if not recent_factor(request, return_to="/settings/payment-gateway/"):
        return redirect("account_security")
    form = ReconciliationForm(request.POST, auto_id="reconciliation_%s")
    if form.is_valid():
        try:
            save_reconciliation_policy(request.user, form.cleaned_data["enabled"], form.cleaned_data["include_settled"],
                form.cleaned_data["interval"], form.cleaned_data["version"], request.session["mfa_verified_at"],
                request.session["access_version"])
        except (ValidationError, IntegrityError, OperationalError):
            form.add_error(None, "Kebijakan belum disimpan. Periksa kredensial sandbox dan muat ulang jika pengaturan berubah.")
        else:
            messages.success(request, "Kebijakan pemeriksaan otomatis disimpan. Worker mengikuti jadwal dan persetujuan terbaru.")
            return redirect("gateway_settings")
    return settings_response(request, policy_form=form, status=400)


@require_POST
def checkout(request, pk):
    invoice = allowed_invoice(request, pk)
    member = membership(request.user)
    if not can_checkout(member):
        raise PermissionDenied
    if member.role != "customer" and not recent_factor(request, return_to=f"/invoices/{invoice.pk}/"):
        return redirect("account_security")
    if throttle(request, "gateway_create", request.user.pk):
        return JsonResponse({"error": "Terlalu banyak permintaan. Coba kembali nanti."}, status=429)
    try:
        order = start_checkout(request.user, invoice.pk)
    except (ValidationError, GatewayUnavailable, IntegrityError, OperationalError):
        messages.error(request, "Checkout belum tersedia. Periksa status order yang sama sebelum mencoba kembali; tagihan belum otomatis lunas.")
        return redirect("invoice_detail", pk=invoice.pk)
    return redirect("gateway_order", pk=order.pk)


def allowed_order(request, pk):
    return get_object_or_404(scoped_orders(request), pk=pk)


def scoped_orders(request):
    if not request.user.is_authenticated:
        raise PermissionDenied
    member = membership(request.user)
    if not {"invoices", "portal"} & capabilities(member):
        raise PermissionDenied
    orders = GatewayOrder.objects.filter(tenant=member.tenant, invoice__tenant=member.tenant)
    if member.role == "customer":
        orders = orders.filter(invoice__customer_id=member.customer_id)
    return orders.select_related("invoice", "created_by")


@require_GET
def order_list(request):
    orders = scoped_orders(request)
    counts = dict(orders.order_by().values("status").annotate(total=Count("pk")).values_list("status", "total"))
    query = request.GET.get("q", "").strip()[:100]
    state = request.GET.get("status", "")
    if state in dict(GatewayOrder.STATES):
        orders = orders.filter(status=state)
    if query:
        search = Q(invoice__customer_name__icontains=query)
        try:
            identifier = uuid.UUID(query.removeprefix("TN-SBX-"))
        except ValueError:
            pass
        else:
            search |= Q(pk=identifier)
        orders = orders.filter(search)
    return render(request, "gateway/list.html", {"page": paginate(request, orders), "query": query,
        "selected_status": state, "states": GatewayOrder.STATES, "summary": {
            "total": sum(counts.values()), "waiting": counts.get("creating", 0) + counts.get("pending", 0),
            "review": counts.get("review", 0), "settled": counts.get("settled", 0)}})


@require_GET
def order_detail(request, pk):
    order = allowed_order(request, pk)
    owner = membership(request.user).role == "owner"
    return render(request, "gateway/order.html", {"order": order, "can_refresh": can_checkout(membership(request.user)),
        "events": order.events.order_by("-created_at", "-pk")[:20],
        "expiry_form": expiry_form(order) if owner and not order.payment_id and order.status in {"creating", "pending"} else None,
        "expiry_requests": order.expiry_requests.select_related("requested_by")[:10] if owner else [],
        **refund_context(request,order)})


@require("manage_access")
@require_POST
def order_refund(request, pk):
    if request.member.role!="owner":
        raise PermissionDenied
    order = allowed_order(request,pk)
    if not recent_factor(request,return_to=f"/payments/orders/{order.pk}/"):
        return redirect("account_security")
    if throttle(request,"gateway_refund_record",request.user.pk):
        return JsonResponse({"error":"Terlalu banyak permintaan. Coba kembali nanti."},status=429)
    form = RefundForm(request.POST, auto_id="refund_%s")
    if form.is_valid():
        try:
            reconcile_refund(request.user,order.pk,form.cleaned_data["request_key"],form.cleaned_data["reason"],
                request.session["mfa_verified_at"],request.session["access_version"])
        except (ValidationError,GatewayUnavailable,IntegrityError,OperationalError):
            form.add_error(None,"Refund penuh belum dapat dicatat. Pastikan refund penuh sudah selesai di Midtrans dan nominalnya sesuai; periksa status lalu coba kembali.")
        else:
            messages.success(request,"Refund penuh yang sudah terverifikasi dicatat pada ledger. Sisa tagihan dibuka kembali; tidak ada permintaan refund baru ke Midtrans.")
            return redirect("gateway_order",pk=pk)
    order.refresh_from_db()
    return render(request,"gateway/order.html",{"order":order,"can_refresh":True,
        "events":order.events.order_by("-created_at","-pk")[:20],"expiry_form":None,
        "expiry_requests":order.expiry_requests.select_related("requested_by")[:10],
        **refund_context(request,order,form=form)},status=400)


@require("manage_access")
@require_POST
def order_expire(request, pk):
    if request.member.role != "owner":
        raise PermissionDenied
    order = allowed_order(request, pk)
    if not recent_factor(request, return_to=f"/payments/orders/{order.pk}/"):
        return redirect("account_security")
    if throttle(request, "gateway_expiry", request.user.pk):
        return JsonResponse({"error": "Terlalu banyak permintaan. Coba kembali nanti."}, status=429)
    form = expiry_form(order, request.POST)
    if form.is_valid():
        try:
            result = expire_order(request.user, order.pk, form.cleaned_data["request_key"], form.cleaned_data["reason"],
                request.session["mfa_verified_at"], request.session["access_version"])
        except (ValidationError, GatewayUnavailable, IntegrityError, OperationalError):
            form.add_error(None, "Permintaan belum dapat diselesaikan. Periksa status order sebelum mencoba kembali.")
        else:
            messages.info(request, result.result)
            return redirect("gateway_order", pk=pk)
    order.refresh_from_db()
    return render(request, "gateway/order.html", {"order": order, "can_refresh": True,
        "events": order.events.order_by("-created_at", "-pk")[:20], "expiry_form": form,
        "expiry_requests": order.expiry_requests.select_related("requested_by")[:10],
        **refund_context(request,order)}, status=400)


@require_POST
def order_refresh(request, pk):
    order = allowed_order(request, pk)
    if not can_checkout(membership(request.user)):
        raise PermissionDenied
    if throttle(request, "gateway_refresh", request.user.pk):
        return JsonResponse({"error": "Terlalu banyak permintaan. Coba kembali nanti."}, status=429)
    try:
        refresh_order(order)
    except (ValidationError, GatewayUnavailable, IntegrityError, OperationalError):
        messages.error(request, "Status belum dapat diverifikasi. Pembayaran tidak diubah; coba kembali nanti.")
    else:
        messages.info(request, "Status diperiksa langsung ke Midtrans Sandbox.")
    return redirect("gateway_order", pk=pk)


@csrf_exempt
@require_POST
@sensitive_variables()
def notification(request):
    if request.content_type != "application/json" or len(request.body) > 16384:
        return JsonResponse({"error": "invalid payload"}, status=400)
    try:
        payload = json_object(request.body)
        order_id = payload.get("order_id", "")
        if not isinstance(order_id, str) or not order_id.startswith("TN-SBX-") or len(order_id) != 39:
            raise ValueError
        order_uuid = uuid.UUID(hex=order_id[7:])
    except (ValueError, UnicodeError, TypeError, RecursionError):
        return JsonResponse({"error": "invalid payload"}, status=400)
    order = GatewayOrder.objects.filter(pk=order_uuid).first()
    if not order:
        return JsonResponse({"error": "unknown order"}, status=404)
    try:
        signed = valid_signature(order, payload)
    except ValidationError:
        return JsonResponse({"error": "verification unavailable"}, status=503)
    if not signed:
        return JsonResponse({"error": "invalid signature"}, status=403)
    try:
        refresh_order(order)
    except (ValidationError, GatewayUnavailable, IntegrityError, OperationalError):
        return JsonResponse({"error": "verification unavailable"}, status=503)
    return JsonResponse({"status": "ok"})
