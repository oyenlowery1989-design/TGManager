"""Exercise packaged storage with a seeded old installation beside it."""
import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
import zipfile

REPO = Path(__file__).resolve().parents[1]

class IsolatedInstallTests(unittest.TestCase):
    def test_packaged_app_ignores_old_data_and_never_writes_parent_config(self):
        spec = importlib.util.spec_from_file_location('build_isolated', REPO / 'tools/build_isolated_app.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            app, archive = builder.build(base / 'Applications')
            old = base / 'support/TelegramManager'
            old.mkdir(parents=True)
            sentinel = old / 'manager_data.json'
            sentinel.write_text('{"notes":{"old":"private"}}')
            adjacent = base / 'Applications/data'
            adjacent.mkdir()
            (adjacent / 'manager_config.json').write_text('{"port":9999}')
            (base / 'Applications/TG').mkdir()
            before = {str(p): p.read_bytes() for p in base.rglob('*') if p.is_file()}
            code = '''
import os, sys, json
from pathlib import Path
support=Path(sys.argv[2])
original=os.path.expanduser
os.path.expanduser=lambda p: str(support / p.rsplit('/',1)[-1]) if p.startswith('~/Library/Application Support/') else original(p)
sys.path.insert(0,sys.argv[1])
import state
assert state.ISOLATED_INSTALL
assert state.ROOT_DIR == str(support/'TelegramManagerAyu/TG')
assert state.DATA_DIR == str(support/'TelegramManagerAyu/data')
assert state.PRIVATE_STATE_DIR == str(support/'TelegramManagerAyu')
assert state.config['port'] == 8478
assert state.config['extra_scan_dirs'] == []
assert state.metadata['notes'] == {}
state.save_config(state.config)
state.metadata['notes']['new']='isolated'
state.save_metadata(state.metadata)
print(json.dumps({'root':state.ROOT_DIR,'data':state.DATA_DIR}))
'''
            subprocess.run([sys.executable, '-c', code, str(app/'Contents/Resources'), str(base/'support')], check=True, capture_output=True, text=True)
            self.assertFalse((base/'Applications/manager_config.json').exists())
            for path, data in before.items():
                self.assertEqual(Path(path).read_bytes(), data)
            self.assertTrue((base/'support/TelegramManagerAyu/data/manager_config.json').exists())
            self.assertTrue((base/'support/TelegramManagerAyu/manager_data.json').exists())
            info = plistlib.loads((app/'Contents/Info.plist').read_bytes())
            self.assertEqual(info['CFBundleIdentifier'], 'com.local.telegram-manager.ayugram')
            self.assertEqual(info['CFBundleDisplayName'], 'Telegram Manager Ayu')
            self.assertTrue((app/'Contents/MacOS/launcher.sh').stat().st_mode & 0o111)
            for module in ('server.py', 'state.py', 'backups.py', 'proxy.py', 'keeper.py', 'ayugram.py'):
                self.assertTrue((app/'Contents/Resources'/module).is_file(), module)
            with zipfile.ZipFile(archive) as z:
                names = z.namelist()
                self.assertIn('INSTALL.md', names)
                self.assertFalse(any('/launcher_swift' in n or '/TG/' in n or '/data/' in n for n in names))
                launcher = z.getinfo('Telegram Manager Ayu.app/Contents/MacOS/launcher.sh')
                self.assertTrue((launcher.external_attr >> 16) & 0o111)
            with self.assertRaises(FileExistsError):
                builder.build(base/'Applications')

    def test_legacy_source_has_no_isolated_profile(self):
        self.assertFalse((REPO/'TelegramManager.app/Contents/Resources/installation.json').exists())

if __name__ == '__main__':
    unittest.main()
