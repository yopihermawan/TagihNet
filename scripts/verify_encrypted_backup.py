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
from django.test import override_settings
from core.gateway import reserve_order, apply_status
from core.models import GatewayConfig
from core.tests.test_payment_gateway import account_keys, status_payload
from core.gateway_expiry import expire_order
from core.gateway_refunds import reconcile_refund
from core.tests.test_gateway_refunds import refund_payload, REASON
from core.gateway_worker import claim_policy, save_policy as save_gateway_policy
from unittest.mock import patch
import time
tenants, owners, customers = fixtures()
invoice, _ = issue_invoice(tenants[0], customers[0].pk, date(2026, 10, 1), owners[0])
record_payment(tenants[0], invoice.pk, 100000, uuid.uuid4(), owners[0])
GatewayConfig.objects.create(tenant=tenants[0], merchant_id="G-TEST", enabled=True)
with override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=account_keys(tenants[0])):
    order = reserve_order(owners[0], invoice.pk)
    apply_status(order.pk, status_payload(order))
    from django.test import Client
    from core.midtrans import GatewayUnavailable
    from core.tests.test_payment_gateway import notification_payload
    import json
    with patch("core.gateway.SandboxClient.status",side_effect=GatewayUnavailable):
        assert Client().post("/payments/midtrans/notifications/",data=json.dumps(notification_payload(order)),content_type="application/json").status_code==503
    with patch("core.gateway.SandboxClient.status",return_value=status_payload(order)):
        assert Client().post("/payments/midtrans/notifications/",data=json.dumps(notification_payload(order)),content_type="application/json").status_code==200
    pending, _ = issue_invoice(tenants[0], customers[0].pk, date(2026, 11, 1), owners[0])
    reserve_order(owners[0], pending.pk)
    ending, _ = issue_invoice(tenants[0], customers[0].pk, date(2026, 12, 1), owners[0])
    ending_order = reserve_order(owners[0], ending.pk)
    with patch("core.gateway.SandboxClient.status", side_effect=[status_payload(ending_order,"pending"),status_payload(ending_order,"expire")]), patch("core.gateway_expiry.SandboxClient.expire",return_value={}):
        assert expire_order(owners[0],ending_order.pk,uuid.uuid4(),"Permintaan akhir checkout untuk restore drill",time.time(),1).status=="confirmed"
    refunded, _ = issue_invoice(tenants[0], customers[0].pk, date(2027, 1, 1), owners[0])
    refunded_order = reserve_order(owners[0], refunded.pk)
    apply_status(refunded_order.pk, status_payload(refunded_order))
    with patch("core.gateway_refunds.SandboxClient.status",return_value=refund_payload(refunded_order)):
        reconcile_refund(owners[0],refunded_order.pk,uuid.uuid4(),REASON,time.time(),1)
    polling_policy=save_gateway_policy(owners[0],True,False,60,0,time.time(),1)
    assert claim_policy(polling_policy.pk).lease_token is not None
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
from core.models import Invoice, GatewayOrder, GatewayEvent, GatewayExpiry, GatewayReconciliationPolicy, Payment, PaymentReversal, GatewayRefundResolution
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
assert list(Invoice.objects.order_by("period").values_list("total", flat=True)) == [249750]*4
assert [item.balance for item in Invoice.objects.order_by("period")] == [0, 249750, 249750, 249750]
assert GatewayOrder.objects.filter(status="settled", payment__source="midtrans_sandbox").count() == 1
assert GatewayOrder.objects.filter(status="creating", payment__isnull=True).count() == 1
assert GatewayEvent.objects.count() == 5 and Payment.objects.count() == 3
assert GatewayOrder.objects.filter(status="refunded").count()==1
resolution=GatewayRefundResolution.objects.get()
assert PaymentReversal.objects.count()==1 and resolution.reversal.payment_id==resolution.order.payment_id
assert resolution.amount==resolution.order.amount==resolution.reversal.payment.amount
assert resolution.transaction_id==resolution.order.transaction_id and len(resolution.proof_digest)==64
assert GatewayExpiry.objects.get().status=="confirmed"
from core.models import GatewayNotificationState
notification=GatewayNotificationState.objects.get()
assert notification.delivery_count==2 and notification.last_outcome=="verified" and notification.last_completed_at is not None
policy=GatewayReconciliationPolicy.objects.get()
assert policy.enabled and policy.lease_token is not None and policy.approved_by==user
from datetime import timedelta
from django.test import override_settings
from django.utils import timezone
from unittest.mock import patch
from core.gateway_worker import claim_policy, process_policy
from core.tests.test_payment_gateway import account_keys, status_payload
assert claim_policy(policy.pk) is None
GatewayReconciliationPolicy.objects.filter(pk=policy.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
pending=GatewayOrder.objects.get(status="creating")
with override_settings(MIDTRANS_SANDBOX_ACCOUNT_KEYS=account_keys(user.membership.tenant)), patch("core.gateway.SandboxClient.status",return_value=status_payload(pending,"pending")):
    recovered=process_policy(policy.pk)
assert recovered.last_verified==1 and recovered.lease_token is None and recovered.last_status=="completed"
assert Payment.objects.count()==3 and GatewayRefundResolution.objects.count()==PaymentReversal.objects.count()==1
from core.gateway_reports import WIB, ledger_report
today=timezone.localdate(timezone=WIB)
start,end=today-timedelta(days=1),today+timedelta(days=1)
report=ledger_report(user,start,end)
assert report["counts"]=={"payment":2,"refund":1,"reversal":0}
assert report["totals"]=={"payment":399500,"refund":249750,"reversal":0,"net":149750}
import csv, io
response=client.get("/payments/reports/export.csv",{"start":start.isoformat(),"end":end.isoformat()})
assert response.status_code==200
rows=list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
assert len(rows)==3 and sum(int(row["perubahan_bersih_rupiah"]) for row in rows)==149750
'''
    run("manage.py", "shell", "-c", authenticate, env=restored_env)
    assert fingerprint(source/"tagihnet.sqlite3") == before
    print("PASS: restored password + TOTP login with CSRF, OTP/recovery replay rejected, balance reconciles; wrong MFA key fails independently.", flush=True)
    print("PASS: restored polling approval and active lease preserved; expired lease resumes one verified status check without duplicate payment.",flush=True)
    print("PASS: restored full-refund proof, original payment and reversal reconcile; reopened receivable and one resolution preserved.",flush=True)
    print("PASS: restored gateway ledger report/CSV reconciles 2 payments and 1 refund; manual payment excluded; no merchant access needed.",flush=True)
    print("PASS: restored authenticated notification count/result/timestamps preserved without raw webhook payload or signature.",flush=True)
print("PASS: temporary source/restored databases, plaintext staging, keys and archive cleaned; user demo untouched.")
