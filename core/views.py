import csv
import uuid
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import PasswordResetView
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Count,Q,Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.debug import sensitive_post_parameters
from .billing import audit, issue_invoice, record_payment, reverse_payment
from .forms import CustomerForm, InvoiceForm, PaymentForm
from .customer_filters import DUE_CHOICES, apply_service_filters, positive_id, with_financial_summary
from .customer_profile import invoice_rows, profile_context
from .invoice_queries import with_balances
from .models import AuditEvent, Customer, Invoice, MFADevice, NetworkJob, Payment, Tenant
from .plan_selection import assignment_plan
from .locations import display as location_display
from .pdf import render_invoice
from .permissions import require, membership, capabilities
from .security import complete_login, remember_password, throttle


def landing(member):
    rights = capabilities(member)
    return next((route for right, route in [("portal", "portal"), ("dashboard", "dashboard"),
        ("network", "network"), ("customers", "customers"), ("invoices", "invoices"),
        ("field_work", "ticket_list"), ("tickets", "ticket_list"), ("installations", "installation_list"),
        ("sales", "business_sales"), ("cash", "business_cash"), ("team", "business_team"),
        ("payroll", "business_payroll"), ("hotspot", "business_hotspot"), ("topology", "business_topology")] if right in rights), "account_security")


@sensitive_post_parameters("password")
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
        user = form.get_user()
        member = membership(user)
        enrolled = MFADevice.objects.filter(user=user).exists()
        if enrolled or member.role != "customer":
            remember_password(request, user)
            return redirect("mfa_challenge" if enrolled else "mfa_setup")
        complete_login(request, user)
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
    invoices = with_balances(Invoice.objects.all(),request.member.tenant)
    amounts=invoices.aggregate(receivable=Sum('remaining'),overdue=Sum('remaining',filter=Q(due_date__lt=today)),
        issued=Sum('total',filter=Q(period=first)),issued_count=Count('pk',filter=Q(period=first)))
    payments = Payment.objects.filter(tenant=request.member.tenant, reversal__isnull=True,
        created_at__date__year=first.year, created_at__date__month=first.month)
    payments=payments.aggregate(received=Sum('amount'),payment_count=Count('pk'))
    customers=Customer.objects.filter(tenant=request.member.tenant).aggregate(customer_count=Count('pk'),
        active_customers=Count('pk',filter=Q(service_state='active')))
    context = {"period": first.strftime("%Y-%m"), "today": today, "updated_at": timezone.now(),
        **{key:value or 0 for key,value in amounts.items()},**{key:value or 0 for key,value in payments.items()},**customers,
        "overdue_invoices": invoices.exclude(remaining=0).filter(due_date__lt=today).select_related('customer')[:5],
        "recent_payments": Payment.objects.filter(tenant=request.member.tenant).select_related("invoice", "invoice__customer", "reversal")[:5],
        "open_jobs": NetworkJob.objects.filter(tenant=request.member.tenant, status__in=["pending", "failed"]).count(),
        "events": AuditEvent.objects.filter(tenant=request.member.tenant)[:4]}
    return render(request, "core/dashboard.html", context)


def customer_query(request, today=None):
    result = Customer.objects.filter(tenant=request.member.tenant).select_related("plan", "nas")
    query = request.GET.get("q", "").strip()[:100]
    if query:
        result = result.filter(Q(name__icontains=query) | Q(code__icontains=query) | Q(username__icontains=query) | Q(phone__icontains=query))
    state = request.GET.get("state", "")
    if state in {"active", "isolated", "pending"}:
        result = result.filter(service_state=state)
    plan = request.GET.get("plan", "")
    if plan:
        plan_id = positive_id(plan)
        result = result.filter(plan_id=plan_id, plan__tenant=request.member.tenant) if plan_id else result.none()
    area = request.GET.get("area", "").strip()[:80]
    if area:
        result = result.filter(area=area)
    result = apply_service_filters(result, request.member.tenant, request.GET, today=today)
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
    today = timezone.localdate()
    return render(request, "core/customers.html", {"page": paginate(request,
                  with_financial_summary(customer_query(request, today=today), request.member.tenant)),
                  "plans": request.member.tenant.plan_set.all(),
                  "devices": request.member.tenant.monitoringdevice_set.all(), "due_choices": DUE_CHOICES,
                  "today": today, "updated_at": timezone.now(),
                  "areas": Customer.objects.filter(tenant=request.member.tenant).values_list("area", flat=True).distinct().order_by("area")})


@require("edit_customers")
def customer_form(request, pk=None):
    customer = get_object_or_404(Customer, tenant=request.member.tenant, pk=pk) if pk else Customer()
    previous_nas = customer.nas_id
    form = CustomerForm(request.POST or None, instance=customer, tenant=request.member.tenant)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                Tenant.objects.select_for_update(no_key=True).get(pk=request.member.tenant_id)
                current = Customer.objects.select_for_update().get(pk=pk, tenant=request.member.tenant) if pk else None
                chosen = assignment_plan(request.member.tenant, form.cleaned_data["plan"].pk,
                    current.plan_id if current else None, lock=True)
                proposed = form.save(commit=False)
                item = current if current else Customer(tenant=request.member.tenant)
                for field in CustomerForm._meta.fields:
                    setattr(item, field, getattr(proposed, field))
                item.plan = chosen
                if not item.code:
                    item.code = "TN-" + uuid.uuid4().hex[:10].upper()
                item.save()
                audit(request.member.tenant, request.user, "customer.updated" if pk else "customer.created", item.pk,
                      f"Identitas atau layanan pelanggan disimpan. NAS: {previous_nas or 'belum dipetakan'} → {item.nas_id or 'belum dipetakan'}.", request.correlation_id)
            messages.success(request, "Pelanggan berhasil disimpan.")
            return redirect("customer_detail", pk=item.pk)
        except IntegrityError:
            form.add_error(None, "Username atau telepon sudah digunakan pelanggan lain pada ISP ini.")
        except ValidationError as error:
            form.add_error(None, error)
        except OperationalError:
            form.add_error(None, "Pelanggan belum disimpan. Muat ulang dan periksa paket sebelum mencoba lagi.")
    return render(request, "core/form.html", {"form": form, "title": "Ubah pelanggan" if pk else "Tambah pelanggan",
                  "description": "Identitas, layanan, dan wilayah pelanggan. Harga paket ditentukan di server.", "back": "/customers/"})


@require("customers")
@require_GET
def customer_detail(request, pk):
    query = with_financial_summary(Customer.objects.select_related("plan", "nas"), request.member.tenant)
    item = get_object_or_404(query, tenant=request.member.tenant, pk=pk)
    selected = request.GET.get("tab", "summary") or "summary"
    context, rows = profile_context(request.member, item, selected)
    context.update(today=timezone.localdate(), updated_at=timezone.now())
    size = request.GET.get("size", "10")
    context["profile_size"] = size if size in {"10", "25", "50"} else "10"
    if rows is not None:
        context["page"] = paginate(request, rows)
        if selected == "invoices":
            context["invoice_rows"] = invoice_rows(context["page"].object_list, context["today"])
    return render(request, "core/customer_detail.html", context)


@require("customers")
@require_GET
def customer_preview(request, pk):
    item=get_object_or_404(with_financial_summary(Customer.objects.select_related('plan','nas'),request.member.tenant),
        tenant=request.member.tenant,pk=pk)
    context,_=profile_context(request.member,item,'summary')
    context.update(today=timezone.localdate(),updated_at=timezone.now(),profile_size='10',
        customer_detail_url=reverse_lazy('customer_detail',args=[item.pk]))
    response=render(request,'customers/preview.html',context)
    response['Cache-Control']='private, no-store'
    response['X-Robots-Tag']='noindex, nofollow'
    return response


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
    result = with_balances(Invoice.objects.select_related('customer'),request.member.tenant)
    query = request.GET.get("q", "").strip()[:100]
    if query:
        result = result.filter(Q(customer__name__icontains=query) | Q(customer__code__icontains=query))
    state = request.GET.get("state", "")
    today=timezone.localdate()
    # Derived state is never accepted as a writable invoice field.
    if state=='paid':result=result.filter(remaining=0)
    elif state in {'overdue','issued','partial'}:
        result=result.exclude(remaining=0)
        if state=='overdue':result=result.filter(due_date__lt=today)
        else:
            result=result.filter(due_date__gte=today)
            result=result.filter(ledger_paid=0) if state=='issued' else result.exclude(ledger_paid=0)
    page=paginate(request,result)
    return render(request, "core/invoices.html", {"page":page,'invoice_rows':invoice_rows(page.object_list,today)})


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
    if not ({"invoices", "portal"} & capabilities(member)):
        raise PermissionDenied
    result = Invoice.objects.filter(tenant=member.tenant).select_related("customer", "tenant", "issuer_snapshot", "isolation_terms")
    if member.role == "customer":
        result = result.filter(customer=member.customer)
    return get_object_or_404(result, pk=pk)


def invoice_detail(request, pk):
    if not request.user.is_authenticated:
        return redirect("login")
    invoice = allowed_invoice(request, pk)
    from .document_identity import document_issuer
    from .isolation import invoice_grace_days
    return render(request, "core/invoice_detail.html", {"invoice": invoice, "issuer": document_issuer(invoice),
        "grace_days": invoice_grace_days(invoice),
        "payments": invoice.payments.select_related("recorded_by", "reversal"),
        "payment_form": PaymentForm(tenant=invoice.tenant,initial={"amount": invoice.balance, "event_key": uuid.uuid4()})})


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
    form = PaymentForm(request.POST,tenant=request.member.tenant)
    if form.is_valid():
        try:
            _, created = record_payment(request.member.tenant, invoice.pk, form.cleaned_data["amount"],
                form.cleaned_data["event_key"], request.user, form.cleaned_data["note"], request.correlation_id,
                fund_id=form.cleaned_data['fund'].pk if form.cleaned_data['fund'] else None)
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
    return render(request, "core/network.html", {"jobs": NetworkJob.objects.filter(tenant=request.member.tenant)
        .select_related("customer", "scheduled_isolation").order_by("-id")[:30]})


@require("audit")
def audit_log(request):
    return render(request, "core/audit.html", {"page": paginate(request, AuditEvent.objects.filter(tenant=request.member.tenant).select_related("actor"))})


@require("portal")
def portal(request):
    return render(request, "core/portal.html", {"customer": request.member.customer,
                  "invoices": Invoice.objects.filter(tenant=request.member.tenant, customer=request.member.customer),
                  "location":location_display(request.member.customer)})


@require_GET
def health(request):
    # No tenant data, account identifiers or infrastructure details.
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"status": "ok", "mode": "simulation" if settings.SIMULATION else "integrations-disabled"})
