"""Production-style, isolated simulation. This profile never enables real integrations."""
import os
from django.core.exceptions import ImproperlyConfigured
from .deployment import valid_session_key, validate_staging

validate_staging(os.environ)
from .settings import *  # noqa: E402,F403

if not valid_session_key(SECRET_KEY):
    raise ImproperlyConfigured("Kunci sesi staging harus acak dan minimal 50 karakter.")
STAGING = True
MIDDLEWARE = list(MIDDLEWARE)
MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")
STORAGES = {"default":{"BACKEND":"django.core.files.storage.FileSystemStorage"},
            "staticfiles":{"BACKEND":"whitenoise.storage.CompressedManifestStaticFilesStorage"}}
# TLS terminates in Gunicorn. Never infer HTTPS from an untrusted forwarded header.
SECURE_PROXY_SSL_HEADER = None
USE_X_FORWARDED_HOST = False
SECURE_HSTS_SECONDS = 3600
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
# Mail delivery is deliberately inactive in this simulation profile; never log reset tokens.
EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
EMAIL_TIMEOUT = 10
