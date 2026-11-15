"""Portable local recovery copies with separate key custody; never uploads or resumes workers."""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from core.backup_archive import ArchiveError,CHUNK,MAX_HEADER,MAX_PAYLOAD,atomic_output,decrypt_stream,read_key
from scripts.staging_backup import mfa_key,restore as restore_database

MAX_ARCHIVE=MAX_PAYLOAD+MAX_HEADER+4096
FIELDS={'version','package_id','created_at','exporter_commit','archive_size','archive_sha256','mfa_sha256','mac'}


@contextmanager
def private_file(path,limit=None):
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) not in {0o400,0o600}
                or (limit is not None and info.st_size>limit)):
            raise ArchiveError('File paket harus reguler, privat0400/0600 dan dalam batas ukuran.')
        yield stream


def private_directory(path):
    info=Path(path).lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode)&0o077:
        raise ArchiveError('Direktori paket/kunci harus privat dan bukan symlink.')


def separated(package,keys):
    p=Path(package).resolve();k=Path(keys).resolve()
    if p==k or p in k.parents or k in p.parents:
        raise ArchiveError('Direktori kunci dan paket wajib terpisah, tidak bersarang.')


def new_directory(path):
    path=Path(path)
    missing=[];ancestor=path.parent
    while not ancestor.exists():missing.append(ancestor);ancestor=ancestor.parent
    for parent in reversed(missing):parent.mkdir(mode=0o700)
    path.mkdir(mode=0o700,exist_ok=False)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def json_bytes(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii')


def manifest_mac(metadata,key):
    signing=HKDF(algorithm=hashes.SHA256(),length=32,salt=bytes.fromhex(metadata['package_id']),
        info=b'TagihNet/recovery-handoff/manifest/v1').derive(key.material)
    return hmac.new(signing,json_bytes({k:v for k,v in metadata.items() if k!='mac'}),hashlib.sha256).hexdigest()


def manifest_data(package):
    def unique(pairs):
        value=dict(pairs)
        if len(value)!=len(pairs):raise ArchiveError('Field manifest duplikat.')
        return value
    with private_file(Path(package)/'manifest.json',16384) as stream:raw=stream.read(16385)
    try:value=json.loads(raw,object_pairs_hook=unique)
    except (ValueError,UnicodeError,RecursionError):raise ArchiveError('Manifest tidak valid.') from None
    if not isinstance(value,dict) or set(value)!=FIELDS or type(value['version']) is not int or value['version']!=1:
        raise ArchiveError('Versi/field manifest tidak didukung.')
    for name,length in (('package_id',32),('exporter_commit',40),('archive_sha256',64),('mfa_sha256',64),('mac',64)):
        if not isinstance(value[name],str) or not re.fullmatch('[a-f0-9]{'+str(length)+'}',value[name]):
            raise ArchiveError('Identitas/digest manifest tidak valid.')
    if type(value['archive_size']) is not int or not 1<=value['archive_size']<=MAX_ARCHIVE:
        raise ArchiveError('Ukuran arsip manifest tidak valid.')
    try:
        created=datetime.fromisoformat(value['created_at'])
        if created.utcoffset()!=timezone.utc.utcoffset(created):raise ValueError
    except (ValueError,TypeError):raise ArchiveError('Waktu manifest tidak valid.') from None
    return value


def digest_archive(path):
    digest=hashlib.sha256();size=0
    with private_file(path,MAX_ARCHIVE) as stream:
        while block:=stream.read(CHUNK):
            size+=len(block)
            if size>MAX_ARCHIVE:raise ArchiveError('Arsip melampaui batas.')
            digest.update(block)
    return size,digest.hexdigest()


def copy_archive(source,target):
    size=0
    with private_file(source,MAX_ARCHIVE) as stream,atomic_output(target) as output:
        while block:=stream.read(CHUNK):
            size+=len(block)
            if size>MAX_ARCHIVE:raise ArchiveError('Arsip melampaui batas.')
            output.write(block)


def verify_dump(path,key):
    class Prefix:
        def __init__(self):self.first=b''
        def write(self,data):self.first+=data[:max(0,5-len(self.first))];return len(data)
    sink=Prefix()
    with private_file(path,MAX_ARCHIVE) as stream:header=decrypt_stream(stream,sink,key)
    if header['kind']!='postgresql' or sink.first!=b'PGDMP':raise ArchiveError('Paket memerlukan arsip PostgreSQL custom yang terautentikasi.')


def exporter_commit():
    value=subprocess.run(['git','-C',str(ROOT),'rev-parse','--verify','HEAD^{commit}'],check=True,capture_output=True,text=True).stdout.strip()
    if not re.fullmatch('[a-f0-9]{40}',value):raise ArchiveError('Commit exporter tidak tersedia.')
    return value


def export_package(archive,keyfile,mfa_file,package,keys):
    package=Path(package);keys=Path(keys);separated(package,keys)
    if os.path.lexists(package) or os.path.lexists(keys):raise ArchiveError('Tujuan paket/kunci sudah ada; tidak ditimpa.')
    key=read_key(Path(keyfile));mfa=mfa_key(Path(mfa_file))
    verify_dump(archive,key)
    revision=exporter_commit()
    # Both destinations are exclusive. A partial export has no authenticated completion manifest.
    new_directory(package);new_directory(keys)
    copy_archive(archive,package/'archive.tnbk')
    verify_dump(package/'archive.tnbk',key)
    key_data={'format':'tagihnet-backup-key','version':1,'id':key.identifier,'key':base64.b64encode(key.material).decode('ascii')}
    with atomic_output(keys/'backup-key.json') as stream:stream.write(json_bytes(key_data))
    with atomic_output(keys/'mfa.key') as stream:stream.write(mfa)
    size,digest=digest_archive(package/'archive.tnbk')
    metadata={'version':1,'package_id':uuid.uuid4().hex,'created_at':datetime.now(timezone.utc).isoformat(),
        'exporter_commit':revision,'archive_size':size,'archive_sha256':digest,'mfa_sha256':hashlib.sha256(mfa).hexdigest()}
    metadata['mac']=manifest_mac(metadata,key)
    with atomic_output(package/'manifest.json') as stream:stream.write(json_bytes(metadata))
    verify_package(package,keys)
    return metadata


@dataclass(repr=False)
class Material:
    metadata:dict
    key:object
    mfa:bytes


def materials(package,keys):
    package=Path(package);keys=Path(keys);separated(package,keys)
    private_directory(package);private_directory(keys)
    if {p.name for p in package.iterdir()}!={'archive.tnbk','manifest.json'} or {p.name for p in keys.iterdir()}!={'backup-key.json','mfa.key'}:
        raise ArchiveError('Isi paket/kunci tidak lengkap atau memuat file tambahan.')
    data=manifest_data(package);key=read_key(keys/'backup-key.json')
    if not hmac.compare_digest(data['mac'],manifest_mac(data,key)):raise ArchiveError('Autentikasi manifest gagal.')
    mfa=mfa_key(keys/'mfa.key')
    if not hmac.compare_digest(data['mfa_sha256'],hashlib.sha256(mfa).hexdigest()):raise ArchiveError('Kunci MFA tidak sesuai manifest paket.')
    return Material(data,key,mfa)


def verify_content(archive,material):
    size,digest=digest_archive(archive)
    if size!=material.metadata['archive_size'] or not hmac.compare_digest(digest,material.metadata['archive_sha256']):
        raise ArchiveError('Ukuran/digest arsip tidak sesuai manifest.')
    verify_dump(archive,material.key)


def verify_package(package,keys):
    material=materials(package,keys)
    verify_content(Path(package)/'archive.tnbk',material)
    return material.metadata


def restore_package(package,keys,target):
    material=materials(package,keys)
    # Freeze verified inputs privately before any target operation; never reread mutable key files.
    with tempfile.TemporaryDirectory(prefix='tagihnet-handoff-restore-') as folder:
        archive=Path(folder)/'archive.tnbk';mfa=Path(folder)/'mfa.key'
        copy_archive(Path(package)/'archive.tnbk',archive)
        verify_content(archive,material)
        with atomic_output(mfa) as stream:stream.write(material.mfa)
        restore_database(Path(target),archive,material.key,mfa)
    return material.metadata


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['export','verify','restore'])
    parser.add_argument('--package-dir',type=Path,required=True)
    parser.add_argument('--keys-dir',type=Path,required=True)
    parser.add_argument('--archive',type=Path)
    parser.add_argument('--archive-key-file',type=Path)
    parser.add_argument('--mfa-key-file',type=Path)
    parser.add_argument('--target-dir',type=Path)
    args=parser.parse_args()
    try:
        if args.action=='export':
            if not all((args.archive,args.archive_key_file,args.mfa_key_file)):raise ArchiveError('Export memerlukan arsip dan file kunci backup/MFA asli.')
            export_package(args.archive,args.archive_key_file,args.mfa_key_file,args.package_dir,args.keys_dir)
        elif args.action=='verify':verify_package(args.package_dir,args.keys_dir)
        else:
            if not args.target_dir:raise ArchiveError('Restore memerlukan direktori prepare target baru.')
            restore_package(args.package_dir,args.keys_dir,args.target_dir)
        print(args.action+': selesai; tidak ada secret/faktor yang dicetak. Tidak ada upload atau worker resume.')
    except Exception as error:parser.exit(1,'Ditolak: '+type(error).__name__+'; periksa file/izin/pasangan paket. Secret/diagnostic tidak dicetak.\n')


if __name__=='__main__':main()
