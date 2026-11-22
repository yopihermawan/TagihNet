"""Restore the monitored staging backup into a disposable database in the local PostgreSQL lab."""
import argparse
import hashlib
import inspect
import json
import os
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cryptography.fernet import Fernet
from core.backup_archive import ArchiveError, atomic_output, read_key
from core.backup_database import pg_config, postgres_restore
from scripts import postgres_lab, recovery_package as kit, staging_ops as ops
from scripts.staging_backup import database_container
from scripts.staging_backup_custody import matches_backup
from scripts.staging_lab import compose, manifest


def database_snapshot(connection):
    """Hash all public table rows and capture sequence state without exporting row contents."""
    import hashlib
    import json
    from psycopg import sql

    result = {'tables': {}, 'sequences': {}}
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        cursor.execute("SET LOCAL TIME ZONE 'UTC'")
        cursor.execute("SET LOCAL statement_timeout = '30s'")
        cursor.execute("SET LOCAL lock_timeout = '5s'")
        cursor.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public' ORDER BY tablename")
        tables = cursor.fetchall()
        for (name,) in tables:
            # Order by fixed-length digests so rows with identical contents still count individually.
            cursor.execute(sql.SQL('SELECT digest FROM (SELECT sha256(convert_to(row_to_json(t)::text, \'UTF8\')) AS digest '
                                   'FROM {} t) fingerprints ORDER BY digest').format(sql.Identifier('public', name)))
            digest = hashlib.sha256()
            for (value,) in cursor:
                digest.update(value)
            result['tables'][name] = [cursor.rowcount, digest.hexdigest()]
        # Preserve logical column order. Restore compacts physical gaps left by dropped columns.
        cursor.execute("""SELECT table_name, column_name, udt_name, is_nullable,
                                 column_default, is_identity, is_generated
                            FROM information_schema.columns WHERE table_schema='public'
                            ORDER BY table_name, ordinal_position""")
        result['columns'] = hashlib.sha256(json.dumps(cursor.fetchall()).encode()).hexdigest()
        cursor.execute("SELECT sequencename FROM pg_catalog.pg_sequences WHERE schemaname='public' ORDER BY sequencename")
        for (name,) in cursor.fetchall():
            cursor.execute(sql.SQL('SELECT last_value, is_called FROM {}').format(sql.Identifier('public', name)))
            result['sequences'][name] = list(cursor.fetchone())
    return result


def source_snapshot(directory):
    code = inspect.getsource(database_snapshot) + """
import json
from django.conf import settings
from django.db import connection, transaction
if not settings.SIMULATION:
    raise RuntimeError('Local simulation required')
with transaction.atomic():
    print('SNAPSHOT:' + json.dumps(database_snapshot(connection), sort_keys=True))
"""
    response = compose(directory, 'exec', '-T', 'web', 'python', 'manage.py', 'shell', '-c', code,
                       capture=True, timeout=90).stdout
    return json.loads(next(line.removeprefix('SNAPSHOT:') for line in response.splitlines() if line.startswith('SNAPSHOT:')))


def connect(config, name, *, autocommit=False):
    import psycopg

    return psycopg.connect(dbname=name, host=config['HOST'], port=config['PORT'], user=config['USER'],
                           password=config['PASSWORD'], autocommit=autocommit, **config['OPTIONS'])


@contextmanager
def disposable_database(source_identity):
    lab = postgres_lab.read_manifest()
    info = postgres_lab.inspected(lab)
    ports = info['NetworkSettings']['Ports'].get('5432/tcp')
    if (info['Id'] == source_identity or info['State']['Running'] is not True
            or ports != [{'HostIp': '127.0.0.1', 'HostPort': str(lab['port'])}]):
        raise ArchiveError('Restore uji memerlukan lab PostgreSQL loopback yang terpisah dan aktif.')
    environ = postgres_lab.connection_env(lab)
    config = pg_config(environ)
    target = 'tagihnet_restore_drill_' + uuid.uuid4().hex
    with connect(config, config['NAME'], autocommit=True) as control:
        role = control.execute('SELECT rolsuper, rolcreatedb FROM pg_roles WHERE rolname=current_user').fetchone()
        if role != (False, True):
            raise ArchiveError('Role lab harus memiliki CREATEDB dan tanpa superuser.')
        from psycopg import sql

        control.execute(sql.SQL('CREATE DATABASE {} WITH TEMPLATE template0').format(sql.Identifier(target)))
        try:
            yield target, environ, config
        finally:
            control.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(target)))


def verify_restore(directory, package, keys):
    directory = Path(directory).resolve()
    package, keys = Path(package), Path(keys)
    state = manifest(directory)
    if not state.get('initialized') or state.get('recovery_pending') or state.get('restore_state') in {'preparing', 'failed'}:
        raise ArchiveError('Pilih sumber staging aktif yang sudah diinisialisasi.')
    policy = ops.policy(directory)
    record = directory / 'ops/backup.json'
    job = ops.read_json(record, {})
    if not policy['backup_enabled'] or job.get('failed') or not job.get('last_archive'):
        raise ArchiveError('Backup terakhir harus berhasil dan terpantau.')
    archive = Path(job['last_archive'])
    if not archive.is_absolute() or archive.parent.resolve() != Path(policy['backup_directory']).resolve():
        raise ArchiveError('Arsip terakhir berada di luar direktori backup terpantau.')
    observed = kit.file_identity(archive.lstat())
    details = kit.archive_details(archive, read_key(Path(policy['backup_key_file'])))
    material = kit.materials(package, keys)
    mfa_digest = hashlib.sha256(kit.mfa_key(directory / 'secrets/mfa-key')).hexdigest()
    if not matches_backup(material.metadata, details, mfa_digest):
        raise ArchiveError('Paket harus cocok dengan arsip terpantau dan kunci MFA sumber.')

    with tempfile.TemporaryDirectory(prefix='tagihnet-monitored-restore-') as folder:
        frozen = Path(folder) / 'archive.tnbk'
        kit.copy_archive(package / 'archive.tnbk', frozen)
        kit.verify_content(frozen, material)
        source_identity = database_container(directory)
        before = source_snapshot(directory)
        with disposable_database(source_identity) as (target, environ, config):
            began = time.monotonic()
            postgres_restore(frozen, target, material.key, environ)
            duration = time.monotonic() - began
            with connect(config, target) as restored:
                actual = database_snapshot(restored)
                cipher = Fernet(material.mfa)
                with restored.cursor() as cursor:
                    cursor.execute('SELECT encrypted_secret FROM public.core_mfadevice')
                    for (secret,) in cursor:
                        cipher.decrypt(secret.encode())
                    mfa_count = cursor.rowcount

    issues = []
    if before != source_snapshot(directory):
        issues.append('source_changed_during_check')
    if (state != manifest(directory) or policy != ops.policy(directory) or job != ops.read_json(record, {})
            or kit.file_identity(archive.lstat()) != observed):
        issues.append('backup_changed_during_check')
    matches = {part: actual[part] == before[part] for part in ('tables', 'columns', 'sequences')}
    if not all(matches.values()):
        issues.append('snapshot_differs_from_source')
    return {'status': 'failed' if issues else 'passed', 'scope': 'local_postgresql_lab', 'issues': issues,
            'archive': archive.name, 'archive_created_at': details['created_at'], 'package_id': material.metadata['package_id'],
            'restore_seconds': round(duration, 3), 'tables_compared': len(actual['tables']), 'sequences_compared': len(actual['sequences']),
            'rows_compared': sum(row[0] for row in actual['tables'].values()), 'restored_mfa_records': mfa_count,
            'matches': matches,
            'disposable_database_removed': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / 'data/staging')
    parser.add_argument('--package-dir', type=Path, required=True)
    parser.add_argument('--keys-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if os.path.lexists(args.output) or any(args.output.resolve().is_relative_to(path.resolve())
                                               for path in (args.package_dir, args.keys_dir)):
            raise ValueError('Gunakan output baru di luar direktori paket dan kunci.')
        with atomic_output(args.output) as stream:
            result = verify_restore(args.directory, args.package_dir, args.keys_dir)
            stream.write(json.dumps(result, sort_keys=True, indent=2).encode())
        print(json.dumps(result, sort_keys=True))
        return 0 if result['status'] == 'passed' else 1
    except Exception as error:
        parser.exit(1, f'Uji restore gagal ({type(error).__name__}). Periksa lab, paket, kunci, dan status sumber.\n')


if __name__ == '__main__':
    raise SystemExit(main())
