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
        self.config = dict(schema=1, project='test-project', zone='us-central1-a', instance='test-vm')
        self.path = self.base / 'remote_ayugram.json'
        self.write_config(self.config)
        self.fixture = dict(ok=True, schema=1, ready=True, vnc_port=5905, client_active=True,
                            backup_active=False, backup_last_result='success', backup_last_finished='today')
        for key in ('_tunnel', '_tunnel_config', '_local_port'):
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
                        conn.request(method,prefix+'/api/remote-ayugram/'+action,body='{}')
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

    def test_frontend_unconfigured_and_failure_disable_remote_actions(self):
        script = r'''
const fs = require('fs'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
const elements = new Map();
const document = {getElementById: id => {if (!elements.has(id)) elements.set(id, {}); return elements.get(id)}};
let response = {success:true,configured:false,ready:false,message:'Not deployed'};
const api = async () => response;
eval(source.slice(source.indexOf('let remoteAyuGramBusy'), source.indexOf('let clientReleaseRows')) + `
(async () => {
  await remoteAyuGramAction('status');
  assert.equal(document.getElementById('remote-ayugram-connect').disabled, true);
  assert.equal(document.getElementById('remote-ayugram-backup').disabled, true);
  response = {success:true,configured:true,ready:false,viewer_ready:true,needs_initialization:true};
  await remoteAyuGramAction('status');
  assert.equal(document.getElementById('remote-ayugram-connect').disabled, false);
  response = {success:true,configured:true,ready:true,viewer_ready:true,connected:true};
  await remoteAyuGramAction('status');
  assert.equal(document.getElementById('remote-ayugram-connect').disabled, false);
  response = {success:false,message:'Offline'};
  await remoteAyuGramAction('status');
  assert.equal(document.getElementById('remote-ayugram-connect').disabled, true);
  assert.equal(document.getElementById('remote-ayugram-disconnect').disabled, false);
})().catch(error => {console.error(error); process.exitCode = 1});`);
'''
        result = subprocess.run(['node','-e',script,str(RESOURCES/'index.html')],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__ == '__main__':
    unittest.main()
