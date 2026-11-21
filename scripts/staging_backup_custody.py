"""Find or prepare a local recovery pair for the current monitored staging backup."""
import argparse
import hashlib
import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.backup_archive import ArchiveError, read_key
from scripts import recovery_package as kit
from scripts import staging_ops as ops
from scripts.staging_lab import manifest


def matches_backup(package, archive, mfa_digest):
    return (package['archive_size'] == archive['archive_size']
            and package['archive_sha256'] == archive['archive_sha256']
            and package['mfa_sha256'] == mfa_digest)


def matching_package(package_root, keys_root, archive, mfa_digest):
    for package in sorted(package_root.iterdir()):
        try:
            material = kit.materials(package, keys_root / package.name)
            if matches_backup(material.metadata, archive, mfa_digest):
                kit.verify_content(package / 'archive.tnbk', material)
                return package.name
        except (ArchiveError, OSError, ValueError):
            continue
    return None


def ensure_pair(directory, package_root, keys_root, now=None):
    directory = Path(directory).resolve()
    package_root, keys_root = Path(package_root), Path(keys_root)
    kit.separated(package_root, keys_root)
    kit.private_directory(package_root)
    kit.private_directory(keys_root)
    state = manifest(directory)
    if not state.get('initialized') or state.get('recovery_pending') or state.get('restore_state') in {'preparing', 'failed'}:
        raise ArchiveError('Pilih staging utama yang sudah diinisialisasi.')

    # Separate from the watcher's lock: reading or copying a large archive must not stall scheduled backup.
    with ops.ops_lock(directory, '.custody.lock'):
        config = ops.policy(directory)
        job_path = directory / 'ops/backup.json'
        job = ops.read_json(job_path, {})
        checked_at = time.time() if now is None else now
        interval = config['backup_interval']
        if (not config['backup_enabled'] or job.get('failed')
                or not ops.backup_time_current(job.get('last_success_at'), checked_at, interval)):
            raise ArchiveError('Backup terpantau belum tersedia atau sudah kedaluwarsa.')
        folder = Path(config['backup_directory']).resolve()
        for root in (package_root, keys_root):
            kit.separated(root, folder)
            kit.separated(root, directory / 'secrets')
        archive = Path(job['last_archive'])
        if not archive.is_absolute() or archive.parent.resolve() != folder or archive.suffix != '.tnbk':
            raise ArchiveError('Backup terpantau harus berada di direktori backup yang dikonfigurasi.')
        before = kit.file_identity(archive.lstat())
        keyfile, mfa_file = Path(config['backup_key_file']), directory / 'secrets/mfa-key'
        key = read_key(keyfile)
        mfa_digest = hashlib.sha256(kit.mfa_key(mfa_file)).hexdigest()
        details = kit.archive_details(archive, key)
        created_at = datetime.fromisoformat(details['created_at'])
        if not ops.backup_time_current(created_at.timestamp(), checked_at, interval):
            raise ArchiveError('Tanggal arsip terautentikasi berada di luar jendela backup.')

        name = matching_package(package_root, keys_root, details, mfa_digest)
        status = 'reused'
        if name is None:
            name = f"{state['project']}-{created_at:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}"
            metadata = kit.export_package(archive, keyfile, mfa_file, package_root / name, keys_root / name)
            if not matches_backup(metadata, details, mfa_digest):
                raise ArchiveError('Cadangan atau kunci berubah saat paket disalin.')
            status = 'created'

        # A newer watcher backup or a changed configuration invalidates this result for release.
        finished = time.time() if now is None else now
        if (state != manifest(directory) or config != ops.policy(directory) or job != ops.read_json(job_path, {})
                or before != kit.file_identity(archive.lstat()) or read_key(keyfile) != key
                or hashlib.sha256(kit.mfa_key(mfa_file)).hexdigest() != mfa_digest
                or not ops.backup_time_current(created_at.timestamp(), finished, interval)
                or not ops.backup_time_current(job['last_success_at'], finished, interval)):
            raise ArchiveError('Backup aktif berubah saat paket diperiksa. Jalankan ulang.')
        return {'status': status, 'archive': archive.name, 'archive_created_at': details['created_at'],
                'package_dir': str((package_root / name).resolve()), 'keys_dir': str((keys_root / name).resolve())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / 'data/staging')
    parser.add_argument('--package-root', type=Path, required=True)
    parser.add_argument('--keys-root', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = ensure_pair(args.directory, args.package_root, args.keys_root)
    except ArchiveError as error:
        # ArchiveError from the backup helpers carries a fixed operator-facing reason.
        parser.exit(1, f'Paket ditolak. {error}\n')
    except Exception as error:
        parser.exit(1, f'Paket gagal ({type(error).__name__}). Periksa izin, status backup, dan ruang tujuan.\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
