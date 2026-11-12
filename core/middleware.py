import time
import uuid
import re
from django.contrib.auth import logout
from django.contrib.sessions.middleware import SessionMiddleware
from django.utils.cache import patch_vary_headers
from .models import MFADevice, Membership

BACKGROUND_ORDER_SNAPSHOT = re.compile(r"/payments/orders/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/snapshot/")


class ActivitySessionMiddleware(SessionMiddleware):
    def process_response(self, request, response):
        if getattr(request, "passive_session_read", False) and not request.session.modified:
            # SESSION_SAVE_EVERY_REQUEST remains in force for normal requests.
            # Logout/rotation or any actual session mutation must still save.
            if request.session.accessed:
                patch_vary_headers(response, ("Cookie",))
            return response
        return super().process_response(request, response)

class SessionPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.correlation_id = uuid.uuid4()
        if request.user.is_authenticated:
            now = time.time()
            member = Membership.objects.filter(user=request.user).first()
            device = MFADevice.objects.filter(user=request.user).first()
            revision = str(device.revision) if device else "none"
            authorized_session = bool(member and request.session.get("access_version") == member.session_version
                and request.session.get("mfa_revision") == revision
                and (device or member.role == "customer"))
            started = request.session.get("started_at", now)
            last_seen = request.session.get("last_seen", now)
            if not authorized_session or now - started > 28800 or now - last_seen > 1800:
                logout(request)
            elif request.method == "GET" and BACKGROUND_ORDER_SNAPSHOT.fullmatch(request.path_info):
                # Background reads still enforce expiry/revocation, but do not
                # renew idle activity or keep an unattended session alive.
                request.passive_session_read = True
            else:
                request.session["started_at"] = started
                request.session["last_seen"] = now
        return self.get_response(request)

class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'"
        response["Referrer-Policy"] = "same-origin"
        response["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response["X-Correlation-ID"] = str(request.correlation_id)
        if request.user.is_authenticated or request.path.startswith(("/login", "/reset", "/mfa", "/account")):
            response["Cache-Control"] = "no-store"
        return response
