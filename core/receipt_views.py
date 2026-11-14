from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .models import Payment
from .receipts import receipt_context, render_receipt
from .views import allowed_invoice


@login_required
@require_GET
@never_cache
def payment_receipt(request, invoice_pk, payment_pk, pdf=False):
    invoice = allowed_invoice(request, invoice_pk)
    payment = get_object_or_404(Payment.objects.select_related("recorded_by", "reversal__recorded_by"),
                               tenant=invoice.tenant, invoice=invoice, pk=payment_pk)
    payment.invoice = invoice
    context = receipt_context(payment, timezone.now())
    if not pdf:
        return render(request, "core/payment_receipt.html", context)
    response = HttpResponse(render_receipt(context), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{context["receipt_number"]}.pdf"'
    return response
