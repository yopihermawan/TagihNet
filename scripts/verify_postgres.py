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
        if case == "photo":
            from core.work_photos import upload_photo
            from core.tests.photo_helpers import photo_upload
            return str(upload_photo(actor, data["ticket"], photo_upload(), "Foto proses paralel", True,
                1, uuid.UUID(data["key"]), 1).pk)
        if case == "plan_change":
            from core.plans import confirm_change
            return confirm_change(actor, data["token"], time.time(), 1).pk
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
        from core.plans import review_change
        from core.models import AuditEvent, Plan
        token, _ = review_change(actor, {"name": "Process plan", "speed_mbps": 75, "price": 350000}, 1)
        results = parallel("plan_change", {**common, "token": token})
        assert results[0]["ok"] == results[1]["ok"]
        package = Plan.objects.get(tenant=tenant, name="Process plan")
        assert AuditEvent.objects.filter(action="plan.created", target=str(package.pk)).count() == 1
        print("PASS: 2 processes, same package confirmation -> 1 package/audit", flush=True)
        reviews = [review_change(actor, {"name": package.name, "speed_mbps": 75, "price": price},
            package.version, package.pk)[0] for price in [400000, 500000]]
        results = parallel("plan_change", common, [{"token": token} for token in reviews])
        assert sum("ok" in result for result in results) == 1 and sum("rejected" in result for result in results) == 1
        package.refresh_from_db()
        assert package.version == 2 and package.price in {400000, 500000}
        assert AuditEvent.objects.filter(action="plan.updated", target=str(package.pk)).count() == 1
        print("PASS: 2 processes, competing package reviews -> 1 edit, 1 stale rejection", flush=True)
        # A read-only report snapshot stays coherent while two real payment processes commit.
        from core.billing_reports import BillingReport, report_snapshot
        report_invoice, _ = issue_invoice(tenant, customer.pk, date(2032, 1, 1), actor)
        report_day = timezone.localdate()
        with report_snapshot():
            report = BillingReport(actor, report_day.replace(day=1), report_day)
            initial = report.summary()
            event_count = report.rows("cash").count()
            results = parallel("payment", {**common, "invoice": report_invoice.pk, "amount": 1234, "key": str(uuid.uuid4())})
            assert sorted(result["ok"] for result in results) == ["created", "replayed"]
            assert report.summary() == initial and report.rows("cash").count() == event_count
        refreshed = BillingReport(actor, report_day.replace(day=1), report_day).summary()
        assert refreshed["received"] == initial["received"]+1234 and refreshed["reconciles"]
        print("PASS: repeatable read-only report snapshot across 2 committing payment processes; next read includes payment", flush=True)
        from core.models import WorkPhoto
        from core.operations import create_ticket
        from core.work_photos import upload_photo
        from core.tests.photo_helpers import photo_upload
        photo_ticket = create_ticket(actor, customer.pk, "Photo process fixture", "Private work evidence", "connection")
        results = parallel("photo", {**common, "ticket": photo_ticket.pk, "key": str(uuid.uuid4())})
        assert results[0]["ok"] == results[1]["ok"] and WorkPhoto.objects.filter(ticket=photo_ticket).count() == 1
        print("PASS: 2 processes, same photo request -> 1 binary/photo/timeline", flush=True)
        for index in range(18):
            upload_photo(actor, photo_ticket.pk, photo_upload(), f"Existing slot {index}", True, 1, uuid.uuid4(), 1)
        results = parallel("photo", {**common, "ticket": photo_ticket.pk}, [{"key": str(uuid.uuid4())} for _ in range(2)])
        assert sum("ok" in result for result in results) == 1 and sum("rejected" in result for result in results) == 1
        assert WorkPhoto.objects.filter(ticket=photo_ticket).count() == 20
        print("PASS: 2 processes, final photo slot -> 1 accepted, 1 quota rejection; no overflow", flush=True)
        # Include a non-null mapping in the whole-database restore fingerprint.
        from core.forms import CustomerForm
        from core.models import MonitoringDevice
        nas = MonitoringDevice.objects.create(tenant=tenant, name="Restore mapping fixture")
        fields = {key: getattr(customer, key) for key in ["name", "email", "phone", "username", "area", "address"]}
        form = CustomerForm({**fields, "plan": customer.plan_id, "nas": nas.pk}, tenant=tenant, instance=customer)
        assert form.is_valid(), form.errors
        form.save()
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
