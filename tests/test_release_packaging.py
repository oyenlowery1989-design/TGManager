"""Synthetic release bundles only: no installed apps or profiles are read."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'tools/build_managed_release.py'
spec = importlib.util.spec_from_file_location('release_packaging', SCRIPT)
packaging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packaging)
COMMIT = 'a' * 40
UPSTREAM = 'b' * 40


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = self.root / 'Built AyuGram.app'
        (self.app / 'Contents/MacOS').mkdir(parents=True)
        self.info = {
            'CFBundleIdentifier': 'one.telegrammanager.AyuGramInlineTest',
            'CFBundleShortVersionString': '7.2.9',
            'CFBundleExecutable': 'AyuGram Inline Test',
            'LSMinimumSystemVersion': '12.0',
        }
        self.write_info()
        (self.app / 'Contents/MacOS/AyuGram Inline Test').write_bytes(b'synthetic executable')
        (self.app / 'Contents/MacOS/AyuGram Inline Test').chmod(0o755)
        self.patch = self.root / 'source.patch'
        self.patch.write_text('synthetic source patch\n')
        self.commands = []
        self.runner = mock.patch.object(packaging, 'run', side_effect=self.fake_run)
        self.runner.start()
        self.addCleanup(self.runner.stop)
        self.fingerprint = mock.patch.object(packaging, 'fingerprint', return_value='c' * 64)
        self.fingerprint.start()
        self.addCleanup(self.fingerprint.stop)

    def write_info(self):
        (self.app / 'Contents/Info.plist').write_bytes(plistlib.dumps(self.info))

    def fake_run(self, args, **kwargs):
        self.commands.append(args)
        if args[0] == 'lipo':
            return 'arm64\n'
        if args[0] == 'swiftc':
            binary = Path(args[args.index('-o') + 1])
            binary.write_bytes(b'compiled fixture')
            binary.chmod(0o755)
        return b''

    def build(self, **overrides):
        args = dict(component='ayugram-custom', version='1.2.3', source_commit=COMMIT,
                    architecture='arm64', output=self.root / 'out', app=self.app,
                    patch=self.patch, upstream_commit=UPSTREAM)
        args.update(overrides)
        return packaging.package_release(**args)

    def test_custom_manifest_archive_and_provenance(self):
        framework = self.app / 'Contents/Frameworks/Thing.framework'
        (framework / 'Versions/A').mkdir(parents=True)
        (framework / 'Versions/A/Thing').write_bytes(b'framework')
        (framework / 'Versions/Current').symlink_to('A')
        (framework / 'Thing').symlink_to('Versions/Current/Thing')
        manifest = self.build()
        archive = self.root / 'out/ayugram-custom.tar.gz'
        self.assertEqual(manifest['patch_sha256'], hashlib.sha256(self.patch.read_bytes()).hexdigest())
        self.assertEqual(manifest['upstream_commit'], UPSTREAM)
        self.assertEqual(manifest['source_commit'], COMMIT)
        self.assertEqual(manifest['sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual(manifest['size'], archive.stat().st_size)
        self.assertEqual(manifest['data_compatibility'], 'ayugram-7.2.9-inline-v1')
        self.assertEqual(manifest['bundle'], 'AyuGram.app')
        self.assertEqual(manifest, json.loads((self.root / 'out/ayugram-custom.json').read_text()))
        with tarfile.open(archive) as tar:
            entries = tar.getmembers()
            self.assertTrue(all(item.name == 'AyuGram.app' or item.name.startswith('AyuGram.app/') for item in entries))
            self.assertTrue(all(item.isdir() or item.isfile() or item.issym() for item in entries))
            self.assertTrue(any(item.issym() for item in entries))
        self.assertFalse(any(command[0] == 'swiftc' or '--sign' in command for command in self.commands))
        self.assertEqual((self.app / 'Contents/MacOS/AyuGram Inline Test').read_bytes(), b'synthetic executable')


    def test_custom_executable_name_mode_type_and_symlink_rejected(self):
        binary = self.app / 'Contents/MacOS/AyuGram Inline Test'
        for executable in ('AyuGram', '../AyuGram Inline Test', '', None):
            with self.subTest(executable=executable):
                if executable is None:
                    self.info.pop('CFBundleExecutable', None)
                else:
                    self.info['CFBundleExecutable'] = executable
                self.write_info()
                with self.assertRaisesRegex(ValueError, 'Unexpected bundle executable'):
                    self.build()
        self.info['CFBundleExecutable'] = 'AyuGram Inline Test'
        self.write_info()
        binary.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'Missing regular executable file'):
            self.build()
        binary.unlink()
        binary.mkdir()
        with self.assertRaisesRegex(ValueError, 'Missing regular executable file'):
            self.build()
        binary.rmdir()
        target = binary.with_name('other')
        target.write_bytes(b'executable')
        target.chmod(0o755)
        binary.symlink_to('other')
        with self.assertRaisesRegex(ValueError, 'Missing regular executable file'):
            self.build()

    def test_forbidden_state_even_nested_and_case_insensitive(self):
        for forbidden in ('TData', 'TelegramForcePortable', 'profiles', 'manager_data.json', '__pycache__'):
            with self.subTest(forbidden=forbidden):
                entry = self.app / 'Contents' / forbidden
                entry.mkdir()
                with self.assertRaisesRegex(ValueError, 'Runtime/profile'):
                    self.build()
                entry.rmdir()
        self.assertEqual(self.commands, [])

    def test_unsafe_links_hardlinks_and_special_files_rejected(self):
        entry = self.app / 'Contents/unsafe'
        for target in ('/tmp', '../../../outside'):
            entry.symlink_to(target)
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'Unsafe symlink'):
                self.build()
            entry.unlink()
        os.link(self.app / 'Contents/MacOS/AyuGram Inline Test', entry)
        with self.assertRaisesRegex(ValueError, 'Hardlinked'):
            self.build()
        entry.unlink()
        os.mkfifo(entry)
        with self.assertRaisesRegex(ValueError, 'Special file'):
            self.build()

    def test_invalid_metadata_and_input_identity_rejected(self):
        for overrides in ({'version': '01.2.3'}, {'version': '1.2.3-beta'},
                          {'source_commit': 'HEAD'}, {'upstream_commit': 'bad'},
                          {'architecture': 'universal'}, {'output': self.app / 'out'}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.build(**overrides)
        self.info['LSMinimumSystemVersion'] = '13.0'
        self.write_info()
        with self.assertRaisesRegex(ValueError, 'newer than 12.0'):
            self.build()
        self.info['LSMinimumSystemVersion'] = '12.0'
        self.info['CFBundleIdentifier'] = 'other.app'
        self.write_info()
        with self.assertRaisesRegex(ValueError, 'bundle ID'):
            self.build()

    def test_bad_signature_architecture_and_duplicate_output_rejected(self):
        with mock.patch.object(packaging, 'run', side_effect=subprocess.CalledProcessError(1, 'codesign')):
            with self.assertRaises(subprocess.CalledProcessError):
                self.build()
        with mock.patch.object(packaging, 'run', side_effect=lambda args, **kw: 'x86_64' if args[0] == 'lipo' else b''):
            with self.assertRaisesRegex(ValueError, 'architecture mismatch'):
                self.build()
        self.build()
        with self.assertRaisesRegex(ValueError, 'already exist'):
            self.build()


    def test_manager_rejects_source_commit_without_updater(self):
        tree = b'100644 blob 1\tTelegramManager.app/Contents/Resources/launcher.swift\0'
        def git_runner(args, **kwargs):
            if '-t' in args:
                return 'commit\n'
            return tree if 'ls-tree' in args else b'// synthetic source'
        with mock.patch.object(packaging, 'run', side_effect=git_runner):
            with self.assertRaisesRegex(ValueError, 'required updater file'):
                packaging.package_release('manager', '2.3.4', COMMIT, 'arm64', self.root / 'manager-out')
        self.assertFalse((self.root / 'manager-out/manager.json').exists())

    def test_manager_uses_only_selected_git_blobs_and_stages_version(self):
        files = {
            'Contents/Info.plist': plistlib.dumps(dict(self.info, CFBundleIdentifier='com.local.telegram-manager', CFBundleExecutable='launcher.sh')),
            'Contents/Resources/launcher.swift': b'// synthetic source',
            'Contents/Resources/release_runtime.py': b'# synthetic runtime',
            'Contents/Resources/managed_releases.py': b'# synthetic backend',
            'Contents/MacOS/launcher.sh': b'#!/bin/sh\n',
            'Contents/MacOS/launcher_chrome.sh': b'#!/bin/sh\n# developer alternate\n',
        }
        objects = {str(index): content for index, content in enumerate(files.values())}
        tree = b'\0'.join(f'{"100755" if name.endswith("launcher.sh") else "100644"} blob {index}\tTelegramManager.app/{name}'.encode() for index, name in enumerate(files)) + b'\0'
        def git_runner(args, **kwargs):
            if args[0] == 'git':
                if '-t' in args:
                    return 'commit\n'
                if 'ls-tree' in args:
                    self.assertIn(COMMIT, args)
                    return tree
                return objects[args[-1]]
            if args[0] == 'codesign' and '--sign' in args:
                self.assertFalse((Path(args[-1]) / 'Contents/MacOS/launcher_chrome.sh').exists())
            return self.fake_run(args, **kwargs)
        with mock.patch.object(packaging, 'run', side_effect=git_runner):
            manifest = packaging.package_release('manager', '2.3.4', COMMIT, 'arm64', self.root / 'manager-out')
        self.assertEqual(manifest['app_version'], '2.3.4')
        self.assertEqual(manifest['data_compatibility'], 'manager-state-v1')
        with tarfile.open(self.root / 'manager-out/manager.tar.gz') as tar:
            staged_info = plistlib.loads(tar.extractfile('TelegramManager.app/Contents/Info.plist').read())
            self.assertEqual(staged_info['CFBundleVersion'], '2.3.4')
            self.assertEqual(staged_info['LSMinimumSystemVersion'], '12.0')
            marker = json.load(tar.extractfile('TelegramManager.app/Contents/Resources/managed-release.json'))
            self.assertEqual(marker['version'], '2.3.4')
            self.assertIsNotNone(tar.extractfile('TelegramManager.app/Contents/MacOS/launcher_swift'))
            self.assertNotIn('TelegramManager.app/Contents/MacOS/launcher_chrome.sh', tar.getnames())
            self.assertIsNotNone(tar.extractfile('TelegramManager.app/Contents/MacOS/launcher.sh'))
        self.assertTrue(any(command[0] == 'swiftc' for command in self.commands))
        self.assertTrue(any('--sign' in command for command in self.commands))
        self.assertEqual(self.info['CFBundleShortVersionString'], '7.2.9')


    def test_manager_requires_both_regular_executable_launchers(self):
        cases = [
            ('different.sh', 'valid', 'valid', 'Unexpected bundle executable'),
            ('launcher.sh', 'missing', 'valid', 'launcher.sh'),
            ('launcher.sh', 'nonexecutable', 'valid', 'launcher.sh'),
            ('launcher.sh', 'valid', 'missing', 'launcher_swift'),
            ('launcher.sh', 'valid', 'nonexecutable', 'launcher_swift'),
            ('launcher.sh', 'valid', 'symlink', 'launcher_swift'),
        ]
        for index, (executable, shell_mode, native_mode, failure) in enumerate(cases):
            with self.subTest(executable=executable, shell=shell_mode, native=native_mode):
                def invalid_stage(repo, commit, app, version, architecture):
                    macos = app / 'Contents/MacOS'
                    macos.mkdir(parents=True)
                    info = dict(self.info, CFBundleIdentifier='com.local.telegram-manager', CFBundleExecutable=executable)
                    (app / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
                    for name, mode in (('launcher.sh', shell_mode), ('launcher_swift', native_mode)):
                        path = macos / name
                        if mode == 'missing':
                            continue
                        if mode == 'symlink':
                            path.symlink_to('launcher.sh')
                        else:
                            path.write_bytes(b'fixture')
                            path.chmod(0o755 if mode == 'valid' else 0o644)
                with mock.patch.object(packaging, 'stage_manager', side_effect=invalid_stage):
                    with self.assertRaisesRegex(ValueError, failure):
                        packaging.package_release('manager', '2.3.4', COMMIT, 'arm64', self.root / f'invalid-manager-{index}')



if __name__ == '__main__':
    unittest.main()
