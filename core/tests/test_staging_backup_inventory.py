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

from core.backup_archive import encrypt_stream, generate_key, read_key
from scripts import recovery_package, staging_backup_inventory as inventory, staging_ops as ops


class BackupInventoryTests(SimpleTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='tagihnet-inventory-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stage = self.root / 'stage'
        self.stage.mkdir()
        ops.write_json(self.stage / 'manifest.json', {'project': 'tagihnet-stage-0123456789', 'directory': str(self.stage)})
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
