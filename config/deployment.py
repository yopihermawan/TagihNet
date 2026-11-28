"""Deployment setting validation without importing Django settings."""
import base64
import ipaddress
import re
from django.core.exceptions import ImproperlyConfigured


def valid_dns_name(host):
    return 0 < len(host) <= 253 and all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in host.split(".")
    )


def valid_session_key(value):
    return len(value) >= 50 and len(set(value)) >= 20 and not value.startswith("django-insecure-")


def valid_mfa_key(value):
    try:
        return len(base64.b64decode(value, altchars=b"-_", validate=True)) == 32
    except (ValueError, TypeError):
        return False


def smtp_port(environ):
    value = environ.get("TAGIHNET_EMAIL_PORT", "587")
    if not (value.isascii() and value.isdecimal() and len(value) <= 5 and 1 <= int(value) <= 65535):
        raise ImproperlyConfigured("TAGIHNET_EMAIL_PORT harus berupa nomor port 1–65535.")
    return int(value)


def validate_staging(environ):
    required = {"TAGIHNET_DEBUG":"0", "TAGIHNET_SIMULATION":"1",
                "TAGIHNET_DB_ENGINE":"postgresql", "TAGIHNET_DB_SSLMODE":"verify-full"}
    if any(environ.get(key)!=value for key,value in required.items()):
        raise ImproperlyConfigured("Staging memerlukan DEBUG=0, SIMULATION=1 dan PostgreSQL verify-full secara eksplisit.")
    if environ.get("TAGIHNET_SECURE_COOKIES", "1")!="1":
        raise ImproperlyConfigured("Cookie staging wajib Secure.")
    hosts = environ.get("TAGIHNET_ALLOWED_HOSTS", "").split(",")
    for raw in hosts:
        host = raw.strip()
        if not host or host!=raw or host.startswith(".") or "*" in host:
            raise ImproperlyConfigured("Staging memerlukan ALLOWED_HOSTS eksplisit tanpa wildcard.")
        try:
            ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            if not valid_dns_name(host):
                raise ImproperlyConfigured("Host staging tidak valid; gunakan nama host tanpa URL/port.")
    if not environ.get("TAGIHNET_DB_SSLROOTCERT"):
        raise ImproperlyConfigured("CA PostgreSQL staging wajib dikonfigurasi.")
