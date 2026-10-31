import time
import uuid
from django.contrib.auth import logout

class SessionPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.correlation_id = uuid.uuid4()
        if request.user.is_authenticated:
            now = time.time()
            started = request.session.get("started_at", now)
            last_seen = request.session.get("last_seen", now)
            if now - started > 28800 or now - last_seen > 1800:
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
        if request.user.is_authenticated or request.path.startswith(("/login", "/reset")):
            response["Cache-Control"] = "no-store"
        return response
