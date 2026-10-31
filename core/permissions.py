from functools import wraps
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from .models import Membership

CAPABILITIES = {
    "owner": {"dashboard", "customers", "edit_customers", "invoices", "issue", "bulk_issue", "pay", "reverse", "network", "settings", "audit", "export", "manage_access"},
    "ops": {"dashboard", "customers", "edit_customers", "invoices", "issue", "export"},
    "finance": {"dashboard", "customers", "invoices", "pay", "reverse", "export"},
    "cashier": {"dashboard", "invoices", "pay"},
    "noc": {"network"},
    "customer": {"portal"},
}

CAPABILITY_LABELS = {
    "dashboard": "Lihat dashboard billing", "customers": "Lihat pelanggan",
    "edit_customers": "Tambah/ubah pelanggan", "invoices": "Lihat faktur dan PDF",
    "issue": "Terbitkan faktur", "pay": "Catat pembayaran simulasi",
    "reverse": "Reversal pembayaran", "network": "Lihat antrean jaringan",
    "settings": "Ubah setelan billing", "audit": "Lihat audit", "export": "Ekspor pelanggan",
    "bulk_issue": "Terbitkan faktur massal",
}

def capabilities(member):
    if member.role in {"owner", "customer"} or not member.access_role_id:
        return CAPABILITIES[member.role].copy()
    if member.access_role.tenant_id != member.tenant_id:
        raise PermissionDenied
    return set(member.access_role.capabilities) & set(CAPABILITY_LABELS)

def membership(user):
    try:
        member = user.membership
    except Membership.DoesNotExist:
        raise PermissionDenied("Akun belum memiliki akses ISP.")
    if member.role not in CAPABILITIES:
        raise PermissionDenied
    if member.role == "customer" and (not member.customer_id or member.customer.tenant_id != member.tenant_id):
        raise PermissionDenied
    if member.access_role_id and (member.access_role.tenant_id != member.tenant_id or member.role in {"owner", "customer"}):
        raise PermissionDenied
    return member

def require(capability):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            request.member = membership(request.user)
            if capability not in capabilities(request.member):
                raise PermissionDenied("Anda tidak memiliki izin untuk tindakan ini.")
            return view(request, *args, **kwargs)
        return wrapped
    return decorator
