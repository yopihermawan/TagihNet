"""Controlled same-schema image switch/failure/rollback rehearsal on owned disposable staging only."""
import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import tempfile
import uuid
from datetime import datetime,timezone
from pathlib import Path
from urllib.request import HTTPSHandler,HTTPCookieProcessor,build_opener

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
from core.backup_archive import atomic_output,generate_key,read_key
from scripts.staging_lab import compose,docker,manifest,pin_image,prepare,up
from scripts.staging_backup import backup
from scripts.recovery_package import export_package,restore_package
from scripts.staging_preflight import check
from scripts.preflight_smoke import wait_healthy
from verify_staging import verify
from verify_staging_backup import FIXTURE,fingerprint,port,shell,verify_restored_access

TOKEN_LABEL='io.tagihnet.rollout.token'


def image_info(reference):
    data=json.loads(docker('image','inspect',reference,capture=True,timeout=15).stdout)
    if not isinstance(data,list) or len(data)!=1 or not re.fullmatch('sha256:[a-f0-9]{64}',data[0]['Id']):raise ValueError('Image identity invalid')
    return data[0]


def owned_image(reference,token):
    if not re.fullmatch('[a-f0-9]{32}',token):raise ValueError('Invalid rehearsal token')
    if not re.fullmatch('tagihnet-rollout-'+token+':(?:candidate|failed)',reference):raise ValueError('Unexpected image reference')
    info=image_info(reference)
    if info['Config'].get('Labels',{}).get(TOKEN_LABEL)!=token:raise ValueError('Image is not owned by this rehearsal')
    return info['Id']


def remove_derivative(reference,token):
    if not re.fullmatch('[a-f0-9]{32}',token) or reference not in {'tagihnet-rollout-'+token+':candidate','tagihnet-rollout-'+token+':failed'}:
        raise ValueError('Unexpected cleanup reference')
    # A failed build may leave no tag. A failed daemon/list operation is never absence.
    found=docker('image','ls','--format','{{.Repository}}:{{.Tag}}','--filter','reference='+reference,capture=True,timeout=15).stdout.splitlines()
    if not found:return
    if found!=[reference]:raise ValueError('Ambiguous cleanup reference')
    owned_image(reference,token)
    docker('image','rm',reference,capture=True,timeout=30)


def build_derivative(folder,base_alias,reference,token,failed=False):
    if not re.fullmatch('[a-f0-9]{32}',token):raise ValueError('Invalid rehearsal token')
    if not re.fullmatch('tagihnet-rollout-'+token+':base',base_alias):raise ValueError('Invalid base alias')
    if not re.fullmatch('tagihnet-rollout-'+token+':(?:candidate|failed)',reference):raise ValueError('Invalid derivative reference')
    dockerfile=Path(folder)/('failed.Dockerfile' if failed else 'candidate.Dockerfile')
    text='FROM '+base_alias+'\nLABEL '+TOKEN_LABEL+'="'+token+'"\n'
    if failed:text+='CMD ["python", "-c", "raise SystemExit(37)"]\n'
    dockerfile.write_text(text)
    # Local parent only; no COPY, RUN, dependency download or private-file build context.
    docker('build','--pull=false','--network=none','--file',dockerfile,'--tag',reference,folder,capture=True,timeout=60)
    return owned_image(reference,token)


def snapshot(directory):
    # MigrationRecorder is metadata, not a registered application model; hash it explicitly.
    code="""
import hashlib,json
from django.db import connection
with connection.cursor() as c:
    c.execute('SELECT row_to_json(t)::text FROM django_migrations t ORDER BY id')
    rows=c.fetchall()
print('RECORDER:'+json.dumps([len(rows),hashlib.sha256(json.dumps(rows).encode()).hexdigest()]))
"""
    value=shell(directory,code)
    recorder=json.loads(next(line.removeprefix('RECORDER:') for line in value.splitlines() if line.startswith('RECORDER:')))
    return {'models_and_sequences':fingerprint(directory),'migration_recorder':recorder}


def runtime(directory,include_stopped=False):
    ids=compose(directory,'ps',*(['--all'] if include_stopped else []),'--quiet','web',capture=True).stdout.split()
    if len(ids)!=1:raise ValueError('Web container not running')
    info=json.loads(docker('inspect',ids[0],capture=True).stdout)[0]
    labels=info['Config'].get('Labels',{})
    if labels.get('com.docker.compose.project')!=manifest(directory)['project'] or labels.get('com.docker.compose.service')!='web':raise ValueError('Foreign web container')
    return info


def held(directory):
    state=manifest(directory)
    services=compose(directory,'ps','--services','--filter','status=running',capture=True).stdout.split()
    return state.get('recovery_pending') is True and not {'network-worker','billing-worker','reminder-worker'}&set(services)


def session_valid(directory,identity):
    base='https://localhost:'+str(manifest(directory)['port'])
    client=build_opener(HTTPSHandler(context=ssl.create_default_context(cafile=str(directory/'secrets/ca.crt'))),HTTPCookieProcessor(identity.cookies))
    with client.open(base+'/settings/access/',timeout=10) as response:
        if response.status!=200 or response.url!=base+'/settings/access/':raise AssertionError('Existing target session was lost')


def drill(output):
    output=Path(output)
    if os.path.lexists(output):raise ValueError('Output already exists')
    token=uuid.uuid4().hex;base_alias='tagihnet-rollout-'+token+':base'
    refs=['tagihnet-rollout-'+token+':candidate','tagihnet-rollout-'+token+':failed']
    report={'version':1,'started_at':datetime.now(timezone.utc).isoformat(),'scope':'disposable_same_schema_rollout',
        'production_ready':False,'status':'failed','cleanup':'pending','failures':[],'stages':[]}
    stacks=[];attempted=[];base_tagged=False;base_id=None
    with tempfile.TemporaryDirectory(prefix='tagihnet-rollout-') as folder:
        root=Path(folder)
        def fresh(name):
            directory=root/name;prepare(directory,port());stacks.append(directory);return directory
        try:
            base_id=image_info('tagihnet-staging:local')['Id'];report['base_image']=base_id
            docker('tag',base_id,base_alias,capture=True,timeout=15);base_tagged=True
            for reference,failed in zip(refs,(False,True)):
                attempted.append(reference)
                image=build_derivative(root,base_alias,reference,token,failed)
                report['stages'].append({'image_id':image,'role':'failed' if failed else 'candidate'})
            candidate=owned_image(refs[0],token);bad=owned_image(refs[1],token)
            if len({base_id,candidate,bad})!=3:raise AssertionError('Derivative image IDs must differ')
            source=fresh('source');identity=verify(source)
            compose(source,'stop','web','network-worker','billing-worker','reminder-worker',capture=True)
            shell(source,FIXTURE)
            keyfile=root/'archive-key.json';generate_key(keyfile);archive=root/'source.tnbk'
            backup(source,archive,read_key(keyfile))
            package=root/'package';keys=root/'keys'
            export_package(archive,keyfile,source/'secrets/mfa-key',package,keys)
            target=fresh('candidate-target');restore_package(package,keys,target)
            pin_image(target,base_id);up(target,capture=True);wait_healthy(target)
            assert held(target)
            target_identity=verify_restored_access(target,identity)
            before=snapshot(target)
            # These are measured row counts; verify_restored_access checked the actual balance.
            counts={name:before['models_and_sequences'][table][0] for name,table in (
                ('tenants','core_tenant'),('invoices','core_invoice'),('payments','core_payment'),
                ('reversals','core_paymentreversal'),('photos','core_workphoto'))}
            assert counts=={'tenants':2,'invoices':1,'payments':2,'reversals':1,'photos':1}
            secrets_before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (target/'secrets').iterdir() if p.is_file()}
            source_before=snapshot(source)

            pin_image(target,candidate);up(target,capture=True);wait_healthy(target)
            assert runtime(target)['Image']==candidate and held(target)
            assert snapshot(target)==before
            assert check(target,candidate,package,keys,mode='recovery')['status']=='passed'
            session_valid(target,target_identity)
            report['candidate_passed']=True
            print('PASS: same-schema candidate image actually serves HTTPS; all model/sequence/recorder rows unchanged and existing session survives; workers held.',flush=True)

            before=snapshot(target)  # Authenticated GET deliberately updated only its session activity.
            pin_image(target,bad)
            try:up(target,capture=True,timeout=12)
            except RuntimeError:pass
            else:raise AssertionError('Deliberately failing web became healthy')
            failed_info=runtime(target,include_stopped=True)
            assert failed_info['Image']==bad and failed_info['State']['ExitCode']==37
            assert held(target) and snapshot(target)==before
            report['failed_candidate_detected']=True

            pin_image(target,candidate);up(target,capture=True);wait_healthy(target)
            assert runtime(target)['Image']==candidate and held(target)
            assert snapshot(target)==before
            assert check(target,candidate,package,keys,mode='recovery')['status']=='passed'
            session_valid(target,target_identity)
            assert {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (target/'secrets').iterdir() if p.is_file()}==secrets_before
            assert snapshot(source)==source_before
            assert image_info('tagihnet-staging:local')['Id']==base_id
            report.update(rollback_passed=True,workers_held=True,secrets_unchanged=True,source_unchanged=True,
                integrity={**counts,'balance':149750},status='passed')
            print('PASS: deliberate web failure detected; previous working image restored, data/recorder/sequences/secret files preserved, original target session still valid.',flush=True)
        except Exception as error:report['failures'].append('probe_'+type(error).__name__)
        finally:
            cleanup_errors=[]
            for directory in reversed(stacks):
                try:compose(directory,'down','--volumes','--remove-orphans',capture=True,timeout=60)
                except Exception:cleanup_errors.append('stack_cleanup')
            for reference in reversed(attempted):
                try:remove_derivative(reference,token)
                except Exception:cleanup_errors.append('image_cleanup')
            if base_tagged:
                try:
                    if image_info(base_alias)['Id']!=base_id:raise ValueError('Base alias changed')
                    docker('image','rm',base_alias,capture=True,timeout=30)
                except Exception:cleanup_errors.append('base_alias_cleanup')
            report['cleanup']='failed' if cleanup_errors else 'passed'
            if cleanup_errors:report['status']='failed';report['failures'].extend(cleanup_errors)
            with atomic_output(output) as stream:stream.write(json.dumps(report,indent=2,sort_keys=True).encode())
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        result=drill(args.output)
        print(json.dumps({'status':result['status'],'cleanup':result['cleanup'],'failures':result['failures'],'production_ready':False}))
        return 0 if result['status']=='passed' else 1
    except Exception as error:parser.exit(1,'Rollout rehearsal refused: '+type(error).__name__+'; no secrets/diagnostic printed.\n')


if __name__=='__main__':raise SystemExit(main())
