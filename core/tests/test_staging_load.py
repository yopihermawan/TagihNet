import json
import tempfile
from pathlib import Path
from unittest.mock import Mock,patch
from django.test import SimpleTestCase
from scripts import staging_load as load


class LoadProbeTests(SimpleTestCase):
    def test_percentiles_use_nearest_rank_and_empty_is_unavailable(self):
        values=list(range(100,0,-1))
        self.assertEqual([load.percentile(values,n) for n in (50,95,99)],[50,95,99])
        self.assertIsNone(load.percentile([],95))

    def test_summary_counts_failures_and_includes_failed_latency(self):
        result=load.summary([{'ms':10,'error':None},{'ms':3000,'error':'transport'}],2)
        self.assertEqual(result['failures'],1);self.assertEqual(result['p95_ms'],3000)
        self.assertEqual(result['requests_per_second'],1)
        self.assertEqual(result['errors'],{'transport':1})

    def test_missing_samples_cannot_report_zero_latency(self):
        result=load.summary([],1)
        self.assertIsNone(result['p95_ms']);self.assertIsNone(result['max_ms'])

    def test_login_redirect_and_login_html_are_failures(self):
        self.assertEqual(load.response_problem(302,'text/html',b'Login',b'Faktur'),'http_status')
        self.assertEqual(load.response_problem(200,'text/html',b'Login',b'Faktur'),'unexpected_body')

    def test_wrong_type_and_oversize_body_are_failures(self):
        self.assertEqual(load.response_problem(200,'text/html',b'%PDF-',b'%PDF-'),'unexpected_body')
        self.assertEqual(load.response_problem(200,'text/html',b'x'*(load.MAX_BODY+1),b'x'),'response_too_large')

    def test_other_tenant_canary_rejects_even_successful_page(self):
        self.assertEqual(load.response_problem(200,'text/html',b'Faktur '+load.CANARY,b'Faktur'),'tenant_canary')

    def test_tls_context_is_passed_to_fixed_local_target(self):
        response=Mock();response.status=200;response.getheader.return_value='text/html';response.read.return_value=b'Faktur'
        response.getheaders.return_value=[('Set-Cookie','sessionid=new; Secure; HttpOnly')]
        connection=Mock();connection.getresponse.return_value=response;context=object()
        with patch.object(load.http.client,'HTTPSConnection',return_value=connection) as factory:
            client=load.Client(18443,context,{'sessionid':'private'})
            row=client.request('/invoices/',b'Faktur')
        factory.assert_called_once_with('localhost',18443,context=context,timeout=10)
        self.assertIsNone(row['error']);self.assertEqual(client.cookies['sessionid'],'new')
        self.assertNotIn('private',json.dumps(row));connection.close.assert_called_once()

    def test_transport_diagnostic_is_not_in_report(self):
        with patch.object(load.http.client,'HTTPSConnection',side_effect=OSError('secret body')):
            result=load.Client(18443,object(),{}).request('/',b'Ringkasan')
        self.assertEqual(result['error'],'transport');self.assertNotIn('secret body',json.dumps(result))

    def test_duration_and_concurrency_are_bounded_before_docker(self):
        invalid=[(0,[1],2000),(61,[1],2000),(20,[],2000),(20,[17],2000),(20,[4,1],2000),(20,[1,1],2000),(20,[1,2,3,4,5],2000),(20,[1],0)]
        with patch.object(load,'prepare') as prepare:
            for seconds,levels,p95 in invalid:
                with self.subTest(seconds=seconds,levels=levels),self.assertRaises(ValueError):load.probe('/tmp/never-created-load-report',seconds,levels,p95)
        prepare.assert_not_called()

    def test_existing_report_and_broken_symlink_refuse_before_docker(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(load,'prepare') as prepare:
            path=Path(folder)/'report';path.write_text('retained')
            with self.assertRaises(ValueError):load.probe(path)
            self.assertEqual(path.read_text(),'retained')
            link=Path(folder)/'link';link.symlink_to(Path(folder)/'missing')
            with self.assertRaises(ValueError):load.probe(link)
        prepare.assert_not_called()

    def test_resource_units_and_invalid_values(self):
        self.assertEqual(load.resource_bytes('1.5MiB'),1572864)
        self.assertEqual(load.resource_bytes('512MB'),512000000)
        for value in ('nanMiB','-1B','unknown','infGiB'):
            with self.subTest(value=value),self.assertRaises(ValueError):load.resource_bytes(value)

    def result(self):
        return {'stages':[{'endpoints':{'dashboard':{'attempts':3,'failures':0,'p95_ms':100}}}],
                'monitoring':{'samples':[{'status':'ok'}],'container_changes':{'web':{'restart_delta':0,'same_id':True,'oom_killed':False}}}}

    def test_budget_is_per_endpoint_and_never_relaxes_on_failure(self):
        result=self.result();self.assertEqual(load.problems(result,2000),[])
        result['stages'][0]['endpoints']['dashboard'].update(p95_ms=2500,failures=1,attempts=2)
        self.assertEqual(load.problems(result,2000),['insufficient_samples','latency_budget','requests_failed'])

    def test_unavailable_monitor_and_container_restart_fail_gate(self):
        result=self.result();result['monitoring']['samples']=[{'status':'unknown'}]
        result['monitoring']['container_changes']['web']['restart_delta']=1
        self.assertEqual(load.problems(result,2000),['container_changed','monitor_not_healthy'])

    def test_missing_monitor_is_not_healthy(self):
        result=self.result();result['monitoring']['samples']=[]
        self.assertIn('monitor_not_healthy',load.problems(result,2000))

    def test_fixture_failure_still_writes_private_report_and_cleans_own_stack(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'report.json'
            with patch.object(load,'available_port',return_value=18443),patch.object(load,'prepare'),patch.object(load,'docker',return_value=Mock(stdout='image-id')),patch.object(load,'verify',side_effect=ValueError('private secret')),patch.object(load,'compose') as compose:
                result=load.probe(output)
            self.assertEqual(result['status'],'failed');self.assertEqual(result['cleanup'],'passed')
            self.assertEqual(output.stat().st_mode&0o777,0o600)
            self.assertNotIn('private secret',output.read_text())
            self.assertEqual(compose.call_args.args[1:],('down','--volumes','--remove-orphans'))

    def test_cleanup_failure_is_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'report.json'
            with patch.object(load,'available_port',return_value=18443),patch.object(load,'prepare'),patch.object(load,'docker',return_value=Mock(stdout='image-id')),patch.object(load,'verify',side_effect=ValueError),patch.object(load,'compose',side_effect=OSError):
                result=load.probe(output)
            self.assertEqual(result['cleanup'],'failed');self.assertEqual(result['status'],'failed')
            self.assertIn('cleanup_failed',result['failures'])
