#!/usr/bin/env python3
"""Package a separate edition without including account data or client binaries."""
import argparse
import json
from pathlib import Path
import plistlib
import shutil
import zipfile

REPO = Path(__file__).resolve().parents[1]
APP_NAME = 'Telegram Manager Ayu.app'

def build(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / APP_NAME
    if target.exists():
        raise FileExistsError(f'Refusing to replace {target}')
    source = REPO / 'TelegramManager.app'
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(
        '__pycache__', '.DS_Store', '_CodeSignature', 'launcher_swift*',
        '*.pre-fix-backup', '*.stale-backup'))
    contents = target / 'Contents'
    resources = contents / 'Resources'
    profile = {'edition': 'ayugram-isolated', 'schema': 1}
    (resources / 'installation.json').write_text(json.dumps(profile) + '\n')
    with (contents / 'Info.plist').open('rb') as f:
        info = plistlib.load(f)
    info.update(CFBundleName='Telegram Manager Ayu',
                CFBundleDisplayName='Telegram Manager Ayu',
                CFBundleIdentifier='com.local.telegram-manager.ayugram',
                CFBundleVersion='1.1', CFBundleShortVersionString='1.1',
                CFBundleExecutable='launcher.sh')
    with (contents / 'Info.plist').open('wb') as f:
        plistlib.dump(info, f)
    # Remove alternate legacy entry points which use old shared storage.
    for name in ('launcher_chrome.sh',):
        (contents / 'MacOS' / name).unlink(missing_ok=True)
    (resources / 'Install_NativeApp.command').unlink(missing_ok=True)
    shutil.copyfile(REPO / 'tools/isolated_launcher.sh', contents / 'MacOS/launcher.sh')
    (contents / 'MacOS/launcher.sh').chmod(0o755)
    html = resources / 'index.html'
    html.write_text(html.read_text().replace('Telegram Manager', 'Telegram Manager Ayu'))
    archive = output / 'Telegram-Manager-Ayu-macOS.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for item in sorted(target.rglob('*')):
            z.write(item, item.relative_to(output))
        z.write(REPO / 'docs/SEPARATE_MAC_APP.md', 'INSTALL.md')
        for name in ('SEPARATE_MAC_APP.md', 'OPEN_WITH_AYUGRAM.md', 'AYUGRAM_RELEASE_ASSESSMENT.md'):
            z.write(REPO / 'docs' / name, name)
    return target, archive

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', help='New artifact directory outside the source app')
    args = parser.parse_args()
    for artifact in build(args.output):
        print(artifact)
