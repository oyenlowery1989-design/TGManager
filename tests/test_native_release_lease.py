"""Exercise the actual Swift launch guard without starting a UI or any account."""

import plistlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE = (Path(__file__).resolve().parents[1] / 'TelegramManager.app' /
          'Contents/Resources/launcher.swift')


class LauncherCompileLogTests(unittest.TestCase):
    def test_compile_log_stays_outside_the_signed_bundle(self):
        with tempfile.TemporaryDirectory(prefix='tm-launch-log-') as folder:
            root = Path(folder)
            app = root / 'Manager.app'
            macos = app / 'Contents/MacOS'
            resources = app / 'Contents/Resources'
            macos.mkdir(parents=True)
            resources.mkdir()
            (root / 'data').mkdir()
            (resources / 'launcher.swift').write_text('// compilation fixture')
            script = macos / 'launcher.sh'
            shutil.copy2(SOURCE.parent.parent / 'MacOS/launcher.sh', script)
            commands = root / 'commands'
            commands.mkdir()
            stubs = {
                'pgrep': 'exit 1', 'osascript': 'exit 0', 'codesign': 'exit 0',
                'swiftc': 'while [ "$1" != "-o" ]; do shift; done\n'
                          'printf "#!/bin/sh\\nexit 0\\n" > "$2"\n'
                          'echo "compiler diagnostic" >&2',
            }
            for name, body in stubs.items():
                command = commands / name
                command.write_text('#!/bin/sh\n' + body + '\n')
                command.chmod(0o755)
            result = subprocess.run(['bash', str(script)], capture_output=True,
                                    text=True, timeout=10, env={**os.environ,
                                    'PATH': str(commands) + ':/usr/bin:/bin',
                                    'TG_RELEASE_LAUNCH_GUARD': '1',
                                    'TG_SESSION_TOKEN': 'fixture'})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(sorted(p.name for p in app.iterdir()), ['Contents'])
            self.assertEqual((root / 'data/launcher_compile.log').read_text(),
                             'compiler diagnostic\n')


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'),
                     'Native launcher requires macOS and Swift')
class NativeReleaseLeaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='tm-native-lease-build-')
        cls.addClassCleanup(cls.build.cleanup)
        root = Path(cls.build.name)
        source = root / 'main.swift'
        # Compile the production guard, stopping before any application/UI code.
        source.write_text(SOURCE.read_text().split('func readSessionToken()', 1)[0]
                          + '\nexit(holdReleaseLease() ? 0 : 1)\n')
        cls.binary = root / 'guard'
        subprocess.run(['swiftc', '-module-cache-path', str(root / 'cache'),
                        str(source), '-o', str(cls.binary)],
                       check=True, capture_output=True, text=True, timeout=180)

    def setUp(self):
        fixture = tempfile.TemporaryDirectory(prefix='tm-native-lease-')
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        app = self.root / 'Manager.app'
        contents = app / 'Contents'
        executable_dir = contents / 'MacOS'
        executable_dir.mkdir(parents=True)
        (contents / 'Info.plist').write_bytes(plistlib.dumps({
            'CFBundleExecutable': 'guard', 'CFBundleIdentifier': 'test.lease',
            'CFBundlePackageType': 'APPL',
        }))
        self.executable = executable_dir / 'guard'
        shutil.copy2(self.binary, self.executable)
        (self.root / 'data').mkdir()
        self.updates = self.root / 'data/ReleaseUpdates'

    def launch(self):
        return subprocess.run([str(self.executable)], capture_output=True,
                              text=True, timeout=5).returncode

    def test_relaunch_accepts_existing_update_directory_and_lock(self):
        self.assertEqual(self.launch(), 0)
        self.assertEqual(self.launch(), 0)

    def test_existing_directory_is_accepted_before_lock_exists(self):
        self.updates.mkdir()
        self.assertEqual(self.launch(), 0)

    def test_regular_file_is_rejected_as_update_directory(self):
        self.updates.write_text('not a directory')
        self.assertEqual(self.launch(), 1)
        self.assertEqual(self.updates.read_text(), 'not a directory')

    def test_symlink_update_directory_is_rejected(self):
        target = self.root / 'elsewhere'
        target.mkdir()
        self.updates.symlink_to(target, target_is_directory=True)
        self.assertEqual(self.launch(), 1)
        self.assertEqual(list(target.iterdir()), [])

    def test_pending_transaction_still_blocks_launch(self):
        self.updates.mkdir()
        transaction = self.updates / 'transaction.json'
        transaction.write_text('{}')
        self.assertEqual(self.launch(), 1)
        self.assertEqual(transaction.read_text(), '{}')


if __name__ == '__main__':
    unittest.main()
