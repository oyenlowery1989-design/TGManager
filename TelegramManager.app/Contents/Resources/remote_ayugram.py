"""One isolated VPS desktop, accessed through an authenticated SSH tunnel."""

import atexit
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import tempfile
import time

import state

VNC_PORT = 5905
CONTROL = '/usr/local/sbin/ayugram-remote-control'
_lock = threading.RLock()
_tunnel = None
_tunnel_config = None
_local_port = None
_master_lock = threading.Lock()
_master = None
_SSH_OPTIONS = ['-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
                '-o', 'ForwardAgent=no', '-o', 'ControlMaster=no', '-o', 'ControlPath=none']


def _config():
    path = Path(state.DATA_DIR) / 'remote_ayugram.json'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4096):
            raise ValueError('Remote connection file must be a private, regular file (mode 600).')
        config = json.load(stream)
    patterns = {'project': r'[a-z][a-z0-9-]{4,62}',
                'zone': r'[a-z]+-[a-z0-9]+[0-9]-[a-z]',
                'instance': r'[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?'}
    if (not isinstance(config, dict) or set(config) != {'schema', *patterns}
            or type(config['schema']) is not int or config['schema'] != 1
            or any(not isinstance(config[key], str) or not re.fullmatch(pattern, config[key])
                   for key, pattern in patterns.items())):
        raise ValueError('Remote connection file is invalid; finish VPS setup first.')
    return config


def _ssh(config):
    gcloud = next((p for p in ('/opt/homebrew/bin/gcloud', '/usr/local/bin/gcloud')
                   if os.path.isfile(p) and os.access(p, os.X_OK)), None) or shutil.which('gcloud')
    if not gcloud:
        raise ValueError('Install Google Cloud CLI and sign in before connecting to the VPS.')
    return [gcloud, 'compute', 'ssh', config['instance'],
            '--project=' + config['project'], '--zone=' + config['zone'],
            '--quiet', '--strict-host-key-checking=yes']


def _stop(process):
    # Only process groups created by this module can be stopped here.
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=3)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)


def _master_directory(directory, identity):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if ((info.st_dev, info.st_ino) != identity or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        os.close(fd)
        raise OSError('SSH socket directory was replaced or is not private.')
    return fd


def _remove_master_directory(directory, identity):
    try:
        fd = _master_directory(directory, identity)
        try:
            for name in os.listdir(fd):
                os.unlink(name, dir_fd=fd)
            info = os.lstat(directory)
            if (info.st_dev, info.st_ino) == identity:
                os.rmdir(directory)
        finally:
            os.close(fd)
    except OSError:
        pass  # Never follow or remove a replacement directory.


def _master_ready(master):
    if master['process'].poll() is not None:
        return False
    fd = _master_directory(master['directory'], master['identity'])
    try:
        return any(stat.S_ISSOCK(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode)
                   for name in os.listdir(fd))
    finally:
        os.close(fd)


def _close_master(master):
    process = master['process']
    if not process.stdin.closed:
        process.stdin.close()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.terminate()  # The supervisor handles SIGTERM and reaps its SSH group.
        process.wait(timeout=5)
    _remove_master_directory(master['directory'], master['identity'])


def _start_master(command):
    directory = tempfile.mkdtemp(prefix='tma-', dir='/tmp')
    info = os.lstat(directory)
    identity = (info.st_dev, info.st_ino)
    options = _SSH_OPTIONS[:-4] + ['-M', '-N', '-o', 'ControlPersist=no',
                                 '-o', 'ClearAllForwardings=yes',
                                 '-o', 'ControlPath=' + directory + '/%k-%C']
    try:
        process = subprocess.Popen([sys.executable, __file__, '--master', directory, *map(str, identity),
                                    *command, '--', *options],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        _remove_master_directory(directory, identity)
        raise
    return dict(process=process, directory=directory, identity=identity, command=command,
                created=time.monotonic(), leases=0)


@contextmanager
def _request_command(config, action):
    global _master
    base = _ssh(config)  # Keep gcloud discovery and target refresh on every request.
    options, lease = _SSH_OPTIONS, None
    if action != 'backup':
        with _master_lock:
            try:
                stale = (_master is not None and
                         (_master['command'] != base or _master['process'].poll() is not None
                          or time.monotonic() - _master['created'] >= 300))
                if stale and not _master['leases']:
                    old, _master = _master, None
                    _close_master(old)
                if _master is None:
                    _master = _start_master(base)
                if _master is not None and not stale:
                    if _master_ready(_master):
                        lease = _master
                        lease['leases'] += 1
                        options = _SSH_OPTIONS[:-2] + [
                            '-o', 'ControlPath=' + lease['directory'] + '/%k-%C']
            except (OSError, subprocess.SubprocessError):
                pass  # Warming the connection is optional; never replay a request.
    try:
        yield base + ['--command=sudo -n ' + CONTROL + ' ' + action, '--', *options]
    finally:
        if lease is not None:
            with _master_lock:
                lease['leases'] -= 1


def _shutdown_master():
    global _master
    with _master_lock:
        if _master is not None:
            old, _master = _master, None
            _close_master(old)


def _remote(config, action):
    if action not in ('status', 'backup'):
        raise ValueError('Unsupported remote action.')
    with _request_command(config, action) as command:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        try:
            output, _ = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            _stop(process)
            raise ValueError('VPS connection timed out. Check Google Cloud access and try again.') from None
        if process.returncode or len(output) > 16384:
            raise ValueError('VPS service is unavailable. Check SSH access and finish remote setup.')
        try:
            result = json.loads(output)
        except ValueError:
            raise ValueError('VPS returned an invalid service response.') from None
        if (not isinstance(result, dict) or result.get('ok') is not True
                or result.get('schema') != 1):
            raise ValueError('VPS service is not ready. Check the remote installation.')
        return result


def history_request(action, payload, expected_config=None):
    """Bounded JSON over the fixed SSH helper; no caller-supplied paths or commands."""
    if action not in ('history-catalog', 'history-export') or not isinstance(payload, dict):
        raise ValueError('Unsupported history action.')
    allowed = set() if action == 'history-catalog' else {'user_id', 'known_digest'}
    if set(payload) != allowed:
        raise ValueError('Invalid history request fields.')
    data = json.dumps(payload, ensure_ascii=True).encode('ascii')
    if len(data) > 1024:
        raise ValueError('History request exceeds its size limit.')
    config = _config()
    if config is None or (expected_config is not None and config != expected_config):
        raise ValueError('The VPS connection is missing or changed. Confirm the history binding again.')
    with _request_command(config, action) as command:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        limit = 256 * 1024 if action == 'history-catalog' else 32 * 1024 * 1024 + 4096
        deadline = time.monotonic() + 60
        output = bytearray()
        try:
            process.stdin.write(data)
            process.stdin.close()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('VPS history transfer timed out. Local history was preserved.')
                ready, _, _ = select.select([process.stdout], [], [], min(remaining, 1))
                if not ready:
                    continue
                block = os.read(process.stdout.fileno(), min(65536, limit + 1 - len(output)))
                if not block:
                    break
                output.extend(block)
                if len(output) > limit:
                    raise ValueError('VPS history exceeds the transfer limit. Nothing was imported.')
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if process.returncode:
                raise ValueError('VPS history is unavailable. Check the helper version, login and archive size.')
            result = json.loads(output)
            if (not isinstance(result, dict) or result.get('ok') is not True
                    or type(result.get('schema')) is not int or result['schema'] != 1
                    or not isinstance(result.get('archive'), dict)):
                raise ValueError('VPS history helper returned an unsupported response.')
            return result['archive']
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError('VPS history helper returned invalid JSON.') from None
        except subprocess.TimeoutExpired:
            raise ValueError('VPS history transfer timed out. Local history was preserved.') from None
        finally:
            _stop(process)
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()


def _disconnect():
    global _tunnel, _tunnel_config, _local_port
    _stop(_tunnel)
    _tunnel = _tunnel_config = _local_port = None


def _rfb_ready(port):
    try:
        # SSH forwarding and consecutive VNC probes can exceed one round trip.
        with socket.create_connection(('127.0.0.1', port), timeout=3) as connection:
            data = b''
            while len(data) < 12:
                part = connection.recv(12 - len(data))
                if not part:
                    break
                data += part
            return re.fullmatch(rb'RFB 003\.00[378]\n', data) is not None
    except OSError:
        return False


def _owns_listener(process, port):
    # An ephemeral port can be taken between reservation and SSH startup.
    try:
        result = subprocess.run(['/usr/sbin/lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-Fp'],
                                capture_output=True, text=True, timeout=3)
        owners = {int(line[1:]) for line in result.stdout.splitlines()
                  if line.startswith('p') and line[1:].isdigit()}
        return bool(owners) and all(os.getpgid(pid) == process.pid for pid in owners)
    except (OSError, subprocess.SubprocessError):
        return False


def _connect(config):
    global _tunnel, _tunnel_config, _local_port
    if (_tunnel is not None and _tunnel.poll() is None and _tunnel_config == config
            and _owns_listener(_tunnel, _local_port) and _rfb_ready(_local_port)):
        return _local_port
    _disconnect()
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    command = _ssh(config) + ['--', *_SSH_OPTIONS, '-N', '-o', 'ExitOnForwardFailure=yes',
                              '-L', f'127.0.0.1:{port}:127.0.0.1:{VNC_PORT}']
    # The small supervisor exits on pipe EOF, including a Manager crash/SIGKILL.
    _tunnel = subprocess.Popen([sys.executable, __file__, '--tunnel', *command],
                               stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    _tunnel_config, _local_port = config, port
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and _tunnel.poll() is None:
        if _owns_listener(_tunnel, port) and _rfb_ready(port):
            return port
        time.sleep(0.15)
    _disconnect()
    raise ValueError('Could not connect to the remote desktop. Check that the VPS services are running.')


def _health(remote):
    """Classify service telemetry; never infer Telegram login or message collection."""
    client = remote.get('client_state')
    if client is None:
        client = 'active' if remote.get('client_active') is True else 'unknown'
    if client not in ('active', 'reloading', 'inactive', 'failed', 'activating',
                       'deactivating', 'maintenance', 'refreshing'):
        client = 'unknown'
    total, free = remote.get('disk_total_bytes'), remote.get('disk_free_bytes')
    if not (type(total) is int and type(free) is int and 0 <= free <= total < 2**53 and total > 0):
        total = free = None
    alerts = []
    def alert(code, message):
        alerts.append({'code': code, 'message': message})
    if client in ('inactive', 'failed', 'deactivating', 'maintenance'):
        alert('client_stopped', 'AyuGram is not running on the VPS. Messages may be missed.')
    elif client == 'unknown':
        alert('client_unknown', 'The AyuGram process state could not be verified.')
    elif client in ('activating', 'refreshing'):
        alert('client_starting', 'AyuGram is starting on the VPS. Check again shortly.')
    if remote.get('backup_configured') is not True:
        alert('backup_unconfigured', 'Daily backups are not confirmed active. Check the backup timer and configuration.')
    if remote.get('backup_active') is not True:
        outcome = remote.get('backup_last_result')
        if outcome == 'never':
            alert('backup_never', 'No completed backup has been verified since the backup service started.')
        elif outcome in (None, '', 'unknown'):
            alert('backup_unknown', 'The last backup result could not be verified.')
        elif outcome != 'success':
            alert('backup_failed', 'The last backup failed. Check the VPS backup service before relying on recovery.')
    if free is None:
        alert('disk_unknown', 'VPS free space is unavailable. Check the remote status helper.')
    elif free < max(2_000_000_000, total * 0.10):
        alert('disk_low', f'VPS storage is low: {free / 1e9:.2f} GB free. Free space before history or backups stop saving.')
    if (client == 'active' and remote.get('backup_configured') is True
            and remote.get('viewer_ready', remote.get('ready')) is not True):
        alert('runtime_not_ready', 'The remote desktop or profile validation is not ready. Check the VPS services.')
    return dict(client_state=client, disk_total_bytes=total, disk_free_bytes=free,
                alerts=alerts)


def request(action='status'):
    """No client-supplied hosts, paths, account names, or shell commands."""
    with _lock:
        try:
            if action == 'disconnect':
                _disconnect()
                return {'success': True, 'message': 'Viewer disconnected. AyuGram keeps running on the VPS.'}
            if action not in ('status', 'connect', 'backup'):
                raise ValueError('Unsupported remote action.')
            config = _config()
            if config is None:
                return {'success': True, 'configured': False, 'ready': False,
                        'message': 'Remote AyuGram is not deployed yet. A verified Linux build and VPS setup are required.'}
            remote = _remote(config, 'backup' if action == 'backup' else 'status')
            if action == 'backup':
                if remote.get('queued') is not True:
                    raise ValueError('The remote backup was not queued.')
                return {'success': True, 'message': 'Remote backup queued. Refresh status to check the result.'}
            needs_initialization = remote.get('needs_initialization') is True
            viewer_ready = (remote.get('viewer_ready', remote.get('ready')) is True
                            and remote.get('vnc_port') == VNC_PORT)
            ready = remote.get('ready') is True and viewer_ready and not needs_initialization
            initialization_message = ('First launch: do not sign in. Quit AyuGram normally, wait for its '
                                      'automatic restart, then Refresh before signing in.')
            result = {'success': True, 'configured': True, 'ready': ready,
                      **_health(remote),
                      'viewer_ready': viewer_ready, 'needs_initialization': needs_initialization,
                      'instance': config['instance'], 'client_active': remote.get('client_active') is True,
                      'backup_active': remote.get('backup_active') is True,
                      'backup_last_result': str(remote.get('backup_last_result') or 'Not run')[:80],
                      'backup_last_finished': str(remote.get('backup_last_finished') or 'Not yet')[:80],
                      'connected': _tunnel is not None and _tunnel.poll() is None,
                      'message': initialization_message if viewer_ready and needs_initialization else
                                 'Remote desktop is ready. Sign in there yourself.' if ready else 'Remote desktop is not ready.'}
            if action == 'connect':
                if not viewer_ready:
                    raise ValueError('Remote desktop is not ready. Finish VPS setup first.')
                port = _connect(config)
                opened = subprocess.run(['/usr/bin/open', f'vnc://127.0.0.1:{port}'],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=10)
                if opened.returncode:
                    raise ValueError('Could not open macOS Screen Sharing.')
                message = 'Screen Sharing opened. Enter the VNC password set during VPS setup. '
                message += initialization_message if needs_initialization else 'Sign in to AyuGram there yourself.'
                result.update(connected=True, message=message)
            return result
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            if action == 'connect':
                _disconnect()
            message = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else 'Could not access Remote AyuGram. Check the private connection file and Google Cloud CLI.'
            return {'success': False, 'message': message}


def _supervise_tunnel(command):
    if not command or os.getpgrp() != os.getpid():
        raise ValueError('The tunnel supervisor requires an isolated process group.')
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL)
    try:
        while process.poll() is None:
            readable, _, _ = select.select([sys.stdin.buffer], [], [], 0.5)
            if readable and not os.read(sys.stdin.fileno(), 1):
                break
    finally:
        os.killpg(os.getpgrp(), signal.SIGTERM)


def _supervise_master(directory, identity, command):
    process = None
    stopping = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        os.close(_master_directory(directory, identity))
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True)
        while not stopping and process.poll() is None:
            readable, _, _ = select.select([sys.stdin.buffer], [], [], 0.5)
            if readable and not os.read(sys.stdin.fileno(), 1):
                break
    finally:
        # Reap before removing the directory; the master must never outlive Manager.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            if process is not None:
                # gcloud may exit before its SSH descendant; stop the owned group anyway.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=3)
        finally:
            _remove_master_directory(directory, identity)


if __name__ == '__main__' and sys.argv[1:2] == ['--tunnel']:
    _supervise_tunnel(sys.argv[2:])
elif __name__ == '__main__' and sys.argv[1:2] == ['--master']:
    _supervise_master(sys.argv[2], tuple(map(int, sys.argv[3:5])), sys.argv[5:])
else:
    atexit.register(_disconnect)
    atexit.register(_shutdown_master)
