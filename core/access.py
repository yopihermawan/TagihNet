"""Only ISP owners may manage access. Lock tenant to serialize last-owner checks."""
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F
from .billing import audit, retry_locked
from .models import AccessRole, Customer, Membership, Tenant
from .permissions import CAPABILITIES, CAPABILITY_LABELS


def owner_for_update(actor):
    initial = Membership.objects.get(user=actor)
    tenant = Tenant.objects.select_for_update().get(pk=initial.tenant_id)
    member = Membership.objects.select_for_update().get(user=actor, tenant=tenant)
    if not member.user.is_active or member.role != "owner":
        raise PermissionDenied
    return member


@retry_locked
@transaction.atomic
def save_role(actor, name, rights, role_id=None, correlation_id=None):
    owner = owner_for_update(actor)
    rights = set(rights)
    if not rights <= set(CAPABILITY_LABELS) or not rights <= CAPABILITIES[owner.role]:
        raise PermissionDenied("Izin di luar kewenangan pemilik.")
    dependencies = {"edit_customers": {"customers"}, "issue": {"customers", "invoices"},
        "pay": {"invoices"}, "reverse": {"invoices"}, "export": {"customers"},
        "bulk_issue": {"issue", "invoices", "customers"},
        "activate_installations": {"installations", "edit_customers", "issue", "invoices", "customers"},
        "reminders": {"tickets", "invoices", "customers"}, "manage_monitoring": {"network"}}
    for capability, prerequisites in dependencies.items():
        if capability in rights and not prerequisites <= rights:
            raise ValidationError(f"Izin {capability} memerlukan: {', '.join(sorted(prerequisites))}.")
    if not name.strip() or len(name) > 80:
        raise ValidationError("Nama peran harus 1–80 karakter.")
    role = AccessRole.objects.get(pk=role_id, tenant=owner.tenant) if role_id else AccessRole(tenant=owner.tenant)
    before = set(role.capabilities)
    role.name, role.capabilities = name.strip(), sorted(rights)
    role.full_clean()
    role.save()
    if before != rights:
        Membership.objects.filter(tenant=owner.tenant, access_role=role).update(session_version=F("session_version") + 1)
    audit(owner.tenant, actor, "access.role_saved", role.pk, ", ".join(sorted(rights)) or "Tanpa izin bisnis", correlation_id)
    return role


@retry_locked
@transaction.atomic
def save_member(actor, target_id, role, access_role_id, customer_id, active, correlation_id=None):
    owner = owner_for_update(actor)
    target = Membership.objects.select_for_update().select_related("user").get(pk=target_id, tenant=owner.tenant)
    if role not in CAPABILITIES:
        raise ValidationError("Preset peran tidak valid.")
    if role in {"owner", "customer", "technician"} and access_role_id:
        raise ValidationError("Owner, pelanggan, dan teknisi memakai preset dengan scope khusus; peran kustom tidak tersedia.")
    if access_role_id:
        AccessRole.objects.get(pk=access_role_id, tenant=owner.tenant)
    if role == "customer":
        if not customer_id:
            raise ValidationError("Pilih pelanggan untuk akun portal.")
        Customer.objects.get(pk=customer_id, tenant=owner.tenant)
    elif customer_id:
        raise ValidationError("Relasi pelanggan hanya boleh diisi untuk akun portal.")
    if target.role == "owner" and target.user.is_active and (role != "owner" or not active):
        other_owners = Membership.objects.filter(tenant=owner.tenant, role="owner", user__is_active=True).exclude(pk=target.pk)
        if not other_owners.exists():
            raise ValidationError("Pemilik aktif terakhir tidak boleh dinonaktifkan atau diturunkan perannya.")
    before = f"{target.role}; aktif={target.user.is_active}; role_id={target.access_role_id}"
    target.role, target.access_role_id, target.customer_id = role, access_role_id, customer_id
    target.session_version = F("session_version") + 1
    target.save(update_fields=["role", "access_role", "customer", "session_version"])
    target.user.is_active = active
    target.user.save(update_fields=["is_active"])
    audit(owner.tenant, actor, "access.member_changed", target.pk,
        f"{before} → {role}; aktif={active}; role_id={access_role_id}", correlation_id)
    return target


@retry_locked
@transaction.atomic
def create_member(actor, username, email, password, role, access_role_id=None, customer_id=None, correlation_id=None):
    owner = owner_for_update(actor)
    user = get_user_model().objects.create_user(username=username, email=email, password=password)
    member = Membership.objects.create(user=user, tenant=owner.tenant, role="ops")
    save_member(actor, member.pk, role, access_role_id, customer_id, True, correlation_id)
    audit(owner.tenant, actor, "access.member_created", member.pk, "Akun baru dibuat; MFA wajib untuk operator.", correlation_id)
    return member
