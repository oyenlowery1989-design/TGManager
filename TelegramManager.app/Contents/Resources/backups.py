"""Backup lifecycle: create, list, prune, delete, and restore tdata backups.

Depends on state.py (shared config/locks/helpers) and server.py (scan_accounts,
find_telegram_pid, invalidate_tdata_size — accessed only as `server.<attr>`
inside function bodies, never at import time, so the circular import back to
server.py is safe; see server.py's `sys.modules.setdefault` alias).
"""

import os
import subprocess
import time
from datetime import datetime

import state
import server


def _copy_tdata_excluding_cache(src, dst):
    """Copy tdata src→dst for a backup, skipping regenerable cache dirs so
    backups stay small. Uses rsync --exclude; falls back to a full cp -R (a
    complete, if larger, backup) if rsync is missing or errors. Returns
    (ok, error_message)."""
    os.makedirs(dst, exist_ok=True)
    cmd = ["rsync", "-a"]
    for name in state.BACKUP_EXCLUDE_DIR_NAMES:
        cmd += ["--exclude", name + "/"]   # trailing slash = directories only
    cmd += [src.rstrip("/") + "/", dst.rstrip("/") + "/"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=1800)
        if r.returncode == 0:
            return True, ""
        state._log.warning("backup rsync rc=%d, falling back to cp: %s",
                     r.returncode, r.stderr.decode(errors="replace").strip()[:200])
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        state._log.warning("backup rsync unavailable (%s); falling back to cp", e)
    subprocess.run(["rm", "-rf", dst], capture_output=True, timeout=300)
    r = subprocess.run(["cp", "-R", src, dst], capture_output=True, timeout=1800)
    return (r.returncode == 0), ("" if r.returncode == 0
                                 else r.stderr.decode(errors="replace").strip()[:200])


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
            if os.path.isdir(tdata_src):
                size = state.get_folder_size(tdata_src)
                backups.append({
                    "date":       date_folder,
                    "account":    account,
                    "backup_path": acc_path,
                    "size":       size,
                    "size_human": state.human_size(size),
                })
    return backups


_backup_map_cache = {"ts": 0.0, "map": {}}

def _last_backup_map():
    """Map account name → newest backup date folder ("YYYY-MM-DD_HH-MM").

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
                    result.setdefault(account, date_folder)
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
    backup_root = os.path.realpath(os.path.join(state.DATA_DIR, "Backups"))
    real = os.path.realpath(str(backup_path or ""))
    rel = os.path.relpath(real, backup_root)
    if rel.startswith("..") or len(rel.split(os.sep)) != 2:
        return None
    return real


def delete_backup(backup_path):
    """Delete one backup folder (Backups/<date>/<account>). Returns (ok, msg)."""
    real = _resolve_backup_dir(backup_path)
    if real is None:
        return False, "Invalid backup path"
    if not os.path.isdir(real):
        return False, "Backup not found"
    r = subprocess.run(["rm", "-rf", real], capture_output=True, timeout=120)
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


def prune_backups(account_name):
    """Enforce backup_keep_per_account for one account (0 = keep all)."""
    try:
        keep = int(state.config.get("backup_keep_per_account", 0))
    except (TypeError, ValueError):
        keep = 0
    if keep <= 0:
        return
    mine = [b for b in list_backups() if b["account"] == account_name]  # newest first
    for b in mine[keep:]:
        ok, msg = delete_backup(b["backup_path"])
        if ok:
            state._log.info("prune_backups: removed old backup %s", b["backup_path"])
        else:
            state._log.warning("prune_backups: could not remove %s: %s", b["backup_path"], msg)


@state.serialize_account_op(lambda backup_path, account_path: account_path, (False, state._BUSY_MSG))
def restore_backup(backup_path, account_path):
    """Copy tdata from a backup folder back into the account's TelegramForcePortable/.

    Crash-safe: the backup is first copied to tdata.new inside the account,
    then the live tdata is swapped out via two renames. If the server dies
    mid-copy the live tdata is untouched (a stale tdata.new is cleaned up on
    the next restore).
    """
    real_backup = _resolve_backup_dir(backup_path)
    if real_backup is None:
        state._log.warning("restore_backup: invalid backup path %r", backup_path)
        return False, "Invalid backup path"
    state._log.info("Restoring backup from %s into %s", real_backup, account_path)
    tdata_src = os.path.join(real_backup, "tdata")
    portable  = os.path.join(account_path, "TelegramForcePortable")
    tdata_dst = os.path.join(portable, "tdata")
    tdata_new = tdata_dst + ".new"

    if not os.path.isdir(tdata_src):
        state._log.warning("restore_backup: tdata not found at %s", tdata_src)
        return False, "Backup tdata not found"
    if not os.path.isdir(account_path):
        state._log.warning("restore_backup: account folder not found: %s", account_path)
        return False, "Account folder not found"
    if server.find_telegram_pid(account_path):
        state._log.warning("restore_backup: refused — Telegram is running for %s", account_path)
        return False, "Close Telegram for this account before restoring a backup"

    os.makedirs(portable, exist_ok=True)

    # Re-validate the FINAL destination right before writing: the account path
    # was checked by the route, but a symlink planted at TelegramForcePortable/
    # or tdata could still redirect the copy outside the managed tree.
    if os.path.islink(portable) or os.path.islink(tdata_dst) or not state.is_safe_path(portable):
        state._log.warning("restore_backup: destination failed re-validation: %s", portable)
        return False, "Restore destination is not a valid account folder"

    # Copy to a sibling temp dir first — the live tdata stays intact until the
    # copy has fully succeeded.
    subprocess.run(["rm", "-rf", tdata_new], capture_output=True, timeout=300)
    r = subprocess.run(["cp", "-R", tdata_src, tdata_new], capture_output=True, timeout=1800)
    if r.returncode != 0:
        state._log.error("restore_backup: cp failed: %s", r.stderr.decode(errors="replace").strip())
        subprocess.run(["rm", "-rf", tdata_new], capture_output=True, timeout=300)
        return False, "Copy failed — the current tdata was not touched"

    # Swap: current tdata → timestamped .bak, then tdata.new → tdata.
    bak = None
    try:
        if os.path.isdir(tdata_dst):
            bak = tdata_dst + ".bak." + datetime.now().strftime("%Y%m%d_%H%M%S")
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
        subprocess.run(["rm", "-rf", tdata_new], capture_output=True, timeout=300)
        return False, "Restore failed — original tdata has been restored"

    server.invalidate_tdata_size(account_path)   # tdata was just replaced
    state._log.info("Backup restored successfully")
    return True, "Restored successfully. The previous tdata was kept as a .bak folder."


@state.serialize_account_op(lambda folder_path, account_name: folder_path, (False, state._BUSY_MSG, ""))
def backup_account(folder_path, account_name):
    # account_name comes straight from the client (server.py's /api/backup
    # passes data.get("name") unchecked). Strip it to a bare path component
    # so it can't traverse ("../../etc/pwned") or override the join outright
    # (an absolute value like "/tmp/pwned" makes os.path.join() discard the
    # DATA_DIR/Backups/date_str prefix entirely).
    account_name = os.path.basename(str(account_name or "").strip()) or "account"
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
    date_str   = datetime.now().strftime("%Y-%m-%d_%H-%M")
    backup_dir = os.path.join(state.DATA_DIR, "Backups", date_str, account_name)
    tdata_src  = os.path.join(folder_path, "TelegramForcePortable", "tdata")
    if not os.path.isdir(tdata_src):
        state._log.warning("backup_account: tdata not found at %s", tdata_src)
        return False, "No tdata found", ""
    # Refuse to backup while Telegram is writing to tdata — the copy would be inconsistent
    if server.find_telegram_pid(folder_path):
        state._log.warning("backup_account: refused — Telegram is running for %s", folder_path)
        return False, "Close Telegram for this account before backing up", ""
    # Crash-safe: copy into a .partial dir, rename to the final name only once
    # the copy fully succeeded. A server crash mid-copy leaves a *.partial dir
    # that list_backups() ignores, never a half backup that looks valid.
    partial_dir = backup_dir + ".partial"
    subprocess.run(["rm", "-rf", partial_dir], capture_output=True, timeout=300)
    os.makedirs(partial_dir, exist_ok=True)
    ok, err = _copy_tdata_excluding_cache(tdata_src, os.path.join(partial_dir, "tdata"))
    if not ok:
        subprocess.run(["rm", "-rf", partial_dir], capture_output=True, timeout=300)
        return False, f"Copy failed: {err}", ""
    if os.path.isdir(backup_dir):   # same account backed up twice in one minute
        subprocess.run(["rm", "-rf", backup_dir], capture_output=True, timeout=300)
    os.rename(partial_dir, backup_dir)
    state._log.info("Backup complete: %s", backup_dir)
    prune_backups(account_name)
    _backup_map_cache["ts"] = 0.0
    return True, f"Backed up to Backups/{date_str}/{account_name}", backup_dir
