"""Exercise the bounded stream using local child processes, never SSH."""
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'TelegramManager.app/Contents/Resources'))
import remote_ayugram as remote


class HistoryTransportTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(schema=1, project='test-project', zone='us-central1-a', instance='test-vm')
        patcher = mock.patch.object(remote, '_config', return_value=self.config)
        patcher.start(); self.addCleanup(patcher.stop)

    def run_child(self, code, action='history-catalog', payload=None):
        with mock.patch.object(remote, '_ssh', return_value=[sys.executable, '-c', code]) as command:
            result = remote.history_request(action, {} if payload is None else payload, expected_config=self.config)
        command.assert_called_once_with(self.config)
        return result

    def test_json_roundtrip_uses_fixed_command_and_stdin(self):
        script = "import sys,json; p=json.load(sys.stdin); print(json.dumps({'schema':1,'ok':True,'archive':p}))"
        payload = {'user_id':'123', 'known_digest':'a'*64}
        self.assertEqual(self.run_child(script, 'history-export', payload), payload)

    def test_unknown_actions_fields_and_changed_hosts_do_not_spawn(self):
        with mock.patch.object(remote.subprocess, 'Popen') as spawn:
            for action, payload in [('bad',{}),('history-catalog',{'path':'/etc/passwd'}),('history-export',{})]:
                with self.assertRaises(ValueError):
                    remote.history_request(action, payload)
            with self.assertRaises(ValueError):
                remote.history_request('history-catalog', {}, expected_config={**self.config,'instance':'other'})
        spawn.assert_not_called()

    def test_oversize_nonjson_invalid_envelope_and_nonzero_exit_fail(self):
        cases = ["import sys; sys.stdout.write('x'*262145)", "print('not json')",
                 "print('{}')", "print('{\"schema\":true,\"ok\":true,\"archive\":{}}')",
                 "import sys; print('{}'); sys.exit(1)"]
        for code in cases:
            with self.subTest(code=code), self.assertRaises(ValueError):
                self.run_child(code)

    def test_timeout_stops_only_created_process_group(self):
        with mock.patch.object(remote.time, 'monotonic', side_effect=[0, 61]), self.assertRaisesRegex(ValueError, 'timed out'):
            self.run_child('import time; time.sleep(5)')


if __name__ == '__main__':
    unittest.main()
