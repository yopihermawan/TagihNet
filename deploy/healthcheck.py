"""Container health probe: verify local HTTPS certificate and the database-backed endpoint."""
import json
import ssl
from urllib.request import urlopen

try:
    with urlopen("https://localhost:8443/health/", context=ssl.create_default_context(cafile="/run/tagihnet/ca.crt"), timeout=5) as response:
        valid = response.status==200 and json.load(response)=={"status":"ok","mode":"simulation"}
except Exception:
    valid = False
raise SystemExit(0 if valid else 1)
