"""Review billing/issuer settings without overwriting unrelated tenant configuration."""
import hashlib
import json
import time
import unicodedata
import uuid

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from .billing import audit, retry_locked
from .isolation import GRACE_FIELD, configured_grace_days, validate_grace_days
from .models import MFADevice, Membership, Tenant, TenantIsolationPolicy
from .operations import member_for
from .permissions import capabilities

TENANT_FIELDS = ("name", "address", "due_day", "tax_bp", "invoice_footer")
FIELDS = (*TENANT_FIELDS, GRACE_FIELD)
SALT = "tagihnet.billing-settings.v1"
TOKEN_VERSION = 2
MAX_TOKEN = 65536
SECONDS = 900


class SettingsConflict(ValidationError):
    pass


def manager(actor):
    member = member_for(actor)
    if "settings" not in capabilities(member):
        raise PermissionDenied("Izin mengubah setelan billing tidak tersedia.")
    return member


def values_for(tenant):
    return {**{field: getattr(tenant, field) for field in TENANT_FIELDS},
            GRACE_FIELD: configured_grace_days(tenant)}


def fingerprint(values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


def clean_text(value, limit, *, multiline=False, required=False):
    if not isinstance(value, str):
        raise ValidationError("Isi harus berupa teks.")
    value = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} and not (multiline and char == "\n") for char in value):
        raise ValidationError("Karakter kontrol atau format tersembunyi tidak diizinkan.")
    value = value.strip()
    if len(value) > limit or (required and not value):
        raise ValidationError(f"Isi harus {'1' if required else '0'}–{limit} karakter.")
    return value


@sensitive_variables()
def validate_values(values):
    if not isinstance(values, dict) or set(values) != set(FIELDS):
        raise ValidationError("Field setelan billing tidak valid.")
    result = {}
    errors = {}
    for field, limit, multiline, required in (("name",100,False,True),("address",250,True,True),
            ("invoice_footer",1000,True,False)):
        try:
            result[field] = clean_text(values[field], limit, multiline=multiline, required=required)
        except ValidationError as error:
            errors[field] = error.messages
    for field, lower, upper in (("due_day",1,31),("tax_bp",0,10000)):
        if type(values[field]) is not int or not lower <= values[field] <= upper:
            errors[field] = [f"Gunakan bilangan bulat {lower}–{upper}."]
        else:
            result[field] = values[field]
    try:
        result[GRACE_FIELD] = validate_grace_days(values[GRACE_FIELD])
    except ValidationError as error:
        errors[GRACE_FIELD] = error.messages
    if errors:
        raise ValidationError(errors)
    return result


def device_revision(actor):
    revision = MFADevice.objects.filter(user_id=actor.pk).values_list("revision", flat=True).first()
    if revision is None:
        raise PermissionDenied("MFA operator diperlukan untuk mengubah setelan billing.")
    return str(revision)


def form_token(actor):
    member = manager(actor)
    return signing.dumps({"v":TOKEN_VERSION, "kind":"form", "actor":actor.pk, "tenant":member.tenant_id,
        "access":member.session_version, "mfa":device_revision(actor),
        "state":fingerprint(values_for(member.tenant))}, salt=SALT)


@sensitive_variables()
def read_token(actor, token, kind):
    member = manager(actor)
    try:
        if not isinstance(token, str) or len(token) > MAX_TOKEN:
            raise ValueError
        value = signing.loads(token, salt=SALT, max_age=SECONDS)
        fields = {"v", "kind", "actor", "tenant", "access", "mfa", "state"}
        if kind == "review":
            fields |= {"before", "after"}
        if (kind not in {"form", "review"} or not isinstance(value, dict) or set(value) != fields
                or type(value["v"]) is not int or value["v"] != TOKEN_VERSION or value["kind"] != kind
                or any(type(value[key]) is not int or value[key] < 1 for key in ("actor","tenant","access"))
                or not isinstance(value["state"],str) or len(value["state"]) != 64
                or any(char not in "0123456789abcdef" for char in value["state"])
                or not isinstance(value["mfa"],str) or str(uuid.UUID(value["mfa"])) != value["mfa"]):
            raise ValueError
        if kind == "review":
            before = value["before"]
            if (not isinstance(before,dict) or set(before) != set(FIELDS)
                    or any(not isinstance(before[key],str) for key in ("name","address","invoice_footer"))
                    or any(type(before[key]) is not int for key in ("due_day","tax_bp"))
                    or fingerprint(before) != value["state"] or validate_values(value["after"]) != value["after"]):
                raise ValueError
            validate_grace_days(before[GRACE_FIELD])
    except (signing.BadSignature, ValueError, TypeError, KeyError, AttributeError, ValidationError):
        raise ValidationError("Form atau tinjauan setelan tidak valid atau kedaluwarsa. Muat ulang setelan billing.") from None
    if (value["actor"] != actor.pk or value["tenant"] != member.tenant_id
            or value["access"] != member.session_version or value["mfa"] != device_revision(actor)):
        raise PermissionDenied("Form atau tinjauan bukan milik akun dan versi akses/MFA saat ini.")
    return value


@sensitive_variables()
def review_change(actor, token, values):
    payload = read_token(actor, token, "form")
    member = manager(actor)
    before = values_for(member.tenant)
    if fingerprint(before) != payload["state"]:
        raise SettingsConflict("Setelan berubah sejak form dibuka. Muat ulang dan tinjau lagi.")
    proposal = {**payload, "kind":"review", "before":before, "after":validate_values(values)}
    return signing.dumps(proposal, salt=SALT), proposal


@retry_locked
@transaction.atomic
@sensitive_variables()
def confirm_change(actor, token, verified_at, access_version, correlation_id=None):
    member = manager(actor)
    tenant = Tenant.objects.select_for_update(no_key=True).get(pk=member.tenant_id)
    Membership.objects.select_for_update().get(user_id=actor.pk, tenant_id=tenant.pk)
    member = manager(actor)
    proposal = read_token(actor, token, "review")
    if (type(access_version) is not int or member.session_version != access_version
            or type(verified_at) not in (int,float) or not 0 <= time.time()-verified_at <= 300):
        raise PermissionDenied("Verifikasi MFA terbaru diperlukan untuk menyimpan setelan billing.")
    current = values_for(tenant)
    if current != proposal["before"]:
        raise SettingsConflict("Setelan berubah sejak tinjauan. Muat ulang dan tinjau lagi.")
    after = validate_values(proposal["after"])
    changed = [field for field in FIELDS if current[field] != after[field]]
    if not changed:
        return False
    tenant_changes = [field for field in changed if field in TENANT_FIELDS]
    for field in tenant_changes:
        setattr(tenant, field, after[field])
    if tenant_changes:
        tenant.save(update_fields=tenant_changes)
    if GRACE_FIELD in changed:
        TenantIsolationPolicy.objects.update_or_create(tenant=tenant, defaults={'grace_days': after[GRACE_FIELD]})
    audit(tenant, actor, "billing.settings_updated", tenant.pk,
        "Field setelan diperbarui: "+", ".join(changed)+". Ledger dan snapshot disetujui tetap.", correlation_id)
    return True
