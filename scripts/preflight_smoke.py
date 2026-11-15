"""Release preflight assertions for stacks created by the disposable recovery verifier only."""
import json
import time
from pathlib import Path
from scripts import staging_ops as ops
from scripts.staging_lab import compose,docker,pin_image,up,manifest
from scripts.staging_preflight import check
from scripts.recovery_package import export_package


def image(directory):
    container=compose(directory,'ps','--quiet','web',capture=True).stdout.strip()
    return json.loads(docker('inspect',container,capture=True).stdout)[0]['Image']


def wait_healthy(directory):
    deadline=time.monotonic()+35
    while time.monotonic()<deadline:
        if ops.check(directory)['status']=='ok':return
        time.sleep(.5)
    raise AssertionError('Docker and HTTPS health did not both become ready')


def active(directory,fingerprint):
    policy=ops.policy(directory);job=ops.read_json(directory/'ops/backup.json')
    package=directory.parent/'preflight-package';keys=directory.parent/'preflight-custody'
    export_package(Path(job['last_archive']),Path(policy['backup_key_file']),directory/'secrets/mfa-key',package,keys)
    before=fingerprint(directory);backup_before=(directory/'ops/backup.json').read_bytes()
    expected=image(directory)
    old_ids=compose(directory,'ps','--quiet',capture=True).stdout.split()
    pin_image(directory,expected)
    assert compose(directory,'ps','--quiet',capture=True).stdout.split()==old_ids
    up(directory,capture=True)  # Pinned image is accepted by Compose, without data/schema changes.
    wait_healthy(directory)
    result=check(directory,expected,package,keys)
    assert result['status']=='passed',result['issues']
    assert result['production_ready'] is False and result['scope']=='local_simulation'
    bad=check(directory,'sha256:'+'0'*64,package,keys)
    assert bad['status']=='failed' and any(i['code']=='image_mismatch' for i in bad['issues'])
    assert fingerprint(directory)==before
    assert (directory/'ops/backup.json').read_bytes()==backup_before
    print('PASS: active preflight source/dependency/migration/security/health/backup+custody gates; wrong immutable image fails; all tables/sequences and backup metadata unchanged.',flush=True)


def recovery(directory,package,keys,fingerprint,shell):
    before=fingerprint(directory);expected=image(directory)
    pin_image(directory,expected);up(directory,capture=True)
    wait_healthy(directory)
    assert manifest(directory)['recovery_pending']
    result=check(directory,expected,package,keys,mode='recovery')
    assert result['status']=='passed',result['issues']
    denied=check(directory,expected,package,keys)
    assert denied['status']=='failed' and denied['issues'][0]['code']=='recovery_mode_mismatch'
    record=shell(directory,"from django.db.migrations.recorder import MigrationRecorder; import json; r=MigrationRecorder.Migration.objects.get(app='core',name='0011_billing_schedule'); print('MIGRATION:'+json.dumps({'id':r.pk,'app':r.app,'name':r.name,'applied':r.applied.isoformat()})); r.delete()")
    data=json.loads(next(line.removeprefix('MIGRATION:') for line in record.splitlines() if line.startswith('MIGRATION:')))
    try:
        pending_before=fingerprint(directory)
        result=check(directory,expected,package,keys,mode='recovery')
        assert result['status']=='failed' and any(i['code']=='migrations_pending' for i in result['issues'])
        assert fingerprint(directory)==pending_before  # The gate must not repair/run migrations.
    finally:
        # Restore only the exact fixture record removed above, preserving its ID/time/sequence.
        shell(directory,"from django.db.migrations.recorder import MigrationRecorder; from datetime import datetime; import json; r=json.loads("+repr(json.dumps(data))+"); MigrationRecorder.Migration.objects.create(pk=r['id'],app=r['app'],name=r['name'],applied=datetime.fromisoformat(r['applied']))")
    assert fingerprint(directory)==before
    future_id=data['id']+1000000
    shell(directory,"from django.db.migrations.recorder import MigrationRecorder; from django.utils import timezone; MigrationRecorder.Migration.objects.create(pk="+str(future_id)+",app='core',name='9999_future',applied=timezone.now())")
    try:
        unknown_before=fingerprint(directory)
        result=check(directory,expected,package,keys,mode='recovery')
        assert result['status']=='failed' and any(i['code']=='migrations_unrecognized' for i in result['issues'])
        assert fingerprint(directory)==unknown_before
    finally:shell(directory,"from django.db.migrations.recorder import MigrationRecorder; MigrationRecorder.Migration.objects.get(pk="+str(future_id)+",app='core',name='9999_future').delete()")
    assert fingerprint(directory)==before
    print('PASS: recovery preflight holds workers; active mode refused; real pending/future migration metadata detected without repair; only fixture records restored and all tables/sequences match.',flush=True)
