"""Operations fault injection only on disposable stacks owned by the recovery verifier."""
import json
import time
from pathlib import Path

from core.backup_archive import generate_key, read_key, verify_archive
from scripts import staging_ops as ops
from scripts.staging_lab import compose, docker


def wait_ok(directory,timeout=20):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        report=ops.tick(directory)
        if report['status']=='ok':return report
        time.sleep(.5)
    raise AssertionError('Operations did not recover: '+json.dumps(report['issues']))


def verify_source(directory):
    wait_ok(directory)
    # Stop the actual worker loop, not the container: Docker still reports Running.
    def signal_worker(name):
        compose(directory,'exec','-T','billing-worker','python','-c',
            "import json,os,signal; from pathlib import Path; "
            "pid=json.loads(Path('/tmp/tagihnet-heartbeats/billing.json').read_text())['pid']; "
            "assert Path('/proc/'+str(pid)+'/cmdline').read_bytes().split(bytes([0]))[1:3]==[b'manage.py',b'run_billing_worker']; "
            f"os.kill(pid,signal.{name})",capture=True,timeout=10)
    ops.configure(directory,heartbeat_max_age=15)
    signal_worker('SIGSTOP')
    try:
        time.sleep(16)
        report=ops.tick(directory)
        assert report['checks']['billing-worker']['status']=='running'
        assert {'code':'heartbeat_stale','severity':'critical','component':'billing-worker'} in report['issues']
        again=ops.tick(directory)
        assert again['status']=='critical'
    finally:signal_worker('SIGCONT')
    wait_ok(directory)
    events=[json.loads(line) for line in (directory/'ops/events.jsonl').read_text().splitlines()]
    assert [e['event'] for e in events if e['component']=='billing-worker' and e['code']=='heartbeat_stale']==['raised','resolved']
    print('PASS: running but stalled worker detected by loop heartbeat; resume clears alert without duplicate events.',flush=True)

    keyfile=directory/'secrets/ops-test-key.json';generate_key(keyfile)
    ops.configure(directory,backup_enabled=True,backup_key_file=str(keyfile),check_interval=5)
    report=ops.tick(directory)
    assert report['status']=='ok'
    archive=Path(report['backup']['last_archive']);verify_archive(archive,read_key(keyfile))
    prior=list(archive.parent.glob('*.tnbk'))
    assert ops.tick(directory)['status']=='ok' and list(archive.parent.glob('*.tnbk'))==prior
    # Missing newest archive is an actionable failure, not an implicit new snapshot/reset.
    archive.rename(archive.with_suffix('.retained'))
    try:assert any(i['code']=='backup_missing' for i in ops.tick(directory)['issues'])
    finally:archive.with_suffix('.retained').rename(archive)
    wait_ok(directory)
    previous=ops.read_json(directory/'ops/status.json')['checked_at']
    first=ops.start(directory)
    try:
        assert ops.start(directory)['pid']==first['pid'] and ops.watcher(directory)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            current=ops.status(directory)
            if current['report_fresh'] and current['report']['checked_at']>previous:break
            time.sleep(.2)
        else:raise AssertionError('Watcher produced no report')
    finally:
        assert ops.stop(directory)
        deadline=time.monotonic()+20
        while ops.watcher(directory) and time.monotonic()<deadline:time.sleep(.1)
        assert not ops.watcher(directory)
    # Pause a real service; no automatic restart or unpause is performed by the monitor.
    container=compose(directory,'ps','--quiet','reminder-worker',capture=True).stdout.strip()
    docker('pause',container,capture=True)
    try:assert any(i['code']=='service_paused' and i['component']=='reminder-worker' for i in ops.tick(directory)['issues'])
    finally:docker('unpause',container,capture=True)
    wait_ok(directory)
    # Reports and local event logs must never contain fixture credentials or encryption keys.
    public=(directory/'ops/status.json').read_bytes()+(directory/'ops/events.jsonl').read_bytes()
    for path in (directory/'secrets').iterdir():
        if path.is_file():assert path.read_bytes().strip() not in public
    print('PASS: periodic encrypted archive authenticated, no early duplicate, missing archive/paused service detected, private events and watcher start/idempotence/stop.',flush=True)


def verify_recovery(directory):
    report=wait_ok(directory)
    assert report['status']=='ok' and report['workers_held'] and not report['backup']['enabled']
    assert report['metrics']['billing_due']==1 and report['metrics']['billing_age']==0
    assert all(report['checks'][name]['status']=='held' for name in ops.WORKERS)
    assert not (directory/'ops/backup.json').exists()
    print('PASS: recovered stack reports held workers without starting them, querying only aggregate counters; backups remain opt-in.',flush=True)
