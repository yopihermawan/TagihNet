"""Local backup/restore: encrypted SQLite/PostgreSQL, or legacy unencrypted SQLite compatibility."""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
from core.backup_archive import ArchiveError, MAGIC, generate_key, read_key, verify_archive
from core.backup_database import postgres_backup, postgres_restore, sqlite_backup, sqlite_restore


def legacy_sqlite(action, archive, data):
    if os.getenv("TAGIHNET_DB_ENGINE", "sqlite") != "sqlite":
        raise ArchiveError("Skrip tanpa --key-file khusus SQLite. PostgreSQL memerlukan arsip terenkripsi dengan --key-file.")
    database = data/"tagihnet.sqlite3"
    source, target = (database, archive) if action == "backup" else (archive, database)
    if not source.is_file():
        raise ArchiveError("Database sumber tidak ditemukan.")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(os.path.lexists(str(target)+suffix) for suffix in ["", "-wal", "-shm", "-journal"]):
        raise ArchiveError("Target sudah ada; tidak akan ditimpa.")
    with source.open("rb") as stream:
        if stream.read(len(MAGIC)) == MAGIC:
            raise ArchiveError("Arsip terenkripsi memerlukan --key-file.")
    # Keep existing development/migration scripts compatible. This path is explicitly not encrypted.
    with sqlite3.connect(source.resolve().as_uri()+"?mode=ro", uri=True) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ArchiveError("Integrity check sumber gagal.")
        target.touch(mode=0o600, exist_ok=False)
        try:
            with sqlite3.connect(target) as destination:
                connection.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ArchiveError("Integrity check target gagal.")
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    print(f"{action}: selesai; integrity_check=ok. Mode kompatibilitas SQLite tanpa enkripsi.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "restore", "keygen", "verify"])
    parser.add_argument("archive", type=Path, help="Path arsip, atau path kunci baru untuk keygen.")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--target-database", help="Database PG kosong yang sudah dibuat; harus berbeda dari sumber terkonfigurasi.")
    args = parser.parse_args()
    try:
        if args.action == "keygen":
            if args.key_file or args.target_database:
                raise ArchiveError("keygen hanya menerima path kunci baru.")
            generate_key(args.archive)
            print("Kunci backup baru tersimpan privat. Simpan terpisah dari arsip; nilai kunci tidak ditampilkan.")
            return
        data = Path(os.getenv("TAGIHNET_DATA_DIR", BASE/"data"))
        if not args.key_file:
            if args.action == "verify" or args.target_database:
                raise ArchiveError("Operasi ini memerlukan --key-file.")
            legacy_sqlite(args.action, args.archive, data)
            return
        key = read_key(args.key_file)
        if args.action == "verify":
            header = verify_archive(args.archive, key)
        else:
            engine = os.getenv("TAGIHNET_DB_ENGINE", "sqlite")
            if engine == "sqlite":
                if args.target_database:
                    raise ArchiveError("--target-database hanya untuk PostgreSQL.")
                function = sqlite_backup if args.action == "backup" else sqlite_restore
                params = (data/"tagihnet.sqlite3", args.archive, key) if args.action == "backup" else (args.archive, data/"tagihnet.sqlite3", key)
                header = function(*params)
            elif engine == "postgresql":
                header = (postgres_backup(args.archive, key, os.environ) if args.action == "backup" else
                    postgres_restore(args.archive, args.target_database, key, os.environ))
            else:
                raise ArchiveError("Backend database tidak dikenal; tidak ada fallback.")
        outcome = "arsip AES-256-GCM tersimpan; jalankan verify untuk baca ulang" if args.action == "backup" else "autentikasi AES-256-GCM valid"
        print(f"{args.action}: selesai; {outcome}; jenis={header['kind']}; versi={header['version']}.")
    except ArchiveError as exc:
        parser.exit(1, f"Ditolak: {exc}\n")
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Gagal: {type(exc).__name__}; periksa file, izin, ruang disk dan format. Tidak ada secret ditampilkan.\n")
    except Exception as exc:
        # Driver/config errors may include connection diagnostics. Never echo credential-bearing representations.
        parser.exit(1, f"Gagal: {type(exc).__name__}; periksa konfigurasi/layanan dan log lokal yang terlindungi.\n")


if __name__ == "__main__":
    main()
