import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'TelegramManager.app/Contents/Resources'))
import server
import state


class CachePreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.account = self.root / 'account'
        self.tdata = self.account / 'TelegramForcePortable/tdata'
        self.tdata.mkdir(parents=True)
        self.patches = [mock.patch.object(state, 'ROOT_DIR', str(self.root)),
                        mock.patch.object(state, 'DATA_DIR', str(self.root)),
                        mock.patch.object(server, '_get_ps_output', return_value='1 /unrelated/process')]
        for patch in self.patches: patch.start()

    def tearDown(self):
        for patch in reversed(self.patches): patch.stop()
        self.temp.cleanup()

    def file(self, name, content=b'preserve this data'):
        path = self.tdata / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_cleanup_preserves_auth_and_unknown_history_and_ayu_profile(self):
        protected = [self.file('key_datas'), self.file('ABCDEF/settingss'),
                     self.file('user_data/history.sqlite'), self.file('user_data/custom/cache/keep'), self.file('user_data_custom/cache/keep')]
        ayu = self.account / '.ayugram/TelegramForcePortable/tdata/user_data/media_cache/local-only'
        ayu.parent.mkdir(parents=True); ayu.write_bytes(b'locally saved attachment')
        self.file('user_data/media_cache/download', b'x'*123)
        self.file('user_data#2/cache/file', b'y'*45)
        self.file('emoji/icon', b'z'*10)
        before = {p: hashlib.sha256(p.read_bytes()).digest() for p in protected + [ayu]}
        self.assertEqual(server.clear_account_caches(str(self.account)), (True, 178))
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).digest() for p in before})

    def test_ayu_database_marks_entire_tree_as_protected(self):
        self.file('ayudata.db')
        cache = self.file('user_data/media_cache/local-only')
        self.assertEqual(server._clear_tdata_caches(str(self.tdata)), (True, 0))
        self.assertTrue(cache.exists())

    def test_symlinked_user_data_and_nested_parents_never_followed(self):
        outside = self.root/'outside/cache'; outside.mkdir(parents=True)
        keep = outside/'keep'; keep.write_bytes(b'keep')
        os.symlink(outside.parent, self.tdata/'user_data')
        self.assertEqual(state._find_cache_dirs(str(self.tdata)), [])
        (self.tdata/'user_data').unlink(); (self.tdata/'user_data').mkdir()
        os.symlink(outside.parent, self.tdata/'user_data/browser')
        self.assertEqual(state._find_cache_dirs(str(self.tdata)), [])
        self.assertTrue(keep.exists())

    def test_unknown_or_running_processes_block_cleanup(self):
        cache = self.file('emoji/keep')
        with mock.patch.object(server, '_get_ps_output', return_value=None):
            self.assertEqual(server.clear_account_caches(str(self.account)), (False, 0))
        with mock.patch.object(server, '_telegram_pid_from_ps_output', return_value=42):
            self.assertEqual(server.clear_account_caches(str(self.account)), (False, 0))
        self.assertTrue(cache.exists())

    def test_failed_deletion_does_not_claim_space_freed(self):
        cache = self.file('emoji/keep')
        with mock.patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([],1,b'',b'denied')):
            self.assertEqual(server.clear_account_caches(str(self.account)), (False, 0))
        self.assertTrue(cache.exists())

    def test_timeout_reports_partial_space_and_failure(self):
        first=self.file('emoji/first',b'a'*100);second=self.file('emoji/second',b'b'*20)
        def interrupted(*args, **kwargs):
            first.unlink(); raise subprocess.TimeoutExpired('rm',120)
        with mock.patch.object(server.subprocess, 'run', side_effect=interrupted):
            self.assertEqual(server.clear_account_caches(str(self.account)), (False,100))
        self.assertTrue(second.exists())

    def test_legacy_backup_cleanup_preserves_all_recovery_data(self):
        legacy = self.account/'backup/tdata'
        (legacy/'user_data/media_cache').mkdir(parents=True)
        (legacy/'user_data/media_cache/download').write_bytes(b'x'*99)
        key=legacy/'key_datas';key.write_bytes(b'old login')
        self.assertEqual(server.clear_account_backup_caches(str(self.account)),(True,99))
        self.assertEqual(key.read_bytes(),b'old login')

    def test_api_reports_cleanup_failure_instead_of_no_cache(self):
        handler=object.__new__(server.RequestHandler);handler.send_json=mock.Mock()
        with mock.patch.object(server,'clear_account_caches',return_value=(False,12)):
            handler._post_api_clear_cache({'path':str(self.account)})
        result=handler.send_json.call_args.args[0]
        self.assertFalse(result['success']);self.assertEqual(result['freed'],12)

    def test_verification_backup_api_preserves_existing_versions(self):
        handler=object.__new__(server.RequestHandler);handler.send_json=mock.Mock()
        with mock.patch.object(server, 'backup_account', return_value=(True,'ok','backup')) as backup:
            handler._post_api_backup({'path':str(self.account),'name':'test','preserve_existing':True})
        backup.assert_called_once_with(str(self.account),'test',prune=False)

if __name__=='__main__': unittest.main()
