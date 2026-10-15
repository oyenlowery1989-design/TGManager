"""System proxy management: apply an account's proxy system-wide for a short
window (enough for Telegram to connect), then restore the original setting.

Depends only on state.py — no other repo-internal imports.
"""

import json
import hashlib
import glob
import os
import shlex
import subprocess
from datetime import datetime

import state


def _proxy_recovery_path(service, channel):
    """Contained, stable recovery record for one network service/channel."""
    key = hashlib.sha256(f"{service}\0{channel}".encode()).hexdigest()
    return os.path.join(state.DATA_DIR, "proxy_recovery", f"{key}.json")


def get_active_network_service():
    """Return the macOS network service name for the current default-route
    interface, or "" if it can't be determined.

    Returning "" (rather than guessing) matters: apply_proxy()/
    _recover_stale_proxy() run `networksetup -set*proxy <service> ...`
    against whatever this returns. A machine routing via VPN/Ethernet (whose
    tunnel/wired interface often isn't listed the same way Wi-Fi is) would
    otherwise silently get its proxy applied to — and later "restored" on —
    a guessed service that may not even exist or may not be the one actually
    carrying traffic.
    """
    try:
        r = subprocess.run(["route", "get", "default"], capture_output=True, text=True, timeout=15)
        iface = ""
        for line in r.stdout.split("\n"):
            if "interface:" in line:
                iface = line.split(":")[-1].strip()
                break
        if iface:
            r2 = subprocess.run(["networksetup", "-listnetworkserviceorder"],
                                capture_output=True, text=True, timeout=15)
            lines = r2.stdout.split("\n")
            for i, line in enumerate(lines):
                if f"Device: {iface}" in line:
                    for j in range(i - 1, -1, -1):
                        if ")" in lines[j] and lines[j].strip():
                            return lines[j].split(")", 1)[-1].strip()
    except Exception:
        pass
    # Last resort: "Wi-Fi" only if it's an actual configured service on this
    # Mac, not just a hardcoded guess.
    try:
        r3 = subprocess.run(["networksetup", "-listallnetworkservices"],
                            capture_output=True, text=True, timeout=15)
        if any(line.strip() == "Wi-Fi" for line in r3.stdout.split("\n")):
            return "Wi-Fi"
    except Exception:
        pass
    return ""


def _channel_epoch_path(channel):
    return os.path.join(state.DATA_DIR, f"proxy_epoch_{channel}")


def _bump_channel_epoch(channel):
    """Atomically increment and return the epoch counter for this proxy
    channel ("socks" or "http").

    Each apply_proxy() call on a channel bumps this and bakes the new value
    into its own scheduled restore script. If a second apply on the SAME
    channel happens before the first's 35s restore fires, the second call
    bumps the epoch again — so when the first (now-stale) restore wakes up,
    it can tell it's been superseded and skip restoring, instead of blindly
    cutting the second apply's still-active window short.
    """
    path = _channel_epoch_path(channel)
    with state._proxy_state_lock:
        try:
            with open(path) as f:
                epoch = int(f.read().strip() or "0")
        except (OSError, ValueError):
            epoch = 0
        epoch += 1
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(epoch))
        os.replace(tmp, path)
    return epoch


def _read_proxy_state(service, channel):
    command = {"socks": "-getsocksfirewallproxy", "http": "-getwebproxy",
               "https": "-getsecurewebproxy"}[channel]
    result = subprocess.run(["networksetup", command, service],
                            capture_output=True, text=True, timeout=15)
    fields = dict(line.split(": ", 1) for line in result.stdout.splitlines() if ": " in line)
    if result.returncode or fields.get("Enabled") not in ("Yes", "No"):
        raise ValueError(f"Could not read the original {channel} proxy")
    if fields.get('Authenticated Proxy Enabled', '0') not in ('0', 'No'):
        raise ValueError(f'The original {channel} proxy uses authentication; its credentials cannot be restored safely')
    return {"enabled": fields["Enabled"] == "Yes", "host": fields.get("Server", ""),
            "port": fields.get("Port", "0"), "authenticated": False}


def _restore_proxy_commands(service, channel, value):
    if value.get('authenticated'):
        raise ValueError('Proxy recovery requires credentials; existing record was preserved for manual restoration')
    command = {"socks": "socksfirewallproxy", "http": "webproxy", "https": "securewebproxy"}[channel]
    host, port = str(value.get("host") or ""), str(value.get("port") or "0")
    if value.get("enabled") and not host:
        raise ValueError("The enabled proxy has no saved server")
    lines = []
    if host:
        lines.append(f"networksetup -set{command} {shlex.quote(service)} {shlex.quote(host)} {shlex.quote(port)} off")
    lines.append(f"networksetup -set{command}state {shlex.quote(service)} {'on' if value.get('enabled') else 'off'}")
    return lines


def apply_proxy(proxy_config):
    """
    Temporarily set the macOS system SOCKS proxy so Telegram routes through it.
    Restores original proxy state after 35 seconds (enough for Telegram to connect).
    """

    proxy_type = proxy_config.get("type", "socks5").lower()
    host       = proxy_config.get("host", "")
    port       = str(proxy_config.get("port", 1080))
    if not host:
        return

    state._log.info("Applying %s proxy %s:%s", proxy_type, host, port)
    service = get_active_network_service()
    if not service:
        state._log.warning("apply_proxy: could not determine the active network service — skipping")
        return

    channel = "socks" if proxy_type.startswith("socks") else "http"
    try:
        original = _read_proxy_state(service, channel)
        if channel == "http":
            original["https"] = _read_proxy_state(service, "https")
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        state._log.warning("Proxy unchanged: %s", error)
        return

    # Persist the pre-change state so a failed restore can be recovered at
    # next startup. First writer wins: if a restore is already pending, the
    # file already holds the true original — do not overwrite it.
    #
    # The restore is then built from the FILE's baseline, not from this call's
    # live query: if two accounts on the same proxy channel are opened within
    # the 35 s window, the second's live query returns the FIRST account's
    # proxy (already applied), so baking that in used to restore the wrong
    # proxy and leave it stuck on. Reading back the authoritative baseline
    # makes every concurrent restore converge on the same true original.
    recovery_path = _proxy_recovery_path(service, channel)
    our_saved_at = datetime.now().isoformat()
    with state._proxy_state_lock:
        if not os.path.exists(recovery_path):
            state._save_json_atomic(recovery_path, {
                "service": service, "proxy_type": proxy_type,
                **original,
                "saved_at": our_saved_at,
            })
        try:
            with open(recovery_path) as _pf:
                _base = json.load(_pf)
            _base_channel = "socks" if str(_base.get("proxy_type", "")).startswith("socks") else "http"
            if _base_channel != channel or _base.get("service") != service:
                raise ValueError("Proxy recovery record does not match the network service")
            restore_lines = _restore_proxy_commands(service, channel, _base)
            if channel == "http":
                if not isinstance(_base.get("https"), dict):
                    raise ValueError("Legacy proxy recovery has no HTTPS baseline; check Network settings first")
                restore_lines += _restore_proxy_commands(service, "https", _base["https"])
        except Exception as _e:
            state._log.warning("Proxy unchanged: recovery record unavailable (%s)", _e)
            return

    # Build per-type command strings using shlex.quote() for all user-controlled values.
    # The initial set+enable pair runs via state._run_as_admin() (temp-file approach — no AppleScript
    # string embedding). Restore uses its own administrator script.
    if proxy_type.startswith("socks"):
        set_cmd     = f"networksetup -setsocksfirewallproxy {shlex.quote(service)} {shlex.quote(host)} {shlex.quote(port)} off"
        on_cmd      = f"networksetup -setsocksfirewallproxystate {shlex.quote(service)} on"
    else:
        set_cmd     = (f"networksetup -setwebproxy {shlex.quote(service)} {shlex.quote(host)} {shlex.quote(port)}\n"
                        f"networksetup -setsecurewebproxy {shlex.quote(service)} {shlex.quote(host)} {shlex.quote(port)}")
        on_cmd      = (f"networksetup -setwebproxystate {shlex.quote(service)} on\n"
                        f"networksetup -setsecurewebproxystate {shlex.quote(service)} on")

    result = state._run_as_admin(
        f"set -e\n{set_cmd}\n{on_cmd}\n",
        "TelegramManager is setting a proxy for this Telegram account."
    )
    if result.returncode != 0:
        state._log.warning("apply_proxy: admin prompt rejected or failed (rc=%d)", result.returncode)
        # A failed command may have changed only one channel; keep its baseline.
        return

    state._log.info("Proxy %s:%s active on service %r; will restore in 35 s", host, port, service)

    # Bump AFTER the change is confirmed applied, so this call's restore can
    # tell whether a later same-channel apply has taken over by the time its
    # 35s window elapses — see _bump_channel_epoch's docstring.
    my_epoch = _bump_channel_epoch(channel)

    def schedule_restore():
        import tempfile, stat
        # Runs under `set -e`: only reached when the restore succeeded, so a
        # leftover file always means "system proxy may still be modified".
        restore_lines.append(f"rm -f {shlex.quote(recovery_path)}")

        fd_restore, restore_path = tempfile.mkstemp(suffix=".sh", prefix="tm_proxy_restore_cmd_")
        fd_wrapper, wrapper_path = tempfile.mkstemp(suffix=".sh", prefix="tm_proxy_restore_job_")
        try:
            with os.fdopen(fd_restore, "w") as f:
                f.write("#!/bin/bash\n")
                f.write("set -e\n")
                f.write("\n".join(restore_lines) + "\n")
            os.chmod(restore_path, stat.S_IRWXU)

            prompt = state._as_str("TelegramManager is restoring the proxy settings after Telegram connected.")
            admin_script = (
                f'do shell script "bash {shlex.quote(restore_path)}" '
                f'with administrator privileges with prompt {prompt}'
            )

            epoch_path = _channel_epoch_path(channel)
            with os.fdopen(fd_wrapper, "w") as f:
                f.write("#!/bin/bash\n")
                f.write("sleep 35\n")
                # If a later apply_proxy() call on this same channel has
                # since bumped the epoch, this restore is stale: running it
                # now would cut the newer apply's own 35s window short. Skip
                # the restore (and leave PROXY_ORIGINAL_FILE alone — the
                # newer call's own restore owns that cleanup) but still
                # remove our temp scripts either way.
                f.write(f"cur_epoch=$(cat {shlex.quote(epoch_path)} 2>/dev/null || echo 0)\n")
                f.write(f'if [ "$cur_epoch" = {shlex.quote(str(my_epoch))} ]; then\n')
                f.write(f"  osascript -e {shlex.quote(admin_script)} >/dev/null 2>&1 || true\n")
                f.write("fi\n")
                f.write(f"rm -f {shlex.quote(restore_path)}\n")
                f.write(f"rm -f {shlex.quote(wrapper_path)}\n")
            os.chmod(wrapper_path, stat.S_IRWXU)

            subprocess.Popen(["/usr/bin/nohup", "bash", wrapper_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            state._log.error("apply_proxy: failed to schedule restore for service %r: %s", service, e)
            try:
                os.unlink(restore_path)
            except OSError:
                pass
            try:
                os.unlink(wrapper_path)
            except OSError:
                pass

    schedule_restore()


def _recover_stale_proxy(record_path=None):
    """Restore system proxy settings left modified by a previous run.

    state.PROXY_ORIGINAL_FILE existing at startup means apply_proxy() changed the
    system proxy but its scheduled restore never completed (server killed,
    admin prompt denied, crash). Without this, the whole machine keeps
    routing through the last account's proxy indefinitely.

    Holds state._proxy_state_lock for the same reason apply_proxy() does —
    both read/write/delete the same PROXY_ORIGINAL_FILE, and without a shared
    lock a concurrent apply_proxy() call (e.g. the user opens a proxied
    account during this startup check) could read the file mid-decision here
    and drive an unsynchronized second `networksetup` command against it.
    """
    if record_path is None:
        paths = [state.PROXY_ORIGINAL_FILE]
        paths.extend(glob.glob(os.path.join(state.DATA_DIR, "proxy_recovery", "*.json")))
        for path in paths:
            _recover_stale_proxy(path)
        return

    with state._proxy_state_lock:
        try:
            with open(record_path) as f:
                rec = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            state._log.error("stale proxy record unreadable (%s) — remove %s and check "
                       "System Settings → Network manually", e, record_path)
            return

        service = rec.get("service", "")
        ptype   = str(rec.get("proxy_type", "socks5"))
        if not service:
            try:
                os.unlink(record_path)
            except OSError:
                pass
            return

        state._log.warning("Previous proxy restore did not complete — restoring original "
                     "settings for service %r", service)
        channel = "socks" if ptype.startswith("socks") else "http"
        incomplete = channel == "http" and not isinstance(rec.get("https"), dict)
        try:
            lines = _restore_proxy_commands(service, channel, rec)
            if channel == "http" and not incomplete:
                lines += _restore_proxy_commands(service, "https", rec["https"])
        except (ValueError, TypeError) as error:
            state._log.error("Invalid proxy recovery record: %s", error)
            return

        r = state._run_as_admin(
            "set -e\n" + "\n".join(lines) + "\n",
            "TelegramManager needs to restore proxy settings left over from a previous run.",
        )
        if r.returncode == 0 and incomplete:
            state._log.warning("HTTP proxy restored; legacy recovery has no HTTPS baseline. "
                               "Check HTTPS in Network settings; recovery record retained: %s", record_path)
        elif r.returncode == 0:
            try:
                os.unlink(record_path)
            except OSError:
                pass
            state._log.info("Stale proxy state restored for service %r", service)
        else:
            state._log.error("Stale proxy restore failed (rc=%d) — the system proxy may "
                       "still be set; check System Settings → Network → %s",
                       r.returncode, service)
