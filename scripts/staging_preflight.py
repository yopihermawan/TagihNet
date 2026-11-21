"""Read-only release evidence for the local simulation profile; never deploys or enables production."""
import argparse
import hashlib
import inspect
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from core.backup_archive import atomic_output,read_key
from scripts.staging_lab import PG_IMAGE,compose,docker,manifest
from scripts import staging_ops as ops
from scripts.recovery_package import archive_details,verify_package,mfa_key

APP_SERVICES=('web','network-worker','billing-worker','reminder-worker')
ROOT_FILES=('manage.py','requirements.txt','requirements-postgres.txt','requirements-staging.txt')
DIRECTORIES=('config','core','templates','static','deploy')
ALLOWED_WARNINGS={'security.W005','security.W021'}


def application_digest(root):
    # Self-contained so this exact function can be sent to the inspected container.
    import hashlib
    from pathlib import Path
    root=Path(root);files=[];total=0
    for name in ('config','core','templates','static','deploy'):
        base=root/name
        if base.is_symlink() or not base.is_dir():raise ValueError('Missing application directory')
        for path in base.rglob('*'):
            relative=path.relative_to(root)
            if relative.parts[:2]==('core','tests') or '__pycache__' in relative.parts or path.suffix=='.pyc':continue
            if path.is_symlink():raise ValueError('Application symlink')
            if path.is_file():
                files.append(path)
                if len(files)>20000:raise ValueError('Application tree exceeds limits')
    files.extend(root/name for name in ('manage.py','requirements.txt','requirements-postgres.txt','requirements-staging.txt'))
    digest=hashlib.sha256(b'TagihNet/application-tree/v1\0')
    for path in sorted(files):
        if path.is_symlink() or not path.is_file():raise ValueError('Missing application file')
        size=path.stat().st_size;total+=size
        if size>32*1024**2 or total>128*1024**2 or len(files)>20000:raise ValueError('Application tree exceeds limits')
        name=path.relative_to(root).as_posix().encode()
        digest.update(len(name).to_bytes(4,'big')+name+size.to_bytes(8,'big'))
        with path.open('rb') as stream:
            while chunk:=stream.read(1024*1024):digest.update(chunk)
    return digest.hexdigest()


def dependencies(root):
    pins={};visited=set()
    def read(name):
        if name not in ROOT_FILES[1:] or name in visited:raise ValueError('Unsupported dependency include')
        visited.add(name)
        for raw in (Path(root)/name).read_text().splitlines():
            line=raw.strip()
            if not line or line.startswith('#'):continue
            if line.startswith('-r '):read(line[3:]);continue
            match=re.fullmatch(r'([A-Za-z0-9_-]+)==([A-Za-z0-9_.+-]+)',line)
            if not match:raise ValueError('Dependencies must be pinned')
            package,version=match.groups()
            if package.lower() in pins:raise ValueError('Duplicate dependency')
            pins[package.lower()]=version
    read('requirements-staging.txt')
    return pins


PROBE_CODE='''
import importlib.metadata
from django.conf import settings
from django.core.checks import run_checks,WARNING
from django.db import connection,transaction
from django.db.migrations.executor import MigrationExecutor
with transaction.atomic():
    with connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute("SET LOCAL statement_timeout='5s'")
        cursor.execute("SET LOCAL lock_timeout='5s'")
        cursor.execute("SELECT current_user,ssl,rolsuper,rolcreatedb,rolcreaterole,has_schema_privilege(current_user,'public','CREATE') FROM pg_roles JOIN pg_stat_ssl ON pid=pg_backend_pid() WHERE rolname=current_user")
        role,ssl_on,superuser,createdb,createrole,ddl=cursor.fetchone()
    executor=MigrationExecutor(connection)
    plan=executor.migration_plan(executor.loader.graph.leaf_nodes())
    messages=run_checks(include_deployment_checks=True)
    checks=sorted(message.id or 'unknown' for message in messages if message.level>=WARNING)
    pending=sorted(migration.app_label+'.'+migration.name for migration,backward in plan)
    applied=sorted(app+'.'+name for app,name in executor.loader.applied_migrations)
    unknown=sorted(app+'.'+name for app,name in executor.loader.applied_migrations if (app,name) not in executor.loader.disk_migrations)
result={'version':1,'source_sha256':application_digest('/app'),'checks':checks,'pending_migrations':pending,'unexpected_migrations':unknown,
    'migration_sha256':hashlib.sha256(json.dumps(applied).encode()).hexdigest(),
    'packages':{name:importlib.metadata.version(name) for name in package_names},
    'settings':{'profile':getattr(settings,'STAGING',False) is True,'debug_off':settings.DEBUG is False,
        'simulation':settings.SIMULATION is True,'cookies_secure':settings.SESSION_COOKIE_SECURE and settings.CSRF_COOKIE_SECURE,
        'native_tls':settings.SECURE_PROXY_SSL_HEADER is None and settings.USE_X_FORWARDED_HOST is False,
        'mail_inactive':settings.EMAIL_BACKEND=='django.core.mail.backends.dummy.EmailBackend',
        'local_hsts':settings.SECURE_HSTS_SECONDS==3600 and not settings.SECURE_HSTS_INCLUDE_SUBDOMAINS and not settings.SECURE_HSTS_PRELOAD},
    'database':{'tls':ssl_on is True,'restricted_runtime':role=='tagihnet_runtime' and not any((superuser,createdb,createrole,ddl))}}
print(json.dumps(result,sort_keys=True))
'''


def probe(directory,service,pins):
    code="import os,json,hashlib; assert os.environ.get('DJANGO_SETTINGS_MODULE')=='config.staging_settings'; import django; django.setup()\n"
    code+=inspect.getsource(application_digest)
    code+='\npackage_names='+repr(sorted(pins))+'\n'+PROBE_CODE
    result=compose(directory,'exec','-T',service,'python','-c',code,capture=True,timeout=30)
    if len(result.stdout)>32768:raise ValueError('Probe response exceeds limit')
    data=json.loads(result.stdout)
    if not isinstance(data,dict):raise ValueError('Probe response invalid')
    return data


def container_issues(name,obj,state,image_id):
    issues=[]
    def require(condition,code):
        if not condition:issues.append(code)
    labels=obj['Config'].get('Labels',{});host=obj['HostConfig'];running=obj['State']['Running']
    require(labels.get('com.docker.compose.project')==state['project'] and labels.get('com.docker.compose.service')==name,'container_identity')
    require(host.get('NetworkMode')==state['project']+'_default','network_mode')
    require(not host.get('Privileged') and host.get('PidMode','')!='host','privileged_container')
    require(host.get('ReadonlyRootfs') is True,'writable_root')
    require(not obj['State'].get('Paused') and not obj['State'].get('OOMKilled'),'container_fault')
    require(type(host.get('Memory')) is int and 0<host['Memory']<=512*1024**2,'memory_limit')
    require(type(host.get('NanoCpus')) is int and 0<host['NanoCpus']<=10**9,'cpu_limit')
    require(type(host.get('PidsLimit')) is int and 0<host['PidsLimit']<=128,'pid_limit')
    logs=host.get('LogConfig',{})
    require(logs.get('Type')=='json-file' and logs.get('Config',{}).get('max-size')=='10m' and logs.get('Config',{}).get('max-file')=='3','log_limits')
    ports={port:bindings for port,bindings in obj.get('NetworkSettings',{}).get('Ports',{}).items() if bindings}
    if name=='web':require(ports=={'8443/tcp':[{'HostIp':'127.0.0.1','HostPort':str(state['port'])}]},'published_ports')
    else:require(not ports,'published_ports')
    if name=='db':
        require(obj['Config']['Image']==PG_IMAGE,'postgres_image')
        expected={'/var/lib/postgresql/data':('postgres-data',True),'/run/postgres':('postgres-secrets',False),'/docker-entrypoint-initdb.d':('postgres-secrets',False)}
    else:
        require(obj['Image']==image_id,'image_mismatch')
        require(obj['Config'].get('User')=='10001:10001','runtime_user')
        require('ALL' in host.get('CapDrop',[]) and not host.get('CapAdd'),'capabilities')
        require(any(item in {'no-new-privileges','no-new-privileges:true'} for item in host.get('SecurityOpt',[])),'security_options')
        expected={'/run/tagihnet':('runtime-secrets',False)}
        if name=='web':expected['/run/https']=('https-secrets',False)
    actual={mount['Destination']:mount for mount in obj['Mounts'] if mount.get('Type')!='tmpfs'}
    require(set(actual)==set(expected),'unexpected_mounts')
    for destination,(volume,writable) in expected.items():
        mount=actual.get(destination,{})
        require(mount.get('Type')=='volume' and mount.get('Name')==state['project']+'_'+volume and mount.get('RW') is writable,'volume_identity')
    require(running is True,'service_stopped')
    return sorted(set(issues))


def probe_issues(data,source,pins):
    issues=[]
    fields={'version','source_sha256','checks','pending_migrations','unexpected_migrations','migration_sha256','packages','settings','database'}
    flags={'profile','debug_off','simulation','cookies_secure','native_tls','mail_inactive','local_hsts'}
    if (not isinstance(data,dict) or set(data)!=fields or type(data['version']) is not int or data['version']!=1
        or not isinstance(data['settings'],dict) or set(data['settings'])!=flags
        or not isinstance(data['database'],dict) or set(data['database'])!={'tls','restricted_runtime'}
        or not isinstance(data['source_sha256'],str) or not re.fullmatch('[a-f0-9]{64}',data['source_sha256'])
        or not isinstance(data['checks'],list) or any(not isinstance(i,str) or not re.fullmatch(r'[a-zA-Z0-9_]+\.[A-Z][0-9]{3}',i) for i in data['checks'])
        or not isinstance(data['pending_migrations'],list) or any(not isinstance(i,str) or not re.fullmatch(r'[a-z0-9_]+\.[0-9]{4}_[a-z0-9_]+',i) for i in data['pending_migrations'])
        or not isinstance(data['unexpected_migrations'],list) or any(not isinstance(i,str) or not re.fullmatch(r'[a-z0-9_]+\.[0-9]{4}_[a-z0-9_]+',i) for i in data['unexpected_migrations'])
        or not isinstance(data['migration_sha256'],str) or not re.fullmatch('[a-f0-9]{64}',data['migration_sha256'])):
        return ['probe_invalid']
    if data['source_sha256']!=source:issues.append('source_mismatch')
    if data['packages']!=pins:issues.append('dependency_mismatch')
    if any(value is not True for value in data['settings'].values()):issues.append('settings_invalid')
    if any(value is not True for value in data['database'].values()):issues.append('database_security')
    if set(data['checks'])-ALLOWED_WARNINGS:issues.append('deploy_checks')
    if data['pending_migrations']:issues.append('migrations_pending')
    if data['unexpected_migrations']:issues.append('migrations_unrecognized')
    return issues


def check(directory,image_id,package,keys,mode='active',now=None):
    if not re.fullmatch('sha256:[a-f0-9]{64}',image_id):raise ValueError('Use an immutable image ID')
    if mode not in {'active','recovery'}:raise ValueError('Invalid mode')
    directory=Path(directory).resolve();state=manifest(directory);clock=now
    now=time.time() if now is None else now
    if type(now) not in (int,float) or not math.isfinite(now):raise ValueError('Invalid check time')
    result={'version':1,'checked_at':now,'project':state['project'],'mode':mode,'expected_image':image_id,
            'production_ready':False,'scope':'local_simulation','status':'failed','issues':[],'evidence':{}}
    def issue(code,component):result['issues'].append({'code':code,'component':component})
    if not state.get('initialized') or state.get('restore_state') in {'preparing','failed'}:
        issue('staging_not_initialized','manifest');return result
    held=bool(state.get('recovery_pending'))
    if held!=(mode=='recovery'):issue('recovery_mode_mismatch','manifest');return result
    if not state.get('image_id'):issue('image_not_pinned','manifest')
    elif state['image_id']!=image_id:issue('image_selection_mismatch','manifest')
    try:
        found=docker('image','inspect',image_id,'--format','{{.Id}}',capture=True,timeout=15).stdout.strip()
        if found!=image_id:raise ValueError('Missing selected image')
    except Exception:issue('image_unavailable','candidate')
    try:
        source=application_digest(ROOT);pins=dependencies(ROOT)
        result['evidence']['source_sha256']=source
        result['evidence']['checkout_commit']=subprocess.run(['git','-C',str(ROOT),'rev-parse','HEAD'],check=True,capture_output=True,text=True,timeout=5).stdout.strip()
        result['evidence']['checkout_dirty']=bool(subprocess.run(['git','-C',str(ROOT),'status','--porcelain'],check=True,capture_output=True,text=True,timeout=5).stdout)
        ids=compose(directory,'ps','--all','--quiet',capture=True,timeout=15).stdout.split()
        if not ids:raise ValueError('No containers')
        info=json.loads(docker('inspect',*ids,capture=True,timeout=15).stdout)
        services={}
        for obj in info:
            labels=obj['Config'].get('Labels',{})
            if labels.get('com.docker.compose.project')!=state['project']:raise ValueError('Foreign project')
            name=labels.get('com.docker.compose.service')
            if name in services:raise ValueError('Duplicate service')
            services[name]=obj
        selected=('db','web') if held else ('db',*APP_SERVICES)
        for name in selected:
            if name not in services:issue('service_missing',name);continue
            for code in container_issues(name,services[name],state,image_id):issue(code,name)
            if name!='db' and services[name]['State']['Running']:
                try:
                    data=probe(directory,name,pins)
                    errors=probe_issues(data,source,pins)
                    for code in errors:issue(code,name)
                    if 'probe_invalid' not in errors:
                        result['evidence'][name]={'source_sha256':data['source_sha256'],'migration_sha256':data['migration_sha256'],
                            'deploy_checks':data['checks'],'pending_migrations':data['pending_migrations'],'unexpected_migrations':data['unexpected_migrations']}
                except Exception:issue('probe_unavailable',name)
        if held and any(services.get(name,{}).get('State',{}).get('Running') for name in APP_SERVICES[1:]):issue('unexpected_worker','recovery')
        migration_states={result['evidence'][name]['migration_sha256'] for name in APP_SERVICES if name in result['evidence']}
        if len(migration_states)>1:issue('migration_state_changed','database')
    except Exception:issue('inspection_unavailable','docker')
    try:
        health=ops.check(directory)
        result['evidence']['health_status']=health['status']
        if health['status']!='ok':issue('health_not_ok','health')
        if not held and (health['metrics'] is None or any(health['metrics'][key] for key in ('network_due','billing_due','reminder_due'))):issue('work_not_drained','queue')
    except Exception:issue('health_unavailable','health')
    backup_digest=None
    if not held:
        try:
            policy=ops.policy(directory);job=ops.read_json(directory/'ops/backup.json',{})
            timestamp=job.get('last_success_at')
            if (not policy['backup_enabled'] or job.get('failed') or type(timestamp) not in (int,float)
                or not math.isfinite(timestamp) or not -5<=now-timestamp<=policy['backup_interval']+3600):raise ValueError('Backup not current')
            archive=Path(job['last_archive'])
            details=archive_details(archive,read_key(Path(policy['backup_key_file'])))
            age=(time.time() if clock is None else clock)-datetime.fromisoformat(details['created_at']).timestamp()
            if not -5<=age<=policy['backup_interval']+3600:raise ValueError('Archive timestamp not current')
            backup_digest=details['archive_sha256']
            result['evidence']['backup_at']=timestamp
        except Exception:issue('backup_unverified','backup')
    try:
        metadata=verify_package(Path(package),Path(keys))
        if hashlib.sha256(mfa_key(directory/'secrets/mfa-key')).hexdigest()!=metadata['mfa_sha256']:raise ValueError('Package MFA differs')
        if not held and backup_digest is not None and metadata['archive_sha256']!=backup_digest:issue('recovery_backup_mismatch','recovery')
        result['evidence']['recovery_package_id']=metadata['package_id']
    except Exception:issue('recovery_package_unverified','recovery')
    result['status']='passed' if not result['issues'] else 'failed'
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=ROOT/'data/staging')
    parser.add_argument('--image-id',required=True)
    parser.add_argument('--package-dir',type=Path,required=True)
    parser.add_argument('--keys-dir',type=Path,required=True)
    parser.add_argument('--mode',choices=['active','recovery'],default='active')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        if os.path.lexists(args.output):raise ValueError('Report already exists')
        # Publication is exclusive even if another process creates the same name later.
        with atomic_output(args.output) as stream:
            result=check(args.directory,args.image_id,args.package_dir,args.keys_dir,args.mode)
            stream.write(json.dumps(result,indent=2,sort_keys=True).encode())
        print(json.dumps({'status':result['status'],'scope':result['scope'],'production_ready':False,'issues':result['issues']}))
        return 0 if result['status']=='passed' else 1
    except Exception as error:parser.exit(1,'Preflight refused: '+type(error).__name__+'; no secret/diagnostic printed.\n')


if __name__=='__main__':raise SystemExit(main())
