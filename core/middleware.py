import time
import uuid
from django.contrib.auth import logout
from .models import MFADevice, Membership

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
