"""Static compilation only; no staging credentials or database required."""
from .settings import *  # noqa: F403

STORAGES = {"default":{"BACKEND":"django.core.files.storage.FileSystemStorage"},
            "staticfiles":{"BACKEND":"whitenoise.storage.CompressedManifestStaticFilesStorage"}}
