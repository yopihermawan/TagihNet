"""Bounded HTTPS load on a newly created disposable staging stack."""
import argparse
import http.client
import json
import math
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
from core.backup_archive import atomic_output
from scripts.staging_lab import PG_IMAGE, PREPARE_IMAGE, compose, docker, manifest, pin_image, prepare
from scripts import staging_ops as ops, staging_portal_load as portal
from scripts.ops_smoke import wait_ok
from verify_staging import verify
from verify_staging_backup import shell

MAX_BODY=2*1024*1024
CANARY=b'SECOND-TENANT-CANARY'
LABELS=('dashboard','customers','invoices','profile','report','pdf')
LOAD_IMAGE = 'tagihnet-staging:local'
IMAGE_ID = re.compile(r'sha256:[a-f0-9]{64}')

FIXTURE='''
import json,uuid
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.db import connection,transaction
from django.utils import timezone
from core.models import Tenant,Plan,Customer,Invoice,InvoiceLine,Payment,PaymentReversal
from core.billing import invoice_snapshot,write_snapshot_invoice
actor=get_user_model().objects.get(username='staging-owner')
tenant=actor.membership.tenant
assert not Customer.objects.exists() and not Invoice.objects.exists()
today=timezone.localdate();current=today.replace(day=1)
last=(current-timedelta(days=1)).replace(day=1)
previous=(last-timedelta(days=1)).replace(day=1)
with transaction.atomic():
    # This fixture's documented amounts/calendar must not depend on HTTPS setup settings.
    tenant.tax_bp=0;tenant.due_day=20;tenant.save(update_fields=['tax_bp','due_day'])
    plan=Plan.objects.create(tenant=tenant,name='Paket Uji Beban',speed_mbps=30,price=225000)
    customers=Customer.objects.bulk_create([Customer(tenant=tenant,plan=plan,code=f'LOAD-{i:04}',name=f'Pelanggan Uji {i:04}',username=f'load-{i}',phone=f'081299{i:06}',address='Alamat fiktif uji beban',area='Load') for i in range(1,501)])
    invoices=[]
    for customer in customers:
        for period in (previous,last):
            values=invoice_snapshot(tenant,customer,period)['invoice'].copy()
            invoices.append(Invoice(tenant=tenant,customer=customer,period=period,**values))
    invoices=Invoice.objects.bulk_create(invoices)
    InvoiceLine.objects.bulk_create([InvoiceLine(invoice=i,description='Fixture uji beban',quantity=1,unit_price=i.total) for i in invoices])
    payments=Payment.objects.bulk_create([Payment(tenant=tenant,invoice=i,amount=100000,event_key=uuid.uuid4(),recorded_by=actor,note='Fixture uji beban') for i in invoices[::4]])
    PaymentReversal.objects.bulk_create([PaymentReversal(payment=p,reason='Koreksi fixture uji beban',recorded_by=actor) for p in payments[::10]])
    other=Tenant.objects.create(name='SECOND-TENANT-CANARY')
    op=Plan.objects.create(tenant=other,name='SECOND-TENANT-CANARY',speed_mbps=10,price=100000)
    oc=Customer.objects.create(tenant=other,plan=op,code='SECOND-TENANT-CANARY',name='SECOND-TENANT-CANARY',username='second',phone='081299999999',address='Fiktif',area='Other')
    oi=write_snapshot_invoice(other,oc,current,invoice_snapshot(other,oc,current),actor)
# Fixed warm-database profile: fixture insertion precedes planner statistics collection.
with connection.cursor() as cursor:
    for table in ('core_customer','core_invoice','core_payment','core_paymentreversal','core_invoiceline'):
        cursor.execute('ANALYZE '+table)
print('LOAD:'+json.dumps({'customer':customers[0].pk,'invoice':invoices[0].pk,'other_customer':oc.pk,'other_invoice':oi.pk,'today':today.isoformat(),'start':current.isoformat()}))
'''

START_BATCH='''
import json,time
from django.contrib.auth import get_user_model
from django.utils import timezone
from core.bulk_billing import create_preview,confirm_batch
actor=get_user_model().objects.get(username='staging-owner')
batch=create_preview(actor,timezone.localdate().replace(day=1),{'state':'active','area':'Load'})
batch=confirm_batch(actor,batch.pk,time.time(),actor.membership.session_version)
assert batch.status=='queued' and batch.items.count()==500
print('LOAD:'+json.dumps({'batch':str(batch.pk)}))
'''

INTEGRITY='''
import json
from django.db.models import Count,Sum
from django.contrib.auth import get_user_model
from core.models import Customer,Invoice,Payment,PaymentReversal,BillingBatch
from core.bulk_billing import process_batch
t=get_user_model().objects.get(username='staging-owner').membership.tenant
b=BillingBatch.objects.get(tenant=t)
assert b.status=='completed' and b.items.filter(status='created').count()==500
assert process_batch(b.pk,500)==0
assert Customer.objects.filter(tenant=t).count()==500
q=Invoice.objects.filter(tenant=t)
assert q.count()==1500 and not q.values('customer_id','period').annotate(n=Count('pk')).filter(n__gt=1).exists()
assert Payment.objects.filter(tenant=t).count()==250 and PaymentReversal.objects.filter(payment__tenant=t).count()==25
total=q.aggregate(n=Sum('total'))['n']
valid_paid=Payment.objects.filter(tenant=t,reversal__isnull=True).aggregate(n=Sum('amount'))['n']
assert total==337500000 and valid_paid==22500000 and total-valid_paid==315000000
assert not Customer.objects.filter(tenant__name='SECOND-TENANT-CANARY').exclude(code='SECOND-TENANT-CANARY').exists()
assert Invoice.objects.filter(tenant__name='SECOND-TENANT-CANARY').count()==1
assert not Payment.objects.filter(tenant__name='SECOND-TENANT-CANARY').exists()
print('LOAD:'+json.dumps({'customers':500,'invoices':1500,'payments':250,'reversals':25,'created_by_worker':500,'batch_replay_created':0,'receivable':total-valid_paid}))
'''


def fixture(directory,code):
    result=shell(directory,code)
    return json.loads(next(line.removeprefix('LOAD:') for line in result.splitlines() if line.startswith('LOAD:')))


def percentile(values,percent):
    if not values:return None
    return sorted(values)[max(0,math.ceil(len(values)*percent/100)-1)]


def summary(samples,elapsed):
    durations=[row['ms'] for row in samples]
    return {'attempts':len(samples),'failures':sum(row['error'] is not None for row in samples),
            'errors':dict(Counter(row['error'] for row in samples if row['error'])),
            'requests_per_second':round(len(samples)/elapsed,2) if elapsed>0 else None,
            **{f'p{p}_ms':round(percentile(durations,p),2) if durations else None for p in (50,95,99)},
            'max_ms':round(max(durations),2) if durations else None}


def response_problem(status,content_type,body,marker,*,expected_status=200,forbidden=()):
    if status!=expected_status:return 'http_status'
    if len(body)>MAX_BODY:return 'response_too_large'
    if CANARY in body or any(value in body for value in forbidden):return 'tenant_canary'
    markers = (marker,) if isinstance(marker, bytes) else marker
    expected='application/pdf' if b'%PDF-' in markers else 'text/html'
    if expected not in content_type or any(value not in body for value in markers):return 'unexpected_body'
    return None


class Client:
    def __init__(self,port,context,cookies,*,forbidden=()):
        self.port=port;self.context=context;self.cookies=dict(cookies)
        self.forbidden = forbidden

    def request(self,path,marker,*,expected_status=200):
        started=time.perf_counter();connection=None;error=None
        try:
            connection=http.client.HTTPSConnection('localhost',self.port,context=self.context,timeout=10)
            connection.request('GET',path,headers={'Cookie':'; '.join(k+'='+v for k,v in self.cookies.items())})
            response=connection.getresponse();body=response.read(MAX_BODY+1)
            error=response_problem(response.status,response.getheader('Content-Type',''),body,marker,
                                   expected_status=expected_status,forbidden=self.forbidden)
            for name,value in response.getheaders():
                if name.lower()=='set-cookie':
                    updated=SimpleCookie();updated.load(value)
                    self.cookies.update({key:item.value for key,item in updated.items()})
        except (OSError,http.client.HTTPException,ValueError):error='transport'
        finally:
            if connection:connection.close()
        return {'ms':(time.perf_counter()-started)*1000,'error':error}


def run_stage(sessions,seconds):
    ready=threading.Barrier(len(sessions)+1);started=[0.0]
    def run(number,client,endpoints):
        ready.wait();rows=[];index=number
        while time.perf_counter()<started[0]+seconds:
            label,path,marker=endpoints[index%len(endpoints)]
            rows.append({'label':label,**client.request(path,marker)});index+=1
            if len(rows)>=10000:break
        return rows
    with ThreadPoolExecutor(max_workers=len(sessions)) as pool:
        futures=[pool.submit(run,n,client,endpoints) for n,(client,endpoints) in enumerate(sessions)]
        started[0]=time.perf_counter();ready.wait()
        samples=[row for future in futures for row in future.result()]
    elapsed=time.perf_counter()-started[0]
    return {'concurrency':len(sessions),'duration_seconds':round(elapsed,3),'overall':summary(samples,elapsed),
            'endpoints':{name:summary([r for r in samples if r['label']==name],elapsed) for name,_,_ in sessions[0][1]}}


def portal_sessions(port, context, data, report):
    accounts = data['accounts']
    report['scope_checks'] = {'foreign_documents': 0, 'invalid_months': 0}
    sessions = []
    for index, account in enumerate(accounts):
        cookies = portal.login(port, context, account)
        unrelated = [account['peer'], accounts[1 - index]['own']]
        forbidden = tuple(text.encode() for ref in unrelated for text in (ref['number'], ref['receipt']))
        client = Client(port, context, cookies, forbidden=forbidden)
        for ref in unrelated:
            for kind in ('invoice', 'invoice_pdf', 'receipt', 'receipt_pdf'):
                if client.request(ref[kind], (), expected_status=404)['error']:
                    raise AssertionError('Portal document permission check failed')
                report['scope_checks']['foreign_documents'] += 1
        for path in ('/portal/', '/portal/payments/'):
            invalid = client.request(path + '?month=2026-13', b'Gunakan bulan yang valid', expected_status=400)
            if invalid['error']:
                raise AssertionError('Portal month validation failed')
            report['scope_checks']['invalid_months'] += 1
        sessions.append((client.cookies, portal.routes(account, data['month']), forbidden))
    return sessions


def resource_bytes(text):
    units={'B':1,'KiB':1024,'MiB':1024**2,'GiB':1024**3,'kB':1000,'MB':1000**2,'GB':1000**3}
    for unit,factor in sorted(units.items(),key=lambda p:-len(p[0])):
        if text.strip().endswith(unit):
            value=float(text.strip()[:-len(unit)])*factor
            if math.isfinite(value) and value>=0:return round(value)
    raise ValueError('Invalid resource value')


def containers(directory):
    ids=compose(directory,'ps','--quiet','web','db',*ops.WORKERS,capture=True,timeout=15).stdout.split()
    info=json.loads(docker('inspect',*ids,capture=True,timeout=15).stdout)
    project=manifest(directory)['project']
    if len(info)!=5 or any(o['Config']['Labels']['com.docker.compose.project']!=project for o in info):raise ValueError('Container identity')
    return {o['Config']['Labels']['com.docker.compose.service']:o for o in info}


class Monitor:
    def __init__(self,directory,image_id):
        self.directory=directory;self.stop=threading.Event();self.samples=[]
        self.identities=containers(directory)
        if any(self.identities[name]['Image'] != image_id for name in ('web', *ops.WORKERS)):
            raise ValueError('Load container image differs from the pinned image')
        self.thread=threading.Thread(target=self.run,daemon=True)

    def run(self):
        while not self.stop.is_set():
            try:
                report=ops.check(self.directory)
                ids={o['Id'][:12]:name for name,o in self.identities.items()}
                raw=docker('stats','--no-stream','--format','{{json .}}',*ids,capture=True,timeout=15).stdout
                resources={}
                for line in raw.splitlines():
                    row=json.loads(line);used,limit=row['MemUsage'].split('/')
                    cpu=float(row['CPUPerc'].rstrip('%'))
                    if not math.isfinite(cpu) or cpu<0:raise ValueError('Invalid CPU')
                    resources[ids[row['ID']]]={'cpu_percent':cpu,'memory_bytes':resource_bytes(used),'limit_bytes':resource_bytes(limit)}
                if len(resources)!=5:raise ValueError('Missing resources')
                self.samples.append({'at':time.time(),'status':report['status'],'issues':report['issues'],'metrics':report['metrics'],'resources':resources})
            except Exception:self.samples.append({'at':time.time(),'status':'unknown','issues':[{'code':'sampler_unavailable'}],'metrics':None,'resources':None})
            self.stop.wait(5)

    def finish(self):
        self.stop.set();self.thread.join(timeout=60)
        if self.thread.is_alive():raise RuntimeError('Sampler did not finish')
        after=containers(self.directory)
        return {'samples':self.samples,'container_changes':{name:{'restart_delta':after[name]['RestartCount']-obj['RestartCount'],
                'same_id':after[name]['Id']==obj['Id'],'oom_killed':after[name]['State']['OOMKilled']} for name,obj in self.identities.items()},
                'limits':{name:{'cpu_count':obj['HostConfig']['NanoCpus']/1e9,'memory_bytes':obj['HostConfig']['Memory']} for name,obj in self.identities.items()}}


def problems(report,p95_limit):
    errors=[]
    for stage in report['stages']:
        for label,stats in stage['endpoints'].items():
            if stats['attempts']<3:errors.append('insufficient_samples')
            if stats['failures']:errors.append('requests_failed')
            if stats['p95_ms'] is None or stats['p95_ms']>p95_limit:errors.append('latency_budget')
    monitoring=report['monitoring']
    if not monitoring['samples'] or any(s['status']!='ok' for s in monitoring['samples']):errors.append('monitor_not_healthy')
    if any(c['restart_delta'] or not c['same_id'] or c['oom_killed'] for c in monitoring['container_changes'].values()):errors.append('container_changed')
    return sorted(set(errors))


def validate_options(seconds,levels,p95,*,min_free_mib=None,image_id=None):
    if type(seconds) is not int or not 5<=seconds<=60:raise ValueError('Duration5–60s')
    if not 1<=len(levels)<=4 or levels!=sorted(set(levels)) or any(type(n) is not int or not 1<=n<=16 for n in levels):raise ValueError('Concurrency1–16, unique/sorted, max4levels')
    if type(p95) is not int or not 100<=p95<=10000:raise ValueError('P95 budget100–10000ms')
    if min_free_mib is not None and (type(min_free_mib) is not int or min_free_mib < 1):
        raise ValueError('Minimum ruang kosong harus bilangan bulat positif dalam MiB.')
    if image_id is not None and (not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id)):
        raise ValueError('Gunakan image ID sha256 lengkap.')


def available_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        return listener.getsockname()[1]


def resolve_image(image_id):
    found = docker('image', 'inspect', image_id or LOAD_IMAGE, '--format', '{{.Id}}',
                   capture=True, timeout=15).stdout.strip()
    if not IMAGE_ID.fullmatch(found) or (image_id is not None and found != image_id):
        raise ValueError('ID image yang ditemukan tidak sesuai.')
    return found


def capacity_report(min_free_mib, image_id):
    minimum = min_free_mib * 1024**2 if min_free_mib is not None else None
    report = {'status': 'unknown', 'issue': 'docker_filesystem_unavailable', 'driver': None,
              'docker_filesystem': None, 'minimum_free_bytes': minimum}
    if image_id is None:
        return {**report, 'issue': 'load_image_unavailable'}
    try:
        driver, capacity = ops.docker_filesystem()
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return report
    report.update(driver=driver, docker_filesystem=capacity)
    if capacity is None:
        return report
    available = capacity['available_bytes']
    if available == 0 or capacity['available_inodes'] == 0:
        return {**report, 'status': 'critical', 'issue': 'filesystem_exhausted'}
    if minimum is not None and available < minimum:
        return {**report, 'status': 'warning', 'issue': 'filesystem_below_minimum'}

    if driver == 'vfs':
        try:
            images = docker('image', 'inspect', PREPARE_IMAGE, PG_IMAGE, image_id, '--format', '{{.Size}}',
                            capture=True, timeout=15)
            prepare_bytes, pg_bytes, app_bytes = map(int, images.stdout.split())
            if min(prepare_bytes, pg_bytes, app_bytes) <= 0:
                raise ValueError('Invalid image sizes')
        except (OSError, subprocess.SubprocessError, TypeError, ValueError):
            return {**report, 'issue': 'load_image_estimate_unavailable'}
        # Web, migrate and a transient bootstrap container in addition to the workers.
        app_copies = len(ops.WORKERS) + 3
        estimate = prepare_bytes + pg_bytes + app_copies * app_bytes
        report.update(vfs_app_copies=app_copies, vfs_image_estimate_bytes=estimate,
                      image_sizes_bytes={'prepare': prepare_bytes, 'postgres': pg_bytes, 'application': app_bytes})
        if estimate >= available:
            return {**report, 'status': 'warning', 'issue': 'load_image_estimate_no_headroom'}
    return {**report, 'status': 'ok', 'issue': None}


def probe(output,seconds=20,levels=(1,4,8),p95=2000,*,min_free_mib=None,capacity_only=False,image_id=None,profile='operator'):
    levels = list(levels)
    validate_options(seconds, levels, p95, min_free_mib=min_free_mib, image_id=image_id)
    if profile not in ('operator', 'portal'):
        raise ValueError('Profil uji harus operator atau portal.')
    is_portal = profile == 'portal'
    output=Path(output)
    if os.path.lexists(output):raise ValueError('Report already exists; choose a new file')
    report={'version':1,'started_at':datetime.now(timezone.utc).isoformat(),'profile':'local-https-closed-loop',
            'seconds_per_stage':seconds,'levels':levels,'p95_limit_ms':p95,'fixture':{'customers':500,'initial_invoices':1000,'payments':250,'reversals':25,'other_tenants':1},
            'stages':[],'status':'failed','failures':[],'cleanup':'pending'}
    if is_portal:
        report.update(profile='portal-https-closed-loop', fixture=portal.FIXTURE_COUNTS.copy())
    try:
        selected_image = resolve_image(image_id)
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        selected_image = None
    report['image_reference'] = image_id or LOAD_IMAGE
    report['image_id'] = selected_image
    capacity = capacity_report(min_free_mib, selected_image)
    report['capacity'] = capacity
    refused = (selected_image is None or capacity['status'] in {'warning', 'critical'}
               or (capacity['status'] == 'unknown' and min_free_mib is not None))
    if capacity_only or refused:
        if capacity_only:
            report['profile'] = 'local-docker-capacity'
        report['cleanup'] = 'not_needed'
        if capacity['status'] == 'ok':
            report['status'] = 'passed'
        else:
            report['failures'].append(capacity['issue'])
        with atomic_output(output) as stream:
            stream.write(json.dumps(report, indent=2, sort_keys=True).encode())
        return report
    monitor=None
    with tempfile.TemporaryDirectory(prefix='tagihnet-load-') as folder:
        directory=Path(folder)/'stack'
        port=available_port()
        prepare(directory,port)
        cleanup_needed = False
        phase = 'image_pin'
        try:
            pin_image(directory, selected_image)
            cleanup_needed = True
            phase = 'staging_verification'
            identity=verify(directory)
            phase = 'fixture'
            data=fixture(directory,portal.FIXTURE if is_portal else FIXTURE)
            phase = 'scope_and_warmup'
            context=ssl.create_default_context(cafile=str(directory/'secrets/ca.crt'))
            if is_portal:
                sessions = portal_sessions(port, context, data, report)
                baseline = fixture(directory, portal.SNAPSHOT)
            else:
                cookies={cookie.name:cookie.value for cookie in identity.cookies}
                endpoints=[('dashboard','/',b'Ringkasan billing'),('customers','/customers/?size=50&due=unpaid',b'LOAD-'),
                    ('invoices','/invoices/?state=overdue&size=50',b'Faktur'),('profile',f"/customers/{data['customer']}/?tab=invoices",b'LOAD-0001'),
                    ('report',f"/reports/billing/?mode=receivables&start={data['start']}&end={data['today']}",b'Rekonsiliasi piutang'),
                    ('pdf',f"/invoices/{data['invoice']}/pdf/",b'%PDF-')]
                client = Client(port, context, cookies)
                for path in (f"/customers/{data['other_customer']}/", f"/invoices/{data['other_invoice']}/pdf/"):
                    if client.request(path, (), expected_status=404)['error']:
                        raise AssertionError('Operator document permission check failed')
                sessions = [(client.cookies, endpoints, ())]
            report['warmup'] = {}
            for number, (cookies, endpoints, forbidden) in enumerate(sessions, 1):
                client = Client(port, context, cookies, forbidden=forbidden)
                report['warmup'].update({f'{number}:{label}' if is_portal else label: client.request(path, marker)
                                         for label, path, marker in endpoints})
            phase = 'monitor_start'
            wait_ok(directory)
            monitor=Monitor(directory, selected_image);monitor.thread.start();batch_started=None
            for level in levels:
                if not is_portal and level==levels[-1]:
                    phase = 'billing_start'
                    fixture(directory,START_BATCH);batch_started=time.monotonic()
                phase = 'load_stage'
                clients = []
                for index in range(level):
                    cookies, endpoints, forbidden = sessions[index % len(sessions)]
                    clients.append((Client(port, context, cookies, forbidden=forbidden), endpoints))
                stage=run_stage(clients,seconds)
                report['stages'].append(stage)
                print('LOAD: '+json.dumps({'concurrency':level,**stage['overall']}),flush=True)
            phase = 'billing_integrity'
            if is_portal:
                unchanged = baseline == fixture(directory, portal.SNAPSHOT)
                report['integrity'] = {'billing_rows_unchanged': unchanged}
                if not unchanged:
                    raise AssertionError('Portal reads changed the billing fixture')
            else:
                phase = 'billing_completion'
                deadline=time.monotonic()+120
                while time.monotonic()<deadline:
                    health=ops.check(directory)
                    if health['metrics'] is not None and health['metrics']['billing_due']==0:break
                    time.sleep(1)
                else:raise RuntimeError('Billing completion timeout')
                phase = 'billing_integrity'
                report['integrity']=fixture(directory,INTEGRITY)
                report['batch_seconds_to_observation']=round(time.monotonic()-batch_started,2)
            phase = 'monitor_finish'
            wait_ok(directory)
            report['monitoring']=monitor.finish();monitor=None
            report['failures']=problems(report,p95)
            if any(row['error'] for row in report['warmup'].values()):report['failures'].append('warmup_failed')
            report['status']='passed' if not report['failures'] else 'failed'
        except Exception as error:
            report['failed_phase'] = phase
            report['failures'].append('probe_'+type(error).__name__ if cleanup_needed else 'load_image_pin_failed')
        finally:
            try:
                if monitor:report['monitoring']=monitor.finish()
            except Exception:
                report['status']='failed';report['failures'].append('sampler_finish_failed')
            finally:
                if cleanup_needed:
                    try:compose(directory,'down','--volumes','--remove-orphans',capture=True,timeout=60);report['cleanup']='passed'
                    except Exception:report['cleanup']='failed';report['status']='failed';report['failures'].append('cleanup_failed')
                else:
                    report['cleanup'] = 'not_needed'
            with atomic_output(output) as stream:stream.write(json.dumps(report,indent=2,sort_keys=True).encode())
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'data/evidence'/('staging-load-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'.json'))
    parser.add_argument('--seconds',type=int,default=20)
    parser.add_argument('--levels',type=int,nargs='+',default=[1,4,8])
    parser.add_argument('--p95-ms',type=int,default=2000)
    parser.add_argument('--min-free-mib', type=int, help='Ambang tambahan ruang kosong Docker dalam MiB.')
    parser.add_argument('--capacity-only', action='store_true', help='Ukur ruang Docker tanpa membuat proyek uji.')
    parser.add_argument('--image-id', help='Gunakan ID image lokal sha256 lengkap untuk uji.')
    parser.add_argument('--profile', choices=('operator', 'portal'), default='operator')
    args=parser.parse_args()
    try:
        result=probe(args.output,args.seconds,args.levels,args.p95_ms,
                     min_free_mib=args.min_free_mib,capacity_only=args.capacity_only,image_id=args.image_id,profile=args.profile)
        print('LOAD RESULT: '+json.dumps({'profile':result['profile'],'status':result['status'],
            'image_id':result['image_id'],'capacity_status':result['capacity']['status'],'failures':result['failures'],
            'cleanup':result['cleanup'],'output':str(args.output)}))
        return 0 if result['status']=='passed' else 1
    except Exception as error:parser.exit(1,'Load probe refused: '+type(error).__name__+'; no credentials/diagnostics printed.\n')


if __name__=='__main__':raise SystemExit(main())
