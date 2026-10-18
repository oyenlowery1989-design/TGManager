"""Synthetic deployment checks; never connect to a host or read real profiles."""
import ast
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'tools/ayugram_remote'
spec = importlib.util.spec_from_file_location('remote_runtime', TOOLS / 'runtime.py')
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)
install_spec = importlib.util.spec_from_file_location('remote_install', TOOLS / 'install.py')
installer = importlib.util.module_from_spec(install_spec)
with patch.dict(sys.modules, {'runtime': remote}):
    install_spec.loader.exec_module(installer)


def preferences():
    source = ROOT / 'TelegramManager.app/Contents/Resources/ayugram.py'
    for node in ast.parse(source.read_text()).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(item, ast.Name) and item.id == 'COMMON_PROFILE' for item in node.targets):
            result = ast.literal_eval(node.value)
            result['semiTransparentDeletedMessages'] = True
            return result
    raise AssertionError('Missing common profile')


class RemoteDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)

    def test_installer_rejects_linked_mutable_or_wrong_owner_destinations(self):
        target = self.root / 'destination'
        external = self.root / 'external'
        external.write_text('unchanged')
        external.chmod(0o600)
        installer.safe_file(target, uid=os.getuid())
        target.symlink_to(external)
        with self.assertRaises(ValueError):
            installer.safe_file(target, uid=os.getuid())
        with self.assertRaises(ValueError):
            installer.root_copy(external, target, 0o644)
        target.unlink()
        os.link(external, target)
        with self.assertRaises(ValueError):
            installer.safe_file(target, uid=os.getuid())
        target.unlink()
        target.write_text('existing')
        target.chmod(0o622)
        with self.assertRaises(ValueError):
            installer.safe_file(target, uid=os.getuid())
        target.chmod(0o600)
        with self.assertRaises(ValueError):
            installer.safe_file(target, uid=os.getuid() + 1)
        installer.safe_file(target, uid=os.getuid(), modes={0o600})
        with self.assertRaises(ValueError):
            installer.safe_file(target, uid=os.getuid(), modes={0o640})
        self.assertEqual(external.read_text(), 'unchanged')

    def test_private_resolver_destination_cannot_follow_links_or_modify_other_files(self):
        resolver = self.root / 'runtime-resolv.conf'
        external = self.root / 'host-resolv.conf'
        external.write_text('nameserver 169.254.169.254\n')
        resolver.symlink_to(external)
        with self.assertRaises(ValueError):
            installer.root_write(resolver, 'nameserver 8.8.8.8\n')
        resolver.unlink()
        os.link(external, resolver)
        with self.assertRaises(ValueError):
            installer.root_write(resolver, 'nameserver 8.8.8.8\n')
        self.assertEqual(external.read_text(), 'nameserver 169.254.169.254\n')

    def test_runtime_metadata_filter_and_private_dns_leave_backup_adc_untouched(self):
        units = TOOLS / 'systemd'
        for name in ('client', 'display', 'desktop', 'vnc'):
            source = (units / ('ayugram-remote-' + name + '.service')).read_text()
            self.assertIn('IPAddressDeny=169.254.169.254/32 fd20:ce::254/128', source)
            self.assertIn('BindReadOnlyPaths=/etc/ayugram-remote/runtime-resolv.conf:/etc/resolv.conf', source)
        for name in ('ayugram-remote-backup.service', 'ayugram-remote.slice'):
            source = (units / name).read_text()
            self.assertNotIn('IPAddressDeny=', source)
            self.assertNotIn('runtime-resolv.conf', source)

    def test_installer_refuses_live_transitional_or_unreachable_services(self):
        for state in ('active', 'activating', 'deactivating', 'reloading', ''):
            with self.subTest(state=state), patch.object(installer.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, state + '\n')):
                with self.assertRaises(ValueError):
                    installer.require_stopped()
        for state in ('inactive', 'failed'):
            with patch.object(installer.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, state + '\n')):
                installer.require_stopped()
        with patch.object(installer.subprocess, 'run',
                return_value=subprocess.CompletedProcess([], 1, 'inactive\n')):
            with self.assertRaises(ValueError):
                installer.require_stopped()

    def test_profile_is_fresh_and_reinstall_preserves_every_byte(self):
        profile = self.root / 'profile'
        remote.seed_profile(profile, preferences())
        remote.validate_profile(profile)
        settings = profile / 'tdata/ayu_settings.json'
        settings.write_text('{"custom":true}')
        (profile / 'tdata/key_datas').write_bytes(b'synthetic-session')
        before = {str(p.relative_to(profile)): p.read_bytes() for p in profile.rglob('*') if p.is_file()}
        remote.seed_profile(profile, preferences())
        after = {str(p.relative_to(profile)): p.read_bytes() for p in profile.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual((profile / '.ayugram-inline-test').read_bytes(), remote.MARKER)

    def test_unmarked_or_linked_profile_rejected_without_modification(self):
        profile = self.root / 'profile'
        profile.mkdir()
        with self.assertRaises((ValueError, OSError)):
            remote.seed_profile(profile, preferences())
        self.assertEqual(list(profile.iterdir()), [])
        profile.rmdir()
        remote.seed_profile(profile, preferences())
        (profile / 'tdata/link').symlink_to(self.root / 'external')
        with self.assertRaises(ValueError):
            remote.validate_profile(profile)

    def test_first_native_launch_repairs_then_preserves_preferences(self):
        profile = self.root / 'profile'
        remote.seed_profile(profile, preferences())
        settings = profile / 'tdata/ayu_settings.json'
        settings.write_text('{"ghostModeSettings":{},"useGlobalGhostMode":true}')
        remote.launch_preferences(profile, preferences())
        self.assertTrue((profile / '.remote-preferences-pending').exists())
        (profile / 'tdata/settingss').write_bytes(b'synthetic-native-core-settings')
        remote.launch_preferences(profile, preferences())
        self.assertEqual(json.loads(settings.read_text()), preferences())
        settings.write_text('{"custom":true,"useGlobalGhostMode":false}')
        remote.launch_preferences(profile, preferences())
        self.assertEqual(json.loads(settings.read_text()), dict(custom=True, useGlobalGhostMode=False))

    def test_release_requires_external_digest_pinned_source_patch_and_elf(self):
        header = bytearray(20)
        header[:6] = b'\x7fELF\x02\x01'
        header[18:20] = b'\x3e\x00'
        binary = self.root / remote.NAME
        binary.write_bytes(header)
        binary.chmod(0o755)
        sha = remote.digest(binary)
        manifest = dict(schema=1, binary_name=remote.NAME, source_commit=remote.COMMIT,
                        platform='linux-x86_64', patch_sha256='a' * 64, binary_sha256=sha)
        receipt = self.root / 'release.json'
        receipt.write_text(json.dumps(manifest))
        self.assertEqual(remote.verify_release(self.root, sha, 'a' * 64), binary)
        for field, wrong in [('source_commit', 'wrong'), ('platform', 'macos-arm64'),
                             ('patch_sha256', 'b' * 64), ('binary_sha256', 'b' * 64)]:
            bad = dict(manifest, **{field: wrong})
            receipt.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                remote.verify_release(self.root, sha, 'a' * 64)
        receipt.write_text('[]')
        with self.assertRaises(ValueError):
            remote.verify_release(self.root, sha, 'a' * 64)
        receipt.write_text(json.dumps(manifest))
        binary.write_bytes(b'not an elf')
        with self.assertRaises(ValueError):
            remote.verify_release(self.root, sha, 'a' * 64)

    def test_installed_provenance_rejects_nonobjects_and_path_escape(self):
        trusted = self.root / 'trusted.json'
        with patch.object(remote, 'BASE', self.root):
            for value in ([], {'binary_sha256': '../escape', 'patch_sha256': 'a'*64},
                          {'binary_sha256': None, 'patch_sha256': 'a'*64}):
                trusted.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    remote.installed_binary()

    def test_fixed_helper_rejects_additional_actions_and_arguments(self):
        for arguments in [[], ['status', 'anything'], ['restore'], ['password'], ['--help']]:
            result = subprocess.run(['sh', str(TOOLS / 'ayugram-remote-control'), *arguments], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
        with self.assertRaises(ValueError):
            remote.control('shell')

    def test_status_has_no_secrets_and_needs_all_units_plus_provenance(self):
        def command(*args):
            return subprocess.CompletedProcess(args, 0, 'active\n' if args[0] == 'is-active'
                                               else 'ActiveState=inactive\nResult=success\nExecMainExitTimestamp=now\n')
        with patch.object(remote, 'PROFILE', self.root), \
                patch.object(remote, 'installed_binary'), patch.object(remote, 'validate_profile'), \
                patch.object(remote, 'backup_configuration_safe', return_value=True), \
                patch.object(remote, 'systemctl', side_effect=command):
            status = remote.control('status')
            self.assertTrue(status['ready'])
            self.assertEqual(status['schema'], 1)
            self.assertEqual(status['vnc_port'], 5905)
            self.assertEqual(status['client_state'], 'active')
        with patch.object(remote, 'PROFILE', self.root), \
                patch.object(remote, 'installed_binary', side_effect=ValueError('invalid')), \
                patch.object(remote, 'backup_configuration_safe', return_value=True), \
                patch.object(remote, 'systemctl', side_effect=command):
            self.assertFalse(remote.control('status')['ready'])
        self.assertFalse(any('password' in key for key in status))

    def health_status(self, client=None, backup=None, disk=None):
        def command(*args):
            if args[0] == 'show':
                reply = backup or subprocess.CompletedProcess(args, 0,
                    'ActiveState=inactive\nResult=success\nExecMainExitTimestamp=now\n')
            elif args[1] == remote.UNITS[0] and client is not None:
                reply = client
            else:
                reply = subprocess.CompletedProcess(args, 0, 'active\n')
            if isinstance(reply, Exception):
                raise reply
            return reply
        with patch.object(remote, 'PROFILE', self.root), \
                patch.object(remote, 'installed_binary'), patch.object(remote, 'validate_profile'), \
                patch.object(remote, 'backup_configuration_safe', return_value=True), \
                patch.object(remote, 'systemctl', side_effect=command) as commands, \
                patch.object(remote.os, 'statvfs') as statvfs:
            if isinstance(disk, Exception):
                statvfs.side_effect = disk
            else:
                statvfs.return_value = disk or SimpleNamespace(f_blocks=1000, f_frsize=4096,
                                                              f_bavail=100, f_bfree=200)
            status = remote.control('status')
            statvfs.assert_called_once_with(self.root)
            self.assertTrue(all(call.args[0] in ('is-active', 'show')
                                for call in commands.call_args_list))
            return status

    def test_status_disk_reports_usable_space_including_low_and_zero(self):
        for available in (100, 1, 0):
            with self.subTest(available=available):
                status = self.health_status(disk=SimpleNamespace(
                    f_blocks=1000, f_frsize=4096, f_bavail=available, f_bfree=200))
                self.assertEqual(status['disk_total_bytes'], 4096000)
                self.assertEqual(status['disk_free_bytes'], available * 4096)
                self.assertTrue(status['ready'])
        status = self.health_status(disk=OSError('synthetic unavailable filesystem'))
        self.assertIsNone(status['disk_total_bytes'])
        self.assertIsNone(status['disk_free_bytes'])
        self.assertTrue(status['ready'])

    def test_status_client_distinguishes_failed_stopped_transitional_and_unknown(self):
        for state, code in [('active', 0), ('inactive', 3), ('failed', 3),
                            ('activating', 3), ('deactivating', 3), ('reloading', 0),
                            ('maintenance', 3), ('refreshing', 0)]:
            with self.subTest(state=state):
                status = self.health_status(client=subprocess.CompletedProcess([], code, state + '\n'))
                self.assertEqual(status['client_state'], state)
                self.assertEqual(status['client_active'], state == 'active')
                self.assertEqual(status['ready'], state == 'active')
        for reply in (subprocess.CompletedProcess([], 1, 'inactive\n'),
                      subprocess.CompletedProcess([], 0, 'unexpected\n'),
                      subprocess.CompletedProcess([], 0, ''),
                      OSError('synthetic missing systemctl'),
                      subprocess.TimeoutExpired('systemctl', 15)):
            with self.subTest(reply=reply):
                status = self.health_status(client=reply)
                self.assertEqual(status['client_state'], 'unknown')
                self.assertFalse(status['client_active'])
                self.assertFalse(status['ready'])

    def test_status_backup_preserves_failures_and_query_uncertainty(self):
        cases = [('inactive', 'success', '', 'never'),
                 ('inactive', 'success', 'now', 'success'),
                 ('failed', 'exit-code', '', 'exit-code'),
                 ('failed', 'timeout', 'now', 'timeout'),
                 ('failed', 'success', '', 'unknown'),
                 ('failed', 'success', 'now', 'unknown'),
                 ('unexpected', 'success', 'now', 'unknown'),
                 ('activating', 'success', '', 'unknown'),
                 ('active', 'success', '', 'unknown')]
        for state, result, finished, expected in cases:
            with self.subTest(state=state, result=result, finished=finished):
                status = self.health_status(backup=subprocess.CompletedProcess([], 0,
                    f'ActiveState={state}\nResult={result}\nExecMainExitTimestamp={finished}\n'))
                self.assertEqual(status['backup_last_result'], expected)
                self.assertEqual(status['backup_last_finished'], finished)
                self.assertEqual(status['backup_active'], state in ('active', 'activating'))
        for reply in (subprocess.CompletedProcess([], 1,
                          'ActiveState=inactive\nResult=success\nExecMainExitTimestamp=now\n'),
                      subprocess.CompletedProcess([], 0, ''),
                      subprocess.CompletedProcess([], 0, 'ActiveState=inactive\n'),
                      OSError('synthetic missing systemctl'),
                      subprocess.TimeoutExpired('systemctl', 15)):
            with self.subTest(reply=reply):
                status = self.health_status(backup=reply)
                self.assertEqual(status['backup_last_result'], 'unknown')
                self.assertFalse(status['backup_active'])
                self.assertEqual(status['backup_last_finished'], '')

    def test_pending_bootstrap_allows_viewer_but_never_reports_ready_for_login(self):
        profile = self.root / 'profile'
        remote.seed_profile(profile, preferences())
        command = lambda *args: subprocess.CompletedProcess(args, 0, 'active\n' if args[0] == 'is-active' else '')
        with patch.object(remote, 'PROFILE', profile), patch.object(remote, 'installed_binary'), \
                patch.object(remote, 'backup_configuration_safe', return_value=True), \
                patch.object(remote, 'systemctl', side_effect=command):
            first = remote.control('status')
            self.assertTrue(first['viewer_ready'])
            self.assertTrue(first['needs_initialization'])
            self.assertFalse(first['ready'])
            (profile / 'tdata/settingss').write_bytes(b'synthetic-native-settings')
            remote.launch_preferences(profile, preferences())
            initialized = remote.control('status')
            self.assertTrue(initialized['viewer_ready'])
            self.assertTrue(initialized['ready'])
            self.assertFalse(initialized['needs_initialization'])

    def test_shell_syntax_and_static_resource_authentication_guards(self):
        for path in [TOOLS / 'backup.sh', TOOLS / 'wait-display.sh', TOOLS / 'ayugram-remote-control']:
            subprocess.run(['bash', '-n', str(path)], check=True, capture_output=True)
        units = TOOLS / 'systemd'
        slice_text = (units / 'ayugram-remote.slice').read_text()
        self.assertIn('MemoryMax=640M', slice_text)
        self.assertIn('CPUQuota=50%', slice_text)
        self.assertIn('MemorySwapMax=0', slice_text)
        vnc = (units / 'ayugram-remote-vnc.service').read_text()
        self.assertIn('-listen 127.0.0.1 -localhost -no6 -rfbport 5905', vnc)
        self.assertIn('-rfbauth /etc/ayugram-remote/vnc-password', vnc)
        self.assertNotIn('-nopw', vnc)
        backup = (units / 'ayugram-remote-backup.service').read_text()
        self.assertIn('ExecStopPost=-/usr/bin/systemctl thaw ayugram-remote-client.service', backup)
        self.assertIn('TimeoutStartSec=15min', backup)
        self.assertIn('Upholds=', (units / 'ayugram-remote.target').read_text())
        self.assertIn('Restart=always', (units / 'ayugram-remote-client.service').read_text())
        self.assertIn('StartLimitBurst=3', (units / 'ayugram-remote-client.service').read_text())
        self.assertNotIn('prune', (TOOLS / 'backup.sh').read_text().split('/usr/bin/restic')[-1])

    def backup_fixture(self, failure):
        profile = self.root / 'profile'
        staging = self.root / 'staging'
        profile.mkdir()
        staging.mkdir()
        password = self.root / 'password'
        password.write_text('fixture-only')
        (profile / 'tdata').mkdir()
        database = profile / 'tdata/history.sqlite'
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        connection.execute('pragma journal_mode=wal')
        connection.execute('create table messages (body text)')
        connection.execute("insert into messages values ('preserved WAL message')")
        connection.commit()
        self.assertTrue(Path(str(database) + '-wal').exists())
        log = self.root / 'events'
        fake = self.root / 'fake.py'
        fake.write_text('''#!/usr/bin/env python3
import fcntl, os, pathlib, shutil, sqlite3, sys
command, *args = sys.argv[1:]
log = pathlib.Path(os.environ['FIXTURE_LOG'])
with log.open('a') as f: f.write(command + ' ' + ' '.join(args) + '\\n')
failure = os.environ['FIXTURE_FAILURE']
if command == 'systemctl' and args[0] == 'show': print('inactive' if failure == 'idle' else 'active')
if command == 'systemctl' and args[0] == 'freeze' and failure == 'freeze': sys.exit(1)
if command == 'systemctl' and args[0] == 'thaw' and failure == 'thaw' and log.read_text().count('systemctl thaw') == 1: sys.exit(1)
if command == 'flock': fcntl.flock(int(args[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
if command == 'stat': print('600:0')
if command == 'rsync':
    if failure == 'copy': sys.exit(124)
    shutil.copytree(args[-2], args[-1])
if command == 'restic':
    assert failure == 'idle' or 'systemctl thaw' in log.read_text()
    db = sqlite3.connect('profile/tdata/history.sqlite')
    assert db.execute('select body from messages').fetchone()[0] == 'preserved WAL message'
    if failure == 'upload': sys.exit(1)
''')
        fake.chmod(0o755)
        source = (TOOLS / 'backup.sh').read_text()
        source = source.replace('/var/lib/ayugram-remote/profile', str(profile))
        source = source.replace('/var/lib/ayugram-remote/backup-staging', str(staging))
        source = source.replace('/etc/ayugram-remote/restic-password', str(password))
        lock = self.root / 'lock'
        lock.touch()
        if failure == 'locked':
            held = lock.open('rb')
            self.addCleanup(held.close)
            fcntl.flock(held, fcntl.LOCK_EX)
            (staging / 'profile').mkdir()
            (staging / 'profile/other-running-backup').write_text('preserve')
        source = source.replace('/var/lib/ayugram-remote/launch.lock', str(lock))
        source = source.replace('/run/ayugram-remote-backup.lock', str(self.root / 'backup.lock'))
        source = source.replace('/usr/bin/python3 -I /opt/ayugram-remote/runtime.py verify-backup', 'true')
        source = source.replace('/usr/bin/rm', shutil.which('rm'))
        source = source.replace('/usr/bin/timeout --kill-after=5 45 ', '')
        source = source.replace('/usr/bin/timeout 10 ', '')
        for command in ('systemctl', 'stat', 'rsync', 'restic', 'flock'):
            source = source.replace('/usr/bin/' + command, str(fake) + ' ' + command)
        script = self.root / 'backup-fixture.sh'
        script.write_text(source)
        env = dict(os.environ, RESTIC_REPOSITORY='gs:synthetic-only:/profile',
                   RESTIC_PASSWORD_FILE=str(password), FIXTURE_LOG=str(log), FIXTURE_FAILURE=failure)
        result = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True)
        return result, log.read_text(), staging

    def test_snapshot_preserves_wal_and_upload_happens_after_thaw(self):
        result, events, staging = self.backup_fixture('')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('restic', events)
        self.assertFalse((staging / 'profile').exists())

    def test_launch_lock_prevents_concurrent_snapshot_writes(self):
        result, events, staging = self.backup_fixture('locked')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('rsync', events)
        self.assertNotIn('restic', events)
        self.assertTrue((staging / 'profile/other-running-backup').exists())

    def test_thaw_failure_is_retried_and_does_not_upload(self):
        result, events, staging = self.backup_fixture('thaw')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(events.count('systemctl thaw'), 2)
        self.assertNotIn('restic', events)

    def test_inactive_client_is_copied_without_freezing(self):
        result, events, staging = self.backup_fixture('idle')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('systemctl freeze', events)
        self.assertNotIn('systemctl thaw', events)
        self.assertIn('restic', events)

    def test_freeze_failure_still_thaws(self):
        result, events, staging = self.backup_fixture('freeze')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('systemctl thaw', events)
        self.assertNotIn('restic', events)

    def test_copy_timeout_still_thaws_without_upload(self):
        result, events, staging = self.backup_fixture('copy')
        self.assertEqual(result.returncode, 124)
        self.assertIn('systemctl thaw', events)
        self.assertNotIn('restic', events)
        self.assertFalse((staging / 'profile').exists())

    def test_upload_failure_is_reported_and_staging_removed(self):
        result, events, staging = self.backup_fixture('upload')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('systemctl thaw', events)
        self.assertFalse((staging / 'profile').exists())


if __name__ == '__main__':
    unittest.main()
