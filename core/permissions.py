from functools import wraps
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from .models import Membership

CAPABILITIES = {
    "owner": {"dashboard", "customers", "edit_customers", "invoices", "issue", "pay", "reverse", "network", "settings", "audit", "export"},
    "ops": {"dashboard", "customers", "edit_customers", "invoices", "issue", "export"},
    "finance": {"dashboard", "customers", "invoices", "pay", "reverse", "export"},
    "cashier": {"dashboard", "invoices", "pay"},
    "noc": {"network"},
    "customer": {"portal"},
}

def membership(user):
    try:
        member = user.membership
    except Membership.DoesNotExist:
        raise PermissionDenied("Akun belum memiliki akses ISP.")
    if member.role not in CAPABILITIES:
        raise PermissionDenied
    if member.role == "customer" and (not member.customer_id or member.customer.tenant_id != member.tenant_id):
        raise PermissionDenied
    return member

def require(capability):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            request.member = membership(request.user)
            if capability not in CAPABILITIES[request.member.role]:
                raise PermissionDenied("Anda tidak memiliki izin untuk tindakan ini.")
            return view(request, *args, **kwargs)
        return wrapped
    return decorator
