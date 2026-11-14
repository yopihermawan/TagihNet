"""Offline host-admin backup and fresh-project restore for isolated local staging."""
import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from cryptography.fernet import Fernet, InvalidToken
from core.backup_archive import ArchiveError, generate_key, read_key, verify_archive
from core.backup_database import decrypted_file, postgres_backup_command
from scripts.staging_lab import PG_IMAGE, compose, docker, manifest, operation_lock, write_manifest

EMPTY_SQL = """SELECT NOT (EXISTS(
SELECT 1 FROM pg_depend d JOIN pg_namespace n ON n.oid=d.refobjid
WHERE d.refclassid='pg_namespace'::regclass AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
AND NOT (d.classid='pg_default_acl'::regclass AND d.objid IN
(SELECT oid FROM pg_default_acl WHERE defaclrole=(SELECT oid FROM pg_roles WHERE rolname=current_user)
AND defaclnamespace='public'::regnamespace)))
OR EXISTS(SELECT 1 FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname NOT IN ('public','information_schema'))
OR EXISTS(SELECT 1 FROM pg_extension WHERE extname <> 'plpgsql')
OR EXISTS(SELECT 1 FROM pg_largeobject_metadata))"""


def environment():
    return {key:value for key,value in os.environ.items() if key not in {
        'DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'}}


def database_container(directory):
    state=manifest(directory)
    result=compose(directory,'ps','--quiet','db',capture=True)
    ids=result.stdout.split()
    if len(ids)!=1:
        raise ArchiveError('Database staging harus tersedia dan berjalan.')
    info=json.loads(docker('inspect',ids[0],capture=True).stdout)[0]
    labels=info['Config'].get('Labels',{})
    volume=state['project']+'_postgres-data'
    if (labels.get('com.docker.compose.project')!=state['project'] or labels.get('com.docker.compose.service')!='db'
            or info['Config']['Image']!=PG_IMAGE or not info['State']['Running']
            or not any(mount.get('Name')==volume and mount['Destination']=='/var/lib/postgresql/data' for mount in info['Mounts'])):
        raise ArchiveError('Container database tidak cocok dengan identitas/volume staging.')
    return ids[0]


def command(container,tool,*args):
    return ['docker','--host=unix:///var/run/docker.sock','exec','-i','--user','999:999',container,
            tool,'--username','tagihnet_migrator','--dbname','tagihnet_staging',*args]


def query(container,sql):
    result=subprocess.run(command(container,'psql','--no-psqlrc','--tuples-only','--no-align',
        '--set','ON_ERROR_STOP=1','--command',sql),env=environment(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
    if result.returncode:
        raise ArchiveError('Pemeriksaan database staging gagal; tidak ada diagnostic/secret yang dicetak.')
    return result.stdout.decode().strip()


def backup(directory,archive,key):
    if os.path.lexists(archive):
        raise ArchiveError('Arsip target sudah ada; tidak ditimpa.')
    container=database_container(directory)
    return postgres_backup_command(archive,key,command(container,'pg_dump','--format=custom','--lock-wait-timeout=5000'),environment())


def mfa_key(path):
    descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
    with os.fdopen(descriptor,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o777 not in {0o400,0o600}:
            raise ArchiveError('Kunci MFA sumber harus file privat0400/0600 yang valid.')
        value=stream.read(4097).rstrip(b'\r\n')
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o777 not in {0o400,0o600} or len(value)>4096:
        raise ArchiveError('Kunci MFA sumber harus file privat0400/0600 yang valid.')
    try: Fernet(value)
    except (ValueError,TypeError):
        raise ArchiveError('Format kunci MFA sumber tidak valid.') from None
    return value


def fresh_target(directory):
    state=manifest(directory)
    if state.get('initialized') or state.get('restore_state'):
        raise ArchiveError('Restore hanya ke direktori prepare baru, bukan staging yang pernah digunakan.')
    volume=docker('volume','inspect',state['project']+'_postgres-data',capture=True,check=False)
    if volume.returncode==0:
        raise ArchiveError('Volume target sudah ada; tidak ada database/volume yang ditimpa.')
    # A Docker failure is not proof of a missing volume.
    if 'no such volume' not in volume.stderr.lower():
        raise ArchiveError('Tidak dapat membuktikan volume target belum ada.')
    return state


def restore(directory,archive,key,mfa_file):
    directory=Path(directory).resolve()
    # Authenticate ALL bytes before making any Docker/database request or changing keys.
    with tempfile.TemporaryDirectory(prefix='tagihnet-staging-restore-') as folder:
        payload,header=decrypted_file(archive,key,folder)
        with payload.open('rb') as stream: signature=stream.read(5)
        if header['kind']!='postgresql' or signature!=b'PGDMP':
            raise ArchiveError('Restore staging memerlukan dump PostgreSQL custom terenkripsi.')
        original_mfa=mfa_key(mfa_file)
        if Path(mfa_file).resolve()==(directory/'secrets/mfa-key').resolve():
            raise ArchiveError('Gunakan kunci MFA asli dari backup, bukan kunci baru target.')
        with operation_lock(directory):
            state=fresh_target(directory)
            state.update(restore_state='preparing',recovery_pending=True)
            write_manifest(directory,state)
            try:
                # Replace only a new target's unused MFA key. Keep its new session key to revoke old sessions.
                with (directory/'secrets/mfa-key').open('wb') as target: target.write(original_mfa)
                compose(directory,'up','--detach','--no-build','db',capture=True)
                deadline=time.monotonic()+60
                while time.monotonic()<deadline:
                    result=compose(directory,'exec','-T','db','pg_isready','-h','127.0.0.1','-U','postgres',capture=True,check=False)
                    if not result.returncode: break
                    time.sleep(.25)
                else: raise ArchiveError('Database restore target belum siap.')
                container=database_container(directory)
                if query(container,'SELECT pg_get_userbyid(datdba)=current_user FROM pg_database WHERE datname=current_database()')!='t':
                    raise ArchiveError('Role restore harus pemilik database target.')
                if query(container,EMPTY_SQL)!='t':
                    raise ArchiveError('Target tidak kosong; tidak ada objek yang dihapus.')
                with payload.open('rb') as source,tempfile.TemporaryFile() as errors:
                    result=subprocess.run(command(container,'pg_restore','--no-owner','--no-acl','--single-transaction','--exit-on-error'),
                        env=environment(),stdin=source,stdout=subprocess.DEVNULL,stderr=errors,timeout=120)
                if result.returncode:
                    raise ArchiveError('pg_restore gagal; transaksi di-rollback dan aplikasi target tetap dihentikan.')
                encrypted=query(container,'SELECT encrypted_secret FROM core_mfadevice')
                try:
                    for value in encrypted.splitlines(): Fernet(original_mfa).decrypt(value.encode())
                except (InvalidToken,ValueError,TypeError):
                    raise ArchiveError('Kunci MFA tidak cocok dengan data hasil restore; aplikasi tetap dihentikan.') from None
                state.update(initialized=True,restore_state='restored')
                write_manifest(directory,state)
            except BaseException:
                state.update(initialized=True,restore_state='failed')
                write_manifest(directory,state)
                raise
    return header


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['keygen','backup','verify','restore'])
    parser.add_argument('archive',type=Path)
    parser.add_argument('--directory',type=Path,default=ROOT/'data/staging')
    parser.add_argument('--key-file',type=Path)
    parser.add_argument('--mfa-key-file',type=Path)
    args=parser.parse_args()
    try:
        if args.action=='keygen':
            generate_key(args.archive)
        else:
            if not args.key_file: raise ArchiveError('--key-file wajib, tanpa nilai kunci pada argv.')
            key=read_key(args.key_file)
            if args.action=='backup': backup(args.directory,args.archive,key)
            elif args.action=='verify': verify_archive(args.archive,key)
            else:
                if not args.mfa_key_file: raise ArchiveError('Restore memerlukan file kunci MFA asli.')
                restore(args.directory,args.archive,key,args.mfa_key_file)
        print(f'{args.action}: selesai; tidak ada secret/faktor atau diagnostic database yang dicetak.')
        if args.action=='restore': print('Worker tetap ditahan. Up membuka web untuk peninjauan; resume-workers hanya setelah rekonsiliasi.')
    except ArchiveError as error: parser.exit(1,f'Ditolak: {error}\n')
    except Exception as error: parser.exit(1,f'Gagal: {type(error).__name__}; periksa izin/konfigurasi/log privat. Tidak ada secret dicetak.\n')


if __name__=='__main__': main()
