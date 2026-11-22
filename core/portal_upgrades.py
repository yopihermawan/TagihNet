"""Review a faster package and send an auditable request through the existing support queue."""
import uuid

from django.contrib import messages
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST

from .billing import retry_locked
from .models import Customer, Membership, Tenant, Ticket
from .operations import create_ticket, member_for
from .permissions import require
from .plan_selection import selectable_plans
from .plans import REVIEW_SECONDS, values_for
from .views import paginate


SALT = "tagihnet.portal-upgrade.v1"
TITLE = "Pengajuan upgrade paket"


def own_customer(member):
    return get_object_or_404(Customer.objects.select_related("plan"), pk=member.customer_id,
                             tenant_id=member.tenant_id, plan__tenant_id=member.tenant_id)


def eligible_plans(customer):
    return selectable_plans(customer.tenant_id).filter(speed_mbps__gt=customer.plan.speed_mbps)


def plan_snapshot(plan):
    return {"id": plan.pk, "version": plan.version, **values_for(plan)}


def ticket_description(current, proposed):
    def label(plan):
        price = f'{plan["price"]:,}'.replace(",", ".")
        return f'{plan["name"]} · {plan["speed_mbps"]} Mbps · Rp{price} per bulan'

    return (f"Paket saat ditinjau · {label(current)}\n"
            f"Paket yang diminta · {label(proposed)}\n"
            "Nominal di atas adalah harga katalog. Pengajuan menunggu penanganan operator.")


@retry_locked
@transaction.atomic
def submit_request(actor, token):
    try:
        quote = signing.loads(token, salt=SALT, max_age=REVIEW_SECONDS)
    except (signing.BadSignature, ValueError, TypeError):
        raise ValidationError("Tinjauan tidak valid atau sudah kedaluwarsa. Pilih paket dan tinjau kembali.") from None

    member = member_for(actor)
    Tenant.objects.select_for_update(no_key=True).get(pk=member.tenant_id)
    Membership.objects.select_for_update().get(pk=member.pk, user_id=actor.pk, tenant_id=member.tenant_id)
    member = member_for(actor)
    if (member.role != "customer" or quote["actor"] != actor.pk or quote["tenant"] != member.tenant_id
            or quote["customer"] != member.customer_id or quote["access_version"] != member.session_version):
        raise PermissionDenied("Tinjauan ini hanya berlaku untuk akun pelanggan yang membuatnya.")

    key = uuid.UUID(quote["request_key"])
    current, proposed = quote["current"], quote["proposed"]
    # The tenant lock serializes catalogue changes, assignments, and submissions.
    if not Ticket.objects.filter(tenant_id=member.tenant_id, request_key=key).exists():
        customer = own_customer(member)
        destination = eligible_plans(customer).filter(pk=proposed["id"]).first()
        if plan_snapshot(customer.plan) != current or destination is None or plan_snapshot(destination) != proposed:
            raise ValidationError("Paket atau harga telah berubah. Pilih paket dan tinjau kembali.")
    return create_ticket(actor, member.customer_id, TITLE, ticket_description(current, proposed), "billing",
                         request_key=key)


def render_options(request, *, error=None, status=200):
    customer = own_customer(request.member)
    packages = eligible_plans(customer).order_by("speed_mbps", "price", "name", "pk")
    return render(request, "core/portal_upgrade.html", {"customer": customer, "page": paginate(request, packages),
                  "error": error}, status=status)


@require("portal")
@require_GET
def options(request):
    return render_options(request)


@require("portal")
@require_GET
def review(request, pk):
    member = request.member
    customer = own_customer(member)
    destination = get_object_or_404(eligible_plans(customer), pk=pk)
    quote = {"actor": request.user.pk, "tenant": member.tenant_id, "access_version": member.session_version,
             "customer": customer.pk, "current": plan_snapshot(customer.plan), "proposed": plan_snapshot(destination),
             "request_key": str(uuid.uuid4())}
    token = signing.dumps(quote, salt=SALT, compress=True)
    return render(request, "core/portal_upgrade_review.html", {"quote": quote, "token": token})


@sensitive_post_parameters("quote")
@require("portal")
@require_POST
def confirm(request):
    token = request.POST.get("quote", "")
    try:
        ticket = submit_request(request.user, token)
    except Membership.DoesNotExist:
        raise PermissionDenied from None
    except ValidationError as exc:
        return render_options(request, error=exc.messages[0], status=400)
    except OperationalError:
        return render(request, "core/portal_upgrade_review.html", {
            "token": token, "error": "Pengajuan belum dapat dikonfirmasi. Coba kirim ulang konfirmasi yang sama."}, status=503)
    messages.success(request, "Pengajuan upgrade tercatat di tiket penagihan. Operator akan meninjau permintaan Anda.")
    return redirect("ticket_detail", pk=ticket.pk)
