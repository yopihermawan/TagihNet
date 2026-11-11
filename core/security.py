"""MFA and bounded authentication attempts. Never log factors or provisioning data."""
import base64
import hashlib
import hmac
import io
import re
import secrets
import time
import uuid
from datetime import timedelta
import pyotp
import qrcode
from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, transaction
from django.db.models import F
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from .billing import audit, retry_locked
from .checkout_return import SESSION_KEY, pending_checkout_return
from .models import AuthRateBucket, MFADevice, Membership


@sensitive_variables()
def encrypt_secret(secret):
    return Fernet(settings.MFA_ENCRYPTION_KEY.encode()).encrypt(secret.encode()).decode()


@sensitive_variables()
def decrypt_secret(encrypted):
    return Fernet(settings.MFA_ENCRYPTION_KEY.encode()).decrypt(encrypted.encode()).decode()


class RateLimited(Exception):
    pass


@retry_locked
@transaction.atomic
def reserve_attempt(keys):
    now = timezone.now()
    for key in sorted(set(keys)):
        AuthRateBucket.objects.get_or_create(key=key, defaults={"expires_at": now + timedelta(minutes=15)})
        AuthRateBucket.objects.filter(key=key, expires_at__lte=now).update(attempts=0, expires_at=now + timedelta(minutes=15))
        updated = AuthRateBucket.objects.filter(key=key, attempts__lt=5).update(attempts=F("attempts") + 1)
        if not updated:
            raise RateLimited


def throttle(request, scope, identity=None):
    identity = identity if identity is not None else request.POST.get("username", request.POST.get("email", ""))
    keys = [hashlib.sha256(f"{scope}:{kind}:{value}".encode()).hexdigest() for kind, value in [
        ("ip", request.META.get("REMOTE_ADDR", "unknown")), ("identity", str(identity).strip().lower()[:254])]]
    try:
        reserve_attempt(keys)
        return False
    except (RateLimited, OperationalError):
        return True  # fail closed if the shared limiter cannot reserve an attempt


def remember_password(request, user):
    checkout = pending_checkout_return(request)
    request.session.flush()
    if checkout:
        request.session[SESSION_KEY] = checkout
    request.session["pending_auth"] = {
        "uid": user.pk, "auth_hash": user.get_session_auth_hash(), "started": time.time(),
        "version": user.membership.session_version,
    }


def pending_user(request):
    pending = request.session.get("pending_auth", {})
    if time.time() - pending.get("started", 0) > 300:
        return None
    user = get_user_model().objects.filter(pk=pending.get("uid"), is_active=True).first()
    if not user or not hmac.compare_digest(user.get_session_auth_hash(), pending.get("auth_hash", "")):
        return None
    member = Membership.objects.filter(user=user).first()
    if not member or member.session_version != pending.get("version"):
        return None
    return user


def complete_login(request, user, allow_version_change=False):
    # Flush pending secret/factor state and rotate the session only after MFA succeeds.
    pending = request.session.get("pending_auth")
    member = Membership.objects.select_related("user").get(user=user)
    if not member.user.is_active or (pending and (
        not hmac.compare_digest(member.user.get_session_auth_hash(), pending.get("auth_hash", ""))
        or (not allow_version_change and member.session_version != pending.get("version"))
    )):
        request.session.flush()
        raise PermissionDenied("Akses berubah. Mulai ulang login.")
    user = member.user
    checkout = pending_checkout_return(request)
    request.session.flush()
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    if checkout:
        request.session[SESSION_KEY] = checkout
    device = MFADevice.objects.filter(user=user).first()
    request.session.update({"started_at": time.time(), "last_seen": time.time(),
        "access_version": member.session_version,
        "mfa_revision": str(device.revision) if device else "none",
        "mfa_verified_at": time.time() if device else 0})
    audit(user.membership.tenant, user, "auth.login", user.pk, correlation_id=request.correlation_id)


def recovery_digest(code):
    return hashlib.sha256(code.replace("-", "").replace(" ", "").lower().encode()).hexdigest()


@sensitive_variables()
def new_recovery_codes():
    raw = [secrets.token_hex(16).upper() for _ in range(10)]
    codes = ["-".join(code[index:index+8] for index in range(0, 32, 8)) for code in raw]
    return codes, [recovery_digest(code) for code in codes]


@sensitive_variables()
def matching_step(secret, code, last_step):
    if not re.fullmatch(r"[0-9]{6}", code):
        return None
    step = int(time.time()) // 30
    for candidate in [step, step - 1, step + 1]:
        if candidate > last_step and pyotp.TOTP(secret).verify(code, for_time=candidate * 30):
            return candidate
    return None


@retry_locked
@transaction.atomic
@sensitive_variables()
def verify_factor(user, code):
    device = MFADevice.objects.select_for_update().filter(user=user).first()
    if not device:
        return False
    step = matching_step(decrypt_secret(device.encrypted_secret), code.strip(), device.last_step)
    if step is not None:
        # Conditional update matters on SQLite, where select_for_update is not a row lock.
        return bool(MFADevice.objects.filter(pk=device.pk, last_step__lt=step, revision=device.revision).update(last_step=step))
    if not re.fullmatch(r"[0-9a-fA-F -]{32,40}", code):
        return False
    digest = recovery_digest(code)
    remaining = [item for item in device.recovery_hashes if not hmac.compare_digest(item, digest)]
    if len(remaining) == len(device.recovery_hashes):
        return False
    return bool(MFADevice.objects.filter(pk=device.pk, recovery_hashes=device.recovery_hashes,
        revision=device.revision).update(recovery_hashes=remaining))


@retry_locked
@transaction.atomic
@sensitive_variables()
def confirm_enrollment(user, encrypted, code, correlation_id=None):
    Membership.objects.select_for_update().get(user=user)
    if MFADevice.objects.filter(user=user).exists():
        raise ValidationError("MFA sudah aktif. Secret tidak akan ditimpa.")
    step = matching_step(decrypt_secret(encrypted), code.strip(), -1)
    if step is None:
        raise ValidationError("Kode tidak valid atau kedaluwarsa. Periksa waktu authenticator.")
    codes, hashes = new_recovery_codes()
    MFADevice.objects.create(user=user, encrypted_secret=encrypted, last_step=step, recovery_hashes=hashes)
    Membership.objects.filter(user=user).update(session_version=F("session_version") + 1)
    audit(user.membership.tenant, user, "auth.mfa_enabled", user.pk, correlation_id=correlation_id)
    return codes


@retry_locked
@transaction.atomic
@sensitive_variables()
def regenerate_codes(user, correlation_id=None):
    device = MFADevice.objects.select_for_update().get(user=user)
    codes, hashes = new_recovery_codes()
    device.recovery_hashes = hashes
    device.revision = uuid.uuid4()
    device.save(update_fields=["recovery_hashes", "revision"])
    Membership.objects.filter(user=user).update(session_version=F("session_version") + 1)
    audit(user.membership.tenant, user, "auth.recovery_regenerated", user.pk, correlation_id=correlation_id)
    return codes


@sensitive_variables()
def provisioning(secret, user):
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.username, issuer_name="TagihNet")
    image = qrcode.make(uri)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()
