"""Restore drill in an empty temporary directory; never overwrites live data."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

if os.getenv("TAGIHNET_DB_ENGINE", "sqlite") != "sqlite":
    raise SystemExit("Drill ini khusus SQLite. Gunakan verify_postgres.py untuk restore PostgreSQL termasuk arsip terenkripsi.")

base = Path(__file__).resolve().parent.parent
data = Path(os.getenv("TAGIHNET_DATA_DIR", base / "data"))
def reconcile(path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        customers = db.execute("SELECT COUNT(*) FROM core_customer").fetchone()[0]
        invoices, total = db.execute("SELECT COUNT(*), COALESCE(SUM(total),0) FROM core_invoice").fetchone()
        paid = db.execute("SELECT COALESCE(SUM(p.amount),0) FROM core_payment p LEFT JOIN core_paymentreversal r ON r.payment_id=p.id WHERE r.id IS NULL").fetchone()[0]
        batches = db.execute("SELECT COUNT(*) FROM core_billingbatch").fetchone()[0]
        batch_items = dict(db.execute("SELECT status, COUNT(*) FROM core_billingbatchitem GROUP BY status").fetchall())
        batch_states = dict(db.execute("SELECT status, COUNT(*) FROM core_billingbatch GROUP BY status").fetchall())
        ticket_states = dict(db.execute("SELECT status, COUNT(*) FROM core_ticket GROUP BY status").fetchall())
        installation_states = dict(db.execute("SELECT status, COUNT(*) FROM core_installation GROUP BY status").fetchall())
        work_events = db.execute("SELECT COUNT(*) FROM core_workevent").fetchone()[0]
        reminder_states = dict(db.execute("SELECT status, COUNT(*) FROM core_reminder GROUP BY status").fetchall())
        reminder_policies = db.execute("SELECT tenant_id,billing_enabled,sla_enabled,version,authorization_version FROM core_reminderpolicy ORDER BY tenant_id").fetchall()
        sla_cycles = db.execute("SELECT id,sla_cycle,version FROM core_ticket ORDER BY id").fetchall()
        installation_versions = db.execute("SELECT id,version,scheduled_for FROM core_installation ORDER BY id").fetchall()
        monitor_devices = db.execute("SELECT id,tenant_id,enabled,scenario,version,authorization_version,next_poll_at FROM core_monitoringdevice ORDER BY id").fetchall()
        monitor_polls = db.execute("SELECT id,device_id,tenant_id,device_version,bucket,status,observed_at,sessions FROM core_monitoringpoll ORDER BY id").fetchall()
        import_batches = db.execute("SELECT id,tenant_id,created_by_id,status,fingerprint,authorization_version FROM core_customerimportbatch ORDER BY id").fetchall()
        import_rows = db.execute("SELECT id,batch_id,row_number,status,data,plan_snapshot,error,customer_id FROM core_customerimportrow ORDER BY id").fetchall()
        customer_nas = db.execute("SELECT id,tenant_id,nas_id FROM core_customer ORDER BY id").fetchall()
        plans = db.execute("SELECT id,tenant_id,name,speed_mbps,price,version,creation_key FROM core_plan ORDER BY id").fetchall()
        return {"customers": customers, "invoices": invoices, "invoiced_rupiah": total, "paid_rupiah": paid,
                "receivable_rupiah": total-paid, "billing_batches": batches, "batch_states": batch_states, "batch_items": batch_items,
                "ticket_states": ticket_states, "installation_states": installation_states, "work_events": work_events,
                "reminder_states": reminder_states, "reminder_policies": reminder_policies,
                "sla_cycles": sla_cycles, "installation_versions": installation_versions,
                "monitor_devices": monitor_devices, "monitor_polls": monitor_polls,
                "import_batches": import_batches, "import_rows": import_rows, "plans": plans, "customer_nas": customer_nas}
before = reconcile(data / "tagihnet.sqlite3")
with tempfile.TemporaryDirectory(prefix="tagihnet-restore-") as directory:
    archive = Path(directory) / "backup.sqlite3"
    subprocess.run([sys.executable, str(base / "scripts/backup.py"), "backup", str(archive)], check=True)
    fresh = Path(directory) / "restored"
    subprocess.run([sys.executable, str(base / "scripts/backup.py"), "restore", str(archive)], check=True,
                   env={**os.environ, "TAGIHNET_DATA_DIR": str(fresh)})
    restored = reconcile(fresh / "tagihnet.sqlite3")
    assert before == restored, (before, restored)
print("PASS: restore into empty directory; counts and ledger reconcile:", json.dumps(before))
