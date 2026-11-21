import copy
import inspect
import io
import json
import tempfile
import time
from contextlib import ExitStack,contextmanager,redirect_stderr,redirect_stdout
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from cryptography.fernet import Fernet
from django.test import SimpleTestCase
from core.backup_archive import encrypt_stream,generate_key,read_key
from scripts import staging_ops as ops
from scripts import staging_preflight as pre
from scripts.recovery_package import export_package
from scripts.staging_lab import compose,pin_image,operation_lock


class StagingPreflightTests(SimpleTestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='tagihnet-preflight-unit-');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.directory=self.root/'stage';self.directory.mkdir()
        self.state={'project':'tagihnet-stage-0123456789','directory':str(self.directory),'port':18443,'initialized':True,'image_id':'sha256:'+'a'*64}
        self.save_state();self.now=time.time();self.image='sha256:'+'a'*64;self.source='b'*64
        secret=self.directory/'secrets';secret.mkdir(mode=0o700)
        self.keyfile=secret/'backup-key.json';generate_key(self.keyfile);self.key=read_key(self.keyfile)
        (secret/'mfa-key').write_bytes(Fernet.generate_key());(secret/'mfa-key').chmod(0o600)
        self.archive=self.directory/'backups/latest.tnbk';encrypt_stream(io.BytesIO(b'PGDMPfixture'),self.archive,self.key,'postgresql')
        ops.configure(self.directory,backup_enabled=True,backup_directory=str(self.archive.parent))
        ops.write_json(self.directory/'ops/backup.json',{'last_success_at':self.now,'last_archive':str(self.archive),'failed':False})
        self.package=self.root/'package';self.keys=self.root/'keys'
        with patch('scripts.recovery_package.exporter_commit',return_value='a'*40):export_package(self.archive,self.keyfile,secret/'mfa-key',self.package,self.keys)
        self.pins={'django':'5.2.18'}

    def save_state(self,**changes):
        self.state.update(changes);(self.directory/'manifest.json').write_text(json.dumps(self.state))

    def object(self,name):
        volumes={'/run/tagihnet':('runtime-secrets',False)}
        if name=='web':volumes['/run/https']=('https-secrets',False)
        if name=='db':volumes={'/var/lib/postgresql/data':('postgres-data',True),'/run/postgres':('postgres-secrets',False),'/docker-entrypoint-initdb.d':('postgres-secrets',False)}
        return {'Config':{'Image':pre.PG_IMAGE if name=='db' else 'tagihnet-staging:local','User':'' if name=='db' else '10001:10001',
                         'Labels':{'com.docker.compose.project':self.state['project'],'com.docker.compose.service':name}},
            'Image':self.image,'State':{'Running':True,'Paused':False,'OOMKilled':False},
            'HostConfig':{'NetworkMode':self.state['project']+'_default','Privileged':False,'ReadonlyRootfs':True,'PidMode':'',
                         'Memory':512*1024**2,'NanoCpus':10**9,'PidsLimit':128,'CapDrop':['ALL'],'CapAdd':None,'SecurityOpt':['no-new-privileges:true'],
                         'LogConfig':{'Type':'json-file','Config':{'max-size':'10m','max-file':'3'}}},
            'NetworkSettings':{'Ports':{'8443/tcp':[{'HostIp':'127.0.0.1','HostPort':'18443'}]} if name=='web' else {}},
            'Mounts':[{'Type':'volume','Name':self.state['project']+'_'+volume,'Destination':target,'RW':rw} for target,(volume,rw) in volumes.items()]}

    def data(self):
        return {'version':1,'source_sha256':self.source,'checks':['security.W005','security.W021'],'pending_migrations':[],
                'unexpected_migrations':[],
                'migration_sha256':'c'*64,'packages':self.pins.copy(),
                'settings':{key:True for key in ('profile','debug_off','simulation','cookies_secure','native_tls','mail_inactive','local_hsts')},
                'database':{'tls':True,'restricted_runtime':True}}

    @contextmanager
    def inspection(self,objects=None,data=None,health=None):
        objects=objects if objects is not None else [self.object(name) for name in ('db',*pre.APP_SERVICES)]
        data=data or self.data();health=health or {'status':'ok','metrics':{'network_due':0,'billing_due':0,'reminder_due':0}}
        with ExitStack() as stack:
            for name,value in (('application_digest',lambda root:self.source),('dependencies',lambda root:self.pins),
                ('compose',lambda *a,**kw:SimpleNamespace(stdout='container')),('docker',lambda *a,**kw:SimpleNamespace(stdout=self.image if a[0]=='image' else json.dumps(objects))),
                ('probe',lambda *a,**kw:copy.deepcopy(data))):stack.enter_context(patch.object(pre,name,value))
            stack.enter_context(patch.object(pre.ops,'check',return_value=health))
            stack.enter_context(patch.object(pre.subprocess,'run',return_value=SimpleNamespace(stdout='a'*40)))
            yield

    def check(self,mode='active'):
        return pre.check(self.directory,self.image,self.package,self.keys,mode,now=self.now)

    def custody_roots(self):
        roots = (self.root / 'prepared-packages', self.root / 'prepared-keys')
        for root in roots:
            root.mkdir(mode=0o700)
        return roots

    def prepare(self, roots):
        return pre.check(self.directory, self.image, None, None, now=self.now, custody_roots=roots)

    def codes(self,result):return {issue['code'] for issue in result['issues']}

    def tree(self):
        root=self.root/'tree';root.mkdir()
        for name in pre.DIRECTORIES:(root/name).mkdir()
        for name in pre.ROOT_FILES:(root/name).write_text('fixture')
        (root/'core/model.py').write_text('value=1')
        return root

    def test_digest_is_portable_and_changes_with_application_bytes(self):
        root=self.tree();before=pre.application_digest(root)
        namespace={};exec(inspect.getsource(pre.application_digest),namespace)
        self.assertEqual(namespace['application_digest'](root),before)
        (root/'core/model.py').write_text('value=2');self.assertNotEqual(pre.application_digest(root),before)

    def test_tests_and_bytecode_do_not_change_image_tree_digest(self):
        root=self.tree();before=pre.application_digest(root)
        (root/'core/tests').mkdir();(root/'core/tests/new.py').write_text('test')
        (root/'core/__pycache__').mkdir();(root/'core/__pycache__/model.pyc').write_bytes(b'cache')
        self.assertEqual(pre.application_digest(root),before)

    def test_missing_source_file_and_symlink_are_rejected(self):
        root=self.tree();(root/'manage.py').unlink()
        with self.assertRaises(ValueError):pre.application_digest(root)
        (root/'manage.py').symlink_to(root/'core/model.py')
        with self.assertRaises(ValueError):pre.application_digest(root)

    def test_dependency_parser_requires_pins_and_rejects_cycles(self):
        root=self.root/'requirements';root.mkdir();(root/'requirements-staging.txt').write_text('Django==5.2.18\n')
        self.assertEqual(pre.dependencies(root),{'django':'5.2.18'})
        for value in ('Django>=5.2\n','-r requirements-staging.txt\n','-r ../private\n'):
            (root/'requirements-staging.txt').write_text(value)
            with self.assertRaises(ValueError):pre.dependencies(root)

    def test_container_rejects_privilege_resource_and_port_changes(self):
        for field,value,code in (('Privileged',True,'privileged_container'),('ReadonlyRootfs',False,'writable_root'),
            ('Memory',0,'memory_limit'),('NanoCpus',0,'cpu_limit'),('PidsLimit',-1,'pid_limit'),('CapAdd',['SYS_ADMIN'],'capabilities'),('NetworkMode','host','network_mode')):
            obj=self.object('web');obj['HostConfig'][field]=value
            with self.subTest(field=field):self.assertIn(code,pre.container_issues('web',obj,self.state,self.image))
        obj=self.object('web');obj['NetworkSettings']['Ports']['8443/tcp'][0]['HostIp']='0.0.0.0'
        self.assertIn('published_ports',pre.container_issues('web',obj,self.state,self.image))

    def test_container_rejects_foreign_secret_mount_and_image(self):
        obj=self.object('web');obj['Image']='sha256:'+'d'*64;obj['Mounts'][0]['Name']='other_runtime-secrets'
        self.assertTrue({'image_mismatch','volume_identity'}<=set(pre.container_issues('web',obj,self.state,self.image)))
        obj=self.object('db');obj['NetworkSettings']['Ports']={'5432/tcp':[{'HostIp':'127.0.0.1','HostPort':'5432'}]}
        self.assertIn('published_ports',pre.container_issues('db',obj,self.state,self.image))

    def test_probe_rejects_new_warning_migration_source_or_package_drift(self):
        for changes,code in (({'checks':['security.W018']},'deploy_checks'),({'pending_migrations':['core.9999_pending']},'migrations_pending'),
            ({'source_sha256':'d'*64},'source_mismatch'),({'packages':{'django':'1.0'}},'dependency_mismatch'),
            ({'unexpected_migrations':['core.9999_future']},'migrations_unrecognized')):
            with self.subTest(code=code):self.assertIn(code,pre.probe_issues({**self.data(),**changes},self.source,self.pins))

    def test_probe_requires_secure_settings_and_database(self):
        data=self.data();data['settings']['debug_off']=False;data['database']['tls']=False
        self.assertEqual(pre.probe_issues(data,self.source,self.pins),['settings_invalid','database_security'])

    def test_malformed_probe_is_rejected_without_publishing_diagnostic_body(self):
        data=self.data();data['checks']=['private secret body']
        with self.inspection(data=data):result=self.check()
        self.assertIn('probe_invalid',self.codes(result));self.assertNotIn('private secret body',json.dumps(result))

    def test_active_pass_still_is_not_production_ready(self):
        before=(self.directory/'ops/backup.json').read_bytes()
        with self.inspection(), patch.object(pre, 'ensure_pair') as prepare:
            result=self.check()
        prepare.assert_not_called()
        self.assertEqual(result['status'],'passed');self.assertFalse(result['production_ready'])
        self.assertEqual((self.directory/'ops/backup.json').read_bytes(),before)

    def test_migration_snapshot_changes_between_services_fail(self):
        def probe(directory,service,pins):
            data=self.data()
            if service=='web':data['migration_sha256']='d'*64
            return data
        with self.inspection(),patch.object(pre,'probe',side_effect=probe):result=self.check()
        self.assertIn('migration_state_changed',self.codes(result))

    def test_image_tag_and_invalid_time_are_refused_before_inspection(self):
        for image,now in (('tagihnet-staging:local',self.now),(self.image,float('nan'))):
            with patch.object(pre,'compose') as compose,self.assertRaises(ValueError):pre.check(self.directory,image,self.package,self.keys,now=now)
            compose.assert_not_called()

    def test_held_recovery_requires_explicit_mode(self):
        self.save_state(recovery_pending=True)
        with self.inspection():result=self.check()
        self.assertEqual(self.codes(result),{'recovery_mode_mismatch'})

    def test_recovery_pass_holds_workers_without_requiring_local_backup(self):
        self.save_state(recovery_pending=True)
        objects=[self.object(name) for name in ('db',*pre.APP_SERVICES)]
        for obj in objects[2:]:obj['State']['Running']=False
        (self.directory/'ops/backup.json').unlink()
        with self.inspection(objects=objects):result=self.check('recovery')
        self.assertEqual(result['status'],'passed');self.assertFalse(result['production_ready'])

    def test_recovery_unexpected_worker_is_failure(self):
        self.save_state(recovery_pending=True)
        with self.inspection():result=self.check('recovery')
        self.assertIn('unexpected_worker',self.codes(result))

    def test_missing_docker_is_failure_not_a_healthy_report(self):
        with self.inspection(),patch.object(pre,'docker',side_effect=OSError('private diagnostic')):result=self.check()
        self.assertIn('inspection_unavailable',self.codes(result));self.assertEqual(result['status'],'failed')
        self.assertNotIn('private diagnostic',json.dumps(result))

    def test_pending_work_and_unhealthy_monitor_block_active_gate(self):
        health={'status':'critical','metrics':{'network_due':0,'billing_due':1,'reminder_due':0}}
        with self.inspection(health=health):result=self.check()
        self.assertTrue({'health_not_ok','work_not_drained'}<=self.codes(result))

    def test_corrupt_backup_is_not_replaced_or_repaired(self):
        raw=bytearray(self.archive.read_bytes());raw[-1]^=1;self.archive.write_bytes(raw)
        with self.inspection():result=self.check()
        self.assertIn('backup_unverified',self.codes(result));self.assertEqual(self.archive.read_bytes(),bytes(raw))

    def test_recent_job_timestamp_does_not_hide_old_authenticated_archive(self):
        old=self.directory/'backups/old.tnbk'
        with patch('core.backup_archive.datetime') as clock:
            clock.now.return_value=datetime.now(timezone.utc)-timedelta(days=3)
            encrypt_stream(io.BytesIO(b'PGDMPfixture'),old,self.key,'postgresql')
        ops.write_json(self.directory/'ops/backup.json',{'last_success_at':self.now,'last_archive':str(old),'failed':False})
        with self.inspection():result=self.check()
        self.assertIn('backup_unverified',self.codes(result))

    def test_recovery_copy_must_match_latest_backup_for_active_gate(self):
        another=self.directory/'backups/new.tnbk';encrypt_stream(io.BytesIO(b'PGDMPnew'),another,self.key,'postgresql')
        ops.write_json(self.directory/'ops/backup.json',{'last_success_at':self.now,'last_archive':str(another),'failed':False})
        with self.inspection():result=self.check()
        self.assertIn('recovery_backup_mismatch',self.codes(result))

    def test_invalid_recovery_package_cannot_pass(self):
        (self.package/'manifest.json').unlink()
        with self.inspection():result=self.check()
        self.assertIn('recovery_package_unverified',self.codes(result))

    def test_prepares_a_verified_pair_and_reuses_it_on_repeated_active_checks(self):
        roots = self.custody_roots()
        backup_record = self.directory / 'ops/backup.json'
        before = backup_record.read_bytes()
        with self.inspection():
            first = self.prepare(roots)
            second = self.prepare(roots)
        self.assertEqual([first['status'], second['status']], ['passed', 'passed'])
        self.assertEqual(first['evidence']['custody_preparation'], 'created')
        self.assertEqual(second['evidence']['custody_preparation'], 'reused')
        self.assertEqual(first['evidence']['recovery_package_id'], second['evidence']['recovery_package_id'])
        self.assertEqual(len(list(roots[0].iterdir())), 1)
        self.assertEqual(backup_record.read_bytes(), before)

    def test_custody_preparation_is_skipped_if_active_checks_fail(self):
        roots = self.custody_roots()
        health = {'status': 'ok', 'metrics': {'network_due': 2, 'billing_due': 0, 'reminder_due': 0}}
        with self.inspection(health=health), patch.object(pre, 'ensure_pair') as prepare:
            result = self.prepare(roots)
        prepare.assert_not_called()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.codes(result), {'work_not_drained'})
        self.assertEqual(result['evidence']['custody_preparation'], 'skipped')

    def test_custody_preparation_failure_is_a_release_issue_without_diagnostics(self):
        roots = self.custody_roots()
        with self.inspection(), patch.object(pre, 'ensure_pair', side_effect=OSError('private fixture')):
            result = self.prepare(roots)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.codes(result), {'custody_preparation_failed'})
        self.assertEqual(result['evidence']['custody_preparation'], 'failed')
        self.assertNotIn('private fixture', json.dumps(result))

    def test_active_gate_refuses_backup_metadata_changes_during_package_verification(self):
        verify = pre.verify_package

        def rotate(package, keys):
            metadata = verify(package, keys)
            record = self.directory / 'ops/backup.json'
            ops.write_json(record, {**ops.read_json(record), 'last_archive': str(self.archive.with_name('later.tnbk'))})
            return metadata

        with self.inspection(), patch.object(pre, 'verify_package', side_effect=rotate):
            result = self.check()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.codes(result), {'backup_state_changed'})

    def test_active_gate_refuses_archive_replacement_during_package_verification(self):
        verify = pre.verify_package

        def replace(package, keys):
            metadata = verify(package, keys)
            replacement = self.archive.with_name('copy.tnbk')
            replacement.write_bytes(self.archive.read_bytes())
            replacement.chmod(0o600)
            replacement.replace(self.archive)
            return metadata

        with self.inspection(), patch.object(pre, 'verify_package', side_effect=replace):
            result = self.check()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.codes(result), {'backup_state_changed'})

    def test_custody_cli_rejects_ambiguous_inputs_and_reports_inside_custody(self):
        roots = self.custody_roots()
        output = self.root / 'report.json'
        prepare_args = ['--prepare-custody', *map(str, roots)]
        explicit = ['--package-dir', str(self.package), '--keys-dir', str(self.keys)]
        for flags, destination in ((prepare_args + explicit, output), (prepare_args + ['--mode', 'recovery'], output),
                                   (['--package-dir', str(self.package)], output),
                                   (prepare_args, roots[1] / 'unsafe.json'), (explicit, self.package / 'unsafe.json')):
            with self.subTest(flags=flags, destination=destination):
                argv = ['preflight', '--image-id', self.image, '--output', str(destination), *flags]
                with (patch.object(pre.sys, 'argv', argv), patch.object(pre, 'check') as check,
                      redirect_stderr(io.StringIO()), self.assertRaises(SystemExit)):
                    pre.main()
                check.assert_not_called()
                self.assertFalse(destination.exists())

    def test_custody_cli_writes_the_combined_private_release_report(self):
        roots = self.custody_roots()
        output = self.root / 'combined.json'
        stdout = io.StringIO()
        argv = ['preflight', '--directory', str(self.directory), '--image-id', self.image,
                '--prepare-custody', *map(str, roots), '--output', str(output)]
        with self.inspection(), patch.object(pre.sys, 'argv', argv), redirect_stdout(stdout):
            self.assertEqual(pre.main(), 0)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        report = json.loads(output.read_text())
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['evidence']['custody_preparation'], 'created')
        self.assertEqual(json.loads(stdout.getvalue())['custody_preparation'], 'created')

    def test_failed_restore_never_contacts_docker(self):
        self.save_state(restore_state='failed')
        with patch.object(pre,'compose') as compose:result=self.check()
        compose.assert_not_called();self.assertEqual(result['status'],'failed')

    def test_existing_report_is_refused_before_running_check(self):
        output=self.root/'report.json';output.write_text('retained')
        argv=['staging_preflight','--image-id',self.image,'--package-dir',str(self.package),'--keys-dir',str(self.keys),'--output',str(output)]
        with patch.object(pre.sys,'argv',argv),patch.object(pre,'check') as check,redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):pre.main()
        check.assert_not_called();self.assertEqual(output.read_text(),'retained')

    def test_unpinned_or_differently_selected_image_cannot_pass(self):
        self.state.pop('image_id');self.save_state()
        with self.inspection():result=self.check()
        self.assertIn('image_not_pinned',self.codes(result))
        self.save_state(image_id='sha256:'+'e'*64)
        with self.inspection():result=self.check()
        self.assertIn('image_selection_mismatch',self.codes(result))

    def test_pin_changes_only_manifest_image_without_starting_service(self):
        self.save_state(recovery_pending=True)
        with patch('scripts.staging_lab.docker',return_value=SimpleNamespace(stdout=self.image)),patch('scripts.staging_lab.compose') as run:
            pin_image(self.directory,self.image)
        run.assert_not_called()
        data=json.loads((self.directory/'manifest.json').read_text())
        self.assertEqual(data,{**self.state,'image_id':self.image})

    def test_pin_rejects_mutable_tag_before_contacting_docker(self):
        with patch('scripts.staging_lab.docker') as docker,self.assertRaises(ValueError):pin_image(self.directory,'tagihnet-staging:local')
        docker.assert_not_called()

    def test_missing_image_leaves_selection_unchanged(self):
        before=(self.directory/'manifest.json').read_bytes()
        with patch('scripts.staging_lab.docker',side_effect=OSError),self.assertRaises(OSError):pin_image(self.directory,self.image)
        self.assertEqual((self.directory/'manifest.json').read_bytes(),before)

    def test_pin_respects_restore_lock_and_failed_state(self):
        with operation_lock(self.directory),patch('scripts.staging_lab.docker') as docker,self.assertRaises(RuntimeError):pin_image(self.directory,self.image)
        docker.assert_not_called()
        self.save_state(restore_state='failed')
        with patch('scripts.staging_lab.docker') as docker,self.assertRaises(RuntimeError):pin_image(self.directory,self.image)
        docker.assert_not_called()

    def test_compose_injects_manifest_choice_and_explicit_legacy_fallback(self):
        with patch('scripts.staging_lab.docker') as docker:compose(self.directory,'ps')
        self.assertEqual(docker.call_args.kwargs['stage_image'],self.image)
        self.state.pop('image_id');self.save_state()
        with patch('scripts.staging_lab.docker') as docker:compose(self.directory,'ps')
        self.assertEqual(docker.call_args.kwargs['stage_image'],'tagihnet-staging:local')

    def test_compose_cannot_start_or_pull_missing_pinned_image(self):
        with patch('scripts.staging_lab.docker',side_effect=OSError) as docker,self.assertRaises(OSError):compose(self.directory,'up','--detach')
        self.assertEqual(docker.call_count,1);self.assertEqual(docker.call_args.args[:2],('image','inspect'))

    def test_unavailable_local_image_cannot_pass_even_with_running_services(self):
        def docker(*args,**kwargs):
            if args[0]=='image':raise OSError('missing')
            return SimpleNamespace(stdout=json.dumps([self.object(name) for name in ('db',*pre.APP_SERVICES)]))
        with self.inspection(),patch.object(pre,'docker',side_effect=docker):result=self.check()
        self.assertIn('image_unavailable',self.codes(result))
