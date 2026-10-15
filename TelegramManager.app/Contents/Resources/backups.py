"""Backup lifecycle: create, list, prune, delete, and restore tdata backups.

Depends on state.py (shared config/locks/helpers) and server.py (scan_accounts,
find_telegram_pid, invalidate_tdata_size — accessed only as `server.<attr>`
inside function bodies, never at import time, so the circular import back to
server.py is safe; see server.py's `sys.modules.setdefault` alias).
"""

import json
import hashlib
import os
import stat
import subprocess
import shutil
import sys
import time
import uuid
from datetime import datetime

import state
import server


def _copy_tdata_excluding_cache(src, dst, exclude_cache=True):
    """Use independent APFS clones when supported, otherwise rsync/cp.

    Skip only regenerable Telegram caches; legacy Ayu history stays whole.
    exclude_cache=False also preserves complete isolated Ayu/restore copies.
    """
    caches = (state._find_cache_dirs(src) if exclude_cache
              and not os.path.lexists(os.path.join(src, "ayudata.db")) else [])
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["cp", "-cR", src, dst], capture_output=True, timeout=1800)
            if r.returncode == 0:
                for cache in caches:
                    shutil.rmtree(os.path.join(dst, os.path.relpath(cache, src)))
                return True, ""
        except (OSError, subprocess.TimeoutExpired):
            pass
        shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst, exist_ok=True)
    cmd = ["rsync", "-a"]
    for cache in caches:
        relative = os.path.relpath(cache, src).replace(os.sep, "/")
        cmd += ["--exclude", "/" + relative + "/"]
    cmd += [src.rstrip("/") + "/", dst.rstrip("/") + "/"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=1800)
        if r.returncode == 0:
            return True, ""
        state._log.warning("backup rsync rc=%d, falling back to cp: %s",
                     r.returncode, r.stderr.decode(errors="replace").strip()[:200])
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        state._log.warning("backup rsync unavailable (%s); falling back to cp", e)
    try:
        rm_r = subprocess.run(["rm", "-rf", dst], capture_output=True, timeout=300)
        if rm_r.returncode != 0:
            return False, "could not clear destination: " + rm_r.stderr.decode(errors="replace").strip()[:200]
        r = subprocess.run(["cp", "-R", src, dst], capture_output=True, timeout=1800)
        if r.returncode == 0:
            for cache in caches:
                shutil.rmtree(os.path.join(dst, os.path.relpath(cache, src)))
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, "copy timed out" if isinstance(e, subprocess.TimeoutExpired) else str(e)
    return (r.returncode == 0), ("" if r.returncode == 0
                                 else r.stderr.decode(errors="replace").strip()[:200])


def _safe_tree(path):
    """A profile copy must never follow a link into another account or outside it."""
    current = os.path.abspath(path)
    roots = [state.ROOT_DIR, state.DATA_DIR] + state.config.get("extra_scan_dirs", [])
    real_path = os.path.realpath(current)
    boundary = next((os.path.realpath(root) for root in roots
                     if real_path.startswith(os.path.realpath(root) + os.sep)), None)
    if boundary is None:
        return False
    while os.path.realpath(current) != boundary:
        if os.path.islink(current):
            return False
        current = os.path.dirname(current)
    if not os.path.lexists(path):
        return True
    def walk_error(error):
        raise error
    try:
        for root, dirs, files in os.walk(path, onerror=walk_error):
            if any(os.path.islink(os.path.join(root, name)) for name in dirs + files):
                return False
        return True
    except OSError:
        return False


def _account_closed(account_path):
    output = server._get_ps_output(force=True, allow_stale=False)
    return output is not None and not server._telegram_pid_from_ps_output(account_path, output)


def _portable_path(account_path, client):
    if client == "Telegram":
        return os.path.join(account_path, "TelegramForcePortable")
    if client == "AyuGram":
        return os.path.join(account_path, ".ayugram", "TelegramForcePortable")
    return None


def _valid_profile(tdata, login_marker=None):
    for name in ("key_datas", "key_data0", "key_data1"):
        path = os.path.join(tdata, name)
        try:
            if os.path.isfile(path) and not os.path.islink(path) and os.path.getsize(path) > 0:
                return True
        except OSError:
            continue
    if login_marker and os.path.isfile(login_marker) and not os.path.islink(login_marker):
        try:
            with open(login_marker, encoding="utf-8") as f:
                return json.load(f) == {"new_login": True}
        except (OSError, ValueError):
            pass
    return False


def _tree_inventory(path, exclude_cache=False):
    """Hash regular files only; links and changing files cannot certify a backup."""
    excluded = set(state._find_cache_dirs(path) if exclude_cache
                   and not os.path.lexists(os.path.join(path, "ayudata.db")) else [])
    inventory = {}
    def walk_error(error):
        raise error
    for root, dirs, files in os.walk(path, onerror=walk_error):
        for name in list(dirs):
            directory = os.path.join(root, name)
            if os.path.islink(directory):
                raise OSError("Profile contains a symbolic link")
            if directory in excluded:
                dirs.remove(name)
        for name in files:
            file = os.path.join(root, name)
            before = os.lstat(file)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink > 1:
                raise OSError("Profile contains a non-regular or hard-linked file")
            digest = hashlib.sha256()
            fd = os.open(file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as f:
                opened = os.fstat(f.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise OSError("Profile changed during verification")
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(chunk)
                after = os.fstat(f.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_nlink) != (
                    after.st_size, after.st_mtime_ns, after.st_nlink):
                raise OSError("Profile changed during verification")
            inventory[os.path.relpath(file, path).replace(os.sep, "/")] = {
                "size": before.st_size, "sha256": digest.hexdigest()}
    return inventory


def _backup_inventory(backup_dir):
    inventory = {"tdata/" + name: value for name, value in
                 _tree_inventory(os.path.join(backup_dir, "tdata")).items()}
    ayu = os.path.join(backup_dir, "ayugram")
    if os.path.isdir(ayu):
        inventory.update({"ayugram/" + name: value for name, value in _tree_inventory(ayu).items()})
    return inventory


def _verify_backup(backup_dir):
    try:
        if not _safe_tree(backup_dir) or not _valid_profile(os.path.join(backup_dir, "tdata")):
            return False, "Backup has no usable Telegram session key"
        ayu = os.path.join(backup_dir, "ayugram", "tdata")
        if os.path.isdir(ayu) and not _valid_profile(ayu, os.path.join(backup_dir, "ayugram", ".new-login.json")):
            return False, "Backup has no usable AyuGram session or explicit fresh login"
        manifest_file = os.path.join(backup_dir, "backup.json")
        if os.path.lexists(manifest_file):
            with open(manifest_file, encoding="utf-8") as f:
                manifest = json.load(f)
            if not isinstance(manifest, dict):
                return False, "Backup manifest is invalid"
            if "integrity" in manifest:
                integrity = manifest["integrity"]
                if (not isinstance(integrity, dict) or integrity.get("version") != 1
                        or not isinstance(integrity.get("files"), dict)
                        or integrity["files"] != _backup_inventory(backup_dir)):
                    return False, "Backup integrity check failed; existing session was preserved"
        return True, ""
    except (OSError, ValueError, TypeError) as e:
        state._log.warning("Backup verification failed for %s: %s", backup_dir, e)
        return False, "Backup could not be verified; existing session was preserved"


def _read_backup_manifest(backup_dir):
    """Return a valid backup manifest, or None for legacy/corrupt backups."""
    try:
        with open(os.path.join(backup_dir, "backup.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        if (isinstance(manifest, dict)
                and state.is_canonical_account_id(manifest.get("account_id"))
                and isinstance(manifest.get("account_name"), str) and manifest["account_name"].strip()
                and isinstance(manifest.get("created_at"), str)):
            return manifest
    except (OSError, ValueError, TypeError):
        pass
    return None


def list_backups():
    """Return all tdata backups found in ROOT_DIR/Backups/, newest first."""
    backup_root = os.path.join(state.DATA_DIR, "Backups")
    backups = []
    if not os.path.isdir(backup_root):
        return []
    for date_folder in sorted(os.listdir(backup_root), reverse=True):
        date_path = os.path.join(backup_root, date_folder)
        if not os.path.isdir(date_path):
            continue
        for account in sorted(os.listdir(date_path)):
            if account.endswith(".partial"):   # crashed mid-copy — not a valid backup
                continue
            acc_path  = os.path.join(date_path, account)
            tdata_src = os.path.join(acc_path, "tdata")
            if os.path.isdir(tdata_src) and _safe_tree(acc_path):
                manifest = _read_backup_manifest(acc_path)
                account_name = manifest["account_name"] if manifest else account
                clients = ["Telegram"]
                ayu_tdata = os.path.join(acc_path, "ayugram", "tdata")
                if os.path.isdir(ayu_tdata):
                    clients.append("AyuGram")
                size = state.get_folder_size(tdata_src)
                if "AyuGram" in clients:
                    size += state.get_folder_size(os.path.join(acc_path, "ayugram"))
                backups.append({
                    "date":       date_folder,
                    "account":    account_name,
                    "account_id": manifest["account_id"] if manifest else None,
                    "account_name": account_name,
                    "backup_path": acc_path,
                    "size":       size,
                    "clients":    clients,
                    "size_human": state.human_size(size),
                })
    def sort_key(backup):
        suffix = 1
        name = os.path.basename(backup["backup_path"])
        prefix = (backup["account_id"] or "") + "-"
        if backup["account_id"] and name.startswith(prefix) and name[len(prefix):].isdigit():
            suffix = int(name[len(prefix):])
        return backup["date"], suffix
    backups.sort(key=sort_key, reverse=True)
    return backups


_backup_map_cache = {"ts": 0.0, "map": {}}

def _last_backup_map():
    """Map stable account ID (or legacy name) → newest backup date folder.

    listdir-only (no sizes), cached 30 s — cheap enough for the accounts poll.
    """
    now = time.time()
    if now - _backup_map_cache["ts"] < 30:
        return _backup_map_cache["map"]
    backup_root = os.path.join(state.DATA_DIR, "Backups")
    result = {}
    if os.path.isdir(backup_root):
        try:
            for date_folder in sorted(os.listdir(backup_root), reverse=True):
                date_path = os.path.join(backup_root, date_folder)
                if not os.path.isdir(date_path):
                    continue
                for account in os.listdir(date_path):
                    if account.endswith(".partial"):
                        continue
                    manifest = _read_backup_manifest(os.path.join(date_path, account))
                    result.setdefault(manifest["account_id"] if manifest else account, date_folder)
        except OSError as e:
            state._log.warning("_last_backup_map: %s", e)
    _backup_map_cache["map"] = result
    _backup_map_cache["ts"] = now
    return result


def _resolve_backup_dir(backup_path):
    """Resolve a client-supplied backup path to its realpath, or None.

    Only accepts paths exactly two levels below DATA_DIR/Backups
    (Backups/<date>/<account>) so a crafted request can never touch the whole
    Backups tree, a live account's tdata, or anything outside Backups.
    """
    root_path = os.path.join(state.DATA_DIR, "Backups")
    backup_root = os.path.realpath(root_path)
    data_root = os.path.realpath(state.DATA_DIR)
    if os.path.islink(root_path) or os.path.commonpath((backup_root, data_root)) != data_root:
        return None
    candidate = os.path.abspath(str(backup_path or ""))
    real = os.path.realpath(candidate)
    lexical_rel = os.path.relpath(candidate, os.path.abspath(root_path))
    current = root_path
    for part in lexical_rel.split(os.sep):
        current = os.path.join(current, part)
        if os.path.islink(current):
            return None
    rel = os.path.relpath(real, backup_root)
    if rel.startswith("..") or len(rel.split(os.sep)) != 2:
        return None
    return real


@state.serialize_account_op(lambda backup_path: backup_path, (False, state._BUSY_MSG))
def delete_backup(backup_path):
    """Delete one backup folder (Backups/<date>/<account>). Returns (ok, msg)."""
    real = _resolve_backup_dir(backup_path)
    if real is None:
        return False, "Invalid backup path"
    if not os.path.isdir(real):
        return False, "Backup not found"
    try:
        r = subprocess.run(["rm", "-rf", real], capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, "Delete timed out"
    if r.returncode != 0:
        return False, "Delete failed: " + r.stderr.decode(errors="replace").strip()[:200]
    # drop the date folder too once its last account backup is gone
    try:
        os.rmdir(os.path.dirname(real))
    except OSError:
        pass
    state._log.info("Backup deleted: %s", real)
    _backup_map_cache["ts"] = 0.0
    return True, "Backup deleted"


def prune_backups(account_id):
    """Enforce backup_keep_per_account for one account (0 = keep all)."""
    try:
        keep = int(state.config.get("backup_keep_per_account", 0))
    except (TypeError, ValueError):
        keep = 0
    if keep <= 0:
        return
    mine = [b for b in list_backups() if b["account_id"] == account_id]  # newest first
    latest_ayu = next((b["backup_path"] for b in mine if "AyuGram" in b["clients"]
                       or os.path.lexists(os.path.join(b["backup_path"], "tdata", "ayudata.db"))), None)
    retained = mine[:keep] + [b for b in mine[keep:] if b["backup_path"] == latest_ayu]
    if any(not _verify_backup(b["backup_path"])[0] for b in retained):
        state._log.warning("prune_backups: retained backups could not be verified; keeping all versions")
        return
    for b in mine[keep:]:
        if b["backup_path"] == latest_ayu:
            continue
        ok, msg = delete_backup(b["backup_path"])
        if ok:
            state._log.info("prune_backups: removed old backup %s", b["backup_path"])
        else:
            state._log.warning("prune_backups: could not remove %s: %s", b["backup_path"], msg)


@state.serialize_account_op(lambda account_path, client="Telegram": account_path, False)
def recover_interrupted_restore(account_path, client="Telegram"):
    """Recover original tdata or finish the committed Ayu profile markers."""
    portable = _portable_path(account_path, client)
    if portable is None:
        return False
    marker = os.path.join(portable, ".tdata-restore.json")
    if not os.path.lexists(marker):
        return True
    if not _safe_tree(portable):
        return False
    if not (state.is_managed_account_path(account_path)
            or server._is_fresh_portable_account_path(account_path)):
        return False
    tdata = os.path.join(portable, "tdata")
    try:
        if os.path.islink(marker) or os.path.islink(tdata):
            return False
        with open(marker, encoding="utf-8") as f:
            journal = json.load(f)
        name = journal["backup"]
        if name is not None and (not isinstance(name, str) or not name.startswith("tdata.bak.")
                or os.path.basename(name) != name):
            return False
        original = os.path.join(portable, name) if name else None
        if original and os.path.islink(original):
            return False
        markers = journal.get("markers", {})
        if (not isinstance(markers, dict) or (markers and client != "AyuGram")
                or any(key not in (".common-profile.json", ".new-login.json")
                       or (value is not None and not isinstance(value, dict))
                       for key, value in markers.items())):
            return False
        committed = os.path.isdir(tdata) and (original is None or os.path.isdir(original))
        if not os.path.isdir(tdata):
            if os.path.lexists(tdata) or (original and not os.path.isdir(original)):
                return False
            if not _account_closed(account_path):
                return False
            if original:
                os.rename(original, tdata)
                server.invalidate_tdata_size(account_path)
                state._log.warning("Recovered original tdata after interrupted restore: %s", account_path)
        if committed and markers:
            if not _account_closed(account_path):
                return False
            for key, value in markers.items():
                destination = os.path.join(os.path.dirname(portable), key)
                for path in (destination, destination + ".tmp"):
                    if os.path.lexists(path) and (os.path.islink(path) or not os.path.isfile(path)
                                                 or os.stat(path).st_nlink > 1):
                        return False
                if value is None:
                    if os.path.lexists(destination):
                        os.unlink(destination)
                else:
                    state._save_json_atomic(destination, value)
        os.unlink(marker)
        return True
    except (OSError, ValueError, KeyError, TypeError) as e:
        state._log.error("Could not recover interrupted restore for %s: %s", account_path, e)
        return False


@state.serialize_account_op(lambda backup_path, account_path, client="Telegram": backup_path, (False, state._BUSY_MSG))
@state.serialize_account_op(lambda backup_path, account_path, client="Telegram": account_path, (False, state._BUSY_MSG))
def restore_backup(backup_path, account_path, client="Telegram"):
    """Copy tdata from a backup folder back into the account's TelegramForcePortable/.

    Crash-safe: the backup is first copied to tdata.new inside the account,
    then the live tdata is swapped out via two renames. If the server dies
    mid-copy the live tdata is untouched (a stale tdata.new is cleaned up on
    the next restore).
    """
    portable = _portable_path(account_path, client)
    if portable is None:
        return False, "Unknown client"
    real_backup = _resolve_backup_dir(backup_path)
    if real_backup is None:
        state._log.warning("restore_backup: invalid backup path %r", backup_path)
        return False, "Invalid backup path"
    if not recover_interrupted_restore(account_path, client):
        return False, "Could not recover an interrupted restore — original session was kept"
    if not state.is_managed_account_path(account_path):
        state._log.warning("restore_backup: invalid account path %r", account_path)
        return False, "Restore destination is not a valid account folder"
    state._log.info("Restoring backup from %s into %s", real_backup, account_path)
    tdata_src = os.path.join(real_backup, "ayugram", "tdata") if client == "AyuGram" else os.path.join(real_backup, "tdata")
    tdata_dst = os.path.join(portable, "tdata")
    tdata_new = tdata_dst + ".new"

    if not os.path.isdir(tdata_src) or not _safe_tree(real_backup):
        state._log.warning("restore_backup: tdata not found at %s", tdata_src)
        return False, "Backup tdata not found"
    verified, error = _verify_backup(real_backup)
    if not verified:
        return False, error
    if not os.path.isdir(account_path):
        state._log.warning("restore_backup: account folder not found: %s", account_path)
        return False, "Account folder not found"
    if not _account_closed(account_path):
        state._log.warning("restore_backup: refused — Telegram is running for %s", account_path)
        return False, "Close Telegram and AyuGram before restoring; process status must be available"

    if not _safe_tree(portable):
        return False, "Restore destination is not a valid account folder"
    os.makedirs(portable, exist_ok=True)

    # Re-validate the FINAL destination right before writing: the account path
    # was checked by the route, but a symlink planted at TelegramForcePortable/
    # or tdata could still redirect the copy outside the managed tree.
    if os.path.islink(portable) or os.path.islink(tdata_dst) or not state.is_safe_path(portable):
        state._log.warning("restore_backup: destination failed re-validation: %s", portable)
        return False, "Restore destination is not a valid account folder"

    marker_values = {}
    if client == "AyuGram":
        ayu_root = os.path.dirname(portable)
        for name in (".common-profile.json", ".new-login.json"):
            source = os.path.join(real_backup, "ayugram", name)
            destination = os.path.join(ayu_root, name)
            for path in (destination, destination + ".tmp"):
                if os.path.lexists(path) and (not os.path.isfile(path)
                        or os.path.islink(path) or os.stat(path).st_nlink > 1):
                    return False, "AyuGram profile marker is unsafe"
            if os.path.lexists(source) and (not os.path.isfile(source) or os.stat(source).st_nlink > 1):
                return False, "Backup profile marker is unsafe"
            try:
                if os.path.isfile(source):
                    with open(source, encoding="utf-8") as f:
                        value = json.load(f)
                else:
                    value = None
                if value is not None and not isinstance(value, dict):
                    return False, "Backup profile marker is invalid"
            except (OSError, ValueError):
                return False, "Backup profile marker is unreadable or invalid"
            marker_values[name] = value

    # Copy to a sibling temp dir first — the live tdata stays intact until the
    # copy has fully succeeded.
    try:
        rm_r = subprocess.run(["rm", "-rf", tdata_new], capture_output=True, timeout=300)
        if rm_r.returncode != 0:
            state._log.error("restore_backup: could not clear staging area: %s",
                              rm_r.stderr.decode(errors="replace").strip())
            return False, "Could not clear staging area — try again"
        ok, error = _copy_tdata_excluding_cache(tdata_src, tdata_new, exclude_cache=False)
        if not ok or _tree_inventory(tdata_src) != _tree_inventory(tdata_new):
            state._log.error("restore_backup: copy verification failed: %s", error)
            subprocess.run(["rm", "-rf", tdata_new], capture_output=True, timeout=300)
            return False, "Copy failed — the current tdata was not touched"
    except (OSError, subprocess.TimeoutExpired) as e:
        state._log.error("restore_backup: copy failed: %s", e)
        try:
            subprocess.run(["rm", "-rf", tdata_new], capture_output=True, timeout=300)
        except subprocess.TimeoutExpired:
            pass
        return False, "Copy failed or timed out — the current tdata was not touched"

    if not _account_closed(account_path):
        shutil.rmtree(tdata_new, ignore_errors=True)
        return False, "Account opened during restore; current tdata was not touched"

    # Journal before the first rename so a process crash can be recovered on scan/open.
    bak = None
    marker = os.path.join(portable, ".tdata-restore.json")
    try:
        if os.path.isdir(tdata_dst):
            bak = tdata_dst + ".bak." + uuid.uuid4().hex
        state._save_json_atomic(marker, {"backup": os.path.basename(bak) if bak else None,
                                         "markers": marker_values})
        if bak:
            os.rename(tdata_dst, bak)
            state._log.info("Existing tdata moved to %s", bak)
        os.rename(tdata_new, tdata_dst)
    except OSError as e:
        state._log.error("restore_backup: swap failed: %s", e)
        if bak and os.path.isdir(bak) and not os.path.isdir(tdata_dst):
            try:
                os.rename(bak, tdata_dst)
                state._log.info("restore_backup: original tdata restored from %s", bak)
            except Exception as undo_e:
                state._log.error("restore_backup: rollback also failed: %s", undo_e)
        # Judge by reality, not by which rename failed: the original is safe
        # iff a tdata dir exists at its normal location.
        rolled_back = os.path.isdir(tdata_dst)
        if rolled_back:
            recover_interrupted_restore(account_path, client)
        try:
            subprocess.run(["rm", "-rf", tdata_new], capture_output=True, timeout=300)
        except subprocess.TimeoutExpired:
            pass
        if rolled_back:
            return False, "Restore failed — original tdata has been restored"
        if bak is None:
            return False, "Restore failed — no original tdata existed, so nothing was overwritten"
        return False, ("Restore failed AND the original tdata could not be put back. "
                       f"It is still intact at: {bak} — rename it back to 'tdata' manually.")

    if not recover_interrupted_restore(account_path, client):
        return False, "Tdata restored; profile marker recovery is pending. Previous tdata was kept."
    server.invalidate_tdata_size(account_path)   # tdata was just replaced
    state._log.info("Backup restored successfully")
    return True, "Restored successfully. The previous tdata was kept as a .bak folder."


@state.serialize_account_op(lambda folder_path, account_name, prune=True: folder_path, (False, state._BUSY_MSG, ""))
def backup_account(folder_path, account_name, prune=True):
    if not state.is_managed_account_path(folder_path):
        state._log.warning("backup_account: invalid account path %r", folder_path)
        return False, "Path is not a valid account folder", ""
    # account_name comes straight from the client (server.py's /api/backup
    # passes data.get("name") unchecked). Strip it to a bare path component
    # so it can't traverse ("../../etc/pwned") or override the join outright
    # (an absolute value like "/tmp/pwned" makes os.path.join() discard the
    # DATA_DIR/Backups/date_str prefix entirely).
    account_name = os.path.basename(str(account_name or "").strip()) or "account"
    # A trailing ".partial" would make list_backups/_last_backup_map skip the
    # finished backup (they treat *.partial as in-progress) — strip it.
    while account_name.endswith(".partial"):
        account_name = account_name[:-len(".partial")]
    account_name = account_name or "account"
    if account_name in (".", ".."):
        account_name = "account"
    # Two accounts with the same folder name in different groups would write
    # to the same Backups/<date>/<name> dir (and prune each other) — suffix
    # the parent folder name when the basename is ambiguous.
    basename = os.path.basename(folder_path)
    dupes = [a for a in server.scan_accounts() if os.path.basename(a["path"]) == basename]
    if len(dupes) > 1:
        parent = os.path.basename(os.path.dirname(folder_path))
        account_name = f"{account_name} ({parent})"
    state._log.info("Backing up account %r from %s", account_name, folder_path)
    created_at = datetime.now()
    date_str = created_at.strftime("%Y-%m-%d_%H-%M-%S")
    account_id = state.ensure_account_id(folder_path)
    base = os.path.join(state.DATA_DIR, "Backups", date_str, account_id)
    backup_root = os.path.realpath(os.path.join(state.DATA_DIR, "Backups"))
    data_root = os.path.realpath(state.DATA_DIR)
    try:
        safe_root = os.path.commonpath((backup_root, data_root)) == data_root
    except ValueError:
        safe_root = False
    if not safe_root or _resolve_backup_dir(base) != os.path.realpath(base):
        state._log.error("backup_account: unsafe backup destination %s", base)
        return False, "Could not create a safe backup destination", ""
    backup_dir = base
    suffix = 2
    while os.path.exists(backup_dir) or os.path.exists(backup_dir + ".partial"):
        backup_dir = f"{base}-{suffix}"
        suffix += 1
    tdata_src  = os.path.join(folder_path, "TelegramForcePortable", "tdata")
    if not os.path.isdir(tdata_src):
        state._log.warning("backup_account: tdata not found at %s", tdata_src)
        return False, "No tdata found", ""
    ayu_root = os.path.join(folder_path, ".ayugram")
    ayu_tdata = os.path.join(ayu_root, "TelegramForcePortable", "tdata")
    has_ayu = os.path.isdir(ayu_tdata)
    if (not _safe_tree(tdata_src) or (os.path.lexists(os.path.dirname(ayu_tdata)) and not _safe_tree(os.path.dirname(ayu_tdata)))
            or os.path.islink(ayu_root)
            or any(os.path.lexists(os.path.join(ayu_root, name)) and
                   (os.path.islink(os.path.join(ayu_root, name)) or
                    not os.path.isfile(os.path.join(ayu_root, name)) or
                    os.stat(os.path.join(ayu_root, name)).st_nlink > 1)
                   for name in (".common-profile.json", ".new-login.json"))):
        return False, "Profile contains unsafe symlinks or unreadable folders", ""
    if (not _valid_profile(tdata_src) or (has_ayu and not _valid_profile(
            ayu_tdata, os.path.join(ayu_root, ".new-login.json")))):
        return False, "No usable session key or explicit fresh AyuGram login found", ""
    # Refuse to backup while Telegram is writing to tdata — the copy would be inconsistent
    if not _account_closed(folder_path):
        state._log.warning("backup_account: refused — Telegram is running for %s", folder_path)
        return False, "Close Telegram and AyuGram before backing up; process status must be available", ""
    # Crash-safe: copy into a .partial dir, rename to the final name only once
    # the copy fully succeeded. A server crash mid-copy leaves a *.partial dir
    # that list_backups() ignores, never a half backup that looks valid.
    partial_dir = backup_dir + ".partial"
    try:
        rm_r = subprocess.run(["rm", "-rf", partial_dir], capture_output=True, timeout=300)
        if rm_r.returncode != 0:
            state._log.error("backup_account: could not clear staging area: %s",
                              rm_r.stderr.decode(errors="replace").strip())
            return False, "Could not clear staging area", ""
        os.makedirs(partial_dir, mode=0o700, exist_ok=True)
        expected = {"tdata/" + name: value for name, value in
                    _tree_inventory(tdata_src, exclude_cache=True).items()}
        if has_ayu:
            expected.update({"ayugram/tdata/" + name: value for name, value in _tree_inventory(ayu_tdata).items()})
            for name in (".common-profile.json", ".new-login.json"):
                source = os.path.join(ayu_root, name)
                if os.path.isfile(source):
                    with open(source, "rb") as f:
                        data = f.read()
                    expected["ayugram/" + name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        ok, err = _copy_tdata_excluding_cache(tdata_src, os.path.join(partial_dir, "tdata"))
        if not ok:
            subprocess.run(["rm", "-rf", partial_dir], capture_output=True, timeout=300)
            return False, f"Copy failed: {err}", ""
        clients = ["Telegram"]
        if has_ayu:
            ayu_backup = os.path.join(partial_dir, "ayugram")
            os.makedirs(ayu_backup)
            ok, err = _copy_tdata_excluding_cache(ayu_tdata, os.path.join(ayu_backup, "tdata"), exclude_cache=False)
            if not ok:
                raise OSError("Could not copy AyuGram profile: " + err)
            for name in (".common-profile.json", ".new-login.json"):
                source = os.path.join(ayu_root, name)
                if os.path.isfile(source):
                    shutil.copy2(source, os.path.join(ayu_backup, name))
            clients.append("AyuGram")
        if not _account_closed(folder_path):
            raise OSError("Account opened during backup; copy is not consistent")
        if _backup_inventory(partial_dir) != expected:
            raise OSError("Backup copy did not match the source inventory")
        with open(os.path.join(partial_dir, "backup.json"), "w", encoding="utf-8") as f:
            json.dump({"account_id": account_id, "account_name": account_name,
                       "created_at": created_at.isoformat(timespec="seconds"), "clients": clients,
                       "integrity": {"version": 1, "files": expected}}, f)
    except (OSError, subprocess.TimeoutExpired) as e:
        state._log.error("backup_account: failed backing up %s: %s", folder_path, e)
        try:
            subprocess.run(["rm", "-rf", partial_dir], capture_output=True, timeout=300)
        except subprocess.TimeoutExpired:
            pass
        return False, "Backup timed out" if isinstance(e, subprocess.TimeoutExpired) else "Could not write backup", ""
    try:
        # Copy finished — from here on, never delete partial_dir on failure.
        os.rename(partial_dir, backup_dir)
    except (subprocess.TimeoutExpired, OSError) as e:
        state._log.error("backup_account: finalize failed for %s: %s", folder_path, e)
        return False, f"Backup copied but could not be finalized — kept at {partial_dir}", ""
    state._log.info("Backup complete: %s", backup_dir)
    if prune:
        prune_backups(account_id)
    _backup_map_cache["ts"] = 0.0
    return True, f"Backed up to {os.path.relpath(backup_dir, state.DATA_DIR)}", backup_dir
