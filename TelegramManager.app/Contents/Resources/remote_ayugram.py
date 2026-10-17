"""One isolated VPS desktop, accessed through an authenticated SSH tunnel."""

import atexit
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
import time

import state

VNC_PORT = 5905
CONTROL = '/usr/local/sbin/ayugram-remote-control'
_lock = threading.RLock()
_tunnel = None
_tunnel_config = None
_local_port = None
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


def _remote(config, action):
    if action not in ('status', 'backup'):
        raise ValueError('Unsupported remote action.')
    command = _ssh(config) + ['--command=sudo -n ' + CONTROL + ' ' + action,
                              '--', *_SSH_OPTIONS]
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


if __name__ == '__main__' and sys.argv[1:2] == ['--tunnel']:
    _supervise_tunnel(sys.argv[2:])
else:
    atexit.register(_disconnect)
