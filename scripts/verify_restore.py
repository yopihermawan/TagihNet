"""Restore drill in an empty temporary directory; never overwrites live data."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

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
        return {"customers": customers, "invoices": invoices, "invoiced_rupiah": total, "paid_rupiah": paid,
                "receivable_rupiah": total-paid, "billing_batches": batches, "batch_states": batch_states, "batch_items": batch_items,
                "ticket_states": ticket_states, "installation_states": installation_states, "work_events": work_events}
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
