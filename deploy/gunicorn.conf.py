"""TLS is native, without a trusted proxy or request-URL logging."""
import ssl

bind = "0.0.0.0:8443"
workers = 2
worker_class = "sync"
timeout = 30
graceful_timeout = 30
max_requests = 1000
max_requests_jitter = 100
preload_app = False
accesslog = None
errorlog = "-"
loglevel = "warning"
forwarded_allow_ips = ""
secure_scheme_headers = {}
certfile = "/run/https/server.crt"
keyfile = "/run/https/server.key"
limit_request_line = 4094
limit_request_fields = 64


def ssl_context(conf, default_ssl_context_factory):
    context = default_ssl_context_factory()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context
