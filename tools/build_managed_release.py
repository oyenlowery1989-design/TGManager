#!/usr/bin/env python3
"""Build local managed-release assets; never upload or modify an installed app."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile

REPOSITORY = 'oyenlowery1989-design/TGManager'
ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = {
    'manager': ('TelegramManager.app', 'com.local.telegram-manager', 'manager-state-v1'),
    'ayugram-custom': ('AyuGram.app', 'one.telegrammanager.AyuGramInlineTest',
                       'ayugram-7.2.9-inline-v1'),
}
FORBIDDEN = {'tdata', 'telegramforceportable', 'profiles', 'profile', '.ayugram',
             'backups', '__pycache__', 'manager_config.json', 'manager_data.json',
             'manager_workspaces.json', '.ayugram-inline-test'}


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs).stdout


def validate_tree(app):
    """Reject private state and unsafe filesystem entries without following links."""
    app = Path(app)
    if app.is_symlink() or not app.is_dir():
        raise ValueError('Application must be a real directory, not a symlink')
    root = app.resolve()
    for directory, dirs, files in os.walk(app, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if name.casefold() in FORBIDDEN or name.casefold().endswith(('.pyc', '.pyo')):
                raise ValueError(f'Runtime/profile content is forbidden: {path.relative_to(app)}')
            mode = path.lstat()
            if stat.S_ISLNK(mode.st_mode):
                target = os.readlink(path)
                try:
                    resolved = path.resolve()
                except RuntimeError as error:
                    raise ValueError(f'Symlink cycle: {path.relative_to(app)}') from error
                if os.path.isabs(target) or not resolved.is_relative_to(root):
                    raise ValueError(f'Unsafe symlink: {path.relative_to(app)}')
                if not path.exists():
                    raise ValueError(f'Dangling symlink: {path.relative_to(app)}')
            elif stat.S_ISREG(mode.st_mode):
                if mode.st_nlink != 1:
                    raise ValueError(f'Hardlinked file: {path.relative_to(app)}')
            elif not stat.S_ISDIR(mode.st_mode):
                raise ValueError(f'Special file: {path.relative_to(app)}')


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(app):
    # Load the pure runtime helper only; importing server would load user state.
    path = ROOT / 'TelegramManager.app/Contents/Resources/release_runtime.py'
    spec = importlib.util.spec_from_file_location('managed_release_runtime', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.fingerprint(str(app))


def stage_manager(repo, commit, app, version, architecture):
    prefix = 'TelegramManager.app/'
    if run(['git', '-C', str(repo), 'cat-file', '-t', commit], text=True).strip() != 'commit':
        raise ValueError('source_commit must identify a Git commit, not a tree or blob')
    entries = run(['git', '-C', str(repo), 'ls-tree', '-rz', commit, '--', prefix])
    if not entries:
        raise ValueError('Source commit has no tracked Manager bundle')
    for entry in entries.split(b'\0'):
        if not entry:
            continue
        metadata, raw_path = entry.split(b'\t', 1)
        mode, kind, oid = metadata.decode().split()
        name = raw_path.decode('utf-8')
        relative = Path(name).relative_to(prefix)
        if '..' in relative.parts or kind != 'blob' or mode not in ('100644', '100755', '120000'):
            raise ValueError(f'Unsupported tracked entry: {name}')
        destination = app / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = run(['git', '-C', str(repo), 'cat-file', 'blob', oid])
        if mode == '120000':
            # Refuse links in source staging so no later write can traverse one.
            raise ValueError(f'Manager source must not contain symlinks: {name}')
        destination.write_bytes(content)
        destination.chmod(0o755 if mode == '100755' else 0o644)
    for name in ('release_runtime.py', 'managed_releases.py'):
        if not (app / 'Contents/Resources' / name).is_file():
            raise ValueError(f'Source commit lacks required updater file: {name}')
    validate_tree(app)
    info_path = app / 'Contents/Info.plist'
    with info_path.open('rb') as stream:
        info = plistlib.load(stream)
    info.update(CFBundleVersion=version, CFBundleShortVersionString=version,
                LSMinimumSystemVersion='12.0')
    with info_path.open('wb') as stream:
        plistlib.dump(info, stream)
    (app / 'Contents/Resources/managed-release.json').write_text(json.dumps({
        'schema': 1, 'component': 'manager', 'version': version,
        'data_compatibility': 'manager-state-v1',
    }, sort_keys=True) + '\n')
    run(['swiftc', str(app / 'Contents/Resources/launcher.swift'), '-o',
         str(app / 'Contents/MacOS/launcher_swift'), '-target',
         f'{architecture}-apple-macos12.0', '-framework', 'Cocoa', '-framework',
         'WebKit', '-framework', 'Foundation', '-O'])
    run(['codesign', '--force', '--deep', '--sign', '-', str(app)])


def package_release(component, version, source_commit, architecture, output,
                    app=None, patch=None, upstream_commit=None, repo=ROOT):
    if component not in COMPONENTS:
        raise ValueError('Unknown component')
    if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', version):
        raise ValueError('Version must be strict X.Y.Z')
    if not re.fullmatch(r'[0-9a-f]{40}', source_commit):
        raise ValueError('source_commit must be a lowercase 40-character Git commit')
    if architecture not in ('arm64', 'x86_64'):
        raise ValueError('Architecture must be arm64 or x86_64')
    bundle, bundle_id, compatibility = COMPONENTS[component]
    output = Path(output).resolve()
    if component == 'ayugram-custom':
        if not app or not patch or not re.fullmatch(r'[0-9a-f]{40}', upstream_commit or ''):
            raise ValueError('Custom AyuGram requires --app, --patch and --upstream-commit')
        app = Path(app)
        validate_tree(app)
        run(['codesign', '--verify', '--deep', '--strict', str(app)])
        if output.is_relative_to(app.resolve()):
            raise ValueError('Output must be outside the input app')
        if not Path(patch).is_file() or Path(patch).is_symlink():
            raise ValueError('Patch must be a regular source patch file')
    elif app or patch or upstream_commit:
        raise ValueError('Manager source comes only from its Git commit; omit custom arguments')
    if output.is_relative_to((Path(repo) / 'TelegramManager.app').resolve()):
        raise ValueError('Output must be outside the Manager app')
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'{component}.tar.gz'
    manifest_path = output / f'{component}.json'
    if any(path.exists() or path.is_symlink() for path in (archive, manifest_path)):
        raise ValueError('Output assets already exist; choose an empty release directory')
    with tempfile.TemporaryDirectory(prefix='managed-release-') as temporary:
        staged = Path(temporary) / bundle
        if component == 'manager':
            stage_manager(repo, source_commit, staged, version, architecture)
        else:
            shutil.copytree(app, staged, symlinks=True)
        validate_tree(staged)
        with (staged / 'Contents/Info.plist').open('rb') as stream:
            info = plistlib.load(stream)
        if info.get('CFBundleIdentifier') != bundle_id:
            raise ValueError(f'Unexpected bundle ID; expected {bundle_id}')
        minimum = info.get('LSMinimumSystemVersion', '')
        if not isinstance(minimum, str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){0,2}', minimum):
            raise ValueError('Missing or invalid minimum macOS version')
        minimum_parts = tuple(map(int, minimum.split('.')))
        if (minimum_parts + (0,) * (3 - len(minimum_parts))) > (12, 0, 0):
            raise ValueError('Bundle requires a macOS version newer than 12.0')
        app_version = info.get('CFBundleShortVersionString')
        executable = info.get('CFBundleExecutable')
        if not isinstance(app_version, str) or not app_version:
            raise ValueError('Bundle has no app version')
        expected_executable = 'launcher.sh' if component == 'manager' else 'AyuGram Inline Test'
        if executable != expected_executable:
            raise ValueError(f'Unexpected bundle executable; expected {expected_executable}')
        executables = [expected_executable] + (['launcher_swift'] if component == 'manager' else [])
        for name in executables:
            path = staged / 'Contents/MacOS' / name
            if not path.is_file() or path.is_symlink() or not path.stat().st_mode & 0o111:
                raise ValueError(f'Missing regular executable file: {name}')
        binary = staged / 'Contents/MacOS' / executables[-1]
        actual_arch = run(['lipo', '-archs', str(binary)], text=True).strip().split()
        if actual_arch != [architecture]:
            raise ValueError(f'Binary architecture mismatch: {actual_arch}')
        run(['codesign', '--verify', '--deep', '--strict', str(staged)])
        manifest = dict(schema=1, component=component, version=version,
                        app_version=app_version, bundle_id=bundle_id, bundle=bundle,
                        archive=archive.name, fingerprint=fingerprint(staged),
                        source_commit=source_commit, architecture=architecture,
                        min_macos='12.0', data_compatibility=compatibility)
        if component == 'ayugram-custom':
            manifest.update(patch_sha256=sha256(patch), upstream_commit=upstream_commit)
        temporary_archive = Path(temporary) / archive.name
        with tarfile.open(temporary_archive, 'w:gz', dereference=False) as tar:
            tar.add(staged, arcname=bundle)
        manifest.update(sha256=sha256(temporary_archive), size=temporary_archive.stat().st_size)
        # Publish the manifest last so an interrupted run cannot advertise a partial archive.
        shutil.copyfile(temporary_archive, archive)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('component', choices=COMPONENTS)
    parser.add_argument('--version', required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--architecture', choices=('arm64', 'x86_64'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repo', type=Path, default=ROOT, help='Manager source Git repository')
    parser.add_argument('--app', type=Path, help='Built custom AyuGram app (never a profile)')
    parser.add_argument('--patch', type=Path, help='Complete custom source patch')
    parser.add_argument('--upstream-commit')
    args = parser.parse_args()
    try:
        manifest = package_release(**vars(args))
    except (ValueError, OSError, subprocess.CalledProcessError, plistlib.InvalidFileException) as error:
        parser.exit(1, f'Release build failed: {error}\n')
    print(f"Built {args.component}-v{manifest['version']} for {REPOSITORY}: {args.output}")


if __name__ == '__main__':
    main()
