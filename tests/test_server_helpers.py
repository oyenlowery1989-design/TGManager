import copy
import hashlib
import importlib
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

RESOURCES_DIR = Path(__file__).resolve().parents[1] / "TelegramManager.app" / "Contents" / "Resources"
sys.path.insert(0, str(RESOURCES_DIR))
os.environ.setdefault("TG_SESSION_TOKEN", "unit-test-token")

server = importlib.import_module("server")
state = importlib.import_module("state")
backups = importlib.import_module("backups")


class ServerHelperTests(unittest.TestCase):
    def test_metadata_is_relocated_to_private_state(self):
        self.assertIn("Application Support/TelegramManager", server.METADATA_FILE)
        self.assertIn("Application Support/TelegramManager", server.WORKSPACES_FILE)
        self.assertTrue(server.METADATA_FILE.endswith("manager_data.json"))
        self.assertTrue(server.WORKSPACES_FILE.endswith("manager_workspaces.json"))

    def test_route_path_uses_session_token(self):
        self.assertEqual(server.ROUTE_PREFIX, "/unit-test-token")
        self.assertEqual(server._route_path("/unit-test-token/"), "/")
        self.assertEqual(server._route_path("/unit-test-token/api/accounts"), "/api/accounts")
        self.assertIsNone(server._route_path("/api/accounts"))

    def test_path_validation_blocks_traversal(self):
        safe_path = os.path.join(server.ROOT_DIR, "example-account")
        self.assertTrue(server.is_safe_path(safe_path))
        self.assertFalse(server.is_safe_path("/tmp/../etc/passwd"))
        self.assertFalse(server.is_safe_path("../outside"))

    def test_shell_escaping_helpers(self):
        self.assertEqual(server._sq("a'b"), "'a'\\''b'")
        self.assertEqual(server._as_str("plain"), '"plain"')
        quoted = server._as_str('a"b')
        self.assertIn('(ASCII character 34)', quoted)
        self.assertTrue(quoted.startswith('"a"'))

    def test_validate_import_payload_accepts_good_export(self):
        payload = {
            "metadata": {
                "notes": {"/tmp/account": "note"},
                "usernames": {"/tmp/account": "alice"},
                "order": {"/tmp/account": 1},
                "colors": {"/tmp/account": "#ff0000"},
                "last_opened": {"/tmp/account": "2026-07-02T00:00:00"},
                "pinned": ["/tmp/account"],
                "proxies": {
                    "/tmp/account": {"type": "socks5", "host": "127.0.0.1", "port": 1080}
                },
                "dock_names": {"/tmp/account": "Alice"},
            },
            "config": copy.deepcopy(server.DEFAULT_CONFIG),
            "workspaces": {
                "Ops": {
                    "accounts": ["/tmp/account"],
                    "icon": "📁",
                    "created": "2026-07-02T00:00:00",
                }
            },
        }
        payload["config"]["port"] = 8477
        ok, message, normalized = server._validate_import_payload(payload)
        self.assertTrue(ok, message)
        self.assertEqual(normalized["config"]["port"], 8477)
        self.assertEqual(normalized["metadata"]["notes"]["/tmp/account"], "note")
        self.assertEqual(normalized["workspaces"]["Ops"]["accounts"], ["/tmp/account"])

    def test_validate_import_payload_rejects_bad_types(self):
        payload = {
            "metadata": {"notes": ["not", "a", "dict"]},
            "config": {"port": "8477"},
            "workspaces": {},
        }
        ok, message, normalized = server._validate_import_payload(payload)
        self.assertFalse(ok)
        self.assertIsNone(normalized)
        self.assertTrue(message)


class BackupPathTests(unittest.TestCase):
    """Backup guard + crash-safety behaviors against a throwaway DATA_DIR."""

    def setUp(self):
        import tempfile
        self._orig_data = state.DATA_DIR
        self.tmp = tempfile.mkdtemp(prefix="tm_test_")
        state.DATA_DIR = self.tmp

    def tearDown(self):
        import shutil
        state.DATA_DIR = self._orig_data
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_backup(self, date="2026-01-01_00-00", account="acct"):
        b = os.path.join(self.tmp, "Backups", date, account)
        os.makedirs(os.path.join(b, "tdata"))
        return b

    def test_resolve_backup_dir_accepts_two_level_paths(self):
        b = self._make_backup()
        self.assertEqual(server._resolve_backup_dir(b), os.path.realpath(b))

    def test_resolve_backup_dir_rejects_everything_else(self):
        self._make_backup()
        for bad in (
            os.path.join(self.tmp, "Backups"),                      # root
            os.path.join(self.tmp, "Backups", "2026-01-01_00-00"),  # 1 level
            os.path.join(self.tmp, "some-live-account"),            # outside
            "/etc/passwd",
            "",
            None,
        ):
            self.assertIsNone(server._resolve_backup_dir(bad), bad)

    def test_list_backups_skips_partial_dirs(self):
        self._make_backup(account="good")
        crashed = os.path.join(self.tmp, "Backups", "2026-01-01_00-00", "crashed.partial")
        os.makedirs(os.path.join(crashed, "tdata"))
        names = [b["account"] for b in server.list_backups()]
        self.assertEqual(names, ["good"])

    def test_restore_rejects_non_backup_source(self):
        live = os.path.join(self.tmp, "live-account")
        os.makedirs(os.path.join(live, "TelegramForcePortable", "tdata"))
        ok, msg = server.restore_backup(live, live)
        self.assertFalse(ok)
        self.assertIn("Invalid backup path", msg)

    def test_restore_rejects_symlinked_destination(self):
        import tempfile
        backup = self._make_backup()
        acct = os.path.join(self.tmp, "acct-folder")
        os.makedirs(acct)
        outside = tempfile.mkdtemp(prefix="tm_outside_")
        try:
            # TelegramForcePortable is a symlink escaping the managed tree
            os.symlink(outside, os.path.join(acct, "TelegramForcePortable"))
            ok, msg = server.restore_backup(backup, acct)
            self.assertFalse(ok)
            self.assertIn("not a valid account folder", msg)
            self.assertFalse(os.path.isdir(os.path.join(outside, "tdata")))
        finally:
            import shutil
            shutil.rmtree(outside, ignore_errors=True)


class LockEnforcementTests(unittest.TestCase):
    """Server-side password-lock enforcement (state.config-driven)."""

    def setUp(self):
        self._orig_hash = state.config.get("lock_password_hash")
        self._orig_salt = state.config.get("lock_password_salt")
        self._orig_timeout = state.config.get("lock_timeout_minutes")
        state.config["lock_password_hash"] = None
        state.config["lock_password_salt"] = None
        state.config["lock_timeout_minutes"] = 5
        server._lock_unlocked_at = 0.0
        server._lock_last_activity = 0.0
        server._unlock_fail_count = 0

    def tearDown(self):
        state.config["lock_password_hash"] = self._orig_hash
        state.config["lock_password_salt"] = self._orig_salt
        state.config["lock_timeout_minutes"] = self._orig_timeout
        server._lock_unlocked_at = 0.0
        server._lock_last_activity = 0.0
        server._unlock_fail_count = 0

    def _set_password(self, password="correct-password", salt="testsalt"):
        state.config["lock_password_salt"] = salt
        state.config["lock_password_hash"] = hashlib.sha256((salt + password).encode()).hexdigest()

    def test_lock_enabled_reflects_config(self):
        self.assertFalse(server._lock_enabled())
        self._set_password()
        self.assertTrue(server._lock_enabled())

    def test_verify_lock_password(self):
        self._set_password()
        self.assertTrue(server._verify_lock_password("correct-password"))
        self.assertFalse(server._verify_lock_password("wrong-password"))

    def test_verify_lock_password_false_when_lock_disabled(self):
        self.assertFalse(server._verify_lock_password("anything"))

    def test_server_unlock_and_lock(self):
        self._set_password()
        server._unlock_fail_count = 3
        server._server_unlock()
        self.assertNotEqual(server._lock_unlocked_at, 0.0)
        self.assertNotEqual(server._lock_last_activity, 0.0)
        self.assertEqual(server._unlock_fail_count, 0)
        server._server_lock()
        self.assertEqual(server._lock_unlocked_at, 0.0)

    def test_register_unlock_failure_increments(self):
        self.assertEqual(server._register_unlock_failure(), 1)
        self.assertEqual(server._register_unlock_failure(), 2)
        self.assertEqual(server._register_unlock_failure(), 3)

    def test_check_and_touch_unlocked_true_when_lock_disabled(self):
        state.config["lock_password_hash"] = None
        self.assertEqual(server._lock_unlocked_at, 0.0)
        self.assertTrue(server._check_and_touch_unlocked())

    def test_check_and_touch_unlocked_false_when_never_unlocked(self):
        self._set_password()
        self.assertFalse(server._check_and_touch_unlocked())

    def test_check_and_touch_unlocked_true_after_unlock(self):
        self._set_password()
        state.config["lock_timeout_minutes"] = 5
        server._server_unlock()
        self.assertTrue(server._check_and_touch_unlocked())
        first = server._lock_last_activity
        time.sleep(0.01)
        self.assertTrue(server._check_and_touch_unlocked())
        self.assertGreater(server._lock_last_activity, first)

    def test_check_and_touch_unlocked_expires_and_relocks(self):
        self._set_password()
        state.config["lock_timeout_minutes"] = 60
        server._server_unlock()
        server._lock_last_activity -= (61 * 60)
        self.assertFalse(server._check_and_touch_unlocked())
        self.assertEqual(server._lock_unlocked_at, 0.0)

    def test_check_and_touch_unlocked_never_expires_when_timeout_zero(self):
        self._set_password()
        state.config["lock_timeout_minutes"] = 0
        server._server_unlock()
        server._lock_last_activity -= (999 * 60)
        self.assertTrue(server._check_and_touch_unlocked())

    def test_is_unlocked_no_touch_does_not_mutate_activity(self):
        self._set_password()
        state.config["lock_timeout_minutes"] = 60
        server._server_unlock()
        server._lock_last_activity -= (30 * 60)   # within timeout, not past it
        before = server._lock_last_activity
        self.assertTrue(server._is_unlocked_no_touch())
        self.assertEqual(server._lock_last_activity, before)
        self.assertTrue(server._is_unlocked_no_touch())
        self.assertEqual(server._lock_last_activity, before)


class BackupDeleteTests(unittest.TestCase):
    """delete_backup()'s 2-level guard and cleanup behavior."""

    def setUp(self):
        import tempfile
        self._orig_data = state.DATA_DIR
        self.tmp = tempfile.mkdtemp(prefix="tm_test_")
        state.DATA_DIR = self.tmp

    def tearDown(self):
        import shutil
        state.DATA_DIR = self._orig_data
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_backup(self, date="2026-01-01_00-00", account="acct"):
        b = os.path.join(self.tmp, "Backups", date, account)
        os.makedirs(os.path.join(b, "tdata"))
        return b

    def test_delete_backup_removes_dir_and_empty_parent(self):
        b = self._make_backup()
        ok, msg = backups.delete_backup(b)
        self.assertTrue(ok, msg)
        self.assertFalse(os.path.isdir(b))
        self.assertFalse(os.path.isdir(os.path.dirname(b)))

    def test_delete_backup_rejects_invalid_path(self):
        b = self._make_backup()
        for bad in (
            os.path.join(self.tmp, "Backups"),
            os.path.join(self.tmp, "outside"),
        ):
            ok, msg = backups.delete_backup(bad)
            self.assertFalse(ok)
            self.assertEqual(msg, "Invalid backup path")
        self.assertTrue(os.path.isdir(b))


class CheckHealthTests(unittest.TestCase):
    """check_health()'s session-freshness + integrity heuristics."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="tm_health_")
        self.tdata = os.path.join(self.tmp, "tdata")
        os.makedirs(self.tdata)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch_key_datas(self, age_seconds=0):
        path = os.path.join(self.tdata, "key_datas")
        with open(path, "w") as f:
            f.write("x")
        ts = time.time() - age_seconds
        os.utime(path, (ts, ts))

    def test_fresh_session(self):
        self._touch_key_datas(age_seconds=0)
        result = server.check_health(self.tmp, True, True, self.tdata)
        self.assertEqual(result["expiry"], "fresh")
        self.assertEqual(result["status"], "ok")

    def test_stale_session(self):
        self._touch_key_datas(age_seconds=70 * 86400)
        result = server.check_health(self.tmp, True, True, self.tdata)
        self.assertEqual(result["expiry"], "stale")
        self.assertEqual(result["status"], "warn")

    def test_expired_session_is_still_warn_not_error(self):
        self._touch_key_datas(age_seconds=200 * 86400)
        result = server.check_health(self.tmp, True, True, self.tdata)
        self.assertEqual(result["expiry"], "expired")
        self.assertEqual(result["status"], "warn")

    def test_missing_tdata(self):
        result = server.check_health(self.tmp, True, False, self.tdata)
        self.assertEqual(result["status"], "error")
        self.assertIn("tdata missing", result["issues"])

    def test_empty_tdata(self):
        result = server.check_health(self.tmp, True, True, self.tdata)
        self.assertEqual(result["status"], "error")
        self.assertIn("tdata is empty", result["issues"])


class RestoreBackupRollbackTests(unittest.TestCase):
    """restore_backup()'s crash-safety rollback when the second rename fails."""

    def setUp(self):
        import tempfile
        self._orig_data = state.DATA_DIR
        self.tmp = tempfile.mkdtemp(prefix="tm_restore_")
        state.DATA_DIR = self.tmp

    def tearDown(self):
        import shutil
        state.DATA_DIR = self._orig_data
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_backup(self, date="2026-01-01_00-00", account="acct"):
        b = os.path.join(self.tmp, "Backups", date, account)
        os.makedirs(os.path.join(b, "tdata"))
        with open(os.path.join(b, "tdata", "backup_marker"), "w") as f:
            f.write("backup")
        return b

    def test_restore_rolls_back_original_tdata_on_second_rename_failure(self):
        backup = self._make_backup()
        acct = os.path.join(self.tmp, "live-account")
        tdata_dst = os.path.join(acct, "TelegramForcePortable", "tdata")
        os.makedirs(tdata_dst)
        with open(os.path.join(tdata_dst, "original_marker"), "w") as f:
            f.write("original")

        real_rename = os.rename
        call_count = {"n": 0}

        def flaky_rename(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise OSError("simulated failure")
            return real_rename(*args, **kwargs)

        with mock.patch("backups.os.rename", side_effect=flaky_rename):
            ok, msg = backups.restore_backup(backup, acct)

        self.assertFalse(ok)
        self.assertIn("original tdata has been restored", msg)
        self.assertTrue(os.path.isfile(os.path.join(tdata_dst, "original_marker")))
        self.assertFalse(os.path.isdir(tdata_dst + ".new"))
        bak_dirs = [d for d in os.listdir(os.path.dirname(tdata_dst))
                    if d.startswith("tdata.bak.")]
        self.assertEqual(bak_dirs, [])


class PruneBackupsTests(unittest.TestCase):
    """prune_backups()'s keep-N-per-account enforcement."""

    def setUp(self):
        import tempfile
        self._orig_data = state.DATA_DIR
        self._orig_keep = state.config.get("backup_keep_per_account")
        self.tmp = tempfile.mkdtemp(prefix="tm_prune_")
        state.DATA_DIR = self.tmp

    def tearDown(self):
        import shutil
        state.DATA_DIR = self._orig_data
        state.config["backup_keep_per_account"] = self._orig_keep
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_backup(self, date, account):
        b = os.path.join(self.tmp, "Backups", date, account)
        os.makedirs(os.path.join(b, "tdata"))
        return b

    def test_prune_keeps_only_newest_n(self):
        dates = ["2026-01-01_00-00", "2026-01-02_00-00", "2026-01-03_00-00", "2026-01-04_00-00"]
        for d in dates:
            self._make_backup(d, "acct")
        state.config["backup_keep_per_account"] = 2
        backups.prune_backups("acct")
        remaining = sorted(b["date"] for b in backups.list_backups() if b["account"] == "acct")
        self.assertEqual(remaining, ["2026-01-03_00-00", "2026-01-04_00-00"])

    def test_prune_is_noop_when_keep_is_zero(self):
        dates = ["2026-01-01_00-00", "2026-01-02_00-00", "2026-01-03_00-00"]
        for d in dates:
            self._make_backup(d, "acct")
        state.config["backup_keep_per_account"] = 0
        backups.prune_backups("acct")
        remaining = sorted(b["date"] for b in backups.list_backups() if b["account"] == "acct")
        self.assertEqual(remaining, dates)


class CreateAccountValidationTests(unittest.TestCase):
    """create_account()'s early-return name-validation paths."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="tm_create_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rejects_empty_name(self):
        ok, msg = server.create_account("   ", self.tmp, open_after=False)
        self.assertFalse(ok)
        self.assertEqual(msg, "Account name cannot be empty")

    def test_rejects_bad_characters(self):
        ok, msg = server.create_account("bad/name", self.tmp, open_after=False)
        self.assertFalse(ok)
        self.assertIn("invalid characters", msg)

    def test_rejects_dot_names(self):
        for name in (".", "..", ".hidden"):
            ok, msg = server.create_account(name, self.tmp, open_after=False)
            self.assertFalse(ok, name)
            self.assertEqual(msg, "Account name cannot start with a dot")

    def test_rejects_reserved_names(self):
        ok, msg = server.create_account("Backups", self.tmp, open_after=False)
        self.assertFalse(ok)
        self.assertEqual(msg, '"Backups" is a reserved folder name')

    def test_rejects_duplicate_name(self):
        os.makedirs(os.path.join(self.tmp, "Existing"))
        ok, msg = server.create_account("Existing", self.tmp, open_after=False)
        self.assertFalse(ok)
        self.assertEqual(msg, 'A folder named "Existing" already exists in that location')


if __name__ == "__main__":
    unittest.main()
