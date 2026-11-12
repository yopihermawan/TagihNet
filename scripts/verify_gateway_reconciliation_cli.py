"""Real CLI exit/JSON checks on disposable SQLite data; provider transport is stubbed."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY = "SB-Mid-server-disposable-reconciliation-fixture"
RUNNER = '''
import django, os, sys
django.setup()
from unittest.mock import patch
from django.core.management import execute_from_command_line
from core.midtrans import GatewayUnavailable
def status(order):
    mode = os.environ["TAGIHNET_CLI_FIXTURE_MODE"]
    if mode == "unavailable" or mode == "mixed" and order.invoice.period.month == 1:
        raise GatewayUnavailable("PRIVATE transport failure with key/DSN detail")
    return {"order_id":order.order_id,"merchant_id":order.merchant_id,"currency":"IDR",
        "gross_amount":f"{order.amount}.00","transaction_id":f"txn-{order.pk.hex}",
        "transaction_status":"settlement","status_code":"200","fraud_status":"accept"}
with patch("core.gateway.SandboxClient.status", side_effect=status), patch("core.gateway.SandboxClient.create", side_effect=AssertionError("unexpected checkout")):
    execute_from_command_line(["manage.py", *sys.argv[1:]])
'''


def run(env, *arguments, expected=0, stubbed=False):
    prefix = [sys.executable, "-c", RUNNER] if stubbed else [sys.executable, "manage.py"]
    result = subprocess.run([*prefix, *arguments], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert result.returncode == expected, f"Unexpected status {result.returncode} for {arguments[0]}"
    assert "Traceback" not in result.stderr
    assert KEY not in result.stdout+result.stderr and "PRIVATE" not in result.stdout+result.stderr
    return result


def fingerprint(path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
        result = {}
        for (table,) in database.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            quoted = '"'+table.replace('"', '""')+'"'
            rows = database.execute(f"SELECT * FROM {quoted} ORDER BY rowid").fetchall()
            result[table] = (len(rows), hashlib.sha256(json.dumps(rows).encode()).hexdigest())
        return result


with tempfile.TemporaryDirectory(prefix="tagihnet-reconciliation-cli-") as directory:
    env = {**os.environ, "DJANGO_SETTINGS_MODULE":"config.settings", "TAGIHNET_DATA_DIR":directory,
        "TAGIHNET_DB_ENGINE":"sqlite", "TAGIHNET_DEBUG":"1", "TAGIHNET_SIMULATION":"1",
        "TAGIHNET_SECRET_KEY":"", "TAGIHNET_MFA_KEY":"", "TAGIHNET_PUBLIC_ORIGIN":"",
        "TAGIHNET_ALLOWED_HOSTS":"localhost,127.0.0.1,testserver",
        "TAGIHNET_MIDTRANS_SANDBOX_ACCOUNT_KEYS":json.dumps({"1":{"merchant_id":"G-TEST","server_key":KEY}})}
    path = Path(directory)/"tagihnet.sqlite3"
    command = "reconcile_gateway_payments"
    result = run(env, command, "--dry-run", "--json", expected=1)
    report = json.loads(result.stdout)
    assert report["outcome"] == "database_unavailable" and report["selected"] is None
    assert fingerprint(path) == {}
    run(env, "migrate", "--noinput")
    fixture = '''
from datetime import date
from core.tests.test_workflow import fixtures
from core.models import GatewayConfig
from core.billing import issue_invoice
from core.gateway import reserve_order
tenants, owners, customers = fixtures()
GatewayConfig.objects.create(tenant=tenants[0], merchant_id="G-TEST", enabled=True)
for month in [1, 2]:
    invoice, _ = issue_invoice(tenants[0], customers[0].pk, date(2026, month, 1), owners[0])
    reserve_order(owners[0], invoice.pk)
'''
    run(env, "shell", "-c", fixture)
    before = fingerprint(path)
    report = json.loads(run(env, command, "--dry-run", "--tenant", "1", "--json").stdout)
    assert report["outcome"] == "planned" and report["selected"] == 2 and report["attempted"] == 0
    assert fingerprint(path) == before
    report = json.loads(run(env, command, "--tenant", "2", "--json").stdout)
    assert report["outcome"] == "empty" and report["verified"] == 0
    report = json.loads(run(env, command, "--tenant", "999999", "--json", expected=1).stdout)
    assert report["outcome"] == "unknown_tenant" and report["selected"] is None
    assert fingerprint(path) == before
    print("PASS: real CLI dry-run is read-only by every-table fingerprint; unknown database/scope are errors, empty scope is not verification.", flush=True)

    result = run({**env,"TAGIHNET_CLI_FIXTURE_MODE":"unavailable"}, command, "--json", expected=1, stubbed=True)
    report = json.loads(result.stdout)
    assert (report["outcome"],report["attempted"],report["unavailable"]) == ("failed",2,2)
    assert "CommandError" in result.stderr
    result = run({**env,"TAGIHNET_CLI_FIXTURE_MODE":"mixed"}, command, "--json", expected=1, stubbed=True)
    report = json.loads(result.stdout)
    assert (report["outcome"],report["verified"],report["unavailable"]) == ("partial",1,1)
    with sqlite3.connect(path) as database:
        assert database.execute("SELECT COUNT(*) FROM core_payment").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM core_networkjob").fetchone()[0] == 1
    print("PASS: all-failed and partial batches exit 1 with valid redacted stdout JSON; successful payment/job survive partial failure.", flush=True)

    for _ in range(2):
        report = json.loads(run({**env,"TAGIHNET_CLI_FIXTURE_MODE":"success"}, command, "--include-settled", "--json", stubbed=True).stdout)
        assert (report["outcome"],report["verified"],report["unavailable"]) == ("completed",2,0)
        assert report["generated_at"].endswith("+07:00")
    with sqlite3.connect(path) as database:
        assert database.execute("SELECT COUNT(*) FROM core_payment").fetchone()[0] == 2
        assert database.execute("SELECT COUNT(*) FROM core_networkjob").fetchone()[0] == 2
    print("PASS: complete/replay batches exit 0; two order IDs produce exactly two payments/jobs, no checkout creation.", flush=True)
    setup_policy = '''
import time
from django.contrib.auth import get_user_model
from core.gateway_worker import save_policy
owner=get_user_model().objects.get(username="owner0")
save_policy(owner,True,True,60,0,time.time(),1)
'''
    run(env, "shell", "-c", setup_policy)
    result=run({**env,"TAGIHNET_CLI_FIXTURE_MODE":"success"}, "run_gateway_worker", "--once", stubbed=True)
    assert "status=completed verified=2" in result.stdout
    revoke='''
from django.utils import timezone
from datetime import timedelta
from core.models import GatewayReconciliationPolicy, Membership
GatewayReconciliationPolicy.objects.update(next_run_at=timezone.now()-timedelta(seconds=1))
Membership.objects.filter(user__username="owner0").update(session_version=2)
'''
    run(env,"shell","-c",revoke)
    result=run(env,"run_gateway_worker","--once",expected=1)
    assert "status=blocked" in result.stdout and "verified=" not in result.stdout
    assert "persetujuan ulang" in result.stderr and "Jadwal ulang" not in result.stderr
    before=fingerprint(path)
    result=run(env,"run_gateway_worker","--once")
    assert "0 kebijakan diproses" in result.stdout and fingerprint(path)==before
    renew='''
import time
from django.contrib.auth import get_user_model
from core.gateway_worker import save_policy
from core.models import GatewayReconciliationPolicy
owner=get_user_model().objects.get(username="owner0")
policy=GatewayReconciliationPolicy.objects.get(tenant=owner.membership.tenant)
save_policy(owner,True,True,60,policy.version,time.time(),2)
'''
    run(env,"shell","-c",renew)
    result=run({**env,"TAGIHNET_CLI_FIXTURE_MODE":"success"},"run_gateway_worker","--once",stubbed=True)
    assert "status=completed verified=2" in result.stdout
    with sqlite3.connect(path) as database:
        assert database.execute("SELECT COUNT(*) FROM core_payment").fetchone()[0]==2
        assert database.execute("SELECT COUNT(*) FROM core_networkjob").fetchone()[0]==2
    print("PASS: real worker exits 1 for newly blocked consent without old counts; repeat is read-only, fresh approval resumes without duplicate ledger.",flush=True)
print("PASS: disposable CLI databases, generated development keys and fixtures cleaned; user data untouched.")
