"""Verify the configured staging backup directory and optional local recovery copies offline."""
import argparse
import hashlib
import json
import os
import stat
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.backup_archive import ArchiveError, atomic_output, read_key
from scripts import staging_ops as ops
from scripts.recovery_package import archive_details, mfa_key, private_directory, separated, verify_package


def archive_paths(folder):
    return sorted(path for path in folder.iterdir() if path.suffix == '.tnbk')


def custody_inventory(archives, keys, expected_mfa):
    separated(archives, keys)
    private_directory(archives)
    private_directory(keys)
    packages = []
    matches = defaultdict(list)
    for name in sorted({path.name for path in archives.iterdir()} | {path.name for path in keys.iterdir()}):
        item = {'name': name, 'status': 'unverified'}
        try:
            metadata = verify_package(archives / name, keys / name)
        except (ArchiveError, OSError, ValueError):
            pass
        else:
            compatible_mfa = metadata['mfa_sha256'] == expected_mfa
            item.update(status='verified', mfa_matches_staging=compatible_mfa)
            if compatible_mfa:
                matches[metadata['archive_sha256']].append(name)
        packages.append(item)
    return packages, matches


def inventory(directory, output, package_root=None, keys_root=None, now=None):
    directory = Path(directory).resolve()
    config = ops.policy(directory)
    folder = Path(config['backup_directory'])
    checked_at = now if now is not None else datetime.now(timezone.utc)
    if (package_root is None) != (keys_root is None):
        raise ValueError('Folder paket dan custody wajib diberikan berpasangan.')
    if any(output.resolve().is_relative_to(path.resolve()) for path in (folder, package_root, keys_root) if path):
        raise ValueError('Simpan laporan di luar folder arsip dan custody.')
    private_directory(folder)
    job_path = directory / 'ops/backup.json'
    job = ops.read_json(job_path, {})
    paths = archive_paths(folder)
    key = read_key(Path(config['backup_key_file']))
    packages, custody = None, {}
    if package_root is not None:
        expected_mfa = hashlib.sha256(mfa_key(directory / 'secrets/mfa-key')).hexdigest()
        packages, custody = custody_inventory(package_root, keys_root, expected_mfa)

    records = {}
    for path in paths:
        record = {'name': path.name, 'status': 'unverified', 'bytes': None, 'created_at': None,
                  'custody_packages': None if packages is None else []}
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                record['bytes'] = info.st_size
            details = archive_details(path, key)
        except (ArchiveError, OSError, ValueError):
            pass
        else:
            record.update(status='verified', bytes=details['archive_size'], created_at=details['created_at'])
            if packages is not None:
                record['custody_packages'] = custody.get(details['archive_sha256'], [])
        records[path.name] = record

    recorded = job.get('last_archive')
    latest = None
    if isinstance(recorded, str) and Path(recorded).is_absolute() and Path(recorded).parent.resolve() == folder.resolve():
        latest = records.get(Path(recorded).name)
    failures = []
    if any(record['status'] != 'verified' for record in records.values()):
        failures.append('archive_unverified')
    if packages is not None and any(package['status'] != 'verified' for package in packages):
        failures.append('custody_unverified')
    if config['backup_enabled']:
        if job.get('failed') is True:
            failures.append('backup_job_failed')
        if latest is None or latest['status'] != 'verified':
            failures.append('latest_archive_unverified')
        elif packages is not None and not latest['custody_packages']:
            failures.append('latest_custody_unverified')
    if paths != archive_paths(folder) or job != ops.read_json(job_path, {}) or config != ops.policy(directory):
        failures.append('inventory_changed')
    age = (checked_at - datetime.fromisoformat(latest['created_at'])).total_seconds() if latest and latest['created_at'] else None
    return {'version': 1, 'project': config['project'], 'checked_at': checked_at.isoformat(),
            'status': 'attention' if failures else 'verified', 'issues': failures,
            'backup_enabled': config['backup_enabled'], 'archive_count': len(records),
            'archive_bytes': sum(record['bytes'] or 0 for record in records.values()),
            'latest_archive': latest['name'] if latest else None, 'latest_age_seconds': age,
            'archives': list(records.values()), 'packages': packages}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / 'data/staging')
    parser.add_argument('--package-root', type=Path)
    parser.add_argument('--keys-root', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if os.path.lexists(args.output):
            raise ValueError('Laporan sudah ada.')
        report = inventory(args.directory, args.output, args.package_root, args.keys_root)
        with atomic_output(args.output) as stream:
            stream.write(json.dumps(report, sort_keys=True, indent=2).encode())
        print(json.dumps({name: report[name] for name in ('status', 'archive_count', 'archive_bytes', 'latest_age_seconds', 'issues')}))
        return 0 if report['status'] == 'verified' else 1
    except Exception as error:
        parser.exit(1, f'Inventaris gagal: {type(error).__name__}. Periksa izin, direktori, dan pasangan arsip/kunci.\n')


if __name__ == '__main__':
    raise SystemExit(main())
