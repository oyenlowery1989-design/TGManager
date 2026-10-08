"""Optional AyuGram launch path. Telegram remains the default client.

The official release has no Developer ID signature. Authenticate its complete
payload against a manifest derived from the checksum-verified publisher DMG.
Do not alter signatures, quarantine or the default Telegram verification.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime

import state

CLONE_NAME = ".ayugram-client.app"
EXECUTABLE = "AyuGram"
_install_lock = threading.RLock()


def release_manifest():
    with open(Path(__file__).with_name("ayugram_release.json"), encoding="utf-8") as stream:
        return json.load(stream)


def master_path():
    return os.path.join(state.DATA_DIR, "_apps", "macOS", "AyuGram.app")


def clone_path(account_path):
    return os.path.join(account_path, CLONE_NAME)


def pid_from_ps(account_path, output):
    if not isinstance(output, str) or not output:
        return None
    binary = os.path.join(clone_path(account_path), "Contents", "MacOS", EXECUTABLE)
    for line in output.splitlines():
        try:
            pid, args = line.strip().split(None, 1)
            if args == binary or args.startswith(binary + " "):
                return int(pid)
        except (ValueError, IndexError):
            continue
    return None


def verify_bundle(path):
    """Check every payload file, refusing symlinks, additions and modifications."""
    root = Path(path)
    try:
        if root.is_symlink() or not root.is_dir():
            return False, "AyuGram source must be a real app bundle"
        expected = release_manifest()["files"]
        found = set()
        for directory, dirs, names in os.walk(root, followlinks=False):
            for name in dirs:
                if (Path(directory) / name).is_symlink():
                    return False, "AyuGram bundle contains a symbolic link"
            for name in names:
                item = Path(directory) / name
                if item.is_symlink() or not item.is_file():
                    return False, "AyuGram bundle contains an invalid file"
                # Finder bookkeeping is never loaded as executable app content.
                if name == ".DS_Store":
                    continue
                relative = item.relative_to(root).as_posix()
                if relative not in expected:
                    return False, f"AyuGram bundle contains an unrecognized file: {relative}"
                digest = hashlib.sha256()
                with item.open("rb") as stream:
                    while block := stream.read(1024 * 1024):
                        digest.update(block)
                if digest.hexdigest() != expected[relative]:
                    return False, "AyuGram does not match the supported official release"
                found.add(relative)
        if found != set(expected):
            return False, "AyuGram bundle is incomplete"
        return True, ""
    except (OSError, ValueError, KeyError) as error:
        state._log.warning("AyuGram verification failed: %s", error)
        return False, "Could not verify AyuGram; choose the supported official release"


def install(source):
    """Verify source and copied master before replacement; preserve Telegram."""
    import server
    with _install_lock:
        output = server._get_ps_output(force=True, allow_stale=False)
        if output is None:
            return False, "Could not check running clients; try again"
        if any(pid_from_ps(account["path"], output) for account in server.scan_accounts()):
            return False, "Close AyuGram accounts before replacing its shared app"
        target = master_path()
        if os.path.realpath(source) == os.path.realpath(target):
            return False, "Choose the official AyuGram.app download, not the current shared copy"
        ok, message = verify_bundle(source)
        if not ok:
            return False, message
        parent = os.path.dirname(target)
        data_root = os.path.realpath(state.DATA_DIR)
        if os.path.commonpath((os.path.realpath(parent), data_root)) != data_root:
            return False, "Shared AyuGram directory resolves outside the data folder"
        if os.path.lexists(target) and (os.path.islink(target) or not os.path.isdir(target)):
            return False, "Shared AyuGram path must be a real app directory"
        os.makedirs(parent, exist_ok=True)
        workspace = tempfile.mkdtemp(prefix=".ayugram-install-", dir=parent)
        temporary = os.path.join(workspace, "AyuGram.app")
        previous = os.path.join(workspace, "previous.app")
        try:
            ok, message = server._copy_app_bundle(source, temporary)
            if not ok:
                return False, "Could not copy AyuGram: " + message
            ok, message = verify_bundle(temporary)
            if not ok:
                return False, message
            if os.path.lexists(target):
                if os.path.islink(target):
                    return False, "Shared AyuGram path contains a symbolic link"
                os.rename(target, previous)
            try:
                os.rename(temporary, target)
            except OSError:
                if os.path.exists(previous):
                    os.rename(previous, target)
                raise
            return True, "AyuGram is installed; Telegram remains the default"
        except OSError as error:
            state._log.warning("AyuGram installation failed: %s", error)
            # If rollback failed, preserve previous for manual recovery.
            if os.path.exists(previous) and not os.path.exists(target):
                return False, f"Installation failed; previous AyuGram is preserved at {previous}"
            return False, "Could not install AyuGram"
        finally:
            if not (os.path.exists(previous) and not os.path.exists(target)):
                shutil.rmtree(workspace, ignore_errors=True)


@state.serialize_account_op(lambda path: path, (False, state._BUSY_MSG))
def open_account(path):
    """Open the existing portable session, taking a backup before AyuGram writes."""
    import server
    if not state.is_managed_account_path(path):
        return False, "Choose an existing account with portable session data"
    with _install_lock:
        output = server._get_ps_output(force=True, allow_stale=False)
        if output is None:
            return False, "Could not check running clients; try again"
        if pid_from_ps(path, output):
            return True, "already running"
        if server._telegram_pid_from_ps_output(path, output):
            return False, "Close Telegram for this account before opening AyuGram"
        master = master_path()
        if not os.path.isdir(master):
            return False, "Choose AyuGram.app in Settings → Advanced first"
        app = clone_path(path)
        server._watcher_exempt(path)
        ok, message = server._copy_app_bundle(master, app)
        if not ok:
            return False, "Could not prepare AyuGram: " + message
        ok, message = verify_bundle(app)
        if not ok:
            return False, message
        ok, message, _ = server.backup_account(path, os.path.basename(path))
        if not ok:
            return False, "Backup before opening AyuGram failed: " + message
        # Recheck after copying/backing up: an external client could have started.
        output = server._get_ps_output(force=True, allow_stale=False)
        if output is None or server._telegram_pid_from_ps_output(path, output):
            return False, "A client started for this account; close it before switching"
        if not state.is_managed_account_path(path):
            return False, "Account path changed before launch"
        with state._meta_lock:
            account_proxy = state.metadata.get("proxies", {}).get(path)
        if account_proxy and account_proxy.get("host") and state.config.get("proxy_system_apply"):
            server.apply_proxy(account_proxy)
        try:
            result = subprocess.run(["open", "-n", app], capture_output=True, timeout=10)
            if result.returncode != 0:
                return False, "macOS could not open AyuGram; check the app's launch permissions"
        except (OSError, subprocess.TimeoutExpired):
            return False, "Could not launch AyuGram on this machine"
        for _ in range(20):
            if pid_from_ps(path, server._get_ps_output(force=True, allow_stale=False)):
                with state._meta_lock:
                    state.metadata.setdefault("last_opened", {})[path] = datetime.now().isoformat()
                    state.save_metadata(state.metadata)
                server.invalidate_tdata_size(path)
                server.invalidate_scan_cache()
                return True, "AyuGram opened; a session backup was saved first"
            time.sleep(0.5)
        return False, "AyuGram did not start within 10 seconds; your pre-launch backup was kept"
