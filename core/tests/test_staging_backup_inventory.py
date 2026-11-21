import io
import json
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.test import SimpleTestCase

from core.backup_archive import ArchiveError, encrypt_stream, generate_key, read_key
from scripts import recovery_package, staging_backup_custody as custody, staging_backup_inventory as inventory, staging_ops as ops


class BackupInventoryTests(SimpleTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='tagihnet-inventory-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stage = self.root / 'stage'
        self.stage.mkdir()
        self.state = {'project': 'tagihnet-stage-0123456789', 'directory': str(self.stage), 'initialized': True}
        ops.write_json(self.stage / 'manifest.json', self.state)
        self.secret = self.stage / 'secrets'
        self.secret.mkdir(mode=0o700)
        self.keyfile = self.secret / 'backup-key.json'
        generate_key(self.keyfile)
        self.key = read_key(self.keyfile)
        self.mfa = self.secret / 'mfa-key'
        self.mfa.write_bytes(Fernet.generate_key())
        self.mfa.chmod(0o600)
        self.config = ops.configure(self.stage, backup_enabled=True)
        self.folder = Path(self.config['backup_directory'])
        self.latest = self.folder / 'tracked.tnbk'
        self.checked_at = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
        with patch('core.backup_archive.datetime') as clock:
            clock.now.return_value = self.checked_at - timedelta(hours=5)
            encrypt_stream(io.BytesIO(b'PGDMPprivate fixture'), self.latest, self.key, 'postgresql')
        self.job_file = self.stage / 'ops/backup.json'
        self.job = {'last_archive': str(self.latest), 'last_success_at': self.checked_at.timestamp(), 'failed': False}
        ops.write_json(self.job_file, self.job)
        self.packages = self.root / 'archives'
        self.keys = self.root / 'keys'
        self.packages.mkdir(mode=0o700)
        self.keys.mkdir(mode=0o700)
        self.output = self.root / 'results' / 'inventory.json'

    def export(self, name='current', archive=None):
        with patch.object(recovery_package, 'exporter_commit', return_value='a' * 40):
            return recovery_package.export_package(archive or self.latest, self.keyfile, self.mfa,
                                                   self.packages / name, self.keys / name)

    def inspect(self, custody=False):
        roots = (self.packages, self.keys) if custody else (None, None)
        return inventory.inventory(self.stage, self.output, *roots, now=self.checked_at)

    def ensure(self, now=None):
        with patch.object(recovery_package, 'exporter_commit', return_value='a' * 40):
            return custody.ensure_pair(self.stage, self.packages, self.keys,
                                       now=self.checked_at.timestamp() if now is None else now)

    def test_read_only_inventory_authenticates_age_and_matches_ciphertext(self):
        self.export()
        other = self.folder / 'other.tnbk'
        encrypt_stream(io.BytesIO(b'PGDMPprivate fixture'), other, self.key, 'postgresql')
        os.utime(self.latest, (1, 1))
        files = [self.latest, other, self.job_file, self.packages / 'current' / 'archive.tnbk', self.keyfile, self.mfa]
        before = [path.read_bytes() for path in files]
        report = self.inspect(custody=True)
        self.assertEqual(report['status'], 'verified')
        self.assertEqual(report['latest_archive'], self.latest.name)
        self.assertEqual(report['latest_age_seconds'], 18000)
        self.assertEqual(report['archive_bytes'], self.latest.stat().st_size + other.stat().st_size)
        matches = {record['name']: record['custody_packages'] for record in report['archives']}
        self.assertEqual(matches, {other.name: [], self.latest.name: ['current']})
        self.assertEqual([path.read_bytes() for path in files], before)
        self.assertFalse(self.output.exists())
        public = json.dumps(report)
        self.assertNotIn(str(self.root), public)
        self.assertNotIn(self.mfa.read_text(), public)
        self.assertNotIn('PGDMPprivate fixture', public)

    def test_tampered_archive_link_non_dump_and_missing_custody_are_reported(self):
        self.export()
        (self.keys / 'incomplete').mkdir(mode=0o700)
        link = self.folder / 'alias.tnbk'
        link.symlink_to(self.latest)
        not_dump = self.folder / 'wrong-format.tnbk'
        encrypt_stream(io.BytesIO(b'not a dump'), not_dump, self.key, 'postgresql')
        data = bytearray(self.latest.read_bytes())
        data[-1] ^= 1
        self.latest.write_bytes(data)
        report = self.inspect(custody=True)
        self.assertEqual(report['status'], 'attention')
        self.assertEqual(set(report['issues']), {'archive_unverified', 'latest_archive_unverified', 'custody_unverified'})
        self.assertTrue(all(row['created_at'] is None for row in report['archives']))
        self.assertIsNone(next(row['bytes'] for row in report['archives'] if row['name'] == link.name))
        self.assertIsNone(report['latest_age_seconds'])
        self.assertEqual(self.latest.read_bytes(), bytes(data))

    def test_historical_custody_under_a_different_mfa_key_is_not_a_current_match(self):
        self.export()
        self.mfa.write_bytes(Fernet.generate_key())
        report = self.inspect(custody=True)
        self.assertEqual(report['packages'], [{'name': 'current', 'status': 'verified', 'mfa_matches_staging': False}])
        self.assertEqual(report['issues'], ['latest_custody_unverified'])

    def test_outside_directory_reference_cannot_be_used_as_latest(self):
        outside = self.root / self.latest.name
        self.latest.rename(outside)
        self.latest.write_bytes(outside.read_bytes())
        self.latest.chmod(0o600)
        ops.write_json(self.job_file, {**self.job, 'last_archive': str(outside), 'failed': True})
        report = self.inspect()
        self.assertEqual(report['archive_count'], 1)
        self.assertIsNone(report['latest_archive'])
        self.assertEqual(report['issues'], ['backup_job_failed', 'latest_archive_unverified'])

    def test_metadata_change_during_scan_is_reported(self):
        def verify(path, key):
            result = recovery_package.archive_details(path, key)
            ops.write_json(self.job_file, {**self.job, 'failed': True})
            return result

        with patch.object(inventory, 'archive_details', side_effect=verify):
            report = self.inspect()
        self.assertEqual(report['issues'], ['inventory_changed'])

    def test_cli_publishes_private_no_clobber_report_outside_input_directories(self):
        arguments = ['inventory', '--directory', str(self.stage), '--output', str(self.output)]
        with patch.object(inventory.sys, 'argv', arguments), redirect_stdout(io.StringIO()):
            self.assertEqual(inventory.main(), 0)
        saved = self.output.read_bytes()
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(saved)['status'], 'verified')
        with patch.object(inventory.sys, 'argv', arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            inventory.main()
        self.assertEqual(self.output.read_bytes(), saved)
        with self.assertRaises(ValueError):
            inventory.inventory(self.stage, self.folder / 'unsafe.json')
        with self.assertRaises(ValueError):
            inventory.inventory(self.stage, self.output, self.packages)

    def test_prepare_reuses_exact_existing_pair_without_exporting(self):
        self.export()
        with patch.object(recovery_package, 'export_package') as export:
            result = self.ensure()
        export.assert_not_called()
        self.assertEqual(result['status'], 'reused')
        self.assertEqual(result['package_dir'], str(self.packages / 'current'))
        self.assertEqual(result['keys_dir'], str(self.keys / 'current'))
        self.assertEqual(ops.read_json(self.job_file), self.job)

    def test_prepare_follows_a_new_watcher_backup_then_reuses_the_new_pair(self):
        self.export('older')
        scheduled = self.checked_at.timestamp() + self.config['backup_interval']

        def dump(directory, target, key):
            with patch('core.backup_archive.datetime') as clock:
                clock.now.return_value = datetime.fromtimestamp(scheduled, timezone.utc)
                encrypt_stream(io.BytesIO(b'PGDMPnew snapshot'), target, key, 'postgresql')

        with (patch.object(ops, 'backup', side_effect=dump),
              patch.object(ops, 'check', return_value={'checked_at': scheduled, 'issues': []})):
            ops.tick(self.stage, now=scheduled)

        recorded = ops.read_json(self.job_file)
        first = self.ensure(now=scheduled)
        second = self.ensure(now=scheduled)
        self.assertEqual(first['status'], 'created')
        self.assertEqual(second, {**first, 'status': 'reused'})
        expected = recovery_package.archive_details(Path(recorded['last_archive']), self.key)
        copied = recovery_package.verify_package(first['package_dir'], first['keys_dir'])
        self.assertEqual(copied['archive_sha256'], expected['archive_sha256'])
        self.assertEqual(first['archive'], Path(recorded['last_archive']).name)
        self.assertEqual(ops.read_json(self.job_file), recorded)
        self.assertEqual(len(list(self.packages.iterdir())), 2)

    def test_prepare_refuses_unmanaged_or_inactive_sources_before_export(self):
        changed = ({'recovery_pending': True}, {'restore_state': 'failed'}, {'initialized': False})
        with patch.object(recovery_package, 'export_package') as export:
            for update in changed:
                with self.subTest(update=update), self.assertRaises(ArchiveError):
                    ops.write_json(self.stage / 'manifest.json', {**self.state, **update})
                    self.ensure()
            ops.write_json(self.stage / 'manifest.json', self.state)
            outside = self.root / 'external.tnbk'
            self.latest.rename(outside)
            ops.write_json(self.job_file, {**self.job, 'last_archive': str(outside)})
            with self.assertRaises(ArchiveError):
                self.ensure()
        export.assert_not_called()

    def test_prepare_uses_authenticated_archive_time_as_well_as_job_metadata(self):
        now = self.checked_at.timestamp() + self.config['backup_interval']
        ops.write_json(self.job_file, {**self.job, 'last_success_at': now})
        with self.assertRaisesMessage(ArchiveError, 'Tanggal arsip terautentikasi'):
            self.ensure(now=now)
        self.assertFalse(list(self.packages.iterdir()))

    def test_partial_export_is_preserved_and_next_attempt_creates_a_complete_pair(self):
        with patch.object(recovery_package, 'copy_archive', side_effect=OSError('fixture interrupted copy')):
            with self.assertRaises(OSError):
                self.ensure()
        partial = next(self.packages.iterdir())
        self.assertFalse((partial / 'manifest.json').exists())
        created = self.ensure()
        self.assertEqual(created['status'], 'created')
        self.assertNotEqual(Path(created['package_dir']), partial)
        self.assertEqual(list(partial.iterdir()), [])
        self.assertEqual(self.ensure()['status'], 'reused')

    def test_digest_match_with_tampered_ciphertext_is_not_reused(self):
        self.export()
        old_copy = self.packages / 'current' / 'archive.tnbk'
        damaged = bytearray(old_copy.read_bytes())
        damaged[-1] ^= 1
        old_copy.write_bytes(damaged)
        created = self.ensure()
        self.assertEqual(created['status'], 'created')
        self.assertEqual(old_copy.read_bytes(), bytes(damaged))

    def test_new_backup_published_during_copy_is_not_reported_as_current(self):
        export = recovery_package.export_package

        def rotate_job(*args):
            copied = export(*args)
            ops.write_json(self.job_file, {**self.job, 'last_archive': str(self.folder / 'next.tnbk')})
            return copied

        with patch.object(recovery_package, 'export_package', side_effect=rotate_job):
            with self.assertRaisesMessage(ArchiveError, 'Backup aktif berubah'):
                self.ensure()
        result = next(self.packages.iterdir())
        recovery_package.verify_package(result, self.keys / result.name)

    def test_prepare_lock_serializes_calls_for_the_same_source(self):
        with ops.ops_lock(self.stage, '.custody.lock'), patch.object(recovery_package, 'export_package') as export:
            with self.assertRaises(RuntimeError):
                self.ensure()
        export.assert_not_called()
