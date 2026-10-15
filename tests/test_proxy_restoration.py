import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'TelegramManager.app/Contents/Resources'))
import proxy
import state


class ProxyRestorationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target, value in [('DATA_DIR', str(self.root))]:
            patch = mock.patch.object(state, target, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.patch = mock.patch.object(proxy, 'get_active_network_service', return_value='Wi-Fi')
        self.patch.start()
        self.addCleanup(self.patch.stop)
        original = tempfile.mkstemp
        patch = mock.patch('tempfile.mkstemp', side_effect=lambda **kwargs: original(dir=self.root, **kwargs))
        patch.start()
        self.addCleanup(patch.stop)
        self.states = {
            '-getwebproxy': 'Enabled: Yes\nServer: http-original.example\nPort: 8080\n',
            '-getsecurewebproxy': 'Enabled: No\nServer: https-original.example\nPort: 8443\n',
            '-getsocksfirewallproxy': 'Enabled: No\nServer: socks-original.example\nPort: 1080\n',
        }

    def query(self, args, **kwargs):
        return subprocess.CompletedProcess(args, 0, self.states[args[1]], '')

    def apply(self, result=0):
        with mock.patch.object(proxy.subprocess, 'run', side_effect=self.query), \
             mock.patch.object(proxy.subprocess, 'Popen'), \
             mock.patch.object(state, '_run_as_admin', return_value=mock.Mock(returncode=result)) as admin:
            proxy.apply_proxy({'type': 'http', 'host': 'temporary.example', 'port': 8090})
        return admin

    def test_http_and_https_restore_their_own_host_and_enabled_state(self):
        self.apply()
        script = next(self.root.glob('tm_proxy_restore_cmd_*')).read_text()
        self.assertIn('-setwebproxy Wi-Fi http-original.example 8080', script)
        self.assertIn('-setwebproxystate Wi-Fi on', script)
        self.assertIn('-setsecurewebproxy Wi-Fi https-original.example 8443', script)
        self.assertIn('-setsecurewebproxystate Wi-Fi off', script)
        record = json.loads(Path(proxy._proxy_recovery_path('Wi-Fi', 'http')).read_text())
        self.assertEqual(record['https']['host'], 'https-original.example')
        self.assertFalse(record['https']['enabled'])

    def test_second_apply_keeps_the_original_https_baseline(self):
        self.apply()
        self.states['-getsecurewebproxy'] = 'Enabled: Yes\nServer: temporary.example\nPort: 8090\n'
        self.apply()
        for script in self.root.glob('tm_proxy_restore_cmd_*'):
            self.assertIn('-setsecurewebproxy Wi-Fi https-original.example 8443', script.read_text())

    def test_unknown_https_state_aborts_before_system_change(self):
        self.states['-getsecurewebproxy'] = 'Could not read proxy settings'
        admin = self.apply()
        admin.assert_not_called()
        self.assertFalse((self.root / 'proxy_recovery').exists())

    def test_failed_apply_keeps_recovery_for_partial_system_change(self):
        self.apply(result=1)
        self.assertTrue(Path(proxy._proxy_recovery_path('Wi-Fi', 'http')).is_file())

    def test_startup_recovery_restores_both_channels_independently(self):
        self.apply()
        record = Path(proxy._proxy_recovery_path('Wi-Fi', 'http'))
        with mock.patch.object(state, '_run_as_admin', return_value=mock.Mock(returncode=0)) as admin:
            proxy._recover_stale_proxy(str(record))
        script = admin.call_args.args[0]
        self.assertIn('-setwebproxy Wi-Fi http-original.example 8080', script)
        self.assertIn('-setsecurewebproxy Wi-Fi https-original.example 8443', script)
        self.assertIn('-setsecurewebproxystate Wi-Fi off', script)
        self.assertFalse(record.exists())

    def test_legacy_recovery_does_not_invent_an_https_baseline(self):
        record = self.root / 'legacy.json'
        record.write_text(json.dumps({'service': 'Wi-Fi', 'proxy_type': 'http',
                                      'enabled': True, 'host': 'old.example', 'port': '8080'}))
        with mock.patch.object(state, '_run_as_admin', return_value=mock.Mock(returncode=0)) as admin:
            proxy._recover_stale_proxy(str(record))
        self.assertNotIn('securewebproxy', admin.call_args.args[0])
        self.assertTrue(record.exists())

    def test_authenticated_original_channel_aborts_before_system_change(self):
        for command in ('-getwebproxy', '-getsecurewebproxy'):
            with self.subTest(channel=command):
                original = self.states[command]
                self.states[command] += 'Authenticated Proxy Enabled: 1\n'
                admin = self.apply()
                admin.assert_not_called()
                self.assertFalse((self.root / 'proxy_recovery').exists())
                self.states[command] = original

    def test_authenticated_recovery_is_retained_without_invented_credentials(self):
        record = self.root / 'authenticated.json'
        record.write_text(json.dumps({'service': 'Wi-Fi', 'proxy_type': 'socks5',
                                      'enabled': True, 'host': 'old.example', 'port': '1080',
                                      'authenticated': True}))
        with mock.patch.object(state, '_run_as_admin') as admin:
            proxy._recover_stale_proxy(str(record))
        admin.assert_not_called()
        self.assertTrue(record.exists())

    def test_authenticated_pending_baseline_is_never_overwritten(self):
        record = Path(proxy._proxy_recovery_path('Wi-Fi', 'http'))
        record.parent.mkdir(parents=True)
        contents = json.dumps({'service': 'Wi-Fi', 'proxy_type': 'http', 'enabled': True,
                               'host': 'old.example', 'port': '8080', 'authenticated': True,
                               'https': {'enabled': False, 'host': '', 'port': '0'}})
        record.write_text(contents)
        admin = self.apply()
        admin.assert_not_called()
        self.assertEqual(record.read_text(), contents)


if __name__ == '__main__':
    unittest.main()
