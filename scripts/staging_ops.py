"""Private local staging monitoring and opt-in periodic encrypted backups; no external alerts."""
import argparse
import fcntl
import json
import math
import os
import signal
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from cryptography import x509
from core.backup_archive import read_key,verify_archive
from scripts.staging_backup import backup,database_container
from scripts.staging_lab import compose,docker,manifest

WORKERS={'network-worker':'network','billing-worker':'billing','reminder-worker':'reminder'}
LEVELS={'ok':0,'warning':1,'unknown':2,'critical':3}
METRICS_SQL="""BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL lock_timeout = '5s';
SELECT json_build_object(
'network_due',(SELECT count(*) FROM core_networkjob WHERE status IN ('pending','failed') AND attempts<3 AND run_after<=CURRENT_TIMESTAMP),
'network_age',(SELECT coalesce(max(extract(epoch FROM CURRENT_TIMESTAMP-run_after)),0) FROM core_networkjob WHERE status IN ('pending','failed') AND attempts<3 AND run_after<=CURRENT_TIMESTAMP),
'network_failed',(SELECT count(*) FROM core_networkjob n WHERE n.status='failed' AND n.attempts>=3 AND NOT EXISTS (
  SELECT 1 FROM core_networkjob later WHERE later.tenant_id=n.tenant_id AND later.customer_id=n.customer_id AND later.id>n.id)),
'billing_due',(SELECT count(*) FROM core_billingbatch WHERE status IN ('queued','processing')),
'billing_age',(SELECT greatest(0,coalesce(max(extract(epoch FROM CURRENT_TIMESTAMP-coalesce(confirmed_at,created_at))),0)) FROM core_billingbatch WHERE status IN ('queued','processing')),
'reminder_due',(SELECT count(*) FROM core_reminder WHERE status IN ('pending','failed') AND attempts<3 AND next_attempt_at<=CURRENT_TIMESTAMP),
'reminder_age',(SELECT coalesce(max(extract(epoch FROM CURRENT_TIMESTAMP-next_attempt_at)),0) FROM core_reminder WHERE status IN ('pending','failed') AND attempts<3 AND next_attempt_at<=CURRENT_TIMESTAMP),
'reminder_failed',(SELECT count(*) FROM core_reminder WHERE status='failed' AND attempts>=3),
'blocked',(SELECT count(*) FROM core_billingbatch WHERE status='blocked')+(SELECT count(*) FROM core_reminder WHERE status='blocked'));
COMMIT;"""


def write_json(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w',prefix='.ops-',dir=path.parent,delete=False) as stream:
        temporary=Path(stream.name)
        try:
            json.dump(data,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,path)
            parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(parent)
            finally:os.close(parent)
        finally:temporary.unlink(missing_ok=True)


def read_json(path,default=None):
    try:
        with Path(path).open('rb') as stream:raw=stream.read(65537)
    except FileNotFoundError:return default
    if len(raw)>65536:raise ValueError('File operasi melebihi batas.')
    data=json.loads(raw)
    if not isinstance(data,dict):raise ValueError('Format operasi tidak valid.')
    return data


def policy(directory):
    state=manifest(directory)
    default={'version':1,'project':state['project'],'check_interval':60,'heartbeat_max_age':120,
             'queue_max_age':900,'backup_enabled':False,'backup_interval':86400,
             'backup_directory':str(Path(directory).resolve()/'backups/automated'),
             'backup_key_file':str(Path(directory).resolve()/'secrets/backup-key.json')}
    config=read_json(Path(directory)/'ops/config.json',default)
    return validate_policy(config,default)


def validate_policy(config,default):
    if set(config)!=set(default) or type(config['version']) is not int or config['version']!=1 or config['project']!=default['project']:
        raise ValueError('Konfigurasi operasi tidak sesuai proyek.')
    for name,low,high in (('check_interval',5,3600),('heartbeat_max_age',15,3600),('queue_max_age',30,86400),('backup_interval',3600,604800)):
        if type(config[name]) is not int or not low<=config[name]<=high:raise ValueError('Interval operasi tidak valid.')
    if type(config['backup_enabled']) is not bool:raise ValueError('Status backup harus boolean.')
    for name in ('backup_directory','backup_key_file'):
        if not isinstance(config[name],str) or not Path(config[name]).is_absolute():raise ValueError('Path operasi harus absolut.')
    return config


@contextmanager
def ops_lock(directory,name='.lock'):
    root=Path(directory)/'ops';root.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(root/name,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('Pemeriksaan operasi lain masih berjalan.') from None
        yield
    finally:os.close(fd)


def configure(directory,**changes):
    with ops_lock(directory):
        config=policy(directory);original=config.copy()
        if set(changes)-set(config):raise ValueError('Field konfigurasi tidak dikenal.')
        config.update(changes)
        validate_policy(config,original)
        if config['backup_enabled']:read_key(Path(config['backup_key_file']))
        write_json(Path(directory)/'ops/config.json',config)
    return config


def heartbeat_problem(value,role,now,max_age):
    if (not isinstance(value,dict) or set(value)!={'version','role','state','updated_at','pid'}
        or type(value['version']) is not int or value['version']!=1 or value['role']!=role
        or not isinstance(value['state'],str) or value['state'] not in {'ok','busy','degraded','stopping'}
        or type(value['updated_at']) not in (int,float) or not math.isfinite(value['updated_at'])
        or type(value['pid']) is not int or value['pid']<1):return 'heartbeat_invalid'
    age=now-value['updated_at']
    if not -5<=age<=max_age:return 'heartbeat_stale'
    if value['state'] in {'degraded','stopping'}:return 'heartbeat_degraded'
    return None


def filesystem_capacity(block_size, blocks, available_blocks, inodes, available_inodes):
    if block_size <= 0 or blocks <= 0 or not 0 <= available_blocks <= blocks or not 0 <= available_inodes <= inodes:
        raise ValueError('Invalid filesystem counters')
    return {
        'total_bytes': block_size * blocks,
        'available_bytes': block_size * available_blocks,
        'total_inodes': inodes or None,
        'available_inodes': available_inodes if inodes else None,
    }


def host_filesystem(path):
    try:
        if path.is_dir():
            info = os.statvfs(path)
            return filesystem_capacity(info.f_frsize or info.f_bsize, info.f_blocks, info.f_bavail,
                                       info.f_files, info.f_favail)
    except (OSError, ValueError):
        pass
    return None


def database_filesystems(container):
    paths = {'/': 'container_root', '/var/lib/postgresql/data': 'postgres_volume'}
    if container:
        try:
            output = docker('exec', '--user', '999:999', container, 'stat', '--file-system',
                            '--format=%n %S %b %a %c %d', *paths, capture=True, timeout=10).stdout
            samples = {}
            for line in output.splitlines():
                path, *counters = line.split()
                if paths[path] in samples:
                    raise ValueError('Duplicate filesystem sample')
                samples[paths[path]] = filesystem_capacity(*map(int, counters))
            if len(samples) == len(paths):
                return samples
        except (OSError, subprocess.SubprocessError, TypeError, ValueError, KeyError):
            pass
    return dict.fromkeys(paths.values())


def storage_snapshot(directory, config, container, min_free_mib=None):
    minimum = min_free_mib * 1024**2 if min_free_mib is not None else None
    filesystems = {'checkout': host_filesystem(ROOT), 'staging_directory': host_filesystem(directory)}
    if config['backup_enabled']:
        filesystems['backup_destination'] = host_filesystem(Path(config['backup_directory']))
    filesystems.update(database_filesystems(container))
    issues = []
    for component, sample in filesystems.items():
        if sample is None:
            code, severity = 'filesystem_unavailable', 'unknown'
        elif sample['available_bytes'] == 0 or sample['available_inodes'] == 0:
            code, severity = 'filesystem_exhausted', 'critical'
        elif minimum is not None and sample['available_bytes'] < minimum:
            code, severity = 'filesystem_below_minimum', 'warning'
        else:
            continue
        issues.append({'code': code, 'severity': severity, 'component': component})
    status = max((item['severity'] for item in issues), key=LEVELS.get, default='ok')
    return {'status': status, 'filesystems': filesystems, 'minimum_free_bytes': minimum, 'issues': issues}


def storage(directory, min_free_mib=None):
    if min_free_mib is not None and (type(min_free_mib) is not int or min_free_mib < 1):
        raise ValueError('Minimum ruang kosong harus bilangan bulat positif dalam MiB.')
    directory = Path(directory).resolve()
    state = manifest(directory)
    config = policy(directory)
    container = None
    if state.get('restore_state') not in {'preparing', 'failed'}:
        try:
            container = database_container(directory)
        except Exception:
            pass
    return {'version': 1, 'project': state['project'], 'checked_at': time.time(),
            **storage_snapshot(directory, config, container, min_free_mib)}


def check(directory,config=None,now=None):
    directory=Path(directory).resolve();state=manifest(directory);config=config or policy(directory)
    now=time.time() if now is None else now
    issues=[];checks={};metrics=None
    def issue(code,severity,component):issues.append({'code':code,'severity':severity,'component':component})
    held=bool(state.get('recovery_pending'))
    report={'version':1,'project':state['project'],'checked_at':now,'workers_held':held,'checks':checks,'issues':issues,'metrics':None}
    if state.get('restore_state') in {'preparing','failed'}:
        issue('restore_blocked','critical','recovery')
        report['status']='critical';return report
    try:
        ids=compose(directory,'ps','--all','--quiet',capture=True,timeout=15).stdout.split()
        objects=json.loads(docker('inspect',*ids,capture=True,timeout=15).stdout) if ids else []
        services={}
        for obj in objects:
            labels=obj['Config'].get('Labels',{})
            if labels.get('com.docker.compose.project')!=state['project']:raise ValueError('Foreign container')
            services[labels.get('com.docker.compose.service')]=obj
        for name in ('db','web',*WORKERS):
            obj=services.get(name);running=bool(obj and obj['State']['Running'])
            if held and name in WORKERS:
                checks[name]={'status':'held'}
                if running:issue('unexpected_worker','critical',name)
                continue
            checks[name]={'status':'running' if running else 'stopped','restart_count':obj.get('RestartCount',0) if obj else None}
            if not running:issue('service_stopped','critical',name);continue
            if obj['State'].get('Paused'):issue('service_paused','critical',name);continue
            health=obj['State'].get('Health',{}).get('Status')
            if name in {'db','web'} and health!='healthy':issue('container_unhealthy','critical',name)
            if name in WORKERS:
                role=WORKERS[name]
                code=f"from pathlib import Path; import sys; p=Path('/tmp/tagihnet-heartbeats/{role}.json'); sys.stdout.buffer.write(p.open('rb').read(4097))"
                try:
                    result=compose(directory,'exec','-T',name,'python','-c',code,capture=True,timeout=10)
                    value=json.loads(result.stdout) if len(result.stdout)<=4096 else None
                    reason=heartbeat_problem(value,role,now,config['heartbeat_max_age'])
                    checks[name]['heartbeat_at']=value.get('updated_at') if isinstance(value,dict) else None
                    if reason:issue(reason,'warning' if reason=='heartbeat_degraded' else 'critical',name)
                except Exception:issue('heartbeat_unavailable','unknown',name)
    except Exception:
        services={};issue('docker_unavailable','unknown','docker')
    try:
        if type(state['port']) is not int or not 1024<=state['port']<=65535:raise ValueError('Port invalid')
        with urlopen(f"https://localhost:{state['port']}/health/",context=ssl.create_default_context(cafile=str(directory/'secrets/ca.crt')),timeout=5) as response:
            valid=response.status==200 and json.loads(response.read(4096))=={'status':'ok','mode':'simulation'}
        checks['https']={'status':'ok' if valid else 'failed'}
        if not valid:issue('https_unhealthy','critical','https')
    except Exception:checks['https']={'status':'unknown'};issue('https_unverified','critical','https')
    for name in ('https.crt','postgres.crt','ca.crt'):
        try:
            cert=x509.load_pem_x509_certificate((directory/'secrets'/name).read_bytes())
            remaining=cert.not_valid_after_utc.timestamp()-now
            checks[name]={'expires_at':cert.not_valid_after_utc.timestamp()}
            if remaining<=0 or cert.not_valid_before_utc.timestamp()>now:issue('certificate_expired','critical',name)
            elif remaining<7*86400:issue('certificate_expiring','warning',name)
        except Exception:issue('certificate_unknown','unknown',name)
    container = None
    try:
        container=database_container(directory)
        result=docker('exec','--user','999:999',container,'psql','--username','tagihnet_runtime','--dbname','tagihnet_staging',
            '--no-psqlrc','--quiet','--tuples-only','--no-align','--set','ON_ERROR_STOP=1','--command',METRICS_SQL,capture=True,timeout=15)
        metrics=json.loads(result.stdout.strip())
        fields={'network_due','network_age','network_failed','billing_due','billing_age','reminder_due','reminder_age','reminder_failed','blocked'}
        if not isinstance(metrics,dict) or set(metrics)!=fields or any(type(value) not in (int,float) or not math.isfinite(value) or value<0 for value in metrics.values()):raise ValueError('Metrics invalid')
        report['metrics']=metrics
        if not held:
            for role in ('network','billing','reminder'):
                if metrics[role+'_age']>config['queue_max_age']:issue('queue_stale','warning',role)
            if metrics['network_failed'] or metrics['reminder_failed']:issue('jobs_failed','warning','queue')
            if metrics['blocked']:issue('jobs_blocked','warning','queue')
    except Exception:issue('metrics_unavailable','unknown','database')
    report['storage'] = storage_snapshot(directory, config, container)
    report['status']=max((item['severity'] for item in issues),key=LEVELS.get,default='ok')
    return report


def append_events(directory,report,previous):
    root=Path(directory)/'ops';root.mkdir(parents=True,exist_ok=True,mode=0o700)
    old={i['component']+':'+i['code']:i for i in (previous or {}).get('issues',[])}
    new={i['component']+':'+i['code']:i for i in report['issues']}
    events=[]
    for key,item in new.items():
        if key not in old or old[key]['severity']!=item['severity']:events.append({'at':report['checked_at'],'event':'raised',**item})
    for key,item in old.items():
        if key not in new:events.append({'at':report['checked_at'],'event':'resolved',**item})
    path=root/'events.jsonl'
    if path.exists() and path.stat().st_size>262144:
        (root/'events.3.jsonl').unlink(missing_ok=True)
        for number in (2,1):
            prior=root/f'events.{number}.jsonl'
            if prior.exists():os.replace(prior,root/f'events.{number+1}.jsonl')
        os.replace(path,root/'events.1.jsonl')
    if events:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(fd,'w') as stream:
            for event in events:stream.write(json.dumps(event,sort_keys=True)+'\n')


def tick(directory,now=None):
    directory=Path(directory).resolve();now=time.time() if now is None else now
    with ops_lock(directory):
        config=policy(directory);previous=read_json(directory/'ops/status.json',{})
        job=read_json(directory/'ops/backup.json',{})
        state=manifest(directory)
        blocked=state.get('recovery_pending') or state.get('restore_state') in {'preparing','failed'}
        if job.get('failed'):next_attempt=job.get('next_attempt_at',0)
        elif job:next_attempt=job.get('last_success_at',0)+config['backup_interval']
        else:next_attempt=0
        if config['backup_enabled'] and not blocked and now>=next_attempt:
            job['last_attempt_at']=now
            try:
                key=read_key(Path(config['backup_key_file']))
                filename=datetime.fromtimestamp(now,timezone.utc).strftime('TagihNet-%Y%m%dT%H%M%SZ-')+uuid.uuid4().hex[:8]+'.tnbk'
                target=Path(config['backup_directory'])/filename
                backup(directory,target,key);verify_archive(target,key)
                job.update(last_success_at=now,last_archive=str(target),next_attempt_at=now+config['backup_interval'],failed=False)
            except Exception:job.update(next_attempt_at=now+300,failed=True)
            write_json(directory/'ops/backup.json',job)
        report=check(directory,config,now)
        report['backup']={'enabled':config['backup_enabled'],'last_success_at':job.get('last_success_at'),'last_archive':job.get('last_archive')}
        if config['backup_enabled'] and not blocked:
            if job.get('failed'):report['issues'].append({'code':'backup_failed','severity':'critical','component':'backup'})
            elif not job.get('last_success_at') or not -5<=now-job['last_success_at']<=config['backup_interval']+3600:
                report['issues'].append({'code':'backup_overdue','severity':'warning','component':'backup'})
            elif not Path(job['last_archive']).is_file():
                report['issues'].append({'code':'backup_missing','severity':'critical','component':'backup'})
        report['status']=max((item['severity'] for item in report['issues']),key=LEVELS.get,default='ok')
        append_events(directory,report,previous)
        write_json(directory/'ops/status.json',report)
        return report


def process_ticks(pid):
    try:return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
    except (OSError,IndexError):return None


def watcher(directory):
    data=read_json(Path(directory)/'ops/watcher.json',{})
    pid=data.get('pid')
    if (type(pid) is not int or pid<1 or not data.get('start_ticks') or process_ticks(pid)!=data['start_ticks']
        or data.get('project')!=manifest(directory)['project'] or not re.fullmatch('[a-f0-9]{32}',str(data.get('token')))):return None
    try:args=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    except OSError:return None
    expected={str(Path(__file__).resolve()).encode(),str(Path(directory).resolve()).encode(),str(data.get('token')).encode(),b'watch'}
    return data if expected<=set(args) else None


def start(directory):
    directory=Path(directory).resolve()
    with ops_lock(directory,'.watcher.lock'):
        existing=watcher(directory)
        if existing:return existing
        config=policy(directory);token=uuid.uuid4().hex
        process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'watch','--directory',str(directory),'--watch-token',token],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        data={'pid':process.pid,'start_ticks':process_ticks(process.pid),'token':token,'project':config['project']}
        try:write_json(directory/'ops/watcher.json',data)
        except BaseException:
            process.terminate();process.wait(timeout=5)
            raise
        return data


def stop(directory):
    with ops_lock(directory,'.watcher.lock'):
        data=watcher(directory)
        if not data:return False
        os.kill(data['pid'],signal.SIGTERM)
    return True


def watch(directory,token):
    if not re.fullmatch('[a-f0-9]{32}',token):raise ValueError('Token watcher tidak valid.')
    event=threading.Event()
    signal.signal(signal.SIGTERM,lambda *args:event.set())
    signal.signal(signal.SIGINT,lambda *args:event.set())
    while not event.is_set():
        try:
            tick(directory)
            (Path(directory)/'ops/watcher-failure.json').unlink(missing_ok=True)
        except Exception:
            # Fixed metadata only; retain last complete report but never present it as current.
            write_json(Path(directory)/'ops/watcher-failure.json',{'failed_at':time.time()})
        try:interval=policy(directory)['check_interval']
        except Exception:interval=60
        event.wait(interval)


def status(directory,now=None):
    now=time.time() if now is None else now
    report=read_json(Path(directory)/'ops/status.json',{})
    failure=read_json(Path(directory)/'ops/watcher-failure.json',{})
    age=now-report.get('checked_at',0)
    fresh=-5<=age<=policy(directory)['check_interval']*2+30 and failure.get('failed_at',0)<=report.get('checked_at',0)
    return {'watcher_running':bool(watcher(directory)),'report_fresh':fresh,'status':report.get('status','unknown') if fresh else 'unknown','report':report}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['configure','once','start','stop','status','storage','watch'])
    parser.add_argument('--directory',type=Path,default=ROOT/'data/staging')
    parser.add_argument('--enable-backup',action='store_true')
    parser.add_argument('--disable-backup',action='store_true')
    parser.add_argument('--backup-interval-hours',type=int)
    parser.add_argument('--backup-key-file',type=Path)
    parser.add_argument('--backup-directory',type=Path)
    parser.add_argument('--check-interval',type=int)
    parser.add_argument('--heartbeat-max-age',type=int)
    parser.add_argument('--min-free-mib',type=int,help='Ruang kosong minimum per filesystem untuk pemeriksaan storage.')
    parser.add_argument('--watch-token',default='',help=argparse.SUPPRESS)
    args=parser.parse_args();directory=args.directory.resolve()
    if args.min_free_mib is not None and args.action != 'storage':
        parser.error('--min-free-mib hanya untuk pemeriksaan storage.')
    try:
        if args.action=='configure':
            if args.enable_backup and args.disable_backup:raise ValueError('Pilih satu status backup.')
            values={}
            if args.enable_backup or args.disable_backup:values['backup_enabled']=args.enable_backup
            for arg,key in (('check_interval','check_interval'),('heartbeat_max_age','heartbeat_max_age')):
                if getattr(args,arg) is not None:values[key]=getattr(args,arg)
            if args.backup_interval_hours is not None:values['backup_interval']=args.backup_interval_hours*3600
            for arg in ('backup_key_file','backup_directory'):
                if getattr(args,arg):values[arg]=str(getattr(args,arg).resolve())
            configure(directory,**values);print('Konfigurasi operasi lokal tersimpan; tidak ada pesan eksternal dikirim.')
        elif args.action=='once':
            result=tick(directory);print(json.dumps(result,sort_keys=True));return 0 if result['status']=='ok' else 1
        elif args.action=='status':
            result=status(directory);print(json.dumps(result,sort_keys=True));return 0 if result['status']=='ok' else 1
        elif args.action == 'storage':
            result = storage(directory, args.min_free_mib)
            print(json.dumps(result, sort_keys=True))
            return 0 if result['status'] == 'ok' else 1
        elif args.action=='start':print('Watcher lokal aktif; pid='+str(start(directory)['pid']))
        elif args.action=='stop':print('Stop diminta; job aktif diselesaikan dahulu.' if stop(directory) else 'Watcher milik staging ini tidak aktif.')
        elif args.action=='watch':watch(directory,args.watch_token)
    except Exception as error:parser.exit(1,f'Gagal: {type(error).__name__}; periksa status/izin lokal. Tidak ada diagnostic/secret dicetak.\n')
    return 0


if __name__=='__main__':raise SystemExit(main())
