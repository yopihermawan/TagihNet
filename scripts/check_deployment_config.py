"""Inspect the proposed production process environment without starting Django or contacting services."""
import argparse
import ipaddress
import json
import os
import ssl
import sys
from email.utils import parseaddr
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import validate_email

from config.database import database_config
from config.deployment import smtp_port, valid_dns_name, valid_mfa_key, valid_session_key
from config.secret_files import read_secret


def is_ip(value):
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def allowed_host(value):
    # Django expects IPv6 literals with brackets in ALLOWED_HOSTS.
    if value.startswith("[") and value.endswith("]"):
        try:
            ipaddress.IPv6Address(value[1:-1])
        except ValueError:
            return False
        return True
    return valid_dns_name(value)


def secret(environ, name):
    filename = environ.get(name + "_FILE")
    if filename and not Path(filename).is_file():
        raise ImproperlyConfigured("Secret file must be a regular file.")
    return read_secret(environ, name)


def check_key(environ, name, validate):
    try:
        return validate(secret(environ, name))
    except (OSError, ImproperlyConfigured):
        return False


def check_database(environ):
    if environ.get("TAGIHNET_DB_ENGINE") != "postgresql":
        return False, False
    try:
        filename = environ.get("TAGIHNET_DB_PASSWORD_FILE")
        if filename and not Path(filename).is_file():
            return False, False
        config = database_config(environ, ROOT / "data", False)
    except (OSError, ImproperlyConfigured, ValueError):
        return False, False
    host = config["HOST"]
    host_valid = valid_dns_name(host) or is_ip(host)
    ca = config["OPTIONS"].get("sslrootcert")
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if ca == "system":
            context.load_default_certs()
        else:
            ca_file = Path(ca) if ca else Path.home() / ".postgresql" / "root.crt"
            if not ca_file.is_file():
                return host_valid, False
            context.load_verify_locations(cafile=ca_file)
    except (OSError, ValueError, RuntimeError):
        return host_valid, False
    return host_valid, context.cert_store_stats()["x509_ca"] > 0


def check_smtp(environ):
    host = environ.get("TAGIHNET_EMAIL_HOST", "")
    sender = environ.get("TAGIHNET_FROM_EMAIL", "")
    try:
        smtp_port(environ)
        endpoint = valid_dns_name(host) or is_ip(host)
    except ImproperlyConfigured:
        endpoint = False
    try:
        password = secret(environ, "TAGIHNET_EMAIL_PASSWORD")
        credentials = bool(environ.get("TAGIHNET_EMAIL_USER", "")) == bool(password)
    except (OSError, ImproperlyConfigured):
        credentials = False
    try:
        if "\r" in sender or "\n" in sender:
            raise ValidationError("Sender contains a line break.")
        _, address = parseaddr(sender)
        validate_email(address)
        sender_valid = not address.rsplit("@", 1)[1].lower().endswith(".invalid")
    except ValidationError:
        sender_valid = False
    return endpoint, credentials, sender_valid


def check(environ, hostname):
    names = environ.get("TAGIHNET_ALLOWED_HOSTS", "").split(",")
    allowed = all(allowed_host(name) for name in names)
    target = valid_dns_name(hostname) and "." in hostname and not is_ip(hostname)
    postgres, postgres_ca = check_database(environ)
    smtp, smtp_auth, sender = check_smtp(environ)
    checks = {
        "settings_module": environ.get("DJANGO_SETTINGS_MODULE", "config.settings") == "config.settings",
        "debug_off": environ.get("TAGIHNET_DEBUG") == "0",
        "simulation_off": environ.get("TAGIHNET_SIMULATION") == "0",
        "secure_cookies": environ.get("TAGIHNET_SECURE_COOKIES", "1") == "1",
        "target_hostname": target and hostname.lower() in {name.lower() for name in names},
        "allowed_hosts": allowed,
        "session_key": check_key(environ, "TAGIHNET_SECRET_KEY", valid_session_key),
        "mfa_key": check_key(environ, "TAGIHNET_MFA_KEY", valid_mfa_key),
        "postgresql": postgres,
        "postgresql_ca": postgres_ca,
        "smtp_endpoint": smtp,
        "smtp_authentication": smtp_auth,
        "smtp_sender": sender,
    }
    failures = [key for key, passed in checks.items() if not passed]
    return {
        "version": 1, "scope": "deployment_configuration", "production_ready": False,
        "status": "failed" if failures else "passed", "checks": checks, "failures": failures,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hostname", required=True, help="Hostname publik aplikasi yang akan diperiksa.")
    args = parser.parse_args()
    report = check(os.environ, args.hostname)
    print(json.dumps(report, sort_keys=True))
    return int(report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
