"""Consistent snapshots and validated restores; no web endpoint or public file storage."""
import os
import re
import secrets
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from .backup_archive import ArchiveError, MAX_PAYLOAD, atomic_output, decrypt_stream, encrypt_stream


def sqlite_check(path):
    with sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ArchiveError("Integrity check SQLite gagal.")


def sqlite_backup(database, archive, key):
    if not Path(database).is_file():
        raise ArchiveError("Database SQLite sumber tidak ditemukan.")
    if Path(database).stat().st_size > MAX_PAYLOAD:
        raise ArchiveError("Database sumber melebihi batas 1 GiB.")
    with tempfile.TemporaryDirectory(prefix=".tagihnet-snapshot-", dir=Path(database).parent) as folder:
        snapshot = Path(folder)/"snapshot.sqlite3"
        snapshot.touch(mode=0o600)
        with sqlite3.connect(Path(database).resolve().as_uri()+"?mode=ro", uri=True) as source:
            with sqlite3.connect(snapshot) as destination:
                source.backup(destination)
        sqlite_check(snapshot)
        with snapshot.open("rb") as source:
            return encrypt_stream(source, archive, key, "sqlite")


def decrypted_file(archive, key, folder):
    """Caller owns the private temporary directory. No database is touched before GCM authentication."""
    payload = Path(folder)/"payload"
    with os.fdopen(os.open(payload, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as destination:
        with Path(archive).open("rb") as source:
            header = decrypt_stream(source, destination, key)
    return payload, header


def sqlite_restore(archive, database, key):
    database = Path(database)
    if any(os.path.lexists(str(database)+suffix) for suffix in ["", "-wal", "-shm", "-journal"]):
        raise ArchiveError("Database target sudah ada; restore tidak akan menimpanya.")
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".tagihnet-restore-", dir=database.parent) as folder:
        payload, header = decrypted_file(archive, key, folder)
        if header["kind"] != "sqlite":
            raise ArchiveError("Jenis arsip tidak sesuai dengan target SQLite.")
        sqlite_check(payload)
        if any(os.path.lexists(str(database)+suffix) for suffix in ["-wal", "-shm", "-journal"]):
            raise ArchiveError("Sidecar SQLite target sudah ada; hentikan proses dan gunakan tujuan kosong.")
        with payload.open("rb") as source, atomic_output(database) as destination:
            while chunk := source.read(1024*1024):
                destination.write(chunk)
    return header


def pg_config(environ):
    from config.database import database_config
    config = database_config(environ, Path(environ.get("TAGIHNET_DATA_DIR", "data")), environ.get("TAGIHNET_DEBUG", "1") == "1")
    if config["ENGINE"] != "django.db.backends.postgresql":
        raise ArchiveError("Pilih backend PostgreSQL secara eksplisit.")
    return config


def pg_command(tool, database, config, environ):
    env = dict(environ)
    container = env.get("TAGIHNET_PG_TOOLS_CONTAINER")
    if container:
        for key in ["DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"]:
            env.pop(key, None)
        # Confirm the tools container reaches the SAME server/database as the configured endpoint.
        # A random advisory marker is observable only on that endpoint, without granting admin privileges.
        import psycopg
        token = secrets.randbits(63)
        options = {"sslmode": config["OPTIONS"]["sslmode"], "connect_timeout": 5}
        if config["OPTIONS"].get("sslrootcert"):
            options["sslrootcert"] = config["OPTIONS"]["sslrootcert"]
        with psycopg.connect(dbname=database, host=config["HOST"], port=config["PORT"], user=config["USER"],
                password=config["PASSWORD"], autocommit=True, **options) as connection:
            pid = connection.execute("SELECT pg_backend_pid()").fetchone()[0]
            connection.execute("SELECT pg_advisory_lock(%s)", (token,))
            query = ("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND granted "
                f"AND pid={pid} AND classid={token >> 32} AND objid={token & 0xffffffff} "
                "AND database=(SELECT oid FROM pg_database WHERE datname=current_database()))")
            probe = subprocess.run(["docker", "--host=unix:///var/run/docker.sock", "exec", container,
                "psql", "--username", config["USER"], "--dbname", database, "--tuples-only", "--no-align", "--command", query],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            if probe.returncode or probe.stdout.strip() != b"t":
                raise ArchiveError("Tools container tidak cocok dengan endpoint/database PostgreSQL terkonfigurasi.")
        command = ["docker", "--host=unix:///var/run/docker.sock", "exec", "-i", container, tool,
                   "--username", config["USER"], "--dbname", database]
    else:
        env.update(PGHOST=config["HOST"], PGPORT=str(config["PORT"]), PGUSER=config["USER"],
            PGPASSWORD=config["PASSWORD"], PGSSLMODE=config["OPTIONS"]["sslmode"], PGCONNECT_TIMEOUT="5")
        if config["OPTIONS"].get("sslrootcert"):
            env["PGSSLROOTCERT"] = config["OPTIONS"]["sslrootcert"]
        command = [tool, "--dbname", database]
    command += (["--format=custom", "--lock-wait-timeout=5000"] if tool == "pg_dump"
                else ["--no-owner", "--no-acl", "--single-transaction", "--exit-on-error"])
    return command, env


def postgres_backup(archive, key, environ):
    config = pg_config(environ)
    command, env = pg_command("pg_dump", config["NAME"], config, environ)
    # Dump stdout streams directly into encryption. Diagnostic text never enters the archive or user output.
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=errors)
        try:
            # Stage the encrypted archive until pg_dump also confirms success.
            target = Path(archive)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix=".tagihnet-pg-backup-", dir=target.parent) as folder:
                staged = Path(folder)/"archive.tnbk"
                header = encrypt_stream(process.stdout, staged, key, "postgresql")
                if process.wait(timeout=30) != 0:
                    raise ArchiveError("pg_dump gagal. Arsip tidak diterbitkan; periksa koneksi/izin/client server.")
                with staged.open("rb") as source, atomic_output(target) as destination:
                    while chunk := source.read(1024*1024):
                        destination.write(chunk)
            return header
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()


def postgres_restore(archive, target_database, key, environ):
    import psycopg
    config = pg_config(environ)
    if (not target_database or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", target_database)
            or target_database == config["NAME"] or target_database in {"postgres", "template0", "template1"}):
        raise ArchiveError("Restore PG memerlukan --target-database berbeda dari database sumber terkonfigurasi.")
    with tempfile.TemporaryDirectory(prefix="tagihnet-pg-restore-") as folder:
        payload, header = decrypted_file(archive, key, folder)
        with payload.open("rb") as stream:
            signature = stream.read(5)
        if header["kind"] != "postgresql" or signature != b"PGDMP":
            raise ArchiveError("Payload bukan dump PostgreSQL custom yang sesuai.")
        options = {"sslmode": config["OPTIONS"]["sslmode"], "connect_timeout": 5,
                   "options": "-c lock_timeout=5000 -c statement_timeout=30000"}
        if config["OPTIONS"].get("sslrootcert"):
            options["sslrootcert"] = config["OPTIONS"]["sslrootcert"]
        with psycopg.connect(dbname=target_database, host=config["HOST"], port=config["PORT"], user=config["USER"],
                password=config["PASSWORD"], autocommit=True, **options) as connection:
            if not connection.execute("SELECT pg_try_advisory_lock(6074867403292372993)").fetchone()[0]:
                raise ArchiveError("Restore lain sedang mengunci target ini.")
            if not connection.execute("SELECT pg_get_userbyid(datdba)=current_user FROM pg_database WHERE datname=current_database()").fetchone()[0]:
                raise ArchiveError("Role restore harus pemilik database target.")
            occupied = connection.execute("""SELECT EXISTS(
                SELECT 1 FROM pg_depend d JOIN pg_namespace n ON n.oid=d.refobjid
                WHERE d.refclassid='pg_namespace'::regclass AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
            ) OR EXISTS(SELECT 1 FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname NOT IN ('public','information_schema'))
              OR EXISTS(SELECT 1 FROM pg_extension WHERE extname <> 'plpgsql')
              OR EXISTS(SELECT 1 FROM pg_largeobject_metadata)""").fetchone()[0]
            if occupied:
                raise ArchiveError("Database target tidak kosong; tidak ada objek yang dihapus atau ditimpa.")
            command, env = pg_command("pg_restore", target_database, config, environ)
            with payload.open("rb") as source, tempfile.TemporaryFile() as errors:
                result = subprocess.run(command, env=env, stdin=source, stdout=subprocess.DEVNULL, stderr=errors)
            if result.returncode:
                raise ArchiveError("pg_restore gagal; transaksi target di-rollback. Periksa izin/client/schema.")
        return header
