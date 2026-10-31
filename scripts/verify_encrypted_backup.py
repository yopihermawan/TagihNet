"""Encrypted SQLite restore drill with ledger reconciliation and real restored MFA login. Disposable data only."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent


def fingerprint(path):
    with sqlite3.connect(path) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        result = {}
        for table in tables:
            quoted = '"'+table.replace('"', '""')+'"'
            rows = connection.execute(f"SELECT * FROM {quoted} ORDER BY rowid").fetchall()
            result[table] = (len(rows), hashlib.sha256(json.dumps(rows).encode()).hexdigest())
        return result


def run(*arguments, env, expected=0):
    result = subprocess.run([sys.executable, *map(str, arguments)], cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if expected == 0:
        assert result.returncode == 0, f"Command failed: {arguments[0]} (exit {result.returncode})"
    else:
        assert result.returncode != 0
    assert "Traceback" not in result.stderr if arguments[0] == "scripts/backup.py" else True
    return result


with tempfile.TemporaryDirectory(prefix="tagihnet-encrypted-drill-") as directory:
    directory = Path(directory)
    source, restored = directory/"source", directory/"restored"
    env = {**os.environ, "TAGIHNET_DATA_DIR": str(source), "TAGIHNET_DB_ENGINE": "sqlite",
        "TAGIHNET_DEBUG": "1", "TAGIHNET_SIMULATION": "1", "TAGIHNET_SECRET_KEY": "", "TAGIHNET_MFA_KEY": "",
        "TAGIHNET_ALLOWED_HOSTS": "localhost,127.0.0.1,testserver"}
    run("manage.py", "migrate", "--noinput", env=env)
    fixture = '''
import hashlib, uuid
from datetime import date
from core.tests.test_workflow import fixtures
from core.tests.auth_helpers import TEST_TOTP_SECRET
from core.billing import issue_invoice, record_payment
from core.models import MFADevice
from core.security import encrypt_secret, recovery_digest
tenants, owners, customers = fixtures()
invoice, _ = issue_invoice(tenants[0], customers[0].pk, date(2026, 10, 1), owners[0])
record_payment(tenants[0], invoice.pk, 100000, uuid.uuid4(), owners[0])
recovery = hashlib.sha256(b"tagihnet-isolated-drill-only").hexdigest()[:32]
MFADevice.objects.create(user=owners[0], encrypted_secret=encrypt_secret(TEST_TOTP_SECRET), recovery_hashes=[recovery_digest(recovery)])
'''
    run("manage.py", "shell", "-c", fixture, env=env)
    before = fingerprint(source/"tagihnet.sqlite3")
    key = directory/"separate-keys/backup.json"
    archive = directory/"archives/backup.tnbk"
    run("scripts/backup.py", "keygen", key, env=env)
    run("scripts/backup.py", "backup", archive, "--key-file", key, env=env)
    run("scripts/backup.py", "verify", archive, "--key-file", key, env=env)
    wrong = directory/"wrong-key.json"
    run("scripts/backup.py", "keygen", wrong, env=env)
    restored_env = {**env, "TAGIHNET_DATA_DIR": str(restored)}
    run("scripts/backup.py", "restore", archive, "--key-file", wrong, env=restored_env, expected=1)
    assert not (restored/"tagihnet.sqlite3").exists()
    run("scripts/backup.py", "restore", archive, "--key-file", key, env=restored_env)
    assert fingerprint(restored/"tagihnet.sqlite3") == before
    run("scripts/backup.py", "restore", archive, "--key-file", key, env=restored_env, expected=1)
    assert fingerprint(restored/"tagihnet.sqlite3") == before
    print("PASS: authenticated encrypted SQLite restore; all table counts/payloads match; wrong key and occupied target rejected.", flush=True)
    # Backup key authenticates the archive; it does not replace the separate MFA encryption key.
    wrong_mfa = {**restored_env, "TAGIHNET_MFA_KEY": Fernet.generate_key().decode()}
    check_wrong_mfa = '''
from cryptography.fernet import InvalidToken
from core.models import MFADevice
from core.security import decrypt_secret
try:
    decrypt_secret(MFADevice.objects.get().encrypted_secret)
except InvalidToken:
    pass
else:
    raise AssertionError("MFA key isolation failed")
'''
    run("manage.py", "shell", "-c", check_wrong_mfa, env=wrong_mfa)
    # Restore test fixture keys separately, after database verification, never from inside the archive.
    for filename in [".secret_key", ".mfa_key"]:
        shutil.copy2(source/filename, restored/filename)
        (restored/filename).chmod(0o600)
    authenticate = '''
import hashlib, pyotp
from django.contrib.auth import get_user_model
from django.test import Client
from core.tests.auth_helpers import TEST_TOTP_SECRET
from core.security import verify_factor
from core.models import Invoice
client = Client(enforce_csrf_checks=True)
client.get("/login/")
csrf = client.cookies["csrftoken"].value
response = client.post("/login/", {"username":"owner0", "password":"Local-test-secret-2026!", "csrfmiddlewaretoken":csrf})
assert response.status_code == 302 and response.url == "/mfa/verify/"
client.get(response.url)
code = pyotp.TOTP(TEST_TOTP_SECRET).now()
response = client.post("/mfa/verify/", {"code":code, "csrfmiddlewaretoken":client.cookies["csrftoken"].value})
assert response.status_code == 302 and response.url == "/"
assert client.get("/customers/").status_code == 200
user = get_user_model().objects.get(username="owner0")
assert not verify_factor(user, code)
recovery = hashlib.sha256(b"tagihnet-isolated-drill-only").hexdigest()[:32]
assert verify_factor(user, recovery) and not verify_factor(user, recovery)
assert Invoice.objects.get().balance == 149750
'''
    run("manage.py", "shell", "-c", authenticate, env=restored_env)
    assert fingerprint(source/"tagihnet.sqlite3") == before
    print("PASS: restored password + TOTP login with CSRF, OTP/recovery replay rejected, balance reconciles; wrong MFA key fails independently.", flush=True)
print("PASS: temporary source/restored databases, plaintext staging, keys and archive cleaned; user demo untouched.")
