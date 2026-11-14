import copy
import io
import json
import os
import tempfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from core.backup_archive import encrypt_stream, generate_key
from core.worker_heartbeat import heartbeat
from scripts import staging_ops as ops


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
            return SimpleNamespace(stdout=json.dumps(objects if args[0]=='inspect' else metrics))
        certificate=SimpleNamespace(not_valid_after_utc=datetime.fromtimestamp(self.now+30*86400,timezone.utc),not_valid_before_utc=datetime.fromtimestamp(self.now-60,timezone.utc))
        for name in ('https.crt','postgres.crt','ca.crt'):
            target=self.root/'secrets'/name;target.parent.mkdir(exist_ok=True);target.write_bytes(b'fixture')
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.status=200;response.read.return_value=b'{"status":"ok","mode":"simulation"}'
        with ExitStack() as stack:
            for name,value in (('compose',compose),('docker',docker),('database_container',Mock(return_value='db')),('urlopen',Mock(return_value=response)),('ssl.create_default_context',Mock(return_value=object())),('x509.load_pem_x509_certificate',Mock(return_value=certificate))):
                stack.enter_context(patch.object(ops,name,value) if '.' not in name else patch('scripts.staging_ops.'+name,value))
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
        for changes in ({'check_interval':True},{'heartbeat_max_age':14},{'backup_interval':0},{'backup_directory':'relative'},{'backup_enabled':'yes'},{'unknown':1}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):ops.configure(self.root,**changes)
        self.assertFalse((self.root/'ops/config.json').exists())

    def test_enabled_backup_requires_existing_private_key(self):
        with self.assertRaises(Exception):ops.configure(self.root,backup_enabled=True)
        config=self.enable()
        self.assertTrue(config['backup_enabled'])
        self.assertEqual((self.root/'ops/config.json').stat().st_mode&0o777,0o600)

    def test_check_healthy_and_read_only_counter_query(self):
        with self.checks():report=ops.check(self.root,now=self.now)
        self.assertEqual(report['status'],'ok')
        self.assertEqual(report['metrics']['billing_due'],0)
        self.assertIn('BEGIN TRANSACTION READ ONLY',ops.METRICS_SQL)

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
        with ops.ops_lock(self.root),self.assertRaises(RuntimeError):ops.tick(self.root,self.now)

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
