import json
import os
import shutil
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

RESOURCES = Path(__file__).resolve().parents[1] / 'TelegramManager.app/Contents/Resources'
sys.path.insert(0, str(RESOURCES))
os.environ.setdefault('TG_SESSION_TOKEN', 'unit-test-token')
import server


class StorageReportingTests(unittest.TestCase):
    def handler(self):
        handler = server.RequestHandler.__new__(server.RequestHandler)
        handler.send_json = mock.Mock()
        return handler

    def test_stats_distinguish_logical_files_from_volume_free_space(self):
        handler = self.handler()
        with mock.patch.object(server, 'scan_accounts_cached', return_value=[]), \
             mock.patch.object(server, 'get_disk_stats', return_value={'tg_size': 100, 'backup_size': 200, 'master_size': 300}), \
             mock.patch.object(server.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=900)):
            handler._get_api_stats()
        data = handler.send_json.call_args.args[0]
        self.assertEqual(data['grand_total'], 600)
        self.assertEqual(data['size_kind'], 'logical')
        self.assertEqual(data['volume_free_human'], '900.0 B')

    def test_bulk_removal_preserves_unsafe_apps_and_measures_disk_change(self):
        handler = self.handler()
        with mock.patch.object(server, 'get_shared_app', return_value='/shared'), \
             mock.patch.object(server, 'scan_accounts', return_value=[{'path': '/account'}]), \
             mock.patch.object(server, 'find_account_app', return_value='/telegram.app'), \
             mock.patch.object(server.ayugram, 'find_app', return_value='/ayugram.app'), \
             mock.patch.object(server, 'get_folder_size', return_value=200), \
             mock.patch.object(server, '_remove_clone_if_idle', return_value=False), \
             mock.patch.object(server.ayugram, 'remove_clone_if_idle', return_value=True), \
             mock.patch.object(server.shutil, 'disk_usage', side_effect=[types.SimpleNamespace(free=1000), types.SimpleNamespace(free=1050)]), \
             mock.patch.object(server, 'invalidate_scan_cache'):
            handler._post_api_shared_app_remove_all_account_apps({})
        data = handler.send_json.call_args.args[0]
        self.assertEqual((data['removed'], data['skipped']), (1, 1))
        self.assertEqual(data['removed_logical_bytes'], 200)
        self.assertEqual(data['disk_free_change_bytes'], 50)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for UI behavior check')
    def test_shared_app_status_uses_real_sizes_with_and_without_master(self):
        script = r'''
const fs = require('fs');
const assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
const el = {innerHTML: '', dataset: {}};
const document = {getElementById: () => el};
const esc = value => String(value);
let response = {shared_exists: false, own_count: 2, total_size_human: '1.1 GB', ayugram_own_count: 1, ayugram_size_human: '544 MB'};
const api = async () => response;
eval(source.slice(source.indexOf('async function loadSharedAppStatus()'), source.indexOf('\nasync function setupSharedApp()')));
(async () => {
  await loadSharedAppStatus();
  assert(el.innerHTML.includes('1.1 GB'));
  assert(el.innerHTML.includes('544 MB'));
  assert(el.innerHTML.includes('logical'));
  assert(!el.innerHTML.includes('435'));
  response = {...response, shared_exists: true, shared_path: '/master'};
  await loadSharedAppStatus();
  assert(el.innerHTML.includes('1.1 GB'));
  assert(!el.innerHTML.includes('recoverable'));
})().catch(error => { console.error(error); process.exit(1); });
'''
        result = subprocess.run(['node', '-e', script, str(RESOURCES / 'index.html')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
