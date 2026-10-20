import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

RESOURCES = Path(__file__).resolve().parents[1] / 'TelegramManager.app/Contents/Resources'
sys.path.insert(0, str(RESOURCES))
os.environ.setdefault('TG_SESSION_TOKEN', 'unit-test-token')
import remote_ayugram as remote
import server
import state


class RemoteAyuGramTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        patch = mock.patch.object(state, 'DATA_DIR', str(self.base))
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(remote.shutil, 'which', return_value='/synthetic/gcloud')
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(remote, '_start_master', return_value=None)
        patch.start(); self.addCleanup(patch.stop)
        self.config = dict(schema=1, project='test-project', zone='us-central1-a', instance='test-vm')
        self.path = self.base / 'remote_ayugram.json'
        self.write_config(self.config)
        self.fixture = dict(ok=True, schema=1, ready=True, vnc_port=5905, client_active=True,
                            client_state='active', disk_total_bytes=20_000_000_000, disk_free_bytes=13_000_000_000,
                            backup_configured=True, backup_active=False,
                            backup_last_result='success', backup_last_finished='today')
        for key in ('_tunnel', '_tunnel_config', '_local_port', '_master'):
            patch = mock.patch.object(remote, key, None)
            patch.start(); self.addCleanup(patch.stop)
        self.session = self.base / 'TelegramForcePortable/tdata/key_datas'
        self.session.parent.mkdir(parents=True)
        self.session.write_bytes(b'original synthetic session')

    def tearDown(self):
        self.assertEqual(self.session.read_bytes(), b'original synthetic session')

    def write_config(self, data):
        self.path.write_text(json.dumps(data))
        self.path.chmod(0o600)

    def test_unconfigured_never_contacts_cloud(self):
        self.path.unlink()
        with mock.patch.object(remote, '_remote') as network:
            for action in ('status', 'connect', 'backup'):
                self.assertFalse(remote.request(action)['ready'])
        network.assert_not_called()

    def test_invalid_config_cannot_supply_commands_or_hosts(self):
        for change in ({'instance':'--command=bad'}, {'project':'x; bad'}, {'zone':'us x'},
                       {'instance':'user@host'}, {'extra':'ignored'}, {'schema':True}, {'project':None}):
            with self.subTest(change=change), mock.patch.object(remote, '_remote') as network:
                self.write_config({**self.config, **change})
                self.assertFalse(remote.request()['success'])
                network.assert_not_called()

    def test_config_rejects_readable_symlink_hardlink_and_nonfile(self):
        self.path.chmod(0o644)
        self.assertFalse(remote.request()['success'])
        self.path.unlink()
        source = self.base / 'other'
        source.write_text(json.dumps(self.config)); source.chmod(0o600)
        self.path.symlink_to(source)
        self.assertFalse(remote.request()['success'])
        self.path.unlink(); os.link(source, self.path)
        self.assertFalse(remote.request()['success'])
        self.path.unlink(); self.path.mkdir()
        self.assertFalse(remote.request()['success'])

    def test_status_strips_remote_secrets_and_does_not_launch_viewer(self):
        with mock.patch.object(remote, '_remote', return_value={**self.fixture, 'password':'do-not-return'}) as call, \
                mock.patch.object(remote.subprocess, 'run') as launch:
            status = remote.request()
        self.assertTrue(status['ready'])
        self.assertNotIn('password', status)
        launch.assert_not_called()
        call.assert_called_once_with(self.config, 'status')

    def test_health_alerts_are_read_only_and_distinguish_unknown_states(self):
        cases = [({}, set()),
                 ({'client_state':'failed', 'client_active':False, 'ready':False}, {'client_stopped'}),
                 ({'client_state':'unknown', 'client_active':False, 'ready':False}, {'client_unknown'}),
                 ({'client_state':'activating', 'client_active':False, 'ready':False}, {'client_starting'}),
                 ({'backup_last_result':'exit-code'}, {'backup_failed'}),
                 ({'backup_last_result':'never'}, {'backup_never'}),
                 ({'backup_last_result':'unknown'}, {'backup_unknown'}),
                 ({'backup_active':True, 'backup_last_result':'unknown'}, set()),
                 ({'backup_configured':False}, {'backup_unconfigured'}),
                 ({'disk_free_bytes':0}, {'disk_low'}),
                 ({'disk_free_bytes':1_999_999_999}, {'disk_low'}),
                 ({'disk_free_bytes':2_000_000_000}, set()),
                 ({'disk_total_bytes':100_000_000_000, 'disk_free_bytes':9_000_000_000}, {'disk_low'}),
                 ({'disk_free_bytes':None}, {'disk_unknown'}),
                 ({'viewer_ready':False}, {'runtime_not_ready'})]
        for change, expected in cases:
            with self.subTest(change=change), \
                    mock.patch.object(remote, '_remote', return_value={**self.fixture, **change}) as cloud, \
                    mock.patch.object(remote, '_connect') as connect:
                status = remote.request()
                self.assertEqual({a['code'] for a in status['alerts']}, expected)
                cloud.assert_called_once_with(self.config, 'status')
                connect.assert_not_called()

    def test_invalid_disk_telemetry_never_becomes_healthy_storage(self):
        for free, total in ((True, 10), ('5', 10), (-1, 10), (11, 10), (0, 0), (1, 2**53)):
            with self.subTest(free=free, total=total), mock.patch.object(remote, '_remote', return_value={
                    **self.fixture, 'disk_free_bytes':free, 'disk_total_bytes':total}):
                status = remote.request()
                self.assertIsNone(status['disk_free_bytes'])
                self.assertIsNone(status['disk_total_bytes'])
                self.assertIn('disk_unknown', [a['code'] for a in status['alerts']])

    def test_unready_remote_or_wrong_port_cannot_launch(self):
        for change in ({'ready':False}, {'vnc_port':22}):
            with mock.patch.object(remote, '_remote', return_value={**self.fixture, **change}), \
                    mock.patch.object(remote, '_connect') as connect:
                self.assertFalse(remote.request('connect')['success'])
                connect.assert_not_called()

    def test_connect_opens_loopback_without_credentials_or_local_profile(self):
        with mock.patch.object(remote, '_remote', return_value=self.fixture), \
                mock.patch.object(remote, '_connect', return_value=54321), \
                mock.patch.object(remote.subprocess, 'run', return_value=mock.Mock(returncode=0)) as launch:
            self.assertTrue(remote.request('connect')['connected'])
        self.assertEqual(launch.call_args.args[0], ['/usr/bin/open', 'vnc://127.0.0.1:54321'])

    def test_first_launch_keeps_viewer_access_and_warns_against_login(self):
        fixture = {**self.fixture, 'ready':True, 'viewer_ready':True, 'needs_initialization':True}
        with mock.patch.object(remote, '_remote', return_value=fixture), \
                mock.patch.object(remote, '_connect', return_value=54321) as connect, \
                mock.patch.object(remote.subprocess, 'run', return_value=mock.Mock(returncode=0)):
            status = remote.request('status')
            self.assertFalse(status['ready'])
            self.assertTrue(status['needs_initialization'])
            self.assertTrue(status['viewer_ready'])
            self.assertIn('do not sign in', status['message'])
            connect.assert_not_called()
            opened = remote.request('connect')
            self.assertTrue(opened['success'])
            self.assertTrue(opened['connected'])
            self.assertFalse(opened['ready'])
            self.assertIn('do not sign in', opened['message'])
            self.assertIn('Quit AyuGram normally', opened['message'])
            self.assertIn('Refresh before signing in', opened['message'])
            connect.assert_called_once_with(self.config)

    def test_failed_viewer_closes_tunnel(self):
        with mock.patch.object(remote, '_remote', return_value=self.fixture), \
                mock.patch.object(remote, '_connect', return_value=54321), \
                mock.patch.object(remote, '_disconnect') as disconnect, \
                mock.patch.object(remote.subprocess, 'run', return_value=mock.Mock(returncode=1)):
            self.assertFalse(remote.request('connect')['success'])
        disconnect.assert_called_once()

    def test_backup_queue_not_reported_as_completed(self):
        with mock.patch.object(remote, '_remote', return_value=dict(ok=True,schema=1,queued=True)) as call:
            result = remote.request('backup')
        self.assertTrue(result['success'])
        self.assertIn('queued', result['message'])
        call.assert_called_once_with(self.config, 'backup')

    def test_ssh_uses_fixed_helper_strict_host_key_and_noninteractive_auth(self):
        process = mock.Mock(returncode=0)
        process.communicate.return_value = (json.dumps(self.fixture), None)
        with mock.patch.object(remote.subprocess, 'Popen', return_value=process) as launch:
            result = remote._remote(self.config, 'status')
        self.assertTrue(result['ok'])
        args = launch.call_args.args[0]
        self.assertIn('--strict-host-key-checking=yes', args)
        self.assertIn('--command=sudo -n /usr/local/sbin/ayugram-remote-control status', args)
        self.assertIn('BatchMode=yes', args)
        self.assertTrue(launch.call_args.kwargs['start_new_session'])

    def test_ssh_failure_and_bad_output_never_echo_remote_content(self):
        for code, output in ((1, 'secret-token'), (0, 'secret-token'), (0, '[]'),
                             (0, json.dumps({'ok':True,'schema':2})), (0, 'x'*16385)):
            process = mock.Mock(returncode=code)
            process.communicate.return_value = (output, None)
            with self.subTest(code=code, size=len(output)), \
                    mock.patch.object(remote.subprocess, 'Popen', return_value=process):
                result = remote.request()
                self.assertFalse(result['success'])
                self.assertNotIn('secret-token', result['message'])

    def test_timed_out_ssh_stops_only_owned_process_group(self):
        process = mock.Mock(pid=999999, returncode=None)
        process.poll.return_value = None
        process.communicate.side_effect = subprocess.TimeoutExpired('gcloud',30)
        with mock.patch.object(remote.subprocess, 'Popen', return_value=process), \
                mock.patch.object(remote.os, 'killpg') as stop:
            self.assertFalse(remote.request()['success'])
        stop.assert_called_once_with(999999, signal.SIGTERM)

    def test_tunnel_is_reused_and_disconnect_does_not_stop_vps(self):
        process = mock.Mock(pid=999999)
        process.poll.return_value = None
        with mock.patch.object(remote.subprocess, 'Popen', return_value=process) as launch, \
                mock.patch.object(remote, '_rfb_ready', return_value=True), \
                mock.patch.object(remote, '_owns_listener', return_value=True), \
                mock.patch.object(remote.os, 'killpg') as stop:
            port = remote._connect(self.config)
            self.assertEqual(remote._connect(self.config), port)
            launch.assert_called_once()
            args = launch.call_args.args[0]
            self.assertIn(f'127.0.0.1:{port}:127.0.0.1:5905', args)
            self.assertIn('ExitOnForwardFailure=yes', args)
            self.assertTrue(remote.request('disconnect')['success'])
            self.assertIsNone(remote._tunnel)
        stop.assert_called_once_with(999999, signal.SIGTERM)

    def test_vnc_probe_accepts_delayed_banner_but_rejects_other_protocols(self):
        for banner, expected in ((b'RFB 003.008\n', True), (b'HTTP/1.1 200', False)):
            with self.subTest(banner=banner), socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                listener.listen(1)
                listener.settimeout(5)

                def serve():
                    with listener.accept()[0] as connection:
                        threading.Event().wait(0.75)
                        connection.sendall(banner)

                thread = threading.Thread(target=serve, daemon=True)
                thread.start()
                try:
                    self.assertEqual(remote._rfb_ready(listener.getsockname()[1]), expected)
                finally:
                    thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

    def test_port_collision_rejects_listener_outside_owned_ssh_group(self):
        process = mock.Mock(pid=400)
        with mock.patch.object(remote.subprocess, 'run', return_value=mock.Mock(stdout='p500\n')), \
                mock.patch.object(remote.os, 'getpgid', return_value=600):
            self.assertFalse(remote._owns_listener(process, 5905))
        with mock.patch.object(remote.subprocess, 'run', return_value=mock.Mock(stdout='p500\n')), \
                mock.patch.object(remote.os, 'getpgid', return_value=400):
            self.assertTrue(remote._owns_listener(process, 5905))

    def test_remote_routes_require_manager_token_and_unlock(self):
        httpd = server.ThreadedHTTPServer(('127.0.0.1', 0), server.RequestHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        with mock.patch.dict(state.config, {'lock_password_hash':'enabled','lock_password_salt':'salt'}), \
                mock.patch.object(server, '_lock_unlocked_at', 0.0), \
                mock.patch.object(remote, '_remote') as cloud:
            for action in ('status','connect','disconnect','backup'):
                method = 'GET' if action == 'status' else 'POST'
                for prefix, expected in (('',404), (server.ROUTE_PREFIX,423)):
                    conn = http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=5)
                    try:
                        conn.request(method,prefix+'/api/remote-ayugram/'+action,
                                     body='{}' if method == 'POST' else None)
                        response = conn.getresponse()
                        response.read()
                        self.assertEqual(response.status,expected)
                    finally:
                        conn.close()
            cloud.assert_not_called()

    def test_tunnel_supervisor_exits_when_manager_pipe_closes(self):
        with subprocess.Popen([sys.executable, str(RESOURCES/'remote_ayugram.py'), '--tunnel',
                               sys.executable, '-c', 'import time; time.sleep(60)'],
                              stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, start_new_session=True) as process:
            self.addCleanup(remote._stop, process)
            process.stdin.close()
            process.wait(timeout=5)
            self.assertEqual(process.returncode, -signal.SIGTERM, process.stderr.read())

    def test_health_poll_does_not_extend_password_unlock_and_still_expires(self):
        httpd = server.ThreadedHTTPServer(('127.0.0.1', 0), server.RequestHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        before = server.time.monotonic() - 30
        with mock.patch.dict(state.config, {'lock_password_hash':'enabled', 'lock_timeout_minutes':5}), \
                mock.patch.object(server, '_lock_unlocked_at', before), \
                mock.patch.object(server, '_lock_last_activity', before), \
                mock.patch.object(remote, '_remote', return_value=self.fixture) as cloud:
            for expected in (200, 423):
                conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=5)
                try:
                    conn.request('GET', server.ROUTE_PREFIX + '/api/remote-ayugram/status')
                    response = conn.getresponse(); response.read()
                    self.assertEqual(response.status, expected)
                finally:
                    conn.close()
                if expected == 200:
                    self.assertEqual(server._lock_last_activity, before)
                    server._lock_last_activity = before - 400
            cloud.assert_called_once_with(self.config, 'status')

    def test_frontend_unconfigured_and_failure_disable_remote_actions(self):
        script = r'''
const fs = require('fs'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
const elements = new Map(), intervals = [], windowEvents = {}, documentEvents = {}, calls = [];
let now = 1700000000000;
Date.now = () => now;
const document = {hidden:false, addEventListener:(name,fn) => documentEvents[name] = fn,
  getElementById: id => {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {style:{}, focus:() => {}, classList:{contains:x => classes.has(x), add:x => classes.add(x), remove:x => classes.delete(x)}});
    }
    return elements.get(id);
  }};
const window = {addEventListener:(name,fn) => windowEvents[name] = fn};
const setInterval = (fn,ms) => { intervals.push({fn,ms}); return intervals.length; };
const flush = () => new Promise(resolve => setImmediate(resolve));
const element = id => document.getElementById('remote-ayugram-' + id);
let response = {success:true,configured:false,ready:false,message:'Not deployed'}, pending = null;
const api = async (path,method) => { calls.push({path,method}); if (pending) await pending; return response; };
const healthy = {success:true,configured:true,ready:true,viewer_ready:true,connected:true,instance:'test-vm',
  client_active:true,client_state:'active',backup_active:false,backup_last_result:'success',backup_last_finished:'today',
  disk_free_bytes:13000000000,disk_total_bytes:20000000000,alerts:[],message:'Ready'};
eval(source.slice(source.indexOf('let remoteAyuGramBusy'), source.indexOf('let clientReleaseRows')) + `
(async () => {
  startRemoteAyuGramMonitor(); await flush();
  assert.equal(calls.length, 1);
  assert.equal(intervals.length, 1);
  assert.equal(intervals[0].ms, 300000);
  assert.equal(element('connect').disabled, true);
  assert.equal(element('backup').disabled, true);
  assert.equal(element('notice').style.display, 'none');
  document.getElementById('lock-screen').classList.add('visible');
  now += 300001; await intervals[0].fn();
  assert.equal(calls.length, 1, 'Locked polling must not contact the VPS');
  document.getElementById('lock-screen').classList.remove('visible');
  response = healthy;
  startRemoteAyuGramMonitor(); await flush();
  assert.equal(intervals.length, 1, 'Unlock must not duplicate timers');
  assert.equal(element('button').textContent, 'Remote AyuGram · Running');
  assert.equal(element('connect').disabled, false);
  assert(element('status').textContent.includes('13.00 GB free'));
  let previous = calls.length;
  await windowEvents.focus();
  assert.equal(calls.length, previous, 'Focus before next check must not poll');
  now += 300001; await windowEvents.focus();
  assert.equal(calls.length, previous + 1, 'Return after sleep must catch up');
  const issue = '<img src=x onerror=bad()> VPS storage is low';
  response = {...healthy, alerts:[{code:'disk_low',message:issue}],disk_free_bytes:1000000000};
  await remoteAyuGramAction('status');
  assert.equal(element('notice').style.display, 'flex');
  assert.equal(element('notice-text').textContent, issue);
  assert.equal(element('notice-text').innerHTML, undefined, 'Remote text must not be rendered as HTML');
  hideRemoteAyuGram();
  assert.equal(element('notice').style.display, 'flex', 'Closing modal must not dismiss health alerts');
  await remoteAyuGramAction('status');
  assert.equal(element('notice-text').textContent, issue, 'Repeated checks retain one notice');
  response = healthy; await remoteAyuGramAction('status');
  assert.equal(element('notice').style.display, 'none', 'Recovery clears alerts');
  response = {success:false,message:'VPS connection timed out.'};
  await remoteAyuGramAction('status');
  assert.equal(element('button').textContent, 'Remote AyuGram · Unavailable');
  assert(element('status').textContent.includes('Last confirmed status'));
  assert(element('notice-text').textContent.includes('status unavailable'));
  assert.equal(element('connect').disabled, true);
  assert.equal(element('backup').disabled, true);
  assert.equal(element('disconnect').disabled, false, 'Existing viewer can still be disconnected');
  response = healthy; await remoteAyuGramAction('status');
  now += 600001; renderRemoteAyuGram();
  assert.equal(element('button').textContent, 'Remote AyuGram · Stale');
  assert.equal(element('connect').disabled, true);
  await intervals[0].fn();
  assert.equal(element('button').textContent, 'Remote AyuGram · Running');
  let release;
  pending = new Promise(resolve => release = resolve);
  const inFlight = remoteAyuGramAction('status');
  previous = calls.length;
  await pollRemoteAyuGram(true);
  assert.equal(calls.length, previous, 'Health requests must not overlap');
  release(); await inFlight; pending = null;
  response = {...healthy,ready:false,needs_initialization:true};
  await remoteAyuGramAction('status');
  assert.equal(element('connect').disabled, false, 'First-run Quit must stay accessible');
  response = {success:true,message:'Backup queued'};
  await remoteAyuGramAction('backup');
  assert.equal(element('backup').disabled, true);
  assert(element('status').textContent.includes('Backup: running'));
  response = {...healthy,backup_last_result:'exit-code',alerts:[{code:'backup_failed',message:'The last backup failed.'}]};
  await intervals[0].fn();
  assert.equal(element('button').textContent, 'Remote AyuGram · Needs attention');
  assert(element('notice-text').textContent.includes('backup failed'));
  assert(calls.filter(c => c.path.endsWith('/status')).every(c => c.method === 'GET'));
  assert(!calls.some(c => c.path.endsWith('/connect')), 'Monitoring must never open a viewer');
})().catch(error => {console.error(error); process.exitCode = 1});`);
'''
        result = subprocess.run(['node','-e',script,str(RESOURCES/'index.html')],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)


class SSHMasterTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(schema=1, project='test-project', zone='us-central1-a', instance='test-vm')
        self.base = ['synthetic-gcloud', 'compute', 'ssh', 'test-vm']
        patch = mock.patch.object(remote, '_master', None)
        patch.start(); self.addCleanup(patch.stop)

    def test_unready_master_falls_back_and_backup_never_uses_pool(self):
        with mock.patch.object(remote, '_ssh', return_value=self.base) as discover, \
                mock.patch.object(remote, '_start_master', return_value=None) as start:
            with remote._request_command(self.config, 'status') as command:
                self.assertIn('ControlPath=none', command)
            with remote._request_command(self.config, 'backup') as command:
                self.assertIn('ControlPath=none', command)
        self.assertEqual(discover.call_count, 2)
        start.assert_called_once_with(self.base)

    def test_reuse_is_leased_and_expiry_never_interrupts_an_active_request(self):
        with tempfile.TemporaryDirectory(prefix='tma-', dir='/tmp') as directory:
            listener = socket.socket(socket.AF_UNIX)
            self.addCleanup(listener.close)
            listener.bind(directory + '/synthetic')
            process = mock.Mock()
            process.poll.return_value = None
            info = os.lstat(directory)
            master = dict(process=process, directory=directory, identity=(info.st_dev, info.st_ino), command=self.base,
                          created=remote.time.monotonic(), leases=0)
            with mock.patch.object(remote, '_ssh', return_value=self.base), \
                    mock.patch.object(remote, '_start_master', return_value=master) as start, \
                    mock.patch.object(remote, '_close_master') as close:
                with remote._request_command(self.config, 'history-export') as first:
                    self.assertIn('ControlPath=' + directory + '/%k-%C', first)
                    master['created'] -= 301
                    with remote._request_command(self.config, 'status') as second:
                        self.assertIn('ControlPath=none', second)
                    close.assert_not_called()
                    self.assertEqual(master['leases'], 1)
                with remote._request_command(self.config, 'status'):
                    pass
                close.assert_called_once_with(master)
                self.assertEqual(start.call_count, 2)
                self.assertEqual(master['leases'], 0)

    def test_failed_warmup_does_not_replay_a_remote_command(self):
        process = mock.Mock(returncode=1)
        process.communicate.return_value = ('', None)
        with mock.patch.object(remote, '_ssh', return_value=self.base), \
                mock.patch.object(remote, '_start_master', side_effect=OSError('unavailable')), \
                mock.patch.object(remote.subprocess, 'Popen', return_value=process) as launch:
            with self.assertRaises(ValueError):
                remote._remote(self.config, 'status')
        launch.assert_called_once()
        self.assertIn('ControlPath=none', launch.call_args.args[0])

    def test_timed_out_leased_request_releases_lease_without_stopping_master_or_replay(self):
        master_process = mock.Mock(); master_process.poll.return_value = None
        master = dict(process=master_process, directory='/tmp/synthetic', command=self.base,
                      created=remote.time.monotonic(), leases=0)
        process = mock.Mock()
        process.communicate.side_effect = subprocess.TimeoutExpired('synthetic-gcloud', 30)
        with mock.patch.object(remote, '_ssh', return_value=self.base), \
                mock.patch.object(remote, '_start_master', return_value=master), \
                mock.patch.object(remote, '_master_ready', return_value=True), \
                mock.patch.object(remote.subprocess, 'Popen', return_value=process) as launch, \
                mock.patch.object(remote, '_stop') as stop:
            with self.assertRaises(ValueError):
                remote._remote(self.config, 'status')
        launch.assert_called_once()
        stop.assert_called_once_with(process)
        self.assertEqual(master['leases'], 0)
        master_process.terminate.assert_not_called()
        self.assertIs(remote._master, master)

    def test_master_options_are_private_foreground_and_noninteractive(self):
        process = mock.Mock()
        with mock.patch.object(remote.subprocess, 'Popen', return_value=process) as launch:
            master = remote._start_master(self.base)
        self.addCleanup(remote._remove_master_directory, master['directory'], master['identity'])
        self.assertEqual(os.stat(master['directory']).st_mode & 0o777, 0o700)
        command = launch.call_args.args[0]
        for option in ('-M', '-N', 'ControlPersist=no', 'BatchMode=yes', 'ForwardAgent=no',
                       'ClearAllForwardings=yes'):
            self.assertIn(option, command)
        self.assertIn('ControlPath=' + master['directory'] + '/%k-%C', command)
        self.assertNotIn('ControlMaster=no', command)
        self.assertTrue(launch.call_args.kwargs['start_new_session'])

    def test_replaced_or_public_master_directory_is_never_reused_or_removed(self):
        with tempfile.TemporaryDirectory(prefix='tma-', dir='/tmp') as root:
            directory = Path(root) / 'socket'; directory.mkdir(mode=0o700)
            info = directory.stat(); identity = (info.st_dev, info.st_ino)
            (directory/'keep').write_text('preserved')
            directory.chmod(0o755)
            with self.assertRaises(OSError):
                remote._master_directory(str(directory), identity)
            remote._remove_master_directory(str(directory), identity)
            self.assertTrue((directory/'keep').exists())
            directory.chmod(0o700)
            directory.rename(Path(root)/'original')
            directory.symlink_to(Path(root)/'original', target_is_directory=True)
            with self.assertRaises(OSError):
                remote._master_directory(str(directory), identity)
            remote._remove_master_directory(str(directory), identity)
            self.assertEqual((directory/'keep').read_text(), 'preserved')

    def test_changed_target_and_dead_master_fall_back_while_a_request_is_leased(self):
        with tempfile.TemporaryDirectory(prefix='tma-', dir='/tmp') as directory:
            listener = socket.socket(socket.AF_UNIX); self.addCleanup(listener.close)
            listener.bind(directory + '/synthetic')
            info = os.stat(directory)
            process = mock.Mock(); process.poll.return_value = None
            master = dict(process=process, directory=directory, identity=(info.st_dev, info.st_ino),
                          command=self.base, created=remote.time.monotonic(), leases=0)
            with mock.patch.object(remote, '_ssh', return_value=self.base) as discover, \
                    mock.patch.object(remote, '_start_master', return_value=master) as start, \
                    mock.patch.object(remote, '_close_master') as close:
                with remote._request_command(self.config, 'history-export'):
                    discover.return_value = self.base + ['changed-target']
                    with remote._request_command(self.config, 'status') as command:
                        self.assertIn('ControlPath=none', command)
                        self.assertIn('changed-target', command)
                    process.poll.return_value = 1
                    discover.return_value = self.base
                    with remote._request_command(self.config, 'status') as command:
                        self.assertIn('ControlPath=none', command)
                    close.assert_not_called()
                with remote._request_command(self.config, 'status'):
                    pass
                close.assert_called_once_with(master)
                self.assertEqual(start.call_count, 2)

    def test_manager_signal_closes_master_pipe_and_removes_socket_directory(self):
        for signum in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signum=signum):
                directory = tempfile.mkdtemp(prefix='tma-', dir='/tmp')
                info = os.stat(directory); identity = (info.st_dev, info.st_ino)
                self.addCleanup(remote._remove_master_directory, directory, identity)
                child = Path(directory)/'child.pid'
                script = ('import os,time; from pathlib import Path; '
                          'Path(' + repr(str(child)) + ').write_text(str(os.getpid())); time.sleep(60)')
                command = [sys.executable, str(RESOURCES/'remote_ayugram.py'), '--master', directory,
                           *map(str, identity), sys.executable, '-c', script]
                parent_script = ('import subprocess,time; p=subprocess.Popen(' + repr(command)
                                 + ',stdin=subprocess.PIPE); time.sleep(60)')
                parent = subprocess.Popen([sys.executable, '-c', parent_script],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                          start_new_session=True)
                self.addCleanup(remote._stop, parent)
                deadline = remote.time.monotonic() + 5
                while not child.exists() and parent.poll() is None and remote.time.monotonic() < deadline:
                    threading.Event().wait(0.02)
                self.assertTrue(child.exists())
                pid = int(child.read_text())
                parent.send_signal(signum); parent.wait(timeout=5)
                error = parent.stderr.read(); parent.stderr.close()
                deadline = remote.time.monotonic() + 5
                while Path(directory).exists() and remote.time.monotonic() < deadline:
                    threading.Event().wait(0.02)
                self.assertFalse(Path(directory).exists(), error)
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)

    def test_master_supervisor_cleans_child_and_directory_on_manager_eof(self):
        directory = tempfile.mkdtemp(prefix='tma-', dir='/tmp')
        child = Path(directory) / 'child.pid'
        script = ('import os,time; from pathlib import Path; '
                  'Path(' + repr(str(child)) + ').write_text(str(os.getpid())); time.sleep(60)')
        process = subprocess.Popen([sys.executable, str(RESOURCES/'remote_ayugram.py'),
                                    '--master', directory, str(os.stat(directory).st_dev),
                                    str(os.stat(directory).st_ino), sys.executable, '-c', script],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, start_new_session=True)
        self.addCleanup(remote._stop, process)
        deadline = remote.time.monotonic() + 5
        while not child.exists() and process.poll() is None and remote.time.monotonic() < deadline:
            threading.Event().wait(0.02)
        self.assertTrue(child.exists())
        pid = int(child.read_text())
        process.stdin.close()
        process.wait(timeout=5)
        error = process.stderr.read(); process.stderr.close()
        self.assertEqual(process.returncode, 0, error)
        self.assertFalse(Path(directory).exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


if __name__ == '__main__':
    unittest.main()
