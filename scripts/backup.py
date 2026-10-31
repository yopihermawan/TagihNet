"""Consistent local SQLite backup/restore. Archive encryption is a deployment prerequisite."""
import argparse
import os
import sqlite3
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("action", choices=["backup", "restore"])
parser.add_argument("archive", type=Path)
args = parser.parse_args()
base = Path(__file__).resolve().parent.parent
data = Path(os.getenv("TAGIHNET_DATA_DIR", base / "data"))
database = data / "tagihnet.sqlite3"
source, target = (database, args.archive) if args.action == "backup" else (args.archive, database)
if not source.is_file():
    parser.error("Database sumber tidak ditemukan")
target.parent.mkdir(parents=True, exist_ok=True)
if target.exists():
    parser.error("Target sudah ada; tidak akan ditimpa. Hentikan proses dan gunakan direktori kosong saat restore.")
with sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as connection:
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        parser.error("Integrity check sumber gagal")
    # Exclusive file creation prevents silently replacing an archive.
    target.touch(mode=0o600, exist_ok=False)
    with sqlite3.connect(target) as destination:
        connection.backup(destination)
        assert destination.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
print(f"{args.action}: selesai; integrity_check=ok. Backup berisi data privat, simpan secara terenkripsi.")
