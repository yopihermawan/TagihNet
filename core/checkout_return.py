"""Only a bounded order UUID survives login rotation; never preserve arbitrary URLs."""
import time
import uuid
from django.urls import reverse

SESSION_KEY = "gateway_checkout_return"


def remember_checkout_return(request, order_id):
    request.session[SESSION_KEY] = {"order": str(uuid.UUID(str(order_id))), "created": time.time()}


def pending_checkout_return(request):
    value = request.session.get(SESSION_KEY)
    if not isinstance(value, dict):
        return None
    created, order = value.get("created"), value.get("order")
    if (not isinstance(created, (int, float)) or isinstance(created, bool)
            or not 0 <= time.time()-created <= 600 or not isinstance(order, str)):
        return None
    try:
        order = str(uuid.UUID(order))
    except (ValueError, AttributeError):
        return None
    return {"order": order, "created": created}


def resume_checkout(request):
    pending = pending_checkout_return(request)
    request.session.pop(SESSION_KEY, None)
    if pending:
        # The return view rechecks current tenant/customer permissions after login.
        return reverse("gateway_return", kwargs={"pk": pending["order"]})
    return None
