"""Exercise the real CLI on disposable SQLite data, including an unmigrated database."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(env, *arguments, expected=0):
    result = subprocess.run([sys.executable, "manage.py", *arguments], cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert result.returncode == expected, f"Unexpected CLI status for {arguments[0]}: {result.returncode}"
    assert "Traceback" not in result.stderr
    return result


def fingerprint(path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
        result = {}
        for (table,) in database.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            quoted = '"'+table.replace('"', '""')+'"'
            rows = database.execute(f"SELECT * FROM {quoted} ORDER BY rowid").fetchall()
            result[table] = (len(rows), hashlib.sha256(json.dumps(rows).encode()).hexdigest())
        return result


with tempfile.TemporaryDirectory(prefix="tagihnet-readiness-cli-") as directory:
    env = {**os.environ, "TAGIHNET_DATA_DIR": directory, "TAGIHNET_DB_ENGINE": "sqlite",
        "TAGIHNET_DEBUG": "1", "TAGIHNET_SIMULATION": "1", "TAGIHNET_SECRET_KEY": "", "TAGIHNET_MFA_KEY": "",
        "TAGIHNET_PUBLIC_ORIGIN": "", "TAGIHNET_MIDTRANS_SANDBOX_ACCOUNT_KEYS": "{}",
        "TAGIHNET_ALLOWED_HOSTS": "localhost,127.0.0.1,testserver"}
    database = Path(directory)/"tagihnet.sqlite3"
    report = json.loads(run(env, "check_payment_gateway", "--tenant", "1", "--json", expected=1).stdout)
    assert not report["configuration_ready"] and report["checks"][0]["code"] == "schema_pending"
    assert report["checkout_configured"] is None
    assert fingerprint(database) == {}
    print("PASS: real CLI detects unmigrated database without creating application tables.", flush=True)
    run(env, "migrate", "--noinput")
    run(env, "shell", "-c", 'from core.models import Tenant; Tenant.objects.create(name="Readiness CLI fixture")')
    before = fingerprint(database)
    report = json.loads(run(env, "check_payment_gateway", "--tenant", "1", "--json", expected=1).stdout)
    states = {check["code"]: check["state"] for check in report["checks"]}
    assert states["credentials"] == states["checkout"] == states["origin"] == "blocked"
    assert fingerprint(database) == before
    run(env, "shell", "-c", 'from core.models import GatewayConfig; GatewayConfig.objects.create(tenant_id=1,merchant_id="G-CLI",enabled=True)')
    fixture_key = "SB-Mid-server-disposable-cli-fixture"
    configured = {**env, "TAGIHNET_PUBLIC_ORIGIN": "https://billing.example.test", "TAGIHNET_ALLOWED_HOSTS": "billing.example.test",
        "TAGIHNET_MIDTRANS_SANDBOX_ACCOUNT_KEYS": json.dumps({"1": {"merchant_id": "G-CLI", "server_key": fixture_key}})}
    before = fingerprint(database)
    output = run(configured, "check_payment_gateway", "--tenant", "1", "--json").stdout
    report = json.loads(output)
    assert report["configuration_ready"] and report["remote_verification"] == "not_performed"
    assert report["checked_at"].endswith("+07:00") and fixture_key not in output
    assert fingerprint(database) == before
    print("PASS: blocked/complete local configuration yields JSON and exit 1/0; credentials redacted and database payloads unchanged.", flush=True)
print("PASS: disposable CLI databases and generated development keys cleaned; user database untouched.")
