from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_GET
from .bulk_billing import cancel_batch, confirm_batch, create_preview
from .bulk_forms import BatchConfirmationForm, BatchPreviewForm
from .models import BillingBatch
from .permissions import require
from .security_views import recent_factor
from .views import paginate


def batches_for(request):
    # A batch is private to its creator as well as its tenant, even for another owner.
    return BillingBatch.objects.filter(tenant=request.member.tenant, created_by=request.user)


@require("bulk_issue")
@require_GET
def batch_list(request):
    batches = batches_for(request).annotate(item_count=Count("items")).order_by("-created_at", "-pk")
    return render(request, "billing/batches.html", {"page": paginate(request, batches)})


@require("bulk_issue")
@require_http_methods(["GET", "POST"])
def batch_create(request):
    form = BatchPreviewForm(request.POST or None, tenant=request.member.tenant,
        initial={"period": timezone.localdate().replace(day=1), "state": "active", "discount": 0})
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        filters = {"state": data["state"], "plan": data["plan"].pk if data["plan"] else None, "area": data["area"]}
        try:
            batch = create_preview(request.user, data["period"], filters, data["discount"], request.correlation_id)
        except ValidationError as exc:
            form.add_error(None, exc)
        except OperationalError:
            form.add_error(None, "Database sedang sibuk. Coba lagi untuk membuat pratinjau.")
        else:
            return redirect("billing_batch_detail", pk=batch.pk)
    return render(request, "core/form.html", {"form": form, "title": "Pratinjau faktur massal", "submit_label": "Buat pratinjau", "back": "/invoices/batches/",
        "description": "Belum ada faktur diterbitkan. Pilih maksimal 500 pelanggan; filter status default Aktif. Faktur periode yang sudah ada akan dilewati. Diskon berlaku per faktur baru dan tidak boleh mencapai harga paket."})


def detail_response(request, batch, form=None, status=200):
    items = batch.items.select_related("customer", "invoice")
    new_footer = None
    summary = {"selected": 0, "new": 0, "created": 0, "skipped": 0, "total": 0, "issued_total": 0}
    for item in items:
        summary["selected"] += 1
        if item.status == "skipped":
            summary["skipped"] += 1
        elif item.status == "created":
            new_footer = item.snapshot["invoice"]["footer"]
            summary["created"] += 1
            summary["issued_total"] += item.snapshot["invoice"]["total"]
        else:
            new_footer = item.snapshot["invoice"]["footer"]
            summary["new"] += 1
            summary["total"] += item.snapshot["invoice"]["total"]
    return render(request, "billing/batch_detail.html", {"batch": batch, "page": paginate(request, items),
        "summary": summary, "new_footer": new_footer, "confirmation_form": form or BatchConfirmationForm(),
        "can_confirm": batch.status == "preview" and batch.expires_at >= timezone.now(),
        "can_cancel": batch.status in {"preview", "queued", "processing", "blocked"}}, status=status)


@require("bulk_issue")
@require_GET
def batch_detail(request, pk):
    return detail_response(request, get_object_or_404(batches_for(request), pk=pk))


@require("bulk_issue")
@require_POST
def batch_confirm(request, pk):
    batch = get_object_or_404(batches_for(request), pk=pk)
    if not recent_factor(request, return_to=reverse("billing_batch_detail", args=[pk])):
        return redirect("account_security")
    form = BatchConfirmationForm(request.POST)
    if not form.is_valid():
        return detail_response(request, batch, form, status=400)
    try:
        batch = confirm_batch(request.user, pk, request.session["mfa_verified_at"], request.session["access_version"])
    except OperationalError:
        form.add_error(None, "Database sedang sibuk. Kirim ulang konfirmasi yang sama; batch tidak digandakan.")
        return detail_response(request, batch, form, status=409)
    if batch.status in {"stale", "expired"}:
        messages.error(request, batch.reason)
    elif batch.status in {"queued", "processing", "completed"}:
        messages.success(request, "Konfirmasi tersimpan. Worker hanya menerbitkan faktur yang belum ada.")
    return redirect("billing_batch_detail", pk=pk)


@require("bulk_issue")
@require_POST
def batch_cancel(request, pk):
    get_object_or_404(batches_for(request), pk=pk)
    try:
        cancel_batch(request.user, pk)
    except OperationalError:
        messages.error(request, "Database sedang sibuk. Muat ulang status sebelum mencoba pembatalan lagi.")
    return redirect("billing_batch_detail", pk=pk)
