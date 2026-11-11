"""Validate a configured origin without trusting browser Host/forwarded headers."""
from urllib.parse import urlsplit
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import URLValidator


def public_origin(value, *, debug):
    if value == "":
        return ""
    try:
        if not isinstance(value, str) or len(value) > 300 or any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError
        URLValidator(schemes=["https", "http"])(value)
        parsed = urlsplit(value)
        if (parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"} or parsed.port == 0 or "?" in value or "#" in value):
            raise ValueError
        if parsed.scheme != "https" and not (debug and parsed.scheme == "http"
                and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError
    except (ValueError, TypeError, ValidationError):
        raise ImproperlyConfigured("TAGIHNET_PUBLIC_ORIGIN harus origin HTTPS tanpa path, kredensial, query atau fragment. HTTP loopback hanya untuk DEBUG=1.") from None
    return value.rstrip("/")
