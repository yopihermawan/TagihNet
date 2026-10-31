"""Explicit database selection; SQLite remains the development default."""
import re
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured


def database_config(environ, data_dir, debug):
    engine = environ.get("TAGIHNET_DB_ENGINE", "sqlite")
    if engine == "sqlite":
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": Path(data_dir)/"tagihnet.sqlite3",
                "OPTIONS": {"timeout": 20}}
    if engine != "postgresql":
        raise ImproperlyConfigured("TAGIHNET_DB_ENGINE harus sqlite atau postgresql.")
    values = {}
    for field in ["NAME", "USER", "HOST"]:
        value = environ.get("TAGIHNET_DB_"+field, "").strip()
        if not value or (field in {"NAME", "USER"} and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value)):
            raise ImproperlyConfigured("Konfigurasi PostgreSQL memerlukan NAME, USER dan HOST yang valid.")
        values[field] = value
    password = environ.get("TAGIHNET_DB_PASSWORD", "")
    password_file = environ.get("TAGIHNET_DB_PASSWORD_FILE", "")
    if password and password_file:
        raise ImproperlyConfigured("Pilih satu sumber password PostgreSQL: variabel atau file privat.")
    if password_file:
        try:
            password = Path(password_file).read_text().rstrip("\r\n")
        except OSError:
            raise ImproperlyConfigured("File password PostgreSQL tidak dapat dibaca.") from None
    if not password:
        raise ImproperlyConfigured("Password PostgreSQL wajib dikonfigurasi.")
    port = environ.get("TAGIHNET_DB_PORT", "5432")
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ImproperlyConfigured("Port PostgreSQL tidak valid.")
    sslmode = environ.get("TAGIHNET_DB_SSLMODE", "verify-full")
    if sslmode not in {"verify-full", "disable"} or (not debug and sslmode != "verify-full"):
        raise ImproperlyConfigured("PostgreSQL memerlukan verify-full; disable hanya untuk lab DEBUG=1.")
    options = {"sslmode": sslmode, "connect_timeout": 5,
               "options": "-c lock_timeout=5000 -c statement_timeout=30000"}
    if environ.get("TAGIHNET_DB_SSLROOTCERT"):
        options["sslrootcert"] = environ["TAGIHNET_DB_SSLROOTCERT"]
    return {"ENGINE": "django.db.backends.postgresql", **values, "PASSWORD": password,
            "PORT": int(port), "CONN_MAX_AGE": 0, "OPTIONS": options}
