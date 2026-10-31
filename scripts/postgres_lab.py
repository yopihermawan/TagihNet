"""Disposable local PostgreSQL lab. Keeps the application's SQLite default unchanged."""
import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAB = ROOT/"data/postgres-lab"
MANIFEST = LAB/"manifest.json"
IMAGE = "postgres@sha256:e8a07d2c7579b038938cdba6c539e580e5001caf9ea203d397b5cc83e76bd938"
LABEL = "tagihnet.local-lab"


def docker(*arguments, capture=False, check=True):
    env = {key: value for key, value in os.environ.items()
           if key not in {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}}
    return subprocess.run(["docker", "--host=unix:///var/run/docker.sock", *arguments], env=env,
        check=check, text=True, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture else None)


def read_manifest():
    if not MANIFEST.exists():
        raise RuntimeError("Jalankan postgres_lab.py start terlebih dahulu.")
    return json.loads(MANIFEST.read_text())


def inspected(manifest):
    result = docker("inspect", manifest["container"], capture=True, check=False)
    if result.returncode:
        raise RuntimeError("Container lab tidak ditemukan. Pulihkan lab secara eksplisit; SQLite tidak diubah.")
    info = json.loads(result.stdout)[0]
    if info["Config"].get("Labels", {}).get(LABEL) != manifest["container"]:
        raise RuntimeError("Label container tidak sesuai; operasi ditolak.")
    return info


def connection_env(manifest):
    return {**os.environ, "TAGIHNET_DB_ENGINE": "postgresql", "TAGIHNET_DB_NAME": "tagihnet_lab",
        "TAGIHNET_DB_USER": "tagihnet_app", "TAGIHNET_DB_HOST": "127.0.0.1",
        "TAGIHNET_DB_PORT": str(manifest["port"]), "TAGIHNET_DB_PASSWORD": "",
        "TAGIHNET_DB_PASSWORD_FILE": str(LAB/"app-password"), "TAGIHNET_DB_SSLMODE": "disable",
        "TAGIHNET_DEBUG": "1", "TAGIHNET_SIMULATION": "1", "TAGIHNET_DATA_DIR": str(LAB/"app"),
        "TAGIHNET_PG_TOOLS_CONTAINER": manifest["container"]}


def create_container(manifest):
    if docker("image", "inspect", manifest["image"], capture=True, check=False).returncode:
        docker("pull", manifest["image"])
    result = docker("volume", "inspect", manifest["volume"], capture=True, check=False)
    if result.returncode == 0:
        info = json.loads(result.stdout)[0]
        if (info.get("Labels") or {}).get(LABEL) != manifest["container"]:
            raise RuntimeError("Volume tidak dimiliki lab ini; operasi ditolak.")
    elif "no such volume" in result.stderr.lower():
        docker("volume", "create", "--label", LABEL+"="+manifest["container"], manifest["volume"])
    else:
        raise RuntimeError("Metadata volume lab tidak dapat dibaca.")
    docker("run", "--detach", "--name", manifest["container"], "--label", LABEL+"="+manifest["container"],
        "--publish", f"127.0.0.1:{manifest['port']}:5432", "--env", "POSTGRES_PASSWORD_FILE=/run/secrets/pg-password",
        "--mount", f"type=bind,src={LAB/'admin-password'},dst=/run/secrets/pg-password,readonly",
        "--mount", f"type=volume,src={manifest['volume']},dst=/var/lib/postgresql/data", manifest["image"])


def start():
    import psycopg
    from psycopg import sql
    docker("info", "--format", "{{.ServerVersion}}", capture=True)
    LAB.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not MANIFEST.exists():
        for name in ["admin-password", "app-password"]:
            path = LAB/name
            if not path.exists():
                with os.fdopen(os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600), "w") as stream:
                    stream.write(secrets.token_urlsafe(40))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        name = "tagihnet-pg-lab-"+uuid.uuid4().hex[:10]
        manifest = {"container": name, "volume": name+"-data", "port": port, "image": IMAGE}
        with os.fdopen(os.open(MANIFEST, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600), "w") as stream:
            json.dump(manifest, stream)
    manifest = read_manifest()
    existing = docker("inspect", manifest["container"], capture=True, check=False)
    if existing.returncode:
        if "no such object" not in existing.stderr.lower():
            raise RuntimeError("Metadata container lab tidak dapat dibaca.")
        create_container(manifest)
    info = inspected(manifest)
    if not info["State"]["Running"]:
        docker("start", manifest["container"])
    last_error = None
    for _ in range(100):
        try:
            admin = psycopg.connect(host="127.0.0.1", port=manifest["port"], dbname="postgres", user="postgres",
                password=(LAB/"admin-password").read_text(), sslmode="disable", connect_timeout=1, autocommit=True)
            break
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(.2)
    else:
        raise RuntimeError("PostgreSQL lab belum siap; periksa container lokal.") from last_error
    with admin:
        if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname='tagihnet_app'").fetchone():
            admin.execute(sql.SQL("CREATE ROLE tagihnet_app LOGIN CREATEDB NOSUPERUSER NOCREATEROLE NOREPLICATION PASSWORD {}")
                .format(sql.Literal((LAB/"app-password").read_text())))
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname='tagihnet_lab'").fetchone():
            admin.execute("CREATE DATABASE tagihnet_lab OWNER tagihnet_app")
    env = connection_env(manifest)
    with psycopg.connect(host=env["TAGIHNET_DB_HOST"], port=env["TAGIHNET_DB_PORT"], dbname=env["TAGIHNET_DB_NAME"],
            user=env["TAGIHNET_DB_USER"], password=(LAB/"app-password").read_text(), sslmode="disable") as app:
        version, superuser = app.execute("SELECT version(),rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()
        assert not superuser
        print("PASS: PostgreSQL lab ready; application role is not superuser;", version.split(" on ")[0])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status", "run"])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "start":
        start()
        return
    manifest = read_manifest()
    info = inspected(manifest)
    if args.action == "stop":
        docker("stop", manifest["container"])
        print("Lab dihentikan; volume tetap tersimpan. Database SQLite tidak diubah.")
    elif args.action == "status":
        print("PostgreSQL lab:", "running" if info["State"]["Running"] else "stopped", "port", manifest["port"])
    else:
        if not args.command or not info["State"]["Running"]:
            parser.error("Lab harus berjalan dan perintah Python perlu diberikan.")
        raise SystemExit(subprocess.run([sys.executable, *args.command], cwd=ROOT, env=connection_env(manifest)).returncode)


if __name__ == "__main__":
    main()
