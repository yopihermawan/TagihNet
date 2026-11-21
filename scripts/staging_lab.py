"""Isolated local HTTPS staging. Never reads the development database or its private keys."""
import argparse
import fcntl
import json
import os
import re
import secrets
import ssl
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
PG_IMAGE = "postgres:16-bookworm@sha256:e8a07d2c7579b038938cdba6c539e580e5001caf9ea203d397b5cc83e76bd938"


def docker(*arguments, capture=False, check=True, timeout=None, stage_image=None):
    env = {key:value for key,value in os.environ.items() if key not in {
        "DOCKER_HOST","DOCKER_CONTEXT","DOCKER_TLS","DOCKER_TLS_VERIFY","DOCKER_CERT_PATH"}}
    if stage_image is not None:env['TAGIHNET_STAGING_IMAGE']=stage_image
    return subprocess.run(["docker","--host=unix:///var/run/docker.sock",*map(str,arguments)],env=env,
        check=check,text=True,stdout=subprocess.PIPE if capture else None,stderr=subprocess.PIPE if capture else None,timeout=timeout)


def manifest(directory):
    data = json.loads((Path(directory)/"manifest.json").read_text())
    if not re.fullmatch(r"tagihnet-stage-[a-f0-9]{10}",data["project"]):
        raise ValueError("Identitas proyek staging tidak valid.")
    if Path(data["directory"])!=Path(directory).resolve():
        raise ValueError("Lokasi staging tidak sesuai manifest.")
    if 'image_id' in data and (not isinstance(data['image_id'],str) or not re.fullmatch('sha256:[a-f0-9]{64}',data['image_id'])):
        raise ValueError('Manifest memerlukan image ID immutable yang valid.')
    return data


def write_manifest(directory,data):
    with tempfile.NamedTemporaryFile(mode='w',prefix='.manifest-',dir=directory,delete=False) as stream:
        temporary=Path(stream.name)
        try:
            json.dump(data,stream)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary,Path(directory)/'manifest.json')
            parent=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(parent)
            finally:os.close(parent)
        finally: temporary.unlink(missing_ok=True)


@contextmanager
def operation_lock(directory):
    descriptor=os.open(Path(directory)/'.operation.lock',os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        try: fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Operasi lain sedang memakai direktori staging ini.') from None
        yield
    finally: os.close(descriptor)


def compose(directory,*arguments,**kwargs):
    data=manifest(directory)
    if data.get('image_id') and arguments and arguments[0] in {'up','run','create','start','restart'}:
        require_image(data['image_id'])
    return docker("compose","--project-name",data["project"],"--env-file",Path(directory)/"compose.env",
        "--file",ROOT/"compose.staging.yaml",*arguments,stage_image=data.get('image_id','tagihnet-staging:local'),**kwargs)


def require_image(image_id):
    found=docker('image','inspect',image_id,'--format','{{.Id}}',capture=True,timeout=15).stdout.strip()
    if found!=image_id:raise RuntimeError('Image pilihan tidak tersedia lokal; tidak ada fallback/pull ke tag lain.')


def pin_image(directory,image_id):
    if not isinstance(image_id,str) or not re.fullmatch('sha256:[a-f0-9]{64}',image_id):
        raise ValueError('Gunakan image ID sha256 lengkap, bukan tag.')
    with operation_lock(directory):
        state=manifest(directory)
        if state.get('restore_state') in {'preparing','failed'}:raise RuntimeError('Restore target masih diblokir.')
        require_image(image_id)
        state['image_id']=image_id
        write_manifest(directory,state)
    return image_id


def prepare(directory,port=18443):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.fernet import Fernet
    from cryptography.x509.oid import NameOID
    import ipaddress
    directory=Path(directory).resolve()
    if not re.fullmatch(r"[/A-Za-z0-9_.-]+",str(directory)) or not 1024<=port<=65535:
        raise ValueError("Direktori tanpa spasi/karakter shell dan port1024–65535 diperlukan.")
    directory.mkdir(parents=True,mode=0o700,exist_ok=False)
    private=directory/"secrets"
    private.mkdir(mode=0o700)
    def write(name,content):
        with os.fdopen(os.open(private/name,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),"wb") as stream:
            stream.write(content)
    for name in ("admin-password","runtime-password","migration-password","bootstrap-password"):
        write(name,secrets.token_urlsafe(48).encode())
    write("session-key",secrets.token_urlsafe(64).encode())
    write("mfa-key",Fernet.generate_key())
    now=datetime.now(timezone.utc)
    def key(): return rsa.generate_private_key(public_exponent=65537,key_size=2048)
    def store_key(name,value):
        write(name,value.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    ca_key=key()
    issuer=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"TagihNet local staging CA")])
    ca=(x509.CertificateBuilder().subject_name(issuer).issuer_name(issuer).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=90))
        .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True)
        .add_extension(x509.KeyUsage(False,False,False,False,False,True,True,False,False),critical=True)
        .sign(ca_key,hashes.SHA256()))
    store_key("ca.key",ca_key)
    write("ca.crt",ca.public_bytes(serialization.Encoding.PEM))
    for name,host in (("https","localhost"),("postgres","db")):
        private_key=key()
        cert=(x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,host)]))
            .issuer_name(issuer).public_key(private_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host),x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),critical=False)
            .sign(ca_key,hashes.SHA256()))
        store_key(name+".key",private_key)
        write(name+".crt",cert.public_bytes(serialization.Encoding.PEM))
    project="tagihnet-stage-"+uuid.uuid4().hex[:10]
    (directory/"manifest.json").write_text(json.dumps({"project":project,"port":port,"directory":str(directory),"initialized":False}))
    (directory/"compose.env").write_text(f"TAGIHNET_STAGING_DIR={directory}\nTAGIHNET_STAGING_PORT={port}\n")
    for file in ("manifest.json","compose.env"): (directory/file).chmod(0o600)
    print("Staging prepared: independent private keys, passwords and local CA; nothing seeded or exposed.")


def build(proxy_ca=None):
    if proxy_ca:
        # Session-only CA trust. Mount as BuildKit secret; never copy it into an image layer.
        source=(ROOT/"Dockerfile.staging").read_text().replace("RUN pip install",
            "RUN --mount=type=secret,id=proxy_ca,required=true PIP_CERT=/run/secrets/proxy_ca pip install",1)
        with tempfile.NamedTemporaryFile(mode="w",suffix=".Dockerfile",prefix="tagihnet-stage-",delete=False) as stream:
            stream.write(source)
            filename=Path(stream.name)
        ignore_file=Path(str(filename)+".dockerignore")
        ignore_file.write_text((ROOT/"Dockerfile.staging.dockerignore").read_text())
        try:
            docker("build","--file",filename,"--secret",f"id=proxy_ca,src={Path(proxy_ca).resolve()}","--tag","tagihnet-staging:local",ROOT)
        finally:
            filename.unlink(missing_ok=True)
            ignore_file.unlink(missing_ok=True)
    else:
        docker("build","--file",ROOT/"Dockerfile.staging","--tag","tagihnet-staging:local",ROOT)


def wait_ready(directory,timeout=120):
    data=manifest(directory)
    context=ssl.create_default_context(cafile=str(Path(directory)/"secrets/ca.crt"))
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            with urlopen(f"https://localhost:{data['port']}/health/",context=context,timeout=5) as response:
                if response.status==200 and json.load(response)=={"status":"ok","mode":"simulation"}: return
        except (OSError,ValueError): pass
        time.sleep(.5)
    raise RuntimeError("HTTPS/database health belum siap; periksa compose logs tanpa mencetak secret.")


def up(directory,capture=False,timeout=120):
    if type(timeout) is not int or not 1<=timeout<=120:raise ValueError('Timeout HTTPS harus1–120detik.')
    with operation_lock(directory):
        return _up(directory,capture,timeout)


def require_data_volume(project):
    volume = docker('volume', 'inspect', project + '_postgres-data', capture=True, check=False)
    if volume.returncode:
        raise RuntimeError('Volume staging lama tidak tersedia; pulihkan backup, jangan membuat database pengganti diam-diam.')
    info = json.loads(volume.stdout)[0]
    if info.get('Labels', {}).get('com.docker.compose.project') != project:
        raise RuntimeError('Identitas volume tidak sesuai staging ini.')


def _up(directory,capture=False,timeout=120):
    data=manifest(directory)
    if data.get("restore_state") in {"preparing","failed"}:
        raise RuntimeError("Restore target belum berhasil; jangan menjalankan aplikasi/worker pada target ini.")
    if data.get("initialized"):
        require_data_volume(data['project'])
    services=["web"] if data.get("recovery_pending") else []
    compose(directory,"up","--detach","--no-build",*services,capture=capture)
    wait_ready(directory,timeout=timeout)
    data["initialized"]=True
    write_manifest(directory,data)


def resume_workers(directory,capture=False):
    """Explicit host-operator action after restore reconciliation; never runs during restore/up."""
    with operation_lock(directory):
        data=manifest(directory)
        if not data.get("initialized") or data.get("restore_state") in {"preparing","failed"}:
            raise RuntimeError("Staging/restore belum siap untuk worker.")
        _up(directory,capture=capture)
        compose(directory,"up","--detach","--no-build","network-worker","billing-worker","reminder-worker",capture=capture)
        data=manifest(directory)
        data["recovery_pending"]=False
        write_manifest(directory,data)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=["prepare","build","up","bootstrap","status","stop","resume-workers","pin-image"])
    parser.add_argument("--directory",type=Path,default=ROOT/"data/staging")
    parser.add_argument("--port",type=int,default=18443)
    parser.add_argument("--proxy-ca-file",type=Path)
    parser.add_argument('--image-id')
    parser.add_argument('--timeout',type=int,default=120,help='Batas polling HTTPS setelah up,1–120detik.')
    args=parser.parse_args()
    if args.action=="prepare": prepare(args.directory,args.port)
    elif args.action=="build": build(args.proxy_ca_file)
    elif args.action=='pin-image':
        pin_image(args.directory,args.image_id)
        print('Image ID disimpan untuk proyek ini; tidak ada layanan/migrasi/worker yang dijalankan.')
    elif args.action=="up":
        up(args.directory,timeout=args.timeout)
        print("PASS: isolated HTTPS staging is ready; database verified over TLS.")
        if manifest(args.directory).get('recovery_pending'):
            print('Mode pemulihan: hanya web berjalan; worker tetap ditahan sampai rekonsiliasi.')
    elif args.action=="bootstrap":
        compose(args.directory,"run","--rm","--no-deps","migrate","python","manage.py","bootstrap_isp",
            "--isp-name","ISP Staging Fiktif","--username","staging-owner","--email","staging-owner@example.invalid",
            "--password-file","/run/tagihnet/bootstrap-password")
    elif args.action=="status": compose(args.directory,"ps","--all")
    elif args.action=="resume-workers":
        resume_workers(args.directory)
        print("Workers started by explicit operator action after reconciliation.")
    elif args.action=="stop":
        compose(args.directory,"stop")
        print("Own staging services stopped; named volumes and private files retained.")


if __name__=="__main__": main()
