import copy
import io
import json
import os
import tempfile
from contextlib import ExitStack, contextmanager, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from core.backup_archive import encrypt_stream, generate_key
from core.worker_heartbeat import heartbeat
from scripts import staging_lab as lab
from scripts import staging_ops as ops


STORAGE_STATS = '/ 4096 4000 500 800 200\n/var/lib/postgresql/data 4096 1000 200 400 80\n'


class StagingOperationsTests(SimpleTestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='tagihnet-ops-unit-')
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.state={'project':'tagihnet-stage-0123456789','directory':str(self.root),'port':18443,'initialized':True}
        self.save_state()
        self.now=2000000000

    def save_state(self,**changes):
        self.state.update(changes)
        (self.root/'manifest.json').write_text(json.dumps(self.state))

    def beat(self,**changes):
        return {'version':1,'role':'billing','state':'ok','updated_at':self.now,'pid':1,**changes}

    def healthy(self):
        return {'version':1,'project':self.state['project'],'checked_at':self.now,'workers_held':False,'checks':{},'issues':[],'metrics':{},'status':'ok'}

    def enable(self):
        key=self.root/'key.json';generate_key(key)
        return ops.configure(self.root,backup_enabled=True,backup_key_file=str(key),backup_interval=3600)

    @contextmanager
    def checks(self,beats=None,metrics=None,objects=None):
        metrics=metrics if metrics is not None else {k:0 for k in ('network_due','network_age','network_failed','billing_due','billing_age','reminder_due','reminder_age','reminder_failed','blocked')}
        if objects is None:
            objects=[{'Config':{'Labels':{'com.docker.compose.project':self.state['project'],'com.docker.compose.service':name}},'State':{'Running':True,'Health':{'Status':'healthy'}},'RestartCount':0} for name in ('db','web',*ops.WORKERS)]
        def compose(directory,*args,**kwargs):
            if args[0]=='ps':return SimpleNamespace(stdout='container')
            role=ops.WORKERS[args[2]]
            return SimpleNamespace(stdout=json.dumps((beats or {}).get(role,self.beat(role=role))))
        def docker(*args,**kwargs):
            if args[0]=='exec' and args[4]=='stat':return SimpleNamespace(stdout=STORAGE_STATS)
            return SimpleNamespace(stdout=json.dumps(objects if args[0]=='inspect' else metrics))
        certificate=SimpleNamespace(not_valid_after_utc=datetime.fromtimestamp(self.now+30*86400,timezone.utc),not_valid_before_utc=datetime.fromtimestamp(self.now-60,timezone.utc))
        for name in ('https.crt','postgres.crt','ca.crt'):
            target=self.root/'secrets'/name;target.parent.mkdir(exist_ok=True);target.write_bytes(b'fixture')
        response=Mock()
        response.status=200;response.read.return_value=b'{"status":"ok","mode":"simulation"}'
        with ExitStack() as stack:
            for name,value in (('compose',compose),('docker',docker),('database_container',Mock(return_value='db')),('x509.load_pem_x509_certificate',Mock(return_value=certificate))):
                stack.enter_context(patch.object(ops,name,value) if '.' not in name else patch('scripts.staging_ops.'+name,value))
            connection = Mock()
            connection.getresponse.return_value = response
            stack.enter_context(patch.object(lab, 'HTTPSConnection', return_value=connection))
            stack.enter_context(patch.object(lab.ssl, 'create_default_context', return_value=object()))
            yield response,certificate

    def test_optional_heartbeat_does_not_write_when_unconfigured(self):
        with patch.dict(os.environ,{},clear=True),patch('core.worker_heartbeat.tempfile.NamedTemporaryFile') as writer:
            self.assertIsNone(heartbeat('billing'))
        writer.assert_not_called()

    def test_atomic_heartbeat_is_private_and_replaced(self):
        with patch.dict(os.environ,{'TAGIHNET_HEARTBEAT_DIR':str(self.root/'beats')}):
            self.assertTrue(heartbeat('billing','busy'))
            self.assertTrue(heartbeat('billing','ok'))
        path=self.root/'beats/billing.json'
        self.assertEqual(json.loads(path.read_text())['state'],'ok')
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(len(list(path.parent.iterdir())),1)

    def test_heartbeat_io_failure_does_not_abort_worker(self):
        with patch.dict(os.environ,{'TAGIHNET_HEARTBEAT_DIR':str(self.root/'manifest.json')}):
            self.assertFalse(heartbeat('billing'))

    def test_heartbeat_stale_future_degraded_and_malformed(self):
        for changes,expected in (({},None),({'updated_at':self.now-121},'heartbeat_stale'),({'updated_at':self.now+6},'heartbeat_stale'),({'state':'busy'},None),({'state':'degraded'},'heartbeat_degraded'),({'state':'stopping'},'heartbeat_degraded'),({'updated_at':float('nan')},'heartbeat_invalid'),({'updated_at':float('inf')},'heartbeat_invalid'),({'state':[]},'heartbeat_invalid'),({'role':'network'},'heartbeat_invalid'),({'pid':True},'heartbeat_invalid')):
            with self.subTest(changes=changes):self.assertEqual(ops.heartbeat_problem(self.beat(**changes),'billing',self.now,120),expected)

    def test_default_backup_is_off_and_config_bound_to_project(self):
        self.assertFalse(ops.policy(self.root)['backup_enabled'])
        with self.assertRaises(ValueError):ops.configure(self.root,project='tagihnet-stage-aaaaaaaaaa')
        self.assertFalse((self.root/'ops/config.json').exists())

    def test_policy_rejects_invalid_intervals_and_paths_before_publish(self):
        invalid_changes = (
            {'check_interval': True}, {'heartbeat_max_age': 14}, {'backup_interval': 0},
            {'backup_directory': 'relative'}, {'backup_enabled': 'yes'}, {'unknown': 1},
            {'storage_min_free_mib': False}, {'storage_min_free_mib': 0},
            {'storage_min_free_mib': -1}, {'storage_min_free_mib': '1536'},
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes),self.assertRaises(ValueError):ops.configure(self.root,**changes)
        self.assertFalse((self.root/'ops/config.json').exists())

    def test_enabled_backup_requires_existing_private_key(self):
        with self.assertRaises(Exception):ops.configure(self.root,backup_enabled=True)
        config=self.enable()
        self.assertTrue(config['backup_enabled'])
        self.assertEqual((self.root/'ops/config.json').stat().st_mode&0o777,0o600)

    def test_configure_storage_alert_reads_legacy_policy_and_zero_restores_its_format(self):
        config = self.enable()
        path = self.root / 'ops/config.json'
        previous = path.read_bytes()
        self.assertNotIn('storage_min_free_mib', json.loads(previous))
        self.assertIsNone(ops.policy(self.root)['storage_min_free_mib'])

        argv = ['staging_ops', 'configure', '--directory', str(self.root), '--storage-min-free-mib']
        for supplied, expected in (('1536', 1536), ('0', None)):
            with patch.object(ops.sys, 'argv', [*argv, supplied]), redirect_stdout(io.StringIO()):
                self.assertEqual(ops.main(), 0)
            self.assertEqual(ops.policy(self.root), {**config, 'storage_min_free_mib': expected})
        self.assertEqual(path.read_bytes(), previous)

    def test_check_healthy_and_read_only_counter_query(self):
        with self.checks():report=ops.check(self.root,now=self.now)
        self.assertEqual(report['status'],'ok')
        self.assertEqual(report['metrics']['billing_due'],0)
        self.assertEqual(report['storage']['filesystems']['postgres_volume']['available_bytes'],819200)
        self.assertIn('BEGIN TRANSACTION READ ONLY',ops.METRICS_SQL)

    def test_https_health_uses_the_fixed_local_target_and_does_not_follow_redirects(self):
        context = object()
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 302
        with (patch.dict(os.environ, {'HTTPS_PROXY': 'http://example.invalid:3128'}),
              patch.object(lab.ssl, 'create_default_context', return_value=context) as create_context,
              patch.object(lab, 'HTTPSConnection', return_value=connection) as connect):
            self.assertFalse(lab.https_health(self.root, 18443))
        create_context.assert_called_once_with(cafile=str(self.root / 'secrets/ca.crt'))
        connect.assert_called_once_with('localhost', 18443, context=context, timeout=5)
        connection.request.assert_called_once_with('GET', '/health/')
        response.read.assert_not_called()
        connection.close.assert_called_once()

    def test_filesystem_counts_space_available_to_unprivileged_processes(self):
        counters = SimpleNamespace(f_frsize=4096, f_blocks=1000, f_bfree=200, f_bavail=91,
                                   f_files=100, f_ffree=30, f_favail=19)
        with patch.object(ops.os, 'statvfs', return_value=counters):
            self.assertEqual(ops.host_filesystem(self.root), {
                'total_bytes': 4096000, 'available_bytes': 372736, 'total_inodes': 100, 'available_inodes': 19,
            })
        self.assertIsNone(ops.filesystem_capacity(4096, 1000, 91, 0, 0)['available_inodes'])

    def test_capacity_threshold_compares_each_filesystem_without_writing(self):
        capacity = ops.filesystem_capacity(4096, 4000, 500, 800, 200)
        with (patch.object(ops, 'host_filesystem', return_value=capacity),
              patch.object(ops, 'database_container', return_value='db'),
              patch.object(ops, 'docker', return_value=SimpleNamespace(stdout=STORAGE_STATS)) as docker):
            report = ops.storage(self.root, min_free_mib=1)
            self.assertEqual(ops.storage(self.root)['status'], 'ok')
        self.assertEqual(report['status'], 'warning')
        self.assertEqual(report['minimum_free_bytes'], 1048576)
        self.assertEqual(report['issues'], [{'code': 'filesystem_below_minimum', 'severity': 'warning',
                                             'component': 'postgres_volume'}])
        self.assertNotIn('backup_destination', report['filesystems'])
        self.assertEqual(docker.call_args.args[:5], ('exec', '--user', '999:999', 'db', 'stat'))
        self.assertFalse((self.root/'ops').exists())

    def test_configured_storage_warning_reaches_watcher_status_once_and_resolves(self):
        ops.configure(self.root, storage_min_free_mib=1)
        high = ops.filesystem_capacity(4096, 4000, 500, 800, 200)
        issue = {'code': 'filesystem_below_minimum', 'severity': 'warning', 'component': 'postgres_volume'}
        with self.checks(), patch.object(ops, 'host_filesystem', return_value=high):
            self.assertEqual(ops.check(self.root, now=self.now)['status'], 'ok')
            self.assertEqual(ops.storage(self.root)['issues'], [issue])
            self.assertEqual(ops.tick(self.root, self.now)['issues'], [issue])
            repeated = ops.tick(self.root, self.now + 1)
            self.assertEqual(ops.status(self.root, self.now + 1)['status'], 'warning')
            with patch.object(ops, 'database_filesystems', return_value={'container_root': high, 'postgres_volume': high}):
                self.assertEqual(ops.tick(self.root, self.now + 2)['status'], 'ok')
        self.assertEqual(repeated['storage']['issues'], [issue])
        events = (self.root / 'ops/events.jsonl').read_text().splitlines()
        self.assertEqual([json.loads(line) for line in events], [
            {'event': 'raised', 'at': self.now, **issue},
            {'event': 'resolved', 'at': self.now + 2, **issue},
        ])

    def test_alert_opt_in_propagates_unavailable_and_exhausted_capacity(self):
        high = ops.filesystem_capacity(4096, 4000, 500, 800, 200)
        exhausted = ops.filesystem_capacity(4096, 4000, 0, 800, 200)
        capacities = {'container_root': None, 'postgres_volume': exhausted}
        with (self.checks(), patch.object(ops, 'host_filesystem', return_value=high),
              patch.object(ops, 'database_filesystems', return_value=capacities)):
            self.assertEqual(ops.tick(self.root, self.now)['status'], 'ok')
            ops.configure(self.root, storage_min_free_mib=1)
            report = ops.tick(self.root, self.now + 1)
        self.assertEqual(report['status'], 'critical')
        self.assertEqual(report['issues'], [
            {'code': 'filesystem_unavailable', 'severity': 'unknown', 'component': 'container_root'},
            {'code': 'filesystem_exhausted', 'severity': 'critical', 'component': 'postgres_volume'},
        ])

    def test_known_inode_exhaustion_is_critical_without_byte_threshold(self):
        capacity = ops.filesystem_capacity(4096, 4000, 500, 800, 0)
        with patch.object(ops, 'host_filesystem', return_value=capacity):
            report = ops.storage_snapshot(self.root, ops.policy(self.root), None)
        self.assertEqual(report['status'], 'critical')
        self.assertIn({'code': 'filesystem_exhausted', 'severity': 'critical', 'component': 'staging_directory'},
                      report['issues'])

    def test_malformed_storage_samples_are_unknown_and_discard_diagnostics(self):
        invalid = ('/ 4096 4000 500 800 200\n', STORAGE_STATS + 'private text',
                   STORAGE_STATS.replace('400 80', '400 -80'), STORAGE_STATS + STORAGE_STATS)
        for output in invalid:
            with self.subTest(output=output), patch.object(ops, 'docker', return_value=SimpleNamespace(stdout=output)):
                self.assertEqual(ops.database_filesystems('db'), {'container_root': None, 'postgres_volume': None})
        with patch.object(ops, 'docker', side_effect=OSError('private text')):
            report = ops.storage_snapshot(self.root, ops.policy(self.root), 'db')
        self.assertEqual(report['status'], 'unknown')
        self.assertNotIn('private text', json.dumps(report))

    def test_backup_measurement_requires_the_configured_destination_to_exist(self):
        config = self.enable()
        backup_directory = Path(config['backup_directory'])
        with patch.object(ops, 'docker', return_value=SimpleNamespace(stdout=STORAGE_STATS)):
            before = ops.storage_snapshot(self.root, config, 'db')
            self.assertIsNone(before['filesystems']['backup_destination'])
            self.assertFalse(backup_directory.exists())
            backup_directory.mkdir(parents=True)
            after = ops.storage_snapshot(self.root, config, 'db')
        self.assertEqual(after['status'], 'ok')
        self.assertGreater(after['filesystems']['backup_destination']['total_bytes'], 0)

    def test_storage_reports_host_capacity_without_contacting_blocked_restore(self):
        self.save_state(restore_state='failed')
        with patch.object(ops, 'database_container') as container, patch.object(ops, 'docker') as docker:
            report = ops.storage(self.root)
        container.assert_not_called()
        docker.assert_not_called()
        self.assertEqual(report['status'], 'unknown')
        self.assertIsNone(report['filesystems']['postgres_volume'])
        self.assertIsNotNone(report['filesystems']['checkout'])

    def test_recovery_plan_estimates_four_vfs_containers_and_postgres_on_docker_filesystem(self):
        image = 'sha256:' + 'a' * 64
        self.save_state(image_id=image)
        available = ops.filesystem_capacity(1, 4000, 650, 800, 200)
        responses = [STORAGE_STATS, '["vfs", "/var/lib/docker"]', '85\n150\n225\n', '75 /var/lib/postgresql/data\n']
        with (patch.object(ops, 'host_filesystem', return_value=available) as sample,
              patch.object(ops, 'database_container', return_value='db'),
              patch.object(ops, 'docker', side_effect=[SimpleNamespace(stdout=value) for value in responses]) as docker):
            report = ops.storage(self.root, recovery_plan=True)
        plan = report['recovery_plan']
        self.assertEqual(plan['prepare_image_bytes'], 85)
        self.assertEqual(plan['estimated_bytes'], 760)
        self.assertEqual(plan['remaining_after_estimate_bytes'], -110)
        self.assertEqual(report['status'], 'warning')
        self.assertEqual(report['issues'], [{'code': 'recovery_estimate_exceeds_available', 'severity': 'warning',
                                             'component': 'docker_recovery'}])
        self.assertEqual(sample.call_args.args, (Path('/var/lib/docker'),))
        self.assertEqual(docker.call_args_list[2].args[:5], ('image', 'inspect', ops.PREPARE_IMAGE, ops.PG_IMAGE, image))
        self.assertEqual(docker.call_args.args[:5], ('exec', '--user', '999:999', 'db', 'du'))
        self.assertFalse((self.root/'ops').exists())

    def test_recovery_plan_with_room_reports_remaining_estimate(self):
        responses = ['["vfs", "/var/lib/docker"]', '85\n150\n225\n', '75 /var/lib/postgresql/data\n']
        capacity = ops.filesystem_capacity(1, 4000, 900, 800, 200)
        with (patch.object(ops, 'host_filesystem', return_value=capacity),
              patch.object(ops, 'docker', side_effect=[SimpleNamespace(stdout=value) for value in responses])):
            plan = ops.recovery_storage_plan('test-image', 'db')
        self.assertEqual(plan['status'], 'ok')
        self.assertEqual(plan['remaining_after_estimate_bytes'], 140)

    def test_recovery_plan_reports_exhausted_unknown_or_unsupported_storage(self):
        enough = ops.filesystem_capacity(1, 4000, 900, 800, 200)
        exhausted = ops.filesystem_capacity(1, 4000, 900, 800, 0)
        for driver, capacity, severity, issue in (
            ('vfs', exhausted, 'critical', 'filesystem_exhausted'),
            ('vfs', None, 'unknown', 'recovery_plan_unavailable'),
            ('overlay2', enough, 'unknown', 'recovery_estimate_unsupported_driver'),
        ):
            with (self.subTest(driver=driver, issue=issue),
                  patch.object(ops, 'host_filesystem', return_value=capacity),
                  patch.object(ops, 'docker', return_value=SimpleNamespace(stdout=json.dumps([driver, '/var/lib/docker']))) as docker):
                plan = ops.recovery_storage_plan('test-image', 'db')
                docker.assert_called_once()
            self.assertEqual((plan['status'], plan['issue']), (severity, issue))
            self.assertNotIn('estimated_bytes', plan)

    def test_recovery_plan_never_treats_malformed_measurements_as_success(self):
        with (patch.object(ops, 'host_filesystem', return_value=ops.filesystem_capacity(1, 4000, 900, 800, 200)),
              patch.object(ops, 'docker', side_effect=[SimpleNamespace(stdout='["vfs", "/var/lib/docker"]'),
                                                       SimpleNamespace(stdout='85\n150\n225\n'),
                                                       OSError('private diagnostic')])):
            plan = ops.recovery_storage_plan('test-image', 'db')
        self.assertEqual(plan['status'], 'unknown')
        self.assertNotIn('estimated_bytes', plan)
        self.assertNotIn('private diagnostic', json.dumps(plan))

    def test_storage_cli_returns_nonzero_when_recovery_estimate_does_not_fit(self):
        argv = ['staging_ops', 'storage', '--directory', str(self.root), '--recovery-plan']
        report = {'status': 'warning', 'recovery_plan': {'issue': 'recovery_estimate_exceeds_available'}}
        with (patch.object(ops.sys, 'argv', argv), patch.object(ops, 'storage', return_value=report) as storage,
              redirect_stdout(io.StringIO()) as stream):
            self.assertEqual(ops.main(), 1)
        storage.assert_called_once_with(self.root, None, recovery_plan=True)
        self.assertEqual(json.loads(stream.getvalue()), report)

    def test_running_worker_without_progress_is_critical(self):
        with self.checks(beats={'billing':self.beat(updated_at=self.now-121)}):report=ops.check(self.root,now=self.now)
        self.assertIn({'code':'heartbeat_stale','severity':'critical','component':'billing-worker'},report['issues'])

    def test_held_workers_remain_held_and_running_one_is_error(self):
        self.save_state(recovery_pending=True)
        with self.checks():report=ops.check(self.root,now=self.now)
        self.assertEqual(sum(i['code']=='unexpected_worker' for i in report['issues']),3)
        self.assertEqual(report['checks']['billing-worker']['status'],'held')

    def test_failed_restore_never_contacts_services(self):
        self.save_state(restore_state='failed')
        with patch.object(ops,'compose') as compose:report=ops.check(self.root,now=self.now)
        compose.assert_not_called();self.assertEqual(report['status'],'critical')

    def test_unavailable_docker_and_metrics_are_unknown_not_zero(self):
        with self.checks(),patch.object(ops,'docker',side_effect=OSError('private diagnostic')):
            report=ops.check(self.root,now=self.now)
        self.assertIsNone(report['metrics']);self.assertEqual(report['status'],'unknown')
        self.assertNotIn('private diagnostic',json.dumps(report))

    def test_malformed_metrics_never_become_success(self):
        with self.checks(metrics={'billing_due':float('nan')}):report=ops.check(self.root,now=self.now)
        self.assertIsNone(report['metrics']);self.assertIn('metrics_unavailable',[i['code'] for i in report['issues']])

    def test_https_failure_and_expired_certificate_are_critical(self):
        with self.checks() as (response,cert):
            response.read.return_value=b'{}';cert.not_valid_after_utc=datetime.fromtimestamp(self.now-1,timezone.utc)
            report=ops.check(self.root,now=self.now)
        self.assertEqual(report['status'],'critical')
        self.assertIn('certificate_expired',[i['code'] for i in report['issues']])
        self.assertIn('https_unhealthy',[i['code'] for i in report['issues']])

    def test_backup_schedule_authenticates_and_does_not_duplicate_before_due(self):
        self.enable()
        def dump(directory,target,key):encrypt_stream(io.BytesIO(b'PGDMPfixture'),target,key,'postgresql')
        with patch.object(ops,'backup',side_effect=dump) as dump,patch.object(ops,'check',side_effect=lambda *a:copy.deepcopy(self.healthy())):
            result=ops.tick(self.root,self.now);ops.tick(self.root,self.now+10)
        self.assertEqual(dump.call_count,1);self.assertEqual(result['status'],'ok')
        self.assertTrue(Path(result['backup']['last_archive']).is_file())
        self.assertEqual((self.root/'ops/backup.json').stat().st_mode&0o777,0o600)

    def test_manual_backup_runs_before_due_and_schedules_from_verified_completion(self):
        config = self.enable()
        archive = Path(config['backup_directory']) / 'previous.tnbk'
        key = ops.read_key(Path(config['backup_key_file']))
        encrypt_stream(io.BytesIO(b'PGDMPprevious'), archive, key, 'postgresql')
        previous = archive.read_bytes()
        ops.write_json(self.root / 'ops/backup.json', {'last_success_at': self.now - 60, 'last_archive': str(archive)})

        def dump(directory, target, key):
            encrypt_stream(io.BytesIO(b'PGDMPmanual'), target, key, 'postgresql')

        finished = self.now + 21
        with (patch.object(ops, 'backup', side_effect=dump) as dump_mock,
              patch.object(ops, 'check', side_effect=lambda directory, config, now: {**self.healthy(), 'checked_at': now}),
              patch.object(ops.time, 'time', side_effect=[self.now, finished, finished + 1])):
            result = ops.tick(self.root, force_backup=True)
            ops.tick(self.root, finished + 30)
        dump_mock.assert_called_once()
        self.assertEqual(result['checked_at'], finished + 1)
        self.assertEqual(result['backup']['last_attempt_at'], self.now)
        self.assertEqual(result['backup']['last_success_at'], finished)
        self.assertEqual(result['backup']['next_attempt_at'], finished + config['backup_interval'])
        self.assertFalse(result['backup']['failed'])
        self.assertNotEqual(result['backup']['last_archive'], str(archive))
        self.assertEqual(archive.read_bytes(), previous)

    def test_manual_backup_refuses_unready_held_or_unconfigured_project(self):
        with patch.object(ops, 'backup') as dump:
            with self.assertRaises(ValueError):
                ops.tick(self.root, self.now, force_backup=True)
            self.enable()
            for changes in ({'initialized': False}, {'recovery_pending': True}, {'restore_state': 'failed'}):
                self.save_state(initialized=True, recovery_pending=False, restore_state=None)
                self.save_state(**changes)
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    ops.tick(self.root, self.now, force_backup=True)
        dump.assert_not_called()
        self.assertFalse((self.root / 'ops/backup.json').exists())

    def test_failed_manual_backup_preserves_previous_archive_and_records_retry_from_finish(self):
        self.enable()
        previous = {'last_success_at': self.now - 60, 'last_archive': str(self.root / 'previous.tnbk')}
        ops.write_json(self.root / 'ops/backup.json', previous)
        finished = self.now + 22
        with (patch.object(ops, 'backup', side_effect=OSError('private fixture')),
              patch.object(ops, 'check', side_effect=lambda *args: self.healthy()),
              patch.object(ops.time, 'time', side_effect=[self.now, finished, finished + 1])):
            result = ops.tick(self.root, force_backup=True)
        metadata = ops.read_json(self.root / 'ops/backup.json')
        self.assertEqual(metadata, {**previous, 'last_attempt_at': self.now, 'failed': True, 'next_attempt_at': finished + 300})
        self.assertTrue(result['backup']['failed'])
        self.assertEqual(result['status'], 'critical')
        self.assertNotIn('private fixture', json.dumps(result))

    def test_backup_now_exit_code_reflects_backup_result(self):
        argv = ['staging_ops', 'backup-now', '--directory', str(self.root)]
        for failed, exit_code in ((False, 0), (True, 1)):
            report = {'status': 'warning', 'backup': {'failed': failed}}
            with (patch.object(ops.sys, 'argv', argv), patch.object(ops, 'tick', return_value=report) as tick,
                  redirect_stdout(io.StringIO()) as output):
                self.assertEqual(ops.main(), exit_code)
                tick.assert_called_once_with(self.root, force_backup=True)
                self.assertEqual(json.loads(output.getvalue()), report)

    def test_backup_failure_is_visible_and_retries_with_backoff(self):
        self.enable()
        with patch.object(ops,'backup',side_effect=OSError('secret fixture')) as dump,patch.object(ops,'check',side_effect=lambda *a:copy.deepcopy(self.healthy())):
            first=ops.tick(self.root,self.now);ops.tick(self.root,self.now+10);ops.tick(self.root,self.now+301)
        self.assertEqual(dump.call_count,2);self.assertEqual(first['status'],'critical')
        self.assertNotIn('secret fixture',(self.root/'ops/events.jsonl').read_text())
        self.assertEqual(len((self.root/'ops/events.jsonl').read_text().splitlines()),1)

    def test_shorter_interval_takes_effect_from_last_success(self):
        self.enable()
        ops.write_json(self.root/'ops/backup.json',{'last_success_at':self.now-3601,'last_archive':str(self.root/'old'),'next_attempt_at':self.now+86400})
        def dump(directory,target,key):encrypt_stream(io.BytesIO(b'PGDMPfixture'),target,key,'postgresql')
        with patch.object(ops,'backup',side_effect=dump) as dump,patch.object(ops,'check',return_value=self.healthy()):ops.tick(self.root,self.now)
        dump.assert_called_once()

    def test_recovery_hold_prevents_scheduled_backup(self):
        self.enable();self.save_state(recovery_pending=True)
        report=self.healthy();report['workers_held']=True
        with patch.object(ops,'backup') as dump,patch.object(ops,'check',return_value=report):ops.tick(self.root,self.now)
        dump.assert_not_called();self.assertFalse((self.root/'ops/backup.json').exists())

    def test_disabled_backup_never_calls_dump(self):
        with patch.object(ops,'backup') as dump,patch.object(ops,'check',return_value=self.healthy()):ops.tick(self.root,self.now)
        dump.assert_not_called()

    def test_missing_latest_backup_cannot_appear_healthy(self):
        self.enable()
        ops.write_json(self.root/'ops/backup.json',{'last_success_at':self.now,'last_archive':str(self.root/'missing'),'next_attempt_at':self.now+3600})
        with patch.object(ops,'check',return_value=self.healthy()):report=ops.tick(self.root,self.now)
        self.assertEqual(report['issues'][0]['code'],'backup_missing')

    def test_events_are_transitions_with_private_bounded_rotation(self):
        good=self.healthy();bad=copy.deepcopy(good);bad['issues']=[{'code':'heartbeat_stale','severity':'critical','component':'billing-worker'}]
        ops.append_events(self.root,bad,{})
        ops.append_events(self.root,bad,bad)
        ops.append_events(self.root,good,bad)
        path=self.root/'ops/events.jsonl'
        self.assertEqual([json.loads(line)['event'] for line in path.read_text().splitlines()],['raised','resolved'])
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        path.write_text('x'*262145)
        ops.append_events(self.root,bad,good)
        self.assertTrue((path.parent/'events.1.jsonl').exists());self.assertLess(path.stat().st_size,1024)

    def test_stale_status_and_watcher_failure_are_unknown(self):
        ops.write_json(self.root/'ops/status.json',self.healthy())
        self.assertEqual(ops.status(self.root,self.now)['status'],'ok')
        self.assertEqual(ops.status(self.root,self.now+151)['status'],'unknown')
        ops.write_json(self.root/'ops/watcher-failure.json',{'failed_at':self.now+1})
        self.assertEqual(ops.status(self.root,self.now+2)['status'],'unknown')

    def test_pid_reuse_or_foreign_command_is_never_signalled(self):
        ops.write_json(self.root/'ops/watcher.json',{'pid':os.getpid(),'start_ticks':'wrong','token':'a'*32,'project':self.state['project']})
        with patch.object(ops.os,'kill') as kill:self.assertFalse(ops.stop(self.root))
        kill.assert_not_called()
        data=ops.read_json(self.root/'ops/watcher.json');data['start_ticks']=ops.process_ticks(os.getpid());ops.write_json(self.root/'ops/watcher.json',data)
        self.assertIsNone(ops.watcher(self.root))

    def test_tick_lock_prevents_duplicate_backups(self):
        with ops.ops_lock(self.root):
            for force_backup in (False, True):
                with self.assertRaises(RuntimeError):
                    ops.tick(self.root,self.now,force_backup=force_backup)

    def test_stop_remains_possible_while_tick_holds_lock(self):
        with ops.ops_lock(self.root),patch.object(ops,'watcher',return_value={'pid':42}),patch.object(ops.os,'kill') as kill:
            self.assertTrue(ops.stop(self.root))
        kill.assert_called_once_with(42,ops.signal.SIGTERM)

    def test_start_is_idempotent_for_verified_watcher(self):
        with patch.object(ops,'watcher',return_value={'pid':42}),patch.object(ops.subprocess,'Popen') as spawn:
            self.assertEqual(ops.start(self.root)['pid'],42)
        spawn.assert_not_called()

    def test_oversized_state_is_rejected(self):
        path=self.root/'oversized';path.write_text(' '*65537)
        with self.assertRaises(ValueError):ops.read_json(path)
