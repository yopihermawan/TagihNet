"""Bounded HTTPS client for Midtrans Sandbox only; credentials never reach the browser."""
import base64
import hashlib
import hmac
import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from django.conf import settings
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.views.decorators.debug import sensitive_variables


class GatewayUnavailable(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def json_object(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
    if not isinstance(value, dict):
        raise ValueError("object required")
    return value


def rupiah(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,12}(?:\.00)?", value):
        raise ValidationError("Nominal gateway tidak valid.")
    return int(value.split(".")[0])


def checkout_url(value):
    if not isinstance(value, str) or len(value) > 500:
        raise GatewayUnavailable
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        raise GatewayUnavailable from None
    if (parsed.scheme != "https" or parsed.netloc != "app.sandbox.midtrans.com"
            or not re.fullmatch(r"/snap/v[0-9]+/redirection/[a-zA-Z0-9-]+", parsed.path)
            or parsed.query or parsed.fragment):
        raise GatewayUnavailable
    return value


@sensitive_variables()
def credentials(tenant_id, merchant_id=None):
    if not settings.SIMULATION:
        raise ValidationError("Midtrans Sandbox hanya tersedia dalam mode simulasi.")
    entry = settings.MIDTRANS_SANDBOX_ACCOUNT_KEYS.get(str(tenant_id), {})
    if not isinstance(entry, dict):
        raise ValidationError("Kredensial sandbox ISP belum dikonfigurasi.")
    key, merchant = entry.get("server_key", ""), entry.get("merchant_id", "")
    if (not isinstance(key, str) or not key.startswith("SB-Mid-server-") or len(key) > 250
            or not isinstance(merchant, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", merchant)
            or (merchant_id is not None and not hmac.compare_digest(merchant, merchant_id))):
        raise ValidationError("Kredensial sandbox ISP belum sesuai dengan akun merchant.")
    # One merchant cannot be assigned to multiple ISP tenants.
    for tenant, other in settings.MIDTRANS_SANDBOX_ACCOUNT_KEYS.items():
        if str(tenant) != str(tenant_id) and isinstance(other, dict) and other.get("merchant_id") == merchant:
            raise ValidationError("Akun merchant sandbox harus khusus untuk satu ISP.")
    return merchant, key


@sensitive_variables()
def valid_signature(order, payload):
    _, key = credentials(order.tenant_id, order.merchant_id)
    fields = [payload.get(field) for field in ("order_id", "status_code", "gross_amount", "signature_key")]
    if not all(isinstance(field, str) for field in fields):
        return False
    order_id, code, amount, signature = fields
    if (order_id != order.order_id or not re.fullmatch(r"[0-9]{3}", code)
            or not re.fullmatch(r"[0-9a-fA-F]{128}", signature)):
        return False
    try:
        if rupiah(amount) != order.amount:
            return False
    except ValidationError:
        return False
    expected = hashlib.sha512((order_id + code + amount + key).encode()).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


class SandboxClient:
    @sensitive_variables()
    def request(self, order, method, payload=None, *, action=None):
        if action not in {None, "expire"} or method not in {"GET", "POST"} or (action and method != "POST"):
            raise ValueError("Unsupported sandbox operation")
        _, key = credentials(order.tenant_id, order.merchant_id)
        url = (f"https://api.sandbox.midtrans.com/v2/{order.order_id}/expire" if action == "expire"
            else "https://app.sandbox.midtrans.com/snap/v1/transactions" if method == "POST"
            else f"https://api.sandbox.midtrans.com/v2/{order.order_id}/status")
        auth = base64.b64encode((key + ":").encode()).decode()
        request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": f"Basic {auth}", "Accept": "application/json", "Content-Type": "application/json"}, method=method)
        try:
            # Inherits session proxy and CA trust; refuses redirects, including to production.
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
                if response.status != 200 and not (method == "POST" and response.status == 201):
                    raise GatewayUnavailable
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise GatewayUnavailable
                return json_object(raw)
        except (OSError, ValueError, RecursionError, http.client.HTTPException, urllib.error.URLError):
            raise GatewayUnavailable("Midtrans Sandbox belum dapat dihubungi. Periksa kembali status order yang sama.") from None

    def create(self, order):
        payload = {"transaction_details": {"order_id": order.order_id, "gross_amount": order.amount},
            "credit_card": {"secure": True},
            "item_details": [{"id": str(order.invoice_id), "price": order.amount, "quantity": 1,
                "name": "Tagihan TagihNet Sandbox"}],
            "expiry": {"unit": "hours", "duration": 24}}
        if settings.PUBLIC_ORIGIN:
            payload["callbacks"] = {"finish": settings.PUBLIC_ORIGIN + reverse("gateway_return", kwargs={"pk": order.pk})}
        response = self.request(order, "POST", payload)
        # Do not persist or log the Snap token; the private checkout URL is sufficient.
        return checkout_url(response.get("redirect_url"))

    def status(self, order):
        return self.request(order, "GET")

    def expire(self, order):
        # The response to this write is never used to release the local reservation.
        return self.request(order, "POST", {}, action="expire")
