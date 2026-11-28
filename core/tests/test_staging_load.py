import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock,patch
from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from core.models import Membership, Tenant
from scripts import staging_load as load
from scripts import staging_portal_load as portal


TEST_IMAGE = 'sha256:' + 'a' * 64
OTHER_IMAGE = 'sha256:' + 'b' * 64


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

    def test_portal_body_requires_own_markers_and_rejects_foreign_markers(self):
        required = (b'own document', b'correction')
        foreign = (b'foreign document',)
        self.assertIsNone(load.response_problem(200, 'text/html', b'own document correction', required, forbidden=foreign))
        self.assertEqual(load.response_problem(200, 'text/html', b'correction', required), 'unexpected_body')
        self.assertEqual(load.response_problem(200, 'text/html', b'own document correction foreign document',
                                               required, forbidden=foreign), 'tenant_canary')
        self.assertIsNone(load.response_problem(404, 'text/html', b'Not found', (), expected_status=404))

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
        with patch.object(load,'prepare') as prepare, patch.object(load, 'docker') as docker:
            for seconds,levels,p95 in invalid:
                with self.subTest(seconds=seconds,levels=levels),self.assertRaises(ValueError):load.probe('/tmp/never-created-load-report',seconds,levels,p95)
            with self.assertRaises(ValueError):
                load.probe('/tmp/never-created-load-report', image_id='tagihnet-staging:local')
        prepare.assert_not_called()
        docker.assert_not_called()

    def test_existing_report_and_broken_symlink_refuse_before_docker(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(load,'prepare') as prepare:
            path=Path(folder)/'report';path.write_text('retained')
            with self.assertRaises(ValueError):load.probe(path)
            self.assertEqual(path.read_text(),'retained')
            link=Path(folder)/'link';link.symlink_to(Path(folder)/'missing')
            with self.assertRaises(ValueError):load.probe(link)
        prepare.assert_not_called()

    def test_insufficient_vfs_space_refuses_load_before_provisioning(self):
        capacity = load.ops.filesystem_capacity(1, 7000, 900, 70, 20)
        with (tempfile.TemporaryDirectory() as folder,
              patch.object(load.ops, 'docker_filesystem', return_value=('vfs', capacity)),
              patch.object(load, 'docker', side_effect=[Mock(stdout=TEST_IMAGE), Mock(stdout='10\n100\n150\n')]) as docker,
              patch.object(load, 'available_port') as port,
              patch.object(load, 'prepare') as prepare,
              patch.object(load, 'compose') as compose):
            output = Path(folder) / 'report.json'
            result = load.probe(output, image_id=TEST_IMAGE)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual((result['status'], result['cleanup'], result['stages']), ('failed', 'not_needed', []))
        self.assertEqual(result['failures'], ['load_image_estimate_no_headroom'])
        self.assertEqual(result['capacity']['vfs_app_copies'], 6)
        self.assertEqual(result['capacity']['vfs_image_estimate_bytes'], 1010)
        self.assertEqual(result['image_id'], TEST_IMAGE)
        self.assertEqual(docker.call_count, 2)
        self.assertEqual(docker.call_args_list[0].args[:3], ('image', 'inspect', TEST_IMAGE))
        docker.assert_called_with('image', 'inspect', load.PREPARE_IMAGE, load.PG_IMAGE, TEST_IMAGE,
                                  '--format', '{{.Size}}', capture=True, timeout=15)
        port.assert_not_called()
        prepare.assert_not_called()
        compose.assert_not_called()

    def test_capacity_only_never_provisions_then_reports_explicit_minimum(self):
        capacity = load.ops.filesystem_capacity(1024**2, 2000, 1000, 70, 20)
        with (tempfile.TemporaryDirectory() as folder,
              patch.object(load.ops, 'docker_filesystem', return_value=('overlay2', capacity)),
              patch.object(load, 'prepare') as prepare,
              patch.object(load, 'docker', return_value=Mock(stdout=TEST_IMAGE)) as docker):
            result = load.probe(Path(folder) / 'read-only.json', capacity_only=True)
            self.assertEqual((result['profile'], result['status'], result['cleanup']),
                             ('local-docker-capacity', 'passed', 'not_needed'))
            self.assertEqual(result['stages'], [])
            refused = load.probe(Path(folder) / 'min-free.json', min_free_mib=1100)
            self.assertEqual(refused['failures'], ['filesystem_below_minimum'])
        prepare.assert_not_called()
        self.assertEqual(docker.call_count, 2)
        for call in docker.call_args_list:
            self.assertEqual(call.args, ('image', 'inspect', load.LOAD_IMAGE, '--format', '{{.Id}}'))

    def test_requested_capacity_guard_refuses_unknown_storage_without_diagnostics(self):
        with (tempfile.TemporaryDirectory() as folder,
              patch.object(load.ops, 'docker_filesystem', side_effect=OSError('private diagnostic')),
              patch.object(load, 'docker', return_value=Mock(stdout=TEST_IMAGE)),
              patch.object(load, 'prepare') as prepare):
            path = Path(folder) / 'report.json'
            result = load.probe(path, min_free_mib=1536)
            self.assertNotIn('private diagnostic', path.read_text())
        self.assertEqual((result['status'], result['capacity']['status']), ('failed', 'unknown'))
        self.assertEqual(result['failures'], ['docker_filesystem_unavailable'])
        prepare.assert_not_called()

    def test_missing_or_mismatched_image_refuses_before_provisioning(self):
        for error in (OSError('private diagnostic'), Mock(stdout=OTHER_IMAGE)):
            with (self.subTest(response=type(error).__name__), tempfile.TemporaryDirectory() as folder,
                  patch.object(load, 'docker', side_effect=[error]) as docker,
                  patch.object(load.ops, 'docker_filesystem') as filesystem,
                  patch.object(load, 'prepare') as prepare):
                path = Path(folder) / 'report.json'
                report = load.probe(path, image_id=TEST_IMAGE)
                self.assertEqual((report['image_id'], report['cleanup']), (None, 'not_needed'))
                self.assertEqual(report['failures'], ['load_image_unavailable'])
                self.assertNotIn('private diagnostic', path.read_text())
                docker.assert_called_once_with('image', 'inspect', TEST_IMAGE, '--format', '{{.Id}}',
                                               capture=True, timeout=15)
                filesystem.assert_not_called()
                prepare.assert_not_called()

    def test_image_lost_before_pin_never_starts_compose(self):
        with (tempfile.TemporaryDirectory() as folder,
              patch.object(load, 'capacity_report', return_value={'status': 'ok'}),
              patch.object(load, 'docker', return_value=Mock(stdout=TEST_IMAGE)),
              patch.object(load, 'available_port', return_value=18443),
              patch.object(load, 'prepare'),
              patch.object(load, 'pin_image', side_effect=RuntimeError),
              patch.object(load, 'verify') as verify,
              patch.object(load, 'compose') as compose):
            report = load.probe(Path(folder) / 'report.json')
        self.assertEqual(report['failures'], ['load_image_pin_failed'])
        self.assertEqual(report['cleanup'], 'not_needed')
        verify.assert_not_called()
        compose.assert_not_called()

    def test_monitor_refuses_a_worker_with_an_unexpected_image(self):
        containers = {name: {'Image': TEST_IMAGE} for name in ('web', *load.ops.WORKERS)}
        containers['billing-worker']['Image'] = OTHER_IMAGE
        with patch.object(load, 'containers', return_value=containers), self.assertRaises(ValueError):
            load.Monitor(Path('/tmp/load-fixture'), TEST_IMAGE)

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
            with (patch.object(load, 'capacity_report', return_value={'status': 'unknown'}),
                  patch.object(load,'available_port',return_value=18443),patch.object(load,'prepare'),
                  patch.object(load,'docker',return_value=Mock(stdout=TEST_IMAGE)) as docker,
                  patch.object(load, 'pin_image') as pin, patch.object(load,'compose') as compose):
                def verify(directory):
                    pin.assert_called_once_with(directory, TEST_IMAGE)
                    raise ValueError('private secret')

                with patch.object(load, 'verify', side_effect=verify):
                    result=load.probe(output)
            docker.assert_called_once_with('image', 'inspect', load.LOAD_IMAGE, '--format', '{{.Id}}',
                                           capture=True, timeout=15)
            self.assertEqual(result['image_id'], TEST_IMAGE)
            self.assertEqual(result['status'],'failed');self.assertEqual(result['cleanup'],'passed')
            self.assertEqual(result['failed_phase'], 'staging_verification')
            self.assertEqual(output.stat().st_mode&0o777,0o600)
            self.assertNotIn('private secret',output.read_text())
            self.assertEqual(compose.call_args.args[1:],('down','--volumes','--remove-orphans'))

    def test_cleanup_failure_is_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'report.json'
            with (patch.object(load, 'capacity_report', return_value={'status': 'ok'}),
                  patch.object(load,'available_port',return_value=18443),patch.object(load,'prepare'),
                  patch.object(load,'docker',return_value=Mock(stdout=TEST_IMAGE)),patch.object(load,'pin_image'),
                  patch.object(load,'verify',side_effect=ValueError),patch.object(load,'compose',side_effect=OSError)):
                result=load.probe(output)
            self.assertEqual(result['cleanup'],'failed');self.assertEqual(result['status'],'failed')
            self.assertIn('cleanup_failed',result['failures'])


@override_settings(SIMULATION=True)
class PortalLoadFixtureTests(TestCase):
    def fixture(self, code):
        output = io.StringIO()
        with redirect_stdout(output):
            exec(code, {})
        return json.loads(output.getvalue().split('LOAD:', 1)[1])

    def test_disposable_fixture_produces_two_scoped_sessions_and_stable_ledger(self):
        tenant = Tenant.objects.create(name='ISP uji beban')
        owner = get_user_model().objects.create_user(username='staging-owner')
        Membership.objects.create(user=owner, tenant=tenant, role='owner')
        data = self.fixture(portal.FIXTURE)
        baseline = self.fixture(portal.SNAPSHOT)
        self.assertEqual({key: value[0] for key, value in baseline.items()},
                         {'customer': 500, 'invoice': 2000, 'invoiceline': 2000, 'payment': 8500,
                          'paymentreversal': 500})
        for index, account in enumerate(data['accounts']):
            client = Client(enforce_csrf_checks=True)
            self.assertEqual(client.get('/login/', secure=True).status_code, 200)
            response = client.post('/login/', {
                'username': account['username'], 'password': account['password'],
                'csrfmiddlewaretoken': client.cookies['csrftoken'].value,
            }, secure=True, HTTP_ORIGIN='https://testserver')
            self.assertRedirects(response, '/portal/', fetch_redirect_response=False)

            foreign = (account['peer'], data['accounts'][1 - index]['own'])
            forbidden = tuple(item[key].encode() for item in foreign for key in ('number', 'receipt'))
            for label, path, marker in portal.routes(account, data['month']):
                response = client.get(path, secure=True)
                self.assertIsNone(load.response_problem(response.status_code, response['Content-Type'], response.content,
                                                        marker, forbidden=forbidden), label)
            for document in foreign:
                for key in ('invoice', 'invoice_pdf', 'receipt', 'receipt_pdf'):
                    self.assertEqual(client.get(document[key], secure=True).status_code, 404)
            for path in ('/portal/', '/portal/payments/'):
                self.assertContains(client.get(path + '?month=2026-13', secure=True),
                                    'Gunakan bulan yang valid', status_code=400)
        self.assertEqual(self.fixture(portal.SNAPSHOT), baseline)
