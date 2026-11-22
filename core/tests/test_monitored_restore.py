import io
import tempfile
from contextlib import ExitStack, contextmanager, redirect_stderr
from importlib.util import find_spec
from pathlib import Path
from unittest import skipUnless
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from core.backup_archive import ArchiveError
from scripts import verify_monitored_restore as restore


class DisposableRestoreTests(SimpleTestCase):
    @contextmanager
    def lab(self, *, role=(False, True), running=True, bind='127.0.0.1'):
        control = MagicMock()
        control.__enter__.return_value = control
        control.execute.return_value.fetchone.return_value = role
        info = {'Id': 'lab', 'State': {'Running': running},
                'NetworkSettings': {'Ports': {'5432/tcp': [{'HostIp': bind, 'HostPort': '33333'}]}}}
        with ExitStack() as stack:
            stack.enter_context(patch.object(restore.postgres_lab, 'read_manifest', return_value={'port': 33333}))
            stack.enter_context(patch.object(restore.postgres_lab, 'inspected', return_value=info))
            stack.enter_context(patch.object(restore.postgres_lab, 'connection_env', return_value={}))
            stack.enter_context(patch.object(restore, 'pg_config', return_value={'NAME': 'tagihnet_lab'}))
            connect = stack.enter_context(patch.object(restore, 'connect', return_value=control))
            yield connect, control

    def test_disposable_database_refuses_staging_or_non_loopback_or_stopped_lab(self):
        for source, settings in (('lab', {}), ('source', {'bind': '0.0.0.0'}), ('source', {'running': False})):
            with self.subTest(settings=settings), self.lab(**settings) as (connect, _):
                with self.assertRaises(ArchiveError), restore.disposable_database(source):
                    self.fail('Disallowed target opened')
                connect.assert_not_called()

    def test_disposable_database_refuses_a_superuser_or_role_without_createdb(self):
        for role in ((True, True), (False, False)):
            with self.subTest(role=role), self.lab(role=role) as (_, control):
                with self.assertRaises(ArchiveError), restore.disposable_database('source'):
                    self.fail('Disallowed role accepted')
                control.execute.assert_called_once()

    @skipUnless(find_spec('psycopg'), 'PostgreSQL driver required')
    def test_failure_cleans_only_the_database_created_for_this_drill(self):
        with self.lab() as (_, control):
            with self.assertRaises(OSError), restore.disposable_database('source') as (target, _, _):
                self.assertRegex(target, r'^tagihnet_restore_drill_[a-f0-9]{32}$')
                raise OSError('intentional test failure')
        statements = [call.args[0].as_string() for call in control.execute.call_args_list[1:]]
        self.assertEqual(statements, [f'CREATE DATABASE "{target}" WITH TEMPLATE template0', f'DROP DATABASE "{target}"'])

    def test_cli_rejects_existing_report_or_output_in_custody_before_restore(self):
        with tempfile.TemporaryDirectory(prefix='tagihnet-restore-unit-') as temporary:
            root = Path(temporary)
            report = root / 'report.json'
            report.write_text('keep')
            for output in (report, root / 'keys/unsafe.json', root / 'package/unsafe.json'):
                with self.subTest(output=output):
                    argv = ['restore', '--package-dir', str(root / 'package'), '--keys-dir', str(root / 'keys'),
                            '--output', str(output)]
                    with (patch.object(restore.sys, 'argv', argv), patch.object(restore, 'verify_restore') as verify,
                          redirect_stderr(io.StringIO()), self.assertRaises(SystemExit)):
                        restore.main()
                    verify.assert_not_called()
            self.assertEqual(report.read_text(), 'keep')
