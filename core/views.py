import csv
import hashlib
import time
import uuid
from datetime import timedelta
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import PasswordResetView
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET
from .billing import audit, issue_invoice, record_payment, reverse_payment
from .forms import BillingSettingsForm, CustomerForm, InvoiceForm, PaymentForm
from .models import AuditEvent, AuthAttempt, Customer, Invoice, NetworkJob, Payment
from .pdf import render_invoice
from .permissions import require, membership


def throttle(request, scope):
    # REMOTE_ADDR is trusted; never trust an arbitrary X-Forwarded-For header.
    identity = request.POST.get("username", request.POST.get("email", "")).strip().lower()[:254]
    keys = [hashlib.sha256(f"{scope}:{item}".encode()).hexdigest()
            for item in (request.META.get("REMOTE_ADDR", "unknown"), identity)]
    cutoff = timezone.now() - timedelta(minutes=15)
    AuthAttempt.objects.filter(created_at__lt=cutoff).delete()
    if any(AuthAttempt.objects.filter(key=key, created_at__gte=cutoff).count() >= 5 for key in keys):
        return True
    AuthAttempt.objects.bulk_create([AuthAttempt(key=key) for key in keys])
    return False


def landing(member):
    return "portal" if member.role == "customer" else "network" if member.role == "noc" else "dashboard"


def login_view(request):
    if request.user.is_authenticated:
        return redirect(landing(membership(request.user)))
    if request.method == "POST" and throttle(request, "login"):
        response = render(request, "registration/login.html", {"form": AuthenticationForm(request),
                          "throttled": True}, status=429)
        response["Retry-After"] = "900"
        return response
    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        member = membership(form.get_user())
        login(request, form.get_user())
        request.session["started_at"] = time.time()
        request.session["last_seen"] = time.time()
        return redirect(landing(member))
    return render(request, "registration/login.html", {"form": form})


class ThrottledPasswordResetView(PasswordResetView):
    template_name = "registration/reset_form.html"
    email_template_name = "registration/reset_email.txt"
    subject_template_name = "registration/reset_subject.txt"
    success_url = reverse_lazy("password_reset_done")

    def post(self, request, *args, **kwargs):
        if throttle(request, "reset"):
            response = HttpResponse("Terlalu banyak permintaan. Coba lagi dalam 15 menit.", status=429)
            response["Retry-After"] = "900"
            return response
        return super().post(request, *args, **kwargs)


@require("dashboard")
def dashboard(request):
    today = timezone.localdate()
    period = request.GET.get("period", today.strftime("%Y-%m"))
    try:
        first = timezone.datetime.strptime(period, "%Y-%m").date().replace(day=1)
    except ValueError:
        first = today.replace(day=1)
    invoices = list(Invoice.objects.filter(tenant=request.member.tenant).select_related("customer"))
    month_invoices = [item for item in invoices if item.period == first]
    payments = Payment.objects.filter(tenant=request.member.tenant, reversal__isnull=True,
        created_at__date__year=first.year, created_at__date__month=first.month)
    context = {"period": first.strftime("%Y-%m"), "today": today, "updated_at": timezone.now(),
        "received": sum(payments.values_list("amount", flat=True)), "payment_count": payments.count(),
        "issued": sum(item.total for item in month_invoices), "issued_count": len(month_invoices),
        "receivable": sum(item.balance for item in invoices),
        "overdue": sum(item.balance for item in invoices if item.due_date < today),
        "overdue_invoices": [item for item in invoices if item.status == "overdue"][:5],
        "recent_payments": Payment.objects.filter(tenant=request.member.tenant).select_related("invoice", "invoice__customer")[:5],
        "active_customers": Customer.objects.filter(tenant=request.member.tenant, service_state="active").count(),
        "customer_count": Customer.objects.filter(tenant=request.member.tenant).count(),
        "open_jobs": NetworkJob.objects.filter(tenant=request.member.tenant).exclude(status="confirmed").count(),
        "events": AuditEvent.objects.filter(tenant=request.member.tenant)[:4]}
    return render(request, "core/dashboard.html", context)


def customer_query(request):
    result = Customer.objects.filter(tenant=request.member.tenant).select_related("plan")
    query = request.GET.get("q", "").strip()[:100]
    if query:
        result = result.filter(Q(name__icontains=query) | Q(code__icontains=query) | Q(username__icontains=query) | Q(phone__icontains=query))
    state = request.GET.get("state", "")
    if state in {"active", "isolated"}:
        result = result.filter(service_state=state)
    plan = request.GET.get("plan", "")
    if plan.isdigit():
        result = result.filter(plan_id=plan)
    area = request.GET.get("area", "").strip()[:80]
    if area:
        result = result.filter(area=area)
    ordering = request.GET.get("sort", "name")
    if ordering not in {"name", "-name", "code", "-created_at"}:
        ordering = "name"
    return result.order_by(ordering, "pk")


def paginate(request, result):
    size = request.GET.get("size", "10")
    size = int(size) if size in {"10", "25", "50"} else 10
    return Paginator(result, size).get_page(request.GET.get("page", 1))


@require("customers")
def customers(request):
    return render(request, "core/customers.html", {"page": paginate(request, customer_query(request)),
                  "plans": request.member.tenant.plan_set.all(),
                  "areas": Customer.objects.filter(tenant=request.member.tenant).values_list("area", flat=True).distinct().order_by("area")})


@require("edit_customers")
def customer_form(request, pk=None):
    customer = get_object_or_404(Customer, tenant=request.member.tenant, pk=pk) if pk else Customer()
    form = CustomerForm(request.POST or None, instance=customer, tenant=request.member.tenant)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                item = form.save(commit=False)
                if not item.code:
                    item.code = "TN-" + uuid.uuid4().hex[:10].upper()
                item.save()
                audit(request.member.tenant, request.user, "customer.updated" if pk else "customer.created", item.pk,
                      "Identitas atau layanan pelanggan disimpan.", request.correlation_id)
            messages.success(request, "Pelanggan berhasil disimpan.")
            return redirect("customer_detail", pk=item.pk)
        except IntegrityError:
            form.add_error(None, "Username atau telepon sudah digunakan pelanggan lain pada ISP ini.")
    return render(request, "core/form.html", {"form": form, "title": "Ubah pelanggan" if pk else "Tambah pelanggan",
                  "description": "Identitas, layanan, dan wilayah pelanggan. Harga paket ditentukan di server.", "back": "/customers/"})


@require("customers")
def customer_detail(request, pk):
    item = get_object_or_404(Customer.objects.select_related("plan"), tenant=request.member.tenant, pk=pk)
    invoices = list(Invoice.objects.filter(tenant=request.member.tenant, customer=item))
    return render(request, "core/customer_detail.html", {"customer": item, "invoices": invoices,
                  "balance": sum(invoice.balance for invoice in invoices),
                  "jobs": NetworkJob.objects.filter(tenant=request.member.tenant, customer=item)[:5]})


@require("export")
@require_GET
def customer_export(request):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="tagihnet-pelanggan.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(["ID", "Nama", "Telepon", "Wilayah", "Paket", "Status layanan"])
    def safe(value):
        value = str(value)
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value
    for item in customer_query(request)[:10000]:
        writer.writerow([safe(v) for v in [item.code, item.name, item.phone, item.area, item.plan.name, item.get_service_state_display()]])
    audit(request.member.tenant, request.user, "customer.exported", "csv", "Maksimal 10.000 baris", request.correlation_id)
    return response


@require("invoices")
def invoices(request):
    result = Invoice.objects.filter(tenant=request.member.tenant).select_related("customer")
    query = request.GET.get("q", "").strip()[:100]
    if query:
        result = result.filter(Q(customer__name__icontains=query) | Q(customer__code__icontains=query))
    state = request.GET.get("state", "")
    # Derived state is never accepted as a writable invoice field.
    if state in {"paid", "overdue", "issued", "partial"}:
        result = [item for item in result if item.status == state]
    return render(request, "core/invoices.html", {"page": paginate(request, result)})


@require("issue")
def invoice_create(request):
    initial = {"period": timezone.localdate().replace(day=1), "customer": request.GET.get("customer"), "discount": 0}
    form = InvoiceForm(request.POST or None, tenant=request.member.tenant, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            invoice, created = issue_invoice(request.member.tenant, form.cleaned_data["customer"].pk,
                form.cleaned_data["period"], request.user, form.cleaned_data["discount"], request.correlation_id)
            messages.success(request, "Faktur diterbitkan." if created else "Faktur periode ini sudah ada; tidak digandakan.")
            return redirect("invoice_detail", pk=invoice.pk)
        except ValidationError as exc:
            form.add_error(None, exc)
        except OperationalError:
            form.add_error(None, "Database sedang sibuk. Silakan coba lagi; satu periode tidak akan digandakan.")
    return render(request, "core/form.html", {"form": form, "title": "Buat faktur", "back": "/invoices/",
        "description": "Satu faktur per pelanggan per bulan. Harga dan pajak dihitung di server; tanggal 29–31 mengikuti akhir bulan. Pembayaran sebagian didukung, lebih bayar ditolak."})


def allowed_invoice(request, pk):
    if not request.user.is_authenticated:
        raise PermissionDenied
    member = membership(request.user)
    if member.role not in {"owner", "ops", "finance", "cashier", "customer"}:
        raise PermissionDenied
    result = Invoice.objects.filter(tenant=member.tenant).select_related("customer", "tenant")
    if member.role == "customer":
        result = result.filter(customer=member.customer)
    return get_object_or_404(result, pk=pk)


def invoice_detail(request, pk):
    if not request.user.is_authenticated:
        return redirect("login")
    invoice = allowed_invoice(request, pk)
    return render(request, "core/invoice_detail.html", {"invoice": invoice,
        "payments": invoice.payments.select_related("recorded_by", "reversal"),
        "payment_form": PaymentForm(initial={"amount": invoice.balance, "event_key": uuid.uuid4()})})


@require_GET
def invoice_pdf(request, pk):
    invoice = allowed_invoice(request, pk)
    response = HttpResponse(render_invoice(invoice, settings.SIMULATION), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{invoice.number}.pdf"'
    return response


@require("pay")
@require_POST
def payment_create(request, pk):
    invoice = get_object_or_404(Invoice, tenant=request.member.tenant, pk=pk)
    form = PaymentForm(request.POST)
    if form.is_valid():
        try:
            _, created = record_payment(request.member.tenant, invoice.pk, form.cleaned_data["amount"],
                form.cleaned_data["event_key"], request.user, form.cleaned_data["note"], request.correlation_id)
            messages.success(request, "Pembayaran simulasi tercatat. Rekonsiliasi jaringan masuk antrean." if created else "Pembayaran sudah tercatat; duplikat diabaikan.")
            return redirect("invoice_detail", pk=pk)
        except ValidationError as exc:
            form.add_error(None, exc)
        except OperationalError:
            form.add_error(None, "Database sedang sibuk. Kirim ulang formulir yang sama agar tetap idempoten.")
    return render(request, "core/form.html", {"form": form, "title": "Periksa pembayaran", "back": f"/invoices/{pk}/"}, status=400)


@require("reverse")
@require_POST
def payment_reverse(request, pk):
    payment = get_object_or_404(Payment, tenant=request.member.tenant, pk=pk)
    try:
        reverse_payment(request.member.tenant, payment.pk, request.POST.get("reason", ""), request.user, request.correlation_id)
        messages.success(request, "Reversal tercatat. Pembayaran asli tetap tersimpan dalam riwayat.")
    except ValidationError:
        messages.error(request, "Isi alasan koreksi sepanjang 10–250 karakter.")
    return redirect("invoice_detail", pk=payment.invoice_id)


@require("network")
def network(request):
    return render(request, "core/network.html", {"jobs": NetworkJob.objects.filter(tenant=request.member.tenant).select_related("customer").order_by("-id")[:30]})


@require("settings")
def billing_settings(request):
    form = BillingSettingsForm(request.POST or None, instance=request.member.tenant)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            form.save()
            audit(request.member.tenant, request.user, "billing.settings_updated", request.member.tenant.pk,
                  "Berlaku untuk faktur berikutnya; snapshot faktur terbit tidak berubah.", request.correlation_id)
        messages.success(request, "Setelan disimpan. Faktur yang sudah terbit tetap menggunakan snapshot sebelumnya.")
        return redirect("billing_settings")
    return render(request, "core/form.html", {"form": form, "title": "Setelan billing", "back": "/",
                  "description": "Prabayar bulanan, tanpa prorata pada fondasi ini. Perubahan hanya memengaruhi faktur yang belum diterbitkan. Pajak 0 bp berarti nol, bukan belum diatur. Footer selalu diperlakukan sebagai teks biasa."})


@require("audit")
def audit_log(request):
    return render(request, "core/audit.html", {"page": paginate(request, AuditEvent.objects.filter(tenant=request.member.tenant).select_related("actor"))})


@require("portal")
def portal(request):
    return render(request, "core/portal.html", {"customer": request.member.customer,
                  "invoices": Invoice.objects.filter(tenant=request.member.tenant, customer=request.member.customer)})


@require_GET
def health(request):
    # No tenant data, account identifiers or infrastructure details.
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"status": "ok", "mode": "simulation" if settings.SIMULATION else "integrations-disabled"})
