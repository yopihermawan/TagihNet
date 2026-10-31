import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv("TAGIHNET_DEBUG", "1") == "1"
SIMULATION = os.getenv("TAGIHNET_SIMULATION", "1") == "1"
DATA_DIR = Path(os.getenv("TAGIHNET_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SECRET_KEY = os.getenv("TAGIHNET_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise RuntimeError("TAGIHNET_SECRET_KEY wajib diatur saat DEBUG=0")
    key_path = DATA_DIR / ".secret_key"
    try:
        with os.fdopen(os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as key_file:
            key_file.write(secrets.token_urlsafe(64))
    except FileExistsError:
        pass
    SECRET_KEY = key_path.read_text().strip()
    if len(SECRET_KEY) < 50:
        raise RuntimeError("Kunci lokal tidak valid; pulihkan data/.secret_key atau gunakan TAGIHNET_SECRET_KEY.")
ALLOWED_HOSTS = os.getenv("TAGIHNET_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
INSTALLED_APPS = [
    "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
    "django.contrib.messages", "django.contrib.staticfiles", "core",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware", "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware", "core.middleware.SessionPolicyMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware", "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.SecurityHeadersMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [BASE_DIR / "templates"],
              "APP_DIRS": True, "OPTIONS": {"context_processors": [
                  "django.template.context_processors.request", "django.contrib.auth.context_processors.auth",
                  "django.contrib.messages.context_processors.messages", "core.context.shell",
              ]}}]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": DATA_DIR / "tagihnet.sqlite3",
                         "OPTIONS": {"timeout": 20}}}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "id"
TIME_ZONE = "Asia/Jakarta"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 1800
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_SECURE = os.getenv("TAGIHNET_SECURE_COOKIES", "0" if DEBUG else "1") == "1"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 0 if DEBUG else 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
PASSWORD_RESET_TIMEOUT = 1800
EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend" if DEBUG else "django.core.mail.backends.smtp.EmailBackend"
EMAIL_FILE_PATH = DATA_DIR / "emails"
EMAIL_HOST = os.getenv("TAGIHNET_EMAIL_HOST", "localhost")
EMAIL_HOST_USER = os.getenv("TAGIHNET_EMAIL_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("TAGIHNET_EMAIL_PASSWORD", "")
EMAIL_PORT = 587
EMAIL_USE_TLS = True
DEFAULT_FROM_EMAIL = os.getenv("TAGIHNET_FROM_EMAIL", "TagihNet <noreply@example.invalid>")
DATA_UPLOAD_MAX_MEMORY_SIZE = 262144
FILE_UPLOAD_MAX_MEMORY_SIZE = 262144
