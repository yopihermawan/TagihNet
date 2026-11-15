import io
import json
import tempfile
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from scripts import staging_lab as lab
from scripts import verify_staging_rollout as rollout


class StagingRolloutTests(SimpleTestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='tagihnet-rollout-unit-')
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.token='a'*32
        self.reference='tagihnet-rollout-'+self.token+':candidate'
        self.base='tagihnet-rollout-'+self.token+':base'
        self.image='sha256:'+'b'*64
        self.output=self.root/'report.json'

    def image_info(self,token=None):
        return {'Id':self.image,'Config':{'Labels':{rollout.TOKEN_LABEL:token or self.token}}}

    def target(self):
        target=self.root/'target';target.mkdir()
        data={'project':'tagihnet-stage-1234567890','directory':str(target),'port':19443,
            'initialized':False,'restore_state':'restored','recovery_pending':True,'image_id':self.image}
        lab.write_manifest(target,data)
        return target

    def test_up_invalid_timeout_is_refused_before_lock_or_docker(self):
        for timeout in (0,121,True,1.5,'12',None):
            with self.subTest(timeout=timeout),patch.object(lab,'operation_lock') as lock,patch.object(lab,'docker') as docker,self.assertRaises(ValueError):
                lab.up(self.root,timeout=timeout)
            lock.assert_not_called();docker.assert_not_called()

    def test_readiness_timeout_keeps_selected_image_and_recovery_hold(self):
        target=self.target();before=(target/'manifest.json').read_bytes()
        with patch.object(lab,'compose') as compose,patch.object(lab,'wait_ready',side_effect=RuntimeError) as ready,self.assertRaises(RuntimeError):
            lab.up(target,capture=True,timeout=12)
        ready.assert_called_once_with(target,timeout=12)
        compose.assert_called_once_with(target,'up','--detach','--no-build','web',capture=True)
        self.assertEqual((target/'manifest.json').read_bytes(),before)

    def test_default_readiness_budget_and_hold_survive_success(self):
        target=self.target()
        with patch.object(lab,'compose'),patch.object(lab,'wait_ready') as ready:
            lab.up(target)
        ready.assert_called_once_with(target,timeout=120)
        data=lab.manifest(target)
        self.assertTrue(data['initialized']);self.assertTrue(data['recovery_pending'])
        self.assertEqual(data['image_id'],self.image)

    def test_existing_or_dangling_symlink_report_never_contacts_docker(self):
        for symlink in (False,True):
            if symlink:self.output.symlink_to(self.root/'missing')
            else:self.output.write_text('retained')
            with patch.object(rollout,'docker') as docker,self.assertRaises(ValueError):rollout.drill(self.output)
            docker.assert_not_called()
            if not symlink:self.assertEqual(self.output.read_text(),'retained')
            self.output.unlink()

    def test_shared_or_other_run_image_is_never_eligible_for_cleanup(self):
        for reference,token in (('tagihnet-staging:local',self.token),(self.base,self.token),
                (self.image,self.token),(self.reference,'a.*'),(self.reference,'c'*32)):
            with self.subTest(reference=reference,token=token),patch.object(rollout,'docker') as docker,self.assertRaises(ValueError):
                rollout.remove_derivative(reference,token)
            docker.assert_not_called()

    def test_replaced_tag_with_wrong_label_is_preserved(self):
        listed=SimpleNamespace(stdout=self.reference+'\n')
        with patch.object(rollout,'docker',return_value=listed) as docker,patch.object(rollout,'image_info',return_value=self.image_info('c'*32)),self.assertRaises(ValueError):
            rollout.remove_derivative(self.reference,self.token)
        self.assertEqual(docker.call_count,1)

    def test_missing_partial_build_tag_is_noop(self):
        with patch.object(rollout,'docker',return_value=SimpleNamespace(stdout='')) as docker,patch.object(rollout,'owned_image') as owned:
            rollout.remove_derivative(self.reference,self.token)
        self.assertEqual(docker.call_count,1);owned.assert_not_called()

    def test_daemon_failure_is_reported_not_treated_as_missing_tag(self):
        with patch.object(rollout,'docker',side_effect=OSError) as docker,self.assertRaises(OSError):
            rollout.remove_derivative(self.reference,self.token)
        self.assertEqual(docker.call_count,1)

    def test_cleanup_removes_only_owned_tag_without_force(self):
        with patch.object(rollout,'docker',return_value=SimpleNamespace(stdout=self.reference+'\n')) as docker,patch.object(rollout,'image_info',return_value=self.image_info()):
            rollout.remove_derivative(self.reference,self.token)
        self.assertEqual(docker.call_args.args,('image','rm',self.reference))
        self.assertNotIn('--force',docker.call_args.args)

    def test_failed_build_is_cleaned_without_removing_shared_base_id(self):
        with patch.object(rollout,'image_info',return_value=self.image_info()),patch.object(rollout,'docker') as docker,patch.object(rollout,'build_derivative',side_effect=RuntimeError('private diagnostic')),patch.object(rollout,'remove_derivative') as cleanup:
            report=rollout.drill(self.output)
        self.assertEqual(cleanup.call_count,1)
        self.assertTrue(cleanup.call_args.args[0].endswith(':candidate'))
        self.assertEqual(docker.call_args.args[:2],('image','rm'))
        self.assertTrue(docker.call_args.args[2].endswith(':base'))
        self.assertNotIn(self.image,docker.call_args.args)
        self.assertEqual(report['status'],'failed');self.assertEqual(report['cleanup'],'passed')
        self.assertFalse(report['production_ready']);self.assertNotIn('private diagnostic',self.output.read_text())
        self.assertEqual(self.output.stat().st_mode&0o777,0o600)

    def test_cleanup_failure_cannot_produce_passed_report(self):
        with patch.object(rollout,'image_info',return_value=self.image_info()),patch.object(rollout,'docker'),patch.object(rollout,'build_derivative',side_effect=RuntimeError),patch.object(rollout,'remove_derivative',side_effect=OSError('private diagnostic')):
            report=rollout.drill(self.output)
        self.assertEqual(report['status'],'failed');self.assertEqual(report['cleanup'],'failed')
        self.assertIn('image_cleanup',report['failures']);self.assertNotIn('private diagnostic',self.output.read_text())

    def test_local_base_alias_is_preserved_if_identity_changes(self):
        with patch.object(rollout,'image_info',side_effect=[self.image_info(),{'Id':'sha256:'+'c'*64}]),patch.object(rollout,'docker') as docker,patch.object(rollout,'build_derivative',side_effect=RuntimeError),patch.object(rollout,'remove_derivative'):
            report=rollout.drill(self.output)
        self.assertEqual(docker.call_count,1)
        self.assertEqual(report['cleanup'],'failed');self.assertIn('base_alias_cleanup',report['failures'])

    def test_foreign_web_container_is_refused_even_with_expected_image(self):
        info={'Image':self.image,'Config':{'Labels':{'com.docker.compose.project':'another-project','com.docker.compose.service':'web'}}}
        with patch.object(rollout,'compose',return_value=SimpleNamespace(stdout='container-id')),patch.object(rollout,'docker',return_value=SimpleNamespace(stdout=json.dumps([info]))),patch.object(rollout,'manifest',return_value={'project':'owned-project'}),self.assertRaises(ValueError):
            rollout.runtime(self.root,include_stopped=True)

    def test_any_running_worker_breaks_hold_and_missing_flag_is_not_hold(self):
        for flag,services in ((True,'web\ndb\nbilling-worker'),(False,'web\ndb'),(None,'web\ndb')):
            with patch.object(rollout,'manifest',return_value={'recovery_pending':flag}),patch.object(rollout,'compose',return_value=SimpleNamespace(stdout=services)):
                self.assertFalse(rollout.held(self.root))
        with patch.object(rollout,'manifest',return_value={'recovery_pending':True}),patch.object(rollout,'compose',return_value=SimpleNamespace(stdout='web\ndb')):
            self.assertTrue(rollout.held(self.root))

    def test_cli_does_not_publish_exception_diagnostics(self):
        message=io.StringIO()
        with patch.object(rollout.sys,'argv',['rollout','--output',str(self.output)]),patch.object(rollout,'drill',side_effect=RuntimeError('private diagnostic')),redirect_stderr(message),self.assertRaises(SystemExit) as exit:
            rollout.main()
        self.assertEqual(exit.exception.code,1);self.assertNotIn('private diagnostic',message.getvalue())
