"""Explicit WGS84 coordinates. No geocoder, browser tracking or map requests."""
import uuid
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .billing import audit, retry_locked
from .models import Customer, Installation, MFADevice, Membership, Tenant
from .operations import event, member_for, visible_installations
from .permissions import capabilities

SALT = "tagihnet.location.v1"
MAX_TOKEN = 4096


class LocationConflict(ValidationError):
    pass


@sensitive_variables()
def coordinate(value, limit):
    if value is None or value == "":
        return None
    try:
        if type(value) not in (str, int, Decimal) or (isinstance(value,str) and len(value)>24):
            raise ValueError
        result = Decimal(value)
        if (not result.is_finite() or not -limit <= result <= limit
                or result.as_tuple().exponent < -6):
            raise ValueError
        return (result if result else Decimal(0)).quantize(Decimal("0.000001"))
    except (InvalidOperation,ValueError,TypeError):
        raise ValidationError(f"Gunakan koordinat -{limit} sampai {limit} dengan paling banyak 6 angka desimal.") from None


@sensitive_variables()
def pair(latitude, longitude):
    values = coordinate(latitude,90), coordinate(longitude,180)
    if (values[0] is None) != (values[1] is None):
        raise ValidationError("Isi lintang dan bujur bersama, atau kosongkan keduanya.")
    return values


@sensitive_variables()
def strings(latitude, longitude):
    return [format(value,".6f") if value is not None else None for value in pair(latitude,longitude)]


@sensitive_variables()
def display(item):
    values = strings(item.latitude,item.longitude)
    present = values[0] is not None
    query = {"mlat":values[0],"mlon":values[1]} if present else {"query":item.address}
    url = "https://www.openstreetmap.org/" + ("?" if present else "search?") + urlencode(query)
    if present:
        url += f"#map=18/{values[0]}/{values[1]}"
    return {"present":present,"latitude":values[0],"longitude":values[1],"url":url}


def target(member, kind, pk, *, lock=False):
    if kind == "customer":
        if not {"customers","edit_customers"} <= capabilities(member):
            raise PermissionDenied("Izin mengubah lokasi pelanggan tidak tersedia.")
        query = Customer.objects.filter(tenant_id=member.tenant_id)
    elif kind == "installation":
        query = visible_installations(member)
    else:
        raise PermissionDenied("Jenis lokasi tidak tersedia.")
    if lock:
        query = query.select_for_update(of=("self",))
    item = query.get(pk=pk)
    if kind == "installation" and item.status in {"activated","cancelled"}:
        raise PermissionDenied("Lokasi PSB yang sudah selesai tidak dapat diubah. Kelola lokasi pelanggan melalui akun yang berwenang.")
    return item


def device_revision(actor):
    value = MFADevice.objects.filter(user_id=actor.pk).values_list("revision",flat=True).first()
    if value is None:
        raise PermissionDenied("MFA operator diperlukan.")
    return str(value)


@sensitive_variables()
def form_token(actor, kind, pk):
    member = member_for(actor)
    item = target(member,kind,pk)
    payload = {"v":1,"actor":actor.pk,"tenant":member.tenant_id,"access":member.session_version,
        "mfa":device_revision(actor),"kind":kind,"object":item.pk,"before":strings(item.latitude,item.longitude),
        "version":item.version if kind=="installation" else None,
        "assigned":item.assigned_to_id if kind=="installation" else None}
    return signing.dumps(payload,salt=SALT), item


@sensitive_variables()
def read_token(actor, kind, pk, token):
    member = member_for(actor)
    try:
        if not isinstance(token,str) or len(token)>MAX_TOKEN:
            raise ValueError
        value = signing.loads(token,salt=SALT,max_age=900)
        if (not isinstance(value,dict) or set(value)!={"v","actor","tenant","access","mfa","kind","object","before","version","assigned"}
                or type(value["v"]) is not int or value["v"]!=1
                or value["kind"] not in {"customer","installation"}
                or any(type(value[key]) is not int or value[key]<1 for key in ("actor","tenant","access","object"))
                or not isinstance(value["before"],list) or len(value["before"])!=2
                or strings(*value["before"])!=value["before"]
                or not isinstance(value["mfa"],str) or str(uuid.UUID(value["mfa"]))!=value["mfa"]
                or (value["kind"]=="customer" and (value["version"] is not None or value["assigned"] is not None))
                or (value["kind"]=="installation" and (type(value["version"]) is not int or value["version"]<1))
                or (value["assigned"] is not None and (type(value["assigned"]) is not int or value["assigned"]<1))):
            raise ValueError
    except (signing.BadSignature,ValueError,TypeError,KeyError,AttributeError,ValidationError):
        raise ValidationError("Form lokasi tidak valid atau kedaluwarsa. Muat ulang lokasi.") from None
    if (value["actor"]!=actor.pk or value["tenant"]!=member.tenant_id or value["access"]!=member.session_version
            or value["mfa"]!=device_revision(actor) or value["kind"]!=kind or value["object"]!=pk):
        raise PermissionDenied("Form lokasi bukan milik akun, objek dan versi akses/MFA saat ini.")
    return value


@retry_locked
@transaction.atomic
@sensitive_variables()
def save_location(actor, kind, pk, token, latitude, longitude, access_version, correlation_id=None):
    initial = member_for(actor)
    Tenant.objects.select_for_update(no_key=True).get(pk=initial.tenant_id)
    Membership.objects.select_for_update(no_key=True).get(user_id=actor.pk,tenant_id=initial.tenant_id)
    member = member_for(actor)
    if type(access_version) is not int or member.session_version!=access_version:
        raise PermissionDenied("Versi akses telah berubah.")
    item = target(member,kind,pk,lock=True)
    payload = read_token(actor,kind,pk,token)
    if (strings(item.latitude,item.longitude)!=payload["before"] or (kind=="installation"
            and (item.version!=payload["version"] or item.assigned_to_id!=payload["assigned"]))):
        raise LocationConflict("Lokasi atau PSB berubah sejak form dibuka. Muat ulang dan periksa lokasi terbaru.")
    values = pair(latitude,longitude)
    if strings(*values)==payload["before"]:
        return False
    item.latitude,item.longitude = values
    fields = ["latitude","longitude"]
    if kind=="installation":
        item.version+=1;item.updated_at=timezone.now();fields += ["version","updated_at"]
    item.save(update_fields=fields)
    detail = "Koordinat lokasi dikosongkan." if values[0] is None else "Koordinat lokasi diperbarui; input pengguna, bukan hasil verifikasi otomatis."
    if kind=="installation":
        event(member,item,"location",detail)
    else:
        audit(member.tenant,actor,"customer.location_updated",item.pk,detail,correlation_id)
    return True
