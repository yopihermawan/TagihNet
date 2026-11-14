"""Fail-closed staging configuration, before base settings can create development keys."""
import ipaddress
import re
from django.core.exceptions import ImproperlyConfigured


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
            if len(host)>253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",label) for label in host.split(".")):
                raise ImproperlyConfigured("Host staging tidak valid; gunakan nama host tanpa URL/port.")
    if not environ.get("TAGIHNET_DB_SSLROOTCERT"):
        raise ImproperlyConfigured("CA PostgreSQL staging wajib dikonfigurasi.")
