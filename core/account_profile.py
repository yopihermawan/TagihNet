"""Allowlisted display-name edits; signed form state prevents overwriting a changed profile."""
import hashlib
import hmac
import json
import re
import unicodedata

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from .billing import audit, retry_locked
from .models import MFADevice, Membership

SALT = "tagihnet.account.profile.v1"
MAX_AGE = 900


class ProfileConflict(ValidationError):
    def __init__(self):
        super().__init__("Profil atau sesi telah berubah. Muat ulang profil sebelum menyimpan.")


def clean_name(value):
    if not isinstance(value, str) or len(value) > 150:
        raise ValidationError("Nama paling banyak 150 karakter.")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
        raise ValidationError("Nama tidak boleh berisi karakter kontrol atau arah teks tersembunyi.")
    value = unicodedata.normalize("NFC", value.strip())
    if len(value) > 150:raise ValidationError("Nama paling banyak 150 karakter.")
    return value


def profile_digest(user):
    values = [user.pk, user.username, user.email, user.first_name, user.last_name]
    return hashlib.sha256(json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def session_digest(session_key):
    if not isinstance(session_key, str) or not 1 <= len(session_key) <= 128:
        raise PermissionDenied("Masuk kembali untuk mengubah profil.")
    return hashlib.sha256(session_key.encode()).hexdigest()


def review_token(user, member, session_key):
    if member.user_id != user.pk:raise PermissionDenied
    return signing.dumps({"v": 1, "user": user.pk, "tenant": member.tenant_id,
        "access": member.session_version, "profile": profile_digest(user), "session": session_digest(session_key)}, salt=SALT)


def read_token(token):
    try:
        if not isinstance(token, str) or len(token) > 2048:raise ValueError
        value = signing.loads(token, salt=SALT, max_age=MAX_AGE)
        if (not isinstance(value, dict) or set(value) != {"v", "user", "tenant", "access", "profile", "session"}
                or type(value["v"]) is not int or value["v"] != 1
                or any(type(value[key]) is not int or value[key] < 1 for key in ("user", "tenant", "access"))
                or any(not isinstance(value[key], str) or not re.fullmatch("[a-f0-9]{64}", value[key]) for key in ("profile", "session"))):
            raise ValueError
        return value
    except (signing.BadSignature, ValueError, TypeError):
        raise ValidationError("Formulir kedaluwarsa atau tidak valid. Muat ulang profil Anda.") from None


@retry_locked
@transaction.atomic
@sensitive_variables()
def save_profile(actor, token, first_name, last_name, *, session_key, access_version, mfa_revision,
                 auth_hash, correlation_id=None):
    proposal = read_token(token)
    first_name, last_name = clean_name(first_name), clean_name(last_name)
    try:
        member = Membership.objects.select_for_update().get(user_id=actor.pk)
        user = get_user_model().objects.select_for_update().get(pk=actor.pk, is_active=True)
    except (Membership.DoesNotExist, get_user_model().DoesNotExist):
        raise PermissionDenied("Akses akun berubah. Masuk kembali.") from None
    device = MFADevice.objects.filter(user=user).first()
    if (type(access_version) is not int or member.session_version != access_version
            or (str(device.revision) if device else "none") != mfa_revision
            or not isinstance(auth_hash, str) or not hmac.compare_digest(user.get_session_auth_hash(), auth_hash)
            or (not device and member.role != "customer")):
        raise PermissionDenied("Akses akun berubah. Masuk kembali.")
    if proposal["user"] != user.pk or proposal["tenant"] != member.tenant_id:raise PermissionDenied
    if (proposal["access"] != member.session_version or not hmac.compare_digest(proposal["session"], session_digest(session_key))
            or not hmac.compare_digest(proposal["profile"], profile_digest(user))):
        raise ProfileConflict
    if (first_name, last_name) == (user.first_name, user.last_name):return False
    user.first_name, user.last_name = first_name, last_name
    user.save(update_fields=["first_name", "last_name"])
    audit(member.tenant, user, "account.profile_updated", user.pk, "Nama tampilan diperbarui.", correlation_id)
    return True
