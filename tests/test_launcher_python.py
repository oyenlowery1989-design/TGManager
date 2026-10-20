"""Probe synthetic Python executables; never start the app or an account."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / 'TelegramManager.app/Contents/MacOS/launcher.sh'
SWIFT = ROOT / 'TelegramManager.app/Contents/Resources/launcher.swift'


class PythonSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='tm-python-probe-')
        cls.addClassCleanup(cls.build.cleanup)
        cls.swift_binary = None
        if shutil.which('swiftc'):
            source = Path(cls.build.name) / 'main.swift'
            selector = SWIFT.read_text().split('func selectPython()', 1)[1].split('func readSessionToken()', 1)[0]
            # Redirect standard installation paths to the isolated fixture tree.
            for path in ['/opt/homebrew', '/usr/local', '/Library/Frameworks', '/usr/bin']:
                selector = selector.replace('"' + path, '"' + cls.build.name + path)
            source.write_text('import Foundation\nfunc selectPython()' + selector + '\nprint(selectPython() ?? "MISSING")\n')
            cls.swift_binary = Path(cls.build.name) / 'probe'
            subprocess.run(['swiftc', '-module-cache-path', cls.build.name + '/cache',
                            str(source), '-o', str(cls.swift_binary)],
                           check=True, capture_output=True, timeout=180)

    def setUp(self):
        self.base = Path(self.build.name)
        for name in ['opt', 'usr', 'Library']:
            shutil.rmtree(self.base / name, ignore_errors=True)
        self.log = self.base / 'calls'
        self.log.write_text('')
        self.env = {**os.environ, 'PATH': '/usr/bin:/bin', 'PROBE_LOG': str(self.log)}
        self.env.pop('TG_PYTHON', None)

    def executable(self, relative, success=True):
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\nprintf "%s\\n" "$0" >> "$PROBE_LOG"\nexit ' + ('0' if success else '1') + '\n')
        path.chmod(0o755)
        return path

    def probe(self, native=False):
        if native:
            if self.swift_binary is None:
                self.skipTest('Swift compiler unavailable')
            command = [str(self.swift_binary)]
        else:
            selector = SHELL.read_text().split('select_python() {', 1)[1].split('if ! TG_PYTHON=', 1)[0]
            for path in ['/opt/homebrew', '/usr/local', '/Library/Frameworks', '/usr/bin']:
                selector = selector.replace(path, self.build.name + path)
            command = ['/bin/bash', '-c', 'select_python() {' + selector + '\nselect_python || echo MISSING']
        return subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=10).stdout.strip()

    def test_external_python_with_finder_path_never_invokes_system_stub(self):
        selected = self.executable('opt/homebrew/bin/python3')
        self.executable('usr/bin/python3', False)
        self.executable('usr/bin/xcode-select', False)
        for native in [False, True]:
            with self.subTest(native=native):
                self.log.write_text('')
                self.assertEqual(self.probe(native), str(selected))
                self.assertEqual(self.log.read_text().splitlines(), [str(selected)])

    def test_failed_or_old_python_falls_through_to_python_org(self):
        self.executable('opt/homebrew/bin/python3', False)
        selected = self.executable('Library/Frameworks/Python.framework/Versions/3.13/bin/python3')
        for native in [False, True]:
            with self.subTest(native=native):
                self.assertEqual(self.probe(native), str(selected))

    def test_selected_runtime_is_reused(self):
        selected = self.executable('custom/python3')
        self.executable('opt/homebrew/bin/python3')
        self.env['TG_PYTHON'] = str(selected)
        for native in [False, True]:
            with self.subTest(native=native):
                self.assertEqual(self.probe(native), str(selected))

    def test_missing_python_does_not_invoke_system_stub_without_tools(self):
        stub = self.executable('usr/bin/python3')
        self.executable('usr/bin/xcode-select', False)
        for native in [False, True]:
            with self.subTest(native=native):
                self.assertEqual(self.probe(native), 'MISSING')
                self.assertNotIn(str(stub), self.log.read_text().splitlines())

    def test_system_python_requires_working_tools_and_probe(self):
        selected = self.executable('usr/bin/python3')
        self.executable('usr/bin/xcode-select')
        for native in [False, True]:
            with self.subTest(native=native):
                self.assertEqual(self.probe(native), str(selected))

    def test_guard_uses_selected_python_and_exports_it(self):
        selected = self.base / 'guard-python'
        selected.write_text('#!/bin/sh\n[ "$1" = -I ] && exit 0\nprintf "%s\\n%s\\n" "$TG_PYTHON" "$*"\n')
        selected.chmod(0o755)
        self.env['TG_PYTHON'] = str(selected)
        self.env.pop('TG_RELEASE_LAUNCH_GUARD', None)
        result = subprocess.run(['/bin/bash', str(SHELL)], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], str(selected))
        self.assertIn('release_runtime.py launch ', result.stdout)

    def test_all_shell_python_calls_use_selected_runtime(self):
        source = SHELL.read_text().split('export TG_PYTHON', 1)[1]
        self.assertNotIn('exec python3', source)
        self.assertNotIn('$(python3', source)
        self.assertNotIn('\npython3 ', source)
        self.assertIn('"$TG_PYTHON" "$RESOURCES/server.py" &', source)


if __name__ == '__main__':
    unittest.main()
