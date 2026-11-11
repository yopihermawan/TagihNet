"""Real process races in a fresh disposable PostgreSQL database; never migrates SQLite."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def child(data):
    import django
    django.setup()
    from django.contrib.auth import get_user_model
    from django.core.exceptions import ValidationError
    from django.db.models.query import QuerySet
    from core import billing
    from core.billing import issue_invoice, record_payment
    from core.bulk_billing import process_batch
    from core.models import Customer, Payment, Tenant
    from core.network import run_job
    from core.operations import activate_installation
    from core.reminders import process_reminder
    actor = get_user_model().objects.get(pk=data["actor"])
    tenant = Tenant.objects.get(pk=data["tenant"])
    gate_dir = Path(data["gate"])

    def gate():
        (gate_dir/f"ready-{data['role']}").touch()
        deadline = time.monotonic()+15
        while not (gate_dir/"go").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("Process gate timed out")
            time.sleep(.01)

    def action():
        case = data["case"]
        if case in {"gateway_polling", "gateway_polling_revoked"}:
            from core.gateway_worker import process_policy
            from core.models import Membership
            from core.tests.test_payment_gateway import status_payload
            def wait_for(name):
                deadline=time.monotonic()+15
                while not (gate_dir/name).exists():
                    if time.monotonic()>deadline:
                        raise TimeoutError("Polling process gate timed out")
                    time.sleep(.01)
            if case=="gateway_polling_revoked" and data["role"]==1:
                wait_for("polling-inflight")
                Membership.objects.filter(user=actor).update(session_version=2)
                (gate_dir/"polling-revoked").touch()
                return "revoked"
            calls=0
            def status(order):
                nonlocal calls
                calls+=1
                if case=="gateway_polling_revoked":
                    (gate_dir/"polling-inflight").touch()
                    wait_for("polling-revoked")
                return status_payload(order,merchant_id=order.merchant_id)
            with patch("core.gateway.SandboxClient.status",side_effect=status):
                result=process_policy(data["gateway_policy"])
            return {"processed":result is not None,"calls":calls,"status":result.last_status if result else None}
        if case == "gateway_reserve":
            from core.gateway import reserve_order
            return str(reserve_order(actor, data["invoice"]).pk)
        if case == "gateway_settle":
            from core.gateway import apply_status
            return str(apply_status(data["gateway_order"], data["status_payload"]).payment_id)
        if case in {"gateway_refund", "gateway_refund_late"}:
            from core.gateway import apply_status
            from core.gateway_refunds import reconcile_refund
            from core.models import GatewayOrder
            from core.tests.test_gateway_refunds import refund_payload, REASON
            from core.tests.test_payment_gateway import status_payload
            current = GatewayOrder.objects.get(pk=data["gateway_order"])
            if case == "gateway_refund_late" and data["role"] == 1:
                deadline = time.monotonic()+15
                while not (gate_dir/"refund-recorded").exists():
                    if time.monotonic() > deadline:
                        raise TimeoutError("Refund process gate timed out")
                    time.sleep(.01)
                return apply_status(current.pk, status_payload(current)).status
            with patch("core.gateway_refunds.SandboxClient.status", return_value=refund_payload(current)):
                result = reconcile_refund(actor, current.pk, data["refund_key"], REASON, time.time(), 1)
            if case == "gateway_refund_late":
                (gate_dir/"refund-recorded").touch()
            return str(result.pk)
        if case == "gateway_dispute":
            from core.gateway import apply_status
            if data["role"] == 1:
                deadline = time.monotonic() + 15
                while not (gate_dir/"dispute-applied").exists():
                    if time.monotonic() > deadline:
                        raise TimeoutError("Dispute commit gate timed out")
                    time.sleep(.01)
            result = apply_status(data["gateway_order"], data["status_payload"])
            if data["role"] == 0:
                (gate_dir/"dispute-applied").touch()
            return {"status": result.status, "payment_id": result.payment_id}
        if case in {"gateway_expiry", "gateway_expiry_payment"}:
            from core.gateway import apply_status
            from core.gateway_expiry import expire_order
            from core.midtrans import GatewayUnavailable
            from core.models import GatewayOrder
            from core.tests.test_payment_gateway import status_payload
            current = GatewayOrder.objects.get(pk=data["gateway_order"])
            def wait_for(name):
                deadline = time.monotonic()+15
                while not (gate_dir/name).exists():
                    if time.monotonic()>deadline:
                        raise TimeoutError("Expiry process gate timed out")
                    time.sleep(.01)
            if case == "gateway_expiry_payment" and data["role"] == 1:
                wait_for("expiry-preflight")
                result = apply_status(current.pk, status_payload(current))
                (gate_dir/"payment-committed").touch()
                return str(result.payment_id)
            def status(order):
                state = ("settlement" if (gate_dir/"payment-committed").exists() else
                    "expire" if (gate_dir/"remote-expired").exists() else "pending")
                return status_payload(order, state)
            def expire(order):
                if case == "gateway_expiry_payment":
                    (gate_dir/"expiry-preflight").touch()
                    wait_for("payment-committed")
                    raise GatewayUnavailable
                (gate_dir/"remote-expired").touch()
                return {}
            with patch("core.gateway.SandboxClient.status", side_effect=status), patch("core.gateway_expiry.SandboxClient.expire", side_effect=expire):
                result = expire_order(actor, current.pk, data["expiry_key"], "Permintaan akhir checkout pada lab dua proses", time.time(), 1)
            return {"request": str(result.pk), "status": result.status}
        if case == "issue":
            return issue_invoice(tenant, data["customer"], date.fromisoformat(data["period"]), actor)[0].pk
        if case in {"payment", "cross_payment"}:
            payment, created = record_payment(tenant, data["invoice"], data["amount"], uuid.UUID(data["key"]), actor)
            return "created" if created else "replayed"
        if case == "bulk_single":
            if data["role"] == 0:
                return issue_invoice(tenant, data["customer"], date.fromisoformat(data["period"]), actor)[0].pk
            return process_batch(data["batch"])
        if case == "payment_network":
            if data["role"] == 0:
                return record_payment(tenant, data["invoice"], data["amount"], uuid.UUID(data["key"]), actor)[0].pk
            return run_job(data["job"]).status
        if case == "reminder":
            return process_reminder(data["reminder"]).status
        if case == "activation":
            return activate_installation(actor, data["order"], date.fromisoformat(data["period"]),
                data["fingerprint"], time.time(), 1).customer_id
        if case == "customer_import":
            from core.customer_import import confirm_import
            return confirm_import(actor, data["import_batch"], time.time(), 1).status
        if case == "retry_deadlock":
            from django.db import transaction
            from core.billing import audit, retry_locked
            attempts = 0
            @retry_locked
            @transaction.atomic
            def probe():
                nonlocal attempts
                attempts += 1
                first, second = data["customers"][::1 if data["role"] == 0 else -1]
                Customer.objects.select_for_update().get(pk=first)
                audit(tenant, actor, "lab.deadlock_probe", first)
                gate()
                Customer.objects.select_for_update().get(pk=second)
                return attempts
            return probe()
        raise ValueError("Unknown process case")

    try:
        if data["case"] == "cross_payment":
            original = QuerySet.create
            def create(query, **kwargs):
                if query.model is Payment:
                    gate()
                return original(query, **kwargs)
            with patch.object(QuerySet, "create", create):
                result = action()
        elif data["case"] == "bulk_single":
            if data["role"] == 0:
                original = billing.write_snapshot_invoice
                def write(*args, **kwargs):
                    gate()
                    return original(*args, **kwargs)
                with patch.object(billing, "write_snapshot_invoice", write):
                    result = action()
            else:
                original = QuerySet.get
                def get(query, *args, **kwargs):
                    if query.model is Customer and query.query.select_for_update:
                        gate()
                    return original(query, *args, **kwargs)
                with patch.object(QuerySet, "get", get):
                    result = action()
        elif data["case"] == "retry_deadlock":
            result = action()
        else:
            gate()
            result = action()
        print(json.dumps({"ok": result}), flush=True)
    except ValidationError:
        print(json.dumps({"rejected": True}), flush=True)
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "sqlstate": getattr(exc.__cause__, "sqlstate", None)}), flush=True)
        raise SystemExit(1)


def parallel(case, common, changes=None):
    with tempfile.TemporaryDirectory(prefix="tagihnet-pg-gate-") as folder:
        processes = []
        try:
            for role in range(2):
                payload = {**common, **((changes or [{}, {}])[role]), "case": case, "role": role, "gate": folder}
                processes.append(subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child", json.dumps(payload, default=str)],
                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            deadline = time.monotonic()+20
            while not all((Path(folder)/f"ready-{role}").exists() for role in range(2)):
                if any(process.poll() is not None for process in processes) or time.monotonic() > deadline:
                    raise AssertionError("Worker exited or timed out before reaching process barrier")
                time.sleep(.01)
            (Path(folder)/"go").touch()
            results = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                result = json.loads(stdout.strip())
                assert process.returncode == 0 and "error" not in result, result
                results.append(result)
            return results
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)


def database_fingerprint(connection):
    from django.apps import apps
    from psycopg import sql
    result = {}
    with connection.cursor() as cursor:
        for model in apps.get_models(include_auto_created=True):
            cursor.execute(sql.SQL("SELECT row_to_json(t)::text FROM {} t ORDER BY {}")
                .format(sql.Identifier(model._meta.db_table), sql.Identifier(model._meta.pk.column)))
            rows = cursor.fetchall()
            result[model._meta.db_table] = (len(rows), hashlib.sha256(json.dumps(rows).encode()).hexdigest())
    return result


def pg_tool(tool, database, user, **streams):
    container = os.environ.get("TAGIHNET_PG_TOOLS_CONTAINER")
    env = dict(os.environ)
    if container:
        for key in ["DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"]:
            env.pop(key, None)
        command = ["docker", "--host=unix:///var/run/docker.sock", "exec", "-i", container, tool,
            "--username", user, "--dbname", database]
    else:
        from django.conf import settings
        config = settings.DATABASES["default"]
        env.update(PGHOST=config["HOST"], PGPORT=str(config["PORT"]), PGUSER=user,
            PGPASSWORD=config["PASSWORD"], PGSSLMODE=config["OPTIONS"]["sslmode"])
        if config["OPTIONS"].get("sslrootcert"):
            env["PGSSLROOTCERT"] = config["OPTIONS"]["sslrootcert"]
        command = [tool, "--dbname", database]
    command += ["--format=custom"] if tool == "pg_dump" else ["--no-owner", "--exit-on-error"]
    subprocess.run(command, env=env, check=True, **streams)


def verify():
    import django
    django.setup()
    from django.conf import settings
    from django.core.management import call_command
    from django.db import connection
    from psycopg import sql
    if connection.vendor != "postgresql" or not settings.DEBUG or not settings.SIMULATION:
        raise RuntimeError("Verification requires an explicitly configured DEBUG/SIMULATION PostgreSQL lab.")
    with connection.cursor() as cursor:
        cursor.execute("SELECT rolsuper,rolcreatedb FROM pg_roles WHERE rolname=current_user")
        superuser, createdb = cursor.fetchone()
        if superuser or not createdb:
            raise RuntimeError("Use a non-superuser lab role with CREATEDB; never a production connection.")
    original_name = connection.settings_dict["NAME"]
    database_name = "tagihnet_verify_"+uuid.uuid4().hex[:16]
    restore_name = database_name+"_restore"
    restore_created = False
    encrypted_restore_name = database_name+"_secure"
    encrypted_restore_created = False
    broken_restore_name = database_name+"_broken"
    broken_restore_created = False
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE DATABASE {} WITH TEMPLATE template0").format(sql.Identifier(database_name)))
    connection.close()
    connection.settings_dict["NAME"] = database_name
    os.environ["TAGIHNET_DB_NAME"] = database_name
    try:
        call_command("migrate", verbosity=0)
        from core.billing import issue_invoice
        from core.bulk_billing import confirm_batch, create_preview
        from core.models import Customer, Invoice, NetworkJob, Payment, Reminder
        from core.network import run_job
        from core.operations import activation_quote
        from core.reminders import plan_reminders, save_policy
        from core.tests.test_operations_workflows import installed_order, technician
        from core.tests.test_workflow import fixtures
        from django.utils import timezone
        tenants, owners, customers = fixtures()
        tenant, actor, customer = tenants[0], owners[0], customers[0]
        common = {"actor": actor.pk, "tenant": tenant.pk, "customer": customer.pk}
        period = date(2026, 2, 1)
        results = parallel("issue", {**common, "period": period.isoformat()})
        assert results[0]["ok"] == results[1]["ok"]
        assert Invoice.objects.filter(tenant=tenant, customer=customer, period=period).count() == 1
        print("PASS: 2 processes, same monthly invoice -> 1 invoice", flush=True)
        invoice = Invoice.objects.get(customer=customer, period=period)
        key = str(uuid.uuid4())
        results = parallel("payment", {**common, "invoice": invoice.pk, "amount": invoice.total, "key": key})
        assert sorted(result["ok"] for result in results) == ["created", "replayed"]
        assert Payment.objects.filter(tenant=tenant, event_key=key).count() == 1
        assert invoice.balance == 0
        print("PASS: 2 processes, payment replay -> 1 ledger entry", flush=True)
        other = Customer.objects.create(tenant=tenant, plan=customer.plan, code="TN-RACE", name="Pelanggan race",
            phone="081299900009", username="race", address="Alamat fiktif", area="Lab")
        first, _ = issue_invoice(tenant, customer.pk, date(2026, 3, 1), actor)
        second, _ = issue_invoice(tenant, other.pk, date(2026, 3, 1), actor)
        key = str(uuid.uuid4())
        jobs_before = NetworkJob.objects.count()
        results = parallel("cross_payment", {**common, "amount": first.total, "key": key},
            [{"invoice": first.pk}, {"invoice": second.pk}])
        assert sum(result.get("ok") == "created" for result in results) == 1 and sum("rejected" in result for result in results) == 1
        assert Payment.objects.filter(tenant=tenant, event_key=key).count() == 1
        assert NetworkJob.objects.count() == jobs_before+1
        print("PASS: shared payment UUID across 2 invoices -> 1 accepted, 1 validation rejection", flush=True)
        batch_period = date(2026, 4, 1)
        batch = create_preview(actor, batch_period, {"state": "all"})
        confirm_batch(actor, batch.pk, time.time(), 1)
        parallel("bulk_single", {**common, "period": batch_period.isoformat(), "batch": batch.pk})
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_stat_clear_snapshot()")
            cursor.execute("SELECT deadlocks FROM pg_stat_database WHERE datname=current_database()")
            deadlocks = cursor.fetchone()[0]
            assert deadlocks == 0, f"Unexpected PostgreSQL deadlocks: {deadlocks}"
        assert Invoice.objects.filter(customer=customer, period=batch_period).count() == 1
        batch.refresh_from_db()
        assert batch.status == "completed"
        print("PASS: overlapping bulk and single issuance under forced lock overlap -> 1 invoice per customer", flush=True)
        invoice, _ = issue_invoice(tenant, other.pk, date(2026, 5, 1), actor)
        job = NetworkJob.objects.create(tenant=tenant, customer=other)
        parallel("payment_network", {**common, "invoice": invoice.pk, "amount": invoice.total, "key": str(uuid.uuid4()), "job": job.pk})
        # Clear the other fixture's unpaid March/April invoices through the normal payment service.
        from core.billing import record_payment
        for prior in Invoice.objects.filter(customer=other):
            if prior.balance:
                record_payment(tenant, prior.pk, prior.balance, uuid.uuid4(), actor)
        for pending in NetworkJob.objects.filter(customer=other):
            run_job(pending.pk)
        other.refresh_from_db()
        assert other.service_state == "active" and invoice.balance == 0
        print("PASS: payment/network race -> verified ledger and reconciled simulated service", flush=True)
        save_policy(actor, {"billing_enabled": True, "sla_enabled": False, "lead_days": 3, "billing_hour": 0, "scenario": "normal"}, 1, time.time(), 1)
        from core.models import ReminderPolicy
        plan_reminders(ReminderPolicy.objects.get(tenant=tenant).pk)
        reminder = Reminder.objects.filter(tenant=tenant, status="pending").first()
        assert reminder
        parallel("reminder", {**common, "reminder": reminder.pk})
        reminder.refresh_from_db()
        assert (reminder.status, reminder.attempts) == ("simulated", 1)
        print("PASS: 2 processes, reminder -> 1 simulated receipt/attempt", flush=True)
        tech = technician(tenant)
        order = installed_order(actor, tech)
        activation_period = date(2026, 6, 1)
        fingerprint = activation_quote(order, activation_period)[1]
        results = parallel("activation", {**common, "order": order.pk, "period": activation_period.isoformat(), "fingerprint": fingerprint})
        assert results[0]["ok"] == results[1]["ok"]
        order.refresh_from_db()
        assert Invoice.objects.filter(customer=order.customer).count() == NetworkJob.objects.filter(customer=order.customer).count() == 1
        print("PASS: 2 processes, PSB activation -> 1 customer/invoice/job", flush=True)
        results = parallel("retry_deadlock", {**common, "customers": [customer.pk, other.pk]})
        assert sorted(result["ok"] for result in results) == [1, 2]
        from core.models import AuditEvent
        assert AuditEvent.objects.filter(action="lab.deadlock_probe").count() == 2
        print("PASS: deliberately forced PostgreSQL deadlock retries the rolled-back transaction exactly once", flush=True)
        from core.customer_import import preview_import
        from core.tests.test_customer_import import source
        data = source(tenant, username="import-process-a", telepon="081299977701")
        imported = preview_import(actor, data, uuid.uuid4())
        results = parallel("customer_import", {**common, "import_batch": imported.pk})
        assert [result["ok"] for result in results] == ["completed", "completed"]
        assert imported.rows.filter(status="created").count() == 1
        assert Customer.objects.filter(tenant=tenant, username="import-process-a").count() == 1
        print("PASS: 2 processes, same customer-import confirmation -> 1 customer", flush=True)
        data = source(tenant, username="import-process-b", telepon="081299977702")
        imports = [preview_import(actor, data, uuid.uuid4()) for _ in range(2)]
        results = parallel("customer_import", common, [{"import_batch": batch.pk} for batch in imports])
        assert sorted(result["ok"] for result in results) == ["completed", "stale"]
        assert Customer.objects.filter(tenant=tenant, username="import-process-b").count() == 1
        print("PASS: 2 processes, overlapping imports -> 1 atomic import, 1 stale preview", flush=True)
        from core.gateway import reserve_order
        from core.models import GatewayConfig, GatewayOrder
        from core.tests.test_payment_gateway import account_keys, status_payload
        settings.MIDTRANS_SANDBOX_ACCOUNT_KEYS = account_keys(tenant)
        os.environ["TAGIHNET_MIDTRANS_SANDBOX_ACCOUNT_KEYS"] = json.dumps(settings.MIDTRANS_SANDBOX_ACCOUNT_KEYS)
        GatewayConfig.objects.create(tenant=tenant, merchant_id="G-TEST", enabled=True)
        gateway_invoice, _ = issue_invoice(tenant, customer.pk, date(2026, 9, 1), actor)
        results = parallel("gateway_reserve", {**common, "invoice": gateway_invoice.pk})
        assert results[0]["ok"] == results[1]["ok"]
        gateway_order = GatewayOrder.objects.get(invoice=gateway_invoice)
        print("PASS: 2 processes, gateway checkout reservation -> 1 persisted order ID", flush=True)
        jobs_before = NetworkJob.objects.count()
        results = parallel("gateway_settle", {**common, "gateway_order": str(gateway_order.pk), "status_payload": status_payload(gateway_order)})
        assert results[0]["ok"] == results[1]["ok"]
        gateway_order.refresh_from_db()
        assert gateway_order.payment_id and gateway_order.events.count() == 1
        assert gateway_invoice.balance == 0 and NetworkJob.objects.count() == jobs_before + 1
        print("PASS: 2 processes, gateway settlement replay -> 1 payment/event/network job", flush=True)
        from core.gateway import apply_status
        from core.billing import reverse_payment
        recovery_invoice, _ = issue_invoice(tenant, customer.pk, date(2026, 10, 1), actor)
        recovery_order = reserve_order(actor, recovery_invoice.pk)
        mistaken = Payment.objects.create(tenant=tenant, invoice=recovery_invoice, amount=recovery_invoice.total,
            event_key=uuid.uuid4(), recorded_by=actor)
        assert apply_status(recovery_order.pk, status_payload(recovery_order)).status == "review"
        reverse_payment(tenant, mistaken.pk, "Koreksi pembayaran fixture yang keliru", actor)
        jobs_before = NetworkJob.objects.count()
        results = parallel("gateway_settle", {**common, "gateway_order": str(recovery_order.pk), "status_payload": status_payload(recovery_order)})
        assert results[0]["ok"] == results[1]["ok"]
        recovery_order.refresh_from_db()
        assert recovery_order.payment_id and recovery_order.events.count() == 1
        assert recovery_order.events.get().outcome == "review" and recovery_order.status == "settled"
        assert recovery_invoice.balance == 0 and NetworkJob.objects.count() == jobs_before + 1
        print("PASS: 2 processes, corrected settlement replay -> 1 recovered payment; original review event preserved", flush=True)
        disputed_invoice, _ = issue_invoice(tenant, customer.pk, date(2026, 11, 1), actor)
        disputed_order = reserve_order(actor, disputed_invoice.pk)
        jobs_before = NetworkJob.objects.count()
        results = parallel("gateway_dispute", {**common, "gateway_order": str(disputed_order.pk)},
            [{"status_payload": status_payload(disputed_order, "refund")}, {"status_payload": status_payload(disputed_order)}])
        assert all(result["ok"] == {"status": "review", "payment_id": None} for result in results)
        assert disputed_invoice.balance == disputed_invoice.total and NetworkJob.objects.count() == jobs_before
        print("PASS: 2 processes, refund commits before late settlement -> review, no payment/network job", flush=True)
        from core.models import GatewayExpiry
        ending_invoice, _ = issue_invoice(tenant, customer.pk, date(2027, 1, 1), actor)
        ending_order = reserve_order(actor, ending_invoice.pk)
        jobs_before, payments_before = NetworkJob.objects.count(), Payment.objects.count()
        results = parallel("gateway_expiry", {**common,"gateway_order":str(ending_order.pk),"expiry_key":str(uuid.uuid4())})
        assert results[0]["ok"] == results[1]["ok"] and results[0]["ok"]["status"]=="confirmed"
        assert GatewayExpiry.objects.filter(order=ending_order).count()==1
        assert ending_invoice.balance==ending_invoice.total and NetworkJob.objects.count()==jobs_before and Payment.objects.count()==payments_before
        print("PASS: 2 processes, repeated expiry intent -> 1 confirmed request, no payment/network job", flush=True)
        racing_invoice, _ = issue_invoice(tenant, customer.pk, date(2027, 2, 1), actor)
        racing_order = reserve_order(actor, racing_invoice.pk)
        results = parallel("gateway_expiry_payment", {**common,"gateway_order":str(racing_order.pk),"expiry_key":str(uuid.uuid4())})
        assert results[0]["ok"]["status"]=="blocked" and GatewayExpiry.objects.get(order=racing_order).status=="blocked"
        assert racing_invoice.balance==0 and Payment.objects.count()==payments_before+1 and NetworkJob.objects.count()==jobs_before+1
        print("PASS: 2 processes, payment wins expiry race -> 1 payment, blocked expiry, no lost ledger entry", flush=True)
        from core.gateway_worker import save_policy as save_gateway_policy
        from core.models import GatewayReconciliationPolicy
        polling_tenant, polling_actor, polling_customer=tenants[1], owners[1], customers[1]
        settings.MIDTRANS_SANDBOX_ACCOUNT_KEYS[str(polling_tenant.pk)]={"merchant_id":"G-POLL","server_key":"SB-Mid-server-pg-polling-fixture"}
        os.environ["TAGIHNET_MIDTRANS_SANDBOX_ACCOUNT_KEYS"]=json.dumps(settings.MIDTRANS_SANDBOX_ACCOUNT_KEYS)
        GatewayConfig.objects.create(tenant=polling_tenant,merchant_id="G-POLL",enabled=True)
        polling_invoice,_=issue_invoice(polling_tenant,polling_customer.pk,date(2027,3,1),polling_actor)
        reserve_order(polling_actor,polling_invoice.pk)
        polling_policy=save_gateway_policy(polling_actor,True,False,60,0,time.time(),1)
        polling_common={"actor":polling_actor.pk,"tenant":polling_tenant.pk,"customer":polling_customer.pk,"gateway_policy":polling_policy.pk}
        jobs_before,payments_before=NetworkJob.objects.count(),Payment.objects.count()
        results=parallel("gateway_polling",polling_common)
        assert sum(result["ok"]["processed"] for result in results)==1 and sum(result["ok"]["calls"] for result in results)==1
        assert polling_invoice.balance==0 and Payment.objects.count()==payments_before+1 and NetworkJob.objects.count()==jobs_before+1
        polling_policy.refresh_from_db()
        assert polling_policy.last_verified==1 and polling_policy.lease_token is None
        print("PASS: 2 processes, polling lease -> 1 status request/payment/job",flush=True)
        revoked_invoices=[]
        for month in [4,5]:
            new_invoice,_=issue_invoice(polling_tenant,polling_customer.pk,date(2027,month,1),polling_actor)
            reserve_order(polling_actor,new_invoice.pk)
            revoked_invoices.append(new_invoice)
        save_gateway_policy(polling_actor,True,False,60,polling_policy.version,time.time(),1)
        results=parallel("gateway_polling_revoked",polling_common)
        assert results[0]["ok"]["calls"]==1 and results[0]["ok"]["status"]=="blocked" and results[1]["ok"]=="revoked"
        assert revoked_invoices[0].balance==0 and revoked_invoices[1].balance==revoked_invoices[1].total
        assert Payment.objects.count()==payments_before+2 and NetworkJob.objects.count()==jobs_before+2
        assert not GatewayReconciliationPolicy.objects.get(pk=polling_policy.pk).enabled
        print("PASS: 2 processes, consent revoked in flight -> verified payment preserved, next order not polled",flush=True)
        from core.models import GatewayRefundResolution, PaymentReversal
        for month, case in [(6,"gateway_refund"),(7,"gateway_refund_late")]:
            refund_invoice,_=issue_invoice(tenant,customer.pk,date(2027,month,1),actor)
            refund_order=reserve_order(actor,refund_invoice.pk)
            apply_status(refund_order.pk,status_payload(refund_order))
            jobs_before=NetworkJob.objects.count()
            results=parallel(case,{**common,"gateway_order":str(refund_order.pk),"refund_key":str(uuid.uuid4())})
            resolution=GatewayRefundResolution.objects.get(order=refund_order)
            refund_order.refresh_from_db()
            assert refund_order.status=="refunded" and refund_invoice.balance==refund_invoice.total
            assert Payment.objects.filter(pk=refund_order.payment_id).count()==1
            assert PaymentReversal.objects.filter(payment_id=refund_order.payment_id).count()==1
            assert NetworkJob.objects.count()==jobs_before+1
            if case=="gateway_refund":
                assert results[0]["ok"]==results[1]["ok"]==str(resolution.pk)
                print("PASS: 2 processes, same verified full refund -> 1 reversal/resolution/network job",flush=True)
            else:
                assert results[0]["ok"]==str(resolution.pk) and results[1]["ok"]=="refunded"
                print("PASS: 2 processes, refund commits before late settlement -> reversal preserved, receivable reopened",flush=True)
        import pyotp
        from core.models import MFADevice
        from core.security import encrypt_secret, decrypt_secret
        fixture_secret = pyotp.random_base32()
        MFADevice.objects.create(user=actor, encrypted_secret=encrypt_secret(fixture_secret))
        before = database_fingerprint(connection)
        with tempfile.TemporaryDirectory(prefix="tagihnet-pg-restore-") as folder:
            archive = Path(folder)/"backup.dump"
            with os.fdopen(os.open(archive, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600), "wb") as stream:
                pg_tool("pg_dump", database_name, connection.settings_dict["USER"], stdout=stream)
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("CREATE DATABASE {} WITH TEMPLATE template0").format(sql.Identifier(restore_name)))
            restore_created = True
            with archive.open("rb") as stream:
                pg_tool("pg_restore", restore_name, connection.settings_dict["USER"], stdin=stream, stdout=subprocess.DEVNULL)
            connection.close()
            connection.settings_dict["NAME"] = restore_name
            assert database_fingerprint(connection) == before
        print("PASS: pg_dump/pg_restore into a fresh database; every application/auth table count and payload reconciles", flush=True)
        with tempfile.TemporaryDirectory(prefix="tagihnet-pg-encrypted-") as folder:
            import io
            from core.backup_archive import encrypt_stream, generate_key, read_key
            key = Path(folder)/"backup-key.json"
            wrong_key = Path(folder)/"wrong-key.json"
            encrypted = Path(folder)/"backup.tnbk"
            generate_key(key)
            generate_key(wrong_key)
            def backup_cli(*arguments, expected=0):
                result = subprocess.run([sys.executable, str(ROOT/"scripts/backup.py"), *map(str, arguments)],
                    env=os.environ, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                assert (result.returncode == 0) == (expected == 0), result.stderr
                assert "Traceback" not in result.stderr
                return result
            backup_cli("backup", encrypted, "--key-file", key)
            backup_cli("verify", encrypted, "--key-file", key)
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("CREATE DATABASE {} WITH TEMPLATE template0").format(sql.Identifier(encrypted_restore_name)))
            encrypted_restore_created = True
            backup_cli("restore", encrypted, "--key-file", wrong_key, "--target-database", encrypted_restore_name, expected=1)
            backup_cli("restore", encrypted, "--key-file", key, "--target-database", database_name, expected=1)
            backup_cli("restore", encrypted, "--key-file", key, "--target-database", restore_name, expected=1)
            backup_cli("restore", encrypted, "--key-file", key, "--target-database", encrypted_restore_name)
            connection.close()
            connection.settings_dict["NAME"] = encrypted_restore_name
            assert database_fingerprint(connection) == before
            assert decrypt_secret(MFADevice.objects.get(user_id=actor.pk).encrypted_secret) == fixture_secret
            backup_cli("restore", encrypted, "--key-file", key, "--target-database", encrypted_restore_name, expected=1)
            assert database_fingerprint(connection) == before
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("CREATE DATABASE {} WITH TEMPLATE template0").format(sql.Identifier(broken_restore_name)))
            broken_restore_created = True
            invalid = Path(folder)/"invalid-dump.tnbk"
            encrypt_stream(io.BytesIO(b"PGDMP"+b"\x00"*128), invalid, read_key(key), "postgresql")
            result = backup_cli("restore", invalid, "--key-file", key, "--target-database", broken_restore_name, expected=1)
            assert "pg_restore gagal" in result.stderr
            connection.close()
            connection.settings_dict["NAME"] = broken_restore_name
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'")
                assert cursor.fetchone()[0] == 0
        print("PASS: encrypted PostgreSQL restore reconciles all tables/MFA payload; wrong key/source/occupied/invalid dump targets rejected", flush=True)
    finally:
        connection.close()
        connection.settings_dict["NAME"] = original_name
        os.environ["TAGIHNET_DB_NAME"] = original_name
        with connection.cursor() as cursor:
            if broken_restore_created:
                cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(broken_restore_name)))
            if encrypted_restore_created:
                cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(encrypted_restore_name)))
            if restore_created:
                cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(restore_name)))
            cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))
        connection.close()
        print("Disposable PostgreSQL database removed; original database untouched.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child")
    args = parser.parse_args()
    child(json.loads(args.child)) if args.child else verify()
