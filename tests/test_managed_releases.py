import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'TelegramManager.app/Contents/Resources'))
os.environ.setdefault('TG_SESSION_TOKEN', 'unit-test-token')
import release_runtime as rt
import managed_releases as mr
import state


class ManagedReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        (self.base / 'data').mkdir()
        self.app = self.bundle(self.base / 'TelegramManager.app', 'manager', '1.1.0')
        self.root = rt.workspace(self.app)
        patch = mock.patch.object(state, 'APP_BUNDLE', str(self.app))
        patch.start(); self.addCleanup(patch.stop)
        self.session = self.base / 'TG/account/TelegramForcePortable/tdata/key_datas'
        self.session.parent.mkdir(parents=True)
        self.session.write_bytes(b'original synthetic session')
        self.sign = mock.patch.object(rt.subprocess, 'run', return_value=mock.Mock(returncode=0))
        self.sign.start(); self.addCleanup(self.sign.stop)

    def tearDown(self):
        self.assertEqual(self.session.read_bytes(), b'original synthetic session')

    def bundle(self, path, component, version):
        (path / 'Contents/MacOS').mkdir(parents=True)
        (path / 'Contents/MacOS/client').write_text('synthetic ' + version)
        for name in [rt.EXECUTABLES[component]] + (['launcher_swift'] if component == 'manager' else []):
            executable = path / 'Contents/MacOS' / name
            executable.write_text('synthetic executable')
            executable.chmod(0o755)
        (path / 'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleIdentifier': rt.COMPONENTS[component][1],
            'CFBundleExecutable': rt.EXECUTABLES[component],
            'CFBundleShortVersionString': version}))
        return path

    def manifest(self, app=None, component='manager', version='1.2.0'):
        value = dict(schema=1, component=component, version=version,
                     app_version=version if component == 'manager' else '7.2.9',
                     bundle=rt.COMPONENTS[component][0], bundle_id=rt.COMPONENTS[component][1],
                     archive=component + '.tar.gz', fingerprint=rt.fingerprint(app or self.app),
                     source_commit='a' * 40, architecture='arm64', min_macos='12.0',
                     data_compatibility='manager-state-v1' if component == 'manager' else 'ayugram-7.2.9-inline-v1',
                     sha256='b' * 64, size=100, patch_sha256='c' * 64, upstream_commit='d' * 40)
        return value

    def queue(self, component='manager'):
        target = rt.target_path(self.app, component)
        if component != 'manager':
            self.bundle(target, component, '7.2.9')
        old = self.manifest(target, component, '1.1.0')
        rt.save(rt.metadata_path(self.app, component), rt.native_metadata(old))
        candidate = self.bundle(self.root / 'candidate' / rt.COMPONENTS[component][0],
                                component, '1.2.0' if component == 'manager' else '7.2.9')
        (candidate / 'Contents/MacOS/client').write_text('new application')
        new = self.manifest(candidate, component)
        rt.save(self.root / 'job.json', dict(status='queued', component=component,
                manifest=new, baseline=rt.fingerprint(target), rollback=False))
        return target, candidate, old, new

    def test_install_and_manual_rollback_keep_sessions(self):
        target, candidate, old, new = self.queue()
        with mock.patch.object(rt, 'clients_closed', return_value=True):
            self.assertTrue(rt.apply_job(self.app))
        self.assertEqual(rt.fingerprint(target), new['fingerprint'])
        previous = self.root / 'manager-previous.app'
        self.assertEqual(rt.fingerprint(previous), old['fingerprint'])
        self.assertTrue(rt.rollback_available(self.app, 'manager'))
        with mock.patch.object(mr, 'spawn_worker'):
            scheduled = mr.schedule('manager', rollback=True)
        self.assertTrue(scheduled['success'], scheduled)
        with mock.patch.object(rt, 'clients_closed', return_value=True):
            rt.apply_job(self.app)
        self.assertEqual(rt.fingerprint(target), old['fingerprint'])
        self.assertEqual(rt.read(rt.metadata_path(self.app, 'manager'))['version'], '1.1.0')

    def test_ayugram_rollback_ends_before_first_launch_attempt(self):
        self.queue('ayugram-custom')
        with mock.patch.object(rt, 'clients_closed', return_value=True):
            rt.apply_job(self.app)
        self.assertTrue(rt.rollback_available(self.app, 'ayugram-custom'))
        rt.mark_ayugram_launch(self.app)
        self.assertFalse(rt.rollback_available(self.app, 'ayugram-custom'))
        self.assertFalse(mr.schedule('ayugram-custom', rollback=True)['success'])

    def test_first_updates_restore_local_apps_and_exact_metadata(self):
        for component in ('manager', 'ayugram-custom'):
            with self.subTest(component=component):
                target, _, old, _ = self.queue(component)
                metadata = rt.metadata_path(self.app, component)
                if component == 'manager':
                    metadata.unlink()
                    (target / 'Contents/MacOS/launcher_swift').unlink()
                    marker = target / 'Contents/Resources/managed-release.json'
                    marker.parent.mkdir(parents=True)
                    rt.save(marker, {'data_compatibility': 'manager-state-v1', 'version': '1.1.0'})
                    expected = {}
                else:
                    expected = rt.native_metadata(old)
                    del expected['custom_release']
                    expected['extra_existing_field'] = 'retain exactly'
                    rt.save(metadata, expected)
                old_hash = rt.fingerprint(target)
                job = rt.read(self.root / 'job.json')
                rt.save(self.root / 'job.json', {**job, 'baseline': old_hash})
                with mock.patch.object(rt, 'clients_closed', return_value=True):
                    rt.apply_job(self.app)
                    self.assertTrue(rt.rollback_available(self.app, component))
                    rt.queue_rollback(self.app, component)
                    rt.apply_job(self.app)
                self.assertEqual(rt.fingerprint(target), old_hash)
                self.assertEqual(rt.read(metadata), expected)
                shutil.rmtree(self.root / 'candidate')

    def test_legacy_and_managed_installers_cannot_overlap(self):
        import updates
        target, _, old, _ = self.queue('ayugram-custom')
        journal = target.parent / '.ayugram-update.json'
        rt.save(journal, {'had_master': True, 'manifest': rt.native_metadata(old)})
        with mock.patch.object(rt, 'clients_closed', return_value=True):
            with self.assertRaisesRegex(ValueError, 'official AyuGram'):
                rt.apply_job(self.app)
        self.assertEqual(rt.fingerprint(target), old['fingerprint'])
        with mock.patch.object(updates, 'recover_ayugram_update') as recovery:
            result = updates.install_release('AyuGram', '7.3.1')
            self.assertFalse(result['success'])
            recovery.assert_not_called()

    def test_wrong_executable_and_cyclic_symlink_are_rejected(self):
        target, candidate, _, manifest = self.queue('ayugram-custom')
        (candidate / 'Contents/MacOS/AyuGram Inline Test').chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'executable'):
            rt.verify_bundle(candidate, manifest)
        (target / 'Contents/loop').symlink_to('loop')
        with self.assertRaisesRegex(ValueError, 'cyclic'):
            rt.validate_tree(target)

    def test_download_schedule_checks_version_and_cleans_failed_staging(self):
        source = self.bundle(self.base / 'source/TelegramManager.app', 'manager', '1.2.0')
        package = self.base / 'fixture.tar.gz'
        with tarfile.open(package, 'w:gz') as archive:
            archive.add(source, arcname=source.name)
        manifest = self.manifest(source)
        manifest.update(size=package.stat().st_size, sha256=hashlib.sha256(package.read_bytes()).hexdigest(),
                        download_url='https://example.test/fixture')
        def download(url, limit, digest, destination):
            shutil.copyfile(package, destination)
            return package.stat().st_size
        with mock.patch.object(mr, 'releases', return_value={'manager': {}}), \
             mock.patch.object(mr, 'manifest_for', return_value=manifest), \
             mock.patch.object(mr.platform, 'machine', return_value='arm64'), \
             mock.patch.object(mr.platform, 'mac_ver', return_value=('14.0', '', '')), \
             mock.patch.object(mr, 'download', side_effect=download), \
             mock.patch.object(mr, 'spawn_worker') as worker:
            # A release object is nonempty in the API response.
            with mock.patch.object(mr, 'releases', return_value={'manager': {'tag_name': 'manager-v1.2.0'}}):
                self.assertFalse(mr.schedule('manager', '1.3.0')['success'])
                result = mr.schedule('manager', '1.2.0')
                self.assertTrue(result['success'], result)
                worker.assert_called_once()
                self.assertEqual(rt.read(self.root / 'job.json')['status'], 'queued')
                self.assertFalse((self.root / 'package.tar.gz').exists())
                mr.cancel()
                with mock.patch.object(rt, 'extract', side_effect=ValueError('invalid archive')):
                    self.assertFalse(mr.schedule('manager', '1.2.0')['success'])
                self.assertFalse((self.root / 'package.tar.gz').exists())
                self.assertFalse((self.root / 'candidate').exists())

    def test_running_app_or_unknown_process_state_never_installs(self):
        target, _, old, _ = self.queue()
        with mock.patch.object(rt, 'clients_closed', return_value=False):
            self.assertFalse(rt.apply_job(self.app))
        with mock.patch.object(rt, 'clients_closed', side_effect=ValueError('unavailable')):
            with self.assertRaises(ValueError): rt.apply_job(self.app)
        self.assertEqual(rt.fingerprint(target), old['fingerprint'])

    def test_runtime_lease_blocks_update_and_launcher(self):
        with rt.lease(self.app):
            with self.assertRaises(BlockingIOError):
                rt.lease(self.app, exclusive=True, blocking=False)
        with rt.lease(self.app, exclusive=True):
            with self.assertRaises(BlockingIOError):
                rt.lease(self.app, blocking=False)

    def test_changed_baseline_and_corrupt_candidate_fail_before_swap(self):
        target, candidate, _, _ = self.queue()
        (candidate / 'Contents/MacOS/client').write_text('tampered')
        with mock.patch.object(rt, 'clients_closed', return_value=True), mock.patch.object(rt, 'exchange') as exchange:
            with self.assertRaises(ValueError): rt.apply_job(self.app)
            exchange.assert_not_called()
        self.assertFalse((self.root / 'transaction.json').exists())
        (target / 'Contents/MacOS/client').write_text('other update')
        with mock.patch.object(rt, 'clients_closed', return_value=True):
            with self.assertRaisesRegex(ValueError, 'changed after'): rt.apply_job(self.app)

    def test_interruption_before_and_after_swap_restores_exact_pair(self):
        target, candidate, old, new = self.queue('ayugram-custom')
        old_metadata = rt.read(rt.metadata_path(self.app, 'ayugram-custom'))
        record = dict(component='ayugram-custom', old_fingerprint=old['fingerprint'],
                      new_fingerprint=new['fingerprint'], old_metadata=old_metadata, old_manifest=old)
        rt.save(self.root / 'transaction.json', record)
        self.assertTrue(rt.recover(self.app))
        self.assertEqual(rt.fingerprint(target), old['fingerprint'])
        rt.save(self.root / 'transaction.json', record)
        rt.exchange(target, candidate)
        rt.save(rt.metadata_path(self.app, 'ayugram-custom'), rt.native_metadata(new))
        rt.recover(self.app)
        self.assertEqual(rt.fingerprint(target), old['fingerprint'])
        self.assertEqual(rt.read(rt.metadata_path(self.app, 'ayugram-custom')), old_metadata)

    def test_committed_crash_finishes_instead_of_reverting(self):
        target, candidate, old, new = self.queue()
        record = dict(component='manager', old_fingerprint=old['fingerprint'], new_fingerprint=new['fingerprint'],
                      old_metadata=old, old_manifest=old, committed=True)
        rt.exchange(target, candidate)
        rt.save(rt.metadata_path(self.app, 'manager'), new)
        rt.save(self.root / 'transaction.json', record)
        rt.recover(self.app)
        self.assertEqual(rt.fingerprint(target), new['fingerprint'])
        self.assertEqual(rt.fingerprint(self.root / 'manager-previous.app'), old['fingerprint'])
        self.assertFalse((self.root / 'transaction.json').exists())

    def test_manifest_write_failure_rolls_back(self):
        target, _, old, _ = self.queue()
        save = rt.save
        failed = False
        def fail_once(path, value):
            nonlocal failed
            if Path(path) == rt.metadata_path(self.app, 'manager') and not failed:
                failed = True
                raise OSError('disk full')
            return save(path, value)
        with mock.patch.object(rt, 'clients_closed', return_value=True), mock.patch.object(rt, 'save', side_effect=fail_once):
            with self.assertRaises(OSError): rt.apply_job(self.app)
        self.assertEqual(rt.fingerprint(target), old['fingerprint'])
        self.assertEqual(rt.read(rt.metadata_path(self.app, 'manager'))['version'], '1.1.0')

    def test_cancel_removes_staging_not_previous_or_sessions(self):
        target, _, old, _ = self.queue()
        self.assertTrue(mr.cancel()['success'])
        self.assertFalse((self.root / 'candidate').exists())
        self.assertEqual(rt.fingerprint(target), old['fingerprint'])
        self.assertEqual(rt.read(self.root / 'job.json')['status'], 'cancelled')

    def test_detached_worker_does_not_inherit_lifecycle_leases(self):
        with mock.patch.object(mr.subprocess, 'Popen') as start:
            mr.spawn_worker()
        self.assertTrue(start.call_args.kwargs['close_fds'])
        self.assertTrue(start.call_args.kwargs['start_new_session'])
        self.assertNotIn('pass_fds', start.call_args.kwargs)
        self.assertEqual(Path(start.call_args.args[0][1]).parent, self.root)

    def test_external_helper_waits_for_lifetime_lease_then_installs(self):
        target, _, _, new = self.queue()
        script = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import release_runtime as rt
rt.clients_closed = lambda manager: True
rt.subprocess.run = lambda *a, **k: type('Result', (), {'returncode': 0})()
Path(sys.argv[3]).write_text('ready')
rt.worker(sys.argv[2])
'''
        ready = self.base / 'helper-ready'
        import time
        with rt.lease(self.app):
            child = subprocess.Popen([sys.executable, '-c', script, str(Path(rt.__file__).parent), str(self.app), str(ready)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(lambda: child.kill() if child.poll() is None else None)
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            self.assertEqual(rt.read(self.root / 'job.json')['status'], 'queued')
        _, error = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, error)
        self.assertEqual(rt.fingerprint(target), new['fingerprint'])

    def test_safe_archive_and_framework_symlink(self):
        src = self.bundle(self.base / 'source/TelegramManager.app', 'manager', '1.2.0')
        (src / 'Contents/Frameworks').mkdir()
        (src / 'Contents/Frameworks/client').symlink_to('../MacOS/client')
        manifest = self.manifest(src)
        package = self.base / 'archive.tar.gz'
        with tarfile.open(package, 'w:gz') as archive: archive.add(src, arcname=src.name)
        app = rt.extract(package, self.base / 'extract', manifest)
        self.assertTrue((app / 'Contents/Frameworks/client').is_symlink())
        self.assertEqual(rt.fingerprint(app), manifest['fingerprint'])

    def test_malicious_archives_rejected_before_writes(self):
        for case in ('traversal', 'absolute', 'hardlink', 'linkparent', 'duplicate', 'data', 'device', 'oversize'):
            package = self.base / (case + '.tar.gz')
            with tarfile.open(package, 'w:gz') as archive:
                item = tarfile.TarInfo('TelegramManager.app/Contents/file')
                if case == 'traversal': item.name = '../escape'
                if case == 'absolute': item.name = '/tmp/escape'
                if case == 'hardlink': item.type = tarfile.LNKTYPE; item.linkname = 'outside'
                if case == 'data': item.name = 'TelegramManager.app/tdata/key_datas'
                if case == 'device': item.type = tarfile.CHRTYPE
                if case == 'oversize': item.size = 2
                if case == 'linkparent': item.type = tarfile.SYMTYPE; item.linkname = '../MacOS'
                archive.addfile(item, io.BytesIO(b'xx') if case == 'oversize' else None)
                if case == 'duplicate': archive.addfile(item)
                if case == 'linkparent': archive.addfile(tarfile.TarInfo(item.name + '/child'))
            destination = self.base / ('out-' + case)
            with self.subTest(case=case), mock.patch.object(rt, 'MAX_EXPANDED', 1 if case == 'oversize' else rt.MAX_EXPANDED), self.assertRaises((ValueError, tarfile.ReadError)):
                rt.extract(package, destination, self.manifest())
            self.assertFalse(destination.exists())

    def test_metadata_symlinks_and_hardlinks_rejected(self):
        other = self.base / 'private'
        other.write_text('{}')
        path = self.root / 'job.json'
        path.symlink_to(other)
        with self.assertRaises(ValueError): rt.save(path, {})
        path.unlink(); os.link(other, path)
        with self.assertRaises(ValueError): rt.read(path)

    def test_release_selection_is_independent_and_ignores_prereleases(self):
        payload = [dict(tag_name='manager-v1.2.0'), dict(tag_name='ayugram-custom-v1.0.0'),
                   dict(tag_name='manager-v9.0.0', prerelease=True), dict(tag_name='manager-v9.1.0', draft=True),
                   dict(tag_name='manager-v1.1.0'), dict(tag_name='manager-v1.2.0-rc1')]
        with mock.patch.object(mr, 'download', return_value=json.dumps(payload).encode()):
            selected = mr.releases()
        self.assertEqual(selected['manager']['tag_name'], 'manager-v1.2.0')
        self.assertEqual(selected['ayugram-custom']['tag_name'], 'ayugram-custom-v1.0.0')

    def test_manifest_bound_to_tag_asset_digest_and_component(self):
        value = self.manifest()
        data = json.dumps(value).encode()
        release = {'tag_name': 'manager-v1.2.0', 'assets': []}
        for name, digest, size in [('manager.json', hashlib.sha256(data).hexdigest(), len(data)),
                                   ('manager.tar.gz', value['sha256'], value['size'])]:
            release['assets'].append(dict(name=name, digest='sha256:' + digest, size=size,
                browser_download_url=f'https://github.com/{mr.REPOSITORY}/releases/download/manager-v1.2.0/{name}'))
        with mock.patch.object(mr, 'download', return_value=data):
            self.assertEqual(mr.manifest_for('manager', release)['version'], '1.2.0')
            release['assets'][1]['digest'] = 'sha256:' + 'f' * 64
            with self.assertRaises(ValueError): mr.manifest_for('manager', release)

    def test_download_digest_limit_and_redirect_guards(self):
        url = f'https://github.com/{mr.REPOSITORY}/releases/download/manager-v1.2.0/manager.json'
        payload = b'verified bytes'
        def opener(): return mock.Mock(open=mock.Mock(return_value=io.BytesIO(payload)))
        with mock.patch.object(mr.urllib.request, 'build_opener', side_effect=lambda *args: opener()):
            self.assertEqual(mr.download(url, 100, hashlib.sha256(payload).hexdigest()), payload)
            with self.assertRaises(ValueError): mr.download(url, 2)
            with self.assertRaises(ValueError): mr.download(url, 100, '0' * 64)
        with self.assertRaises(ValueError): mr.download('https://evil.example/package', 100)
        with self.assertRaises(ValueError): mr.ReleaseRedirect().redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/')

    def test_weekly_check_and_startup_catchup_do_not_install(self):
        now = 1792162800
        rows = [{'client': 'Manager', 'update_available': True}]
        with mock.patch.object(mr, 'check_releases', return_value=rows) as check, mock.patch.object(mr, 'schedule') as install:
            mr.run_scheduled_check(now)
            mr.run_scheduled_check(now + 60)
            check.assert_called_once()
            install.assert_not_called()
        self.assertEqual(mr.status()['clients'], rows)

    def test_manual_check_refreshes_cached_rows_after_install(self):
        self.queue()
        row = dict(client='Manager', component='manager', managed_release=True, version='1.2.0', update_available=True)
        with mock.patch.object(mr, 'check_releases', return_value=[row]):
            self.assertEqual(mr.run_scheduled_check(force=True), [row])
        marker = self.app / 'Contents/Resources/managed-release.json'
        marker.parent.mkdir(parents=True, exist_ok=True)
        rt.save(marker, {'version': '1.2.0'})
        self.assertFalse(mr.status()['clients'][0]['update_available'])

    def test_cleanup_failure_still_releases_install_lock(self):
        import updates
        _, _, _, manifest = self.queue()
        mr.cancel()
        (self.root / 'candidate').symlink_to(self.base / 'unrelated')
        manifest['download_url'] = 'https://example.test/fixture'
        with mock.patch.object(mr, 'releases', return_value={'manager': {'tag_name': 'manager-v1.2.0'}}), \
             mock.patch.object(mr, 'manifest_for', return_value=manifest), \
             mock.patch.object(mr.platform, 'machine', return_value='arm64'), \
             mock.patch.object(mr.platform, 'mac_ver', return_value=('14.0', '', '')):
            self.assertFalse(mr.schedule('manager', '1.2.0')['success'])
        acquired = []
        def attempt():
            ok = updates.INSTALL_LOCK.acquire(blocking=False)
            acquired.append(ok)
            if ok: updates.INSTALL_LOCK.release()
        thread = threading.Thread(target=attempt)
        thread.start(); thread.join(2)
        self.assertEqual(acquired, [True])

    def test_http_routes_are_authenticated_existing_dispatch(self):
        import server
        self.assertEqual(server.RequestHandler.GET_ROUTES['/api/managed-release-status'], '_get_api_managed_release_status')
        self.assertEqual(server.RequestHandler.POST_ROUTES['/api/schedule-managed-release'], '_post_api_schedule_managed_release')


if __name__ == '__main__':
    unittest.main()
