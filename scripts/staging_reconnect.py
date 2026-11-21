"""Reconnect a retained local staging project using its existing containers."""
import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts import staging_ops as ops
from scripts.staging_lab import compose, docker, manifest, operation_lock, require_data_volume, require_image, wait_ready
from scripts.staging_preflight import container_issues


class ReconnectRefused(RuntimeError):
    pass


def retained_services(directory, project):
    identities = compose(directory, 'ps', '--all', '--quiet', capture=True, timeout=15).stdout.split()
    if not identities or len(identities) != len(set(identities)) or any(not re.fullmatch('[a-f0-9]{64}', key) for key in identities):
        raise ReconnectRefused('Identitas container tersimpan tidak lengkap.')
    objects = json.loads(docker('inspect', *identities, capture=True, timeout=15).stdout)
    if len(objects) != len(identities) or {obj['Id'] for obj in objects} != set(identities):
        raise ReconnectRefused('Container berubah selama pemeriksaan.')
    services = {}
    for obj in objects:
        labels = obj['Config'].get('Labels', {})
        name = labels.get('com.docker.compose.service')
        if labels.get('com.docker.compose.project') != project or not name or name in services:
            raise ReconnectRefused('Identitas proyek atau service container tidak sesuai.')
        services[name] = obj
    return services


def wait_until(probe, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(.5)
    raise ReconnectRefused(message)


def database_healthy(identity):
    state = json.loads(docker('inspect', '--format', '{{json .State}}', identity, capture=True, timeout=10).stdout)
    return state['Running'] is True and state.get('Health', {}).get('Status') == 'healthy'


def start_stopped(services, names, started):
    pending = [name for name in names if not services[name]['State']['Running']]
    if pending:
        docker('start', *(services[name]['Id'] for name in pending), capture=True, timeout=60)
        started.extend(pending)


def reconnect(directory, mode='active', timeout=120):
    if mode not in {'active', 'recovery'} or type(timeout) is not int or not 1 <= timeout <= 120:
        raise ValueError('Mode atau timeout tidak valid.')
    directory = Path(directory).resolve()
    with operation_lock(directory):
        state = manifest(directory)
        if not state.get('initialized') or state.get('restore_state') in {'preparing', 'failed'} or not state.get('image_id'):
            raise ReconnectRefused('Proyek harus sudah diinisialisasi dan memiliki image yang dipin.')
        held = bool(state.get('recovery_pending'))
        if held != (mode == 'recovery'):
            raise ReconnectRefused('Mode yang dipilih tidak sesuai status pemulihan proyek.')
        if held and ops.watcher(directory):
            raise ReconnectRefused('Target pemulihan masih memiliki watcher aktif.')

        expected = ('db', 'web') if held else ('db', 'web', *ops.WORKERS)
        require_image(state['image_id'])
        require_data_volume(state['project'])
        services = retained_services(directory, state['project'])
        running = {name for name, item in services.items() if item['State']['Running']}
        if running - set(expected):
            raise ReconnectRefused('Ada service aktif di luar cakupan penyambungan ulang.')
        migration = services.get('migrate')
        if (migration is None or migration['Image'] != state['image_id']
                or migration['State'].get('Status') != 'exited' or migration['State'].get('ExitCode') != 0):
            raise ReconnectRefused('Bukti migrasi selesai untuk image yang dipin tidak tersedia.')
        for name in expected:
            if name not in services or container_issues(name, services[name], state, state['image_id'], allow_stopped=True):
                raise ReconnectRefused(f'Container {name} tersimpan hilang atau konfigurasinya tidak sesuai.')

        started = []
        start_stopped(services, ('db',), started)
        wait_until(lambda: database_healthy(services['db']['Id']), timeout, 'Database belum siap.')
        start_stopped(services, ('web',), started)
        wait_ready(directory, timeout=timeout)
        start_stopped(services, expected[2:], started)

        def readiness():
            report = ops.check(directory)
            return report if report['status'] in {'ok', 'warning'} else None

        report = wait_until(readiness, timeout, 'Status layanan atau heartbeat belum siap.')
        if not held and not ops.watcher(directory):
            ops.start(directory)
            wait_until(lambda: ops.watcher(directory), timeout, 'Watcher belum aktif.')
        return {'scope': 'local_simulation', 'project': state['project'], 'mode': mode,
                'status': report['status'], 'services_started': started, 'workers_held': held,
                'watcher_running': bool(ops.watcher(directory)), 'issues': report['issues']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / 'data/staging')
    parser.add_argument('--mode', choices=['active', 'recovery'], default='active')
    parser.add_argument('--timeout', type=int, default=120, help='Batas detik untuk setiap pemeriksaan kesiapan (1–120).')
    args = parser.parse_args()
    try:
        result = reconnect(args.directory, args.mode, args.timeout)
    except ReconnectRefused as error:
        parser.exit(1, f'Penyambungan ulang ditolak. {error}\n')
    except Exception as error:
        parser.exit(1, f'Penyambungan ulang gagal ({type(error).__name__}). Periksa image, volume, dan status layanan.\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
