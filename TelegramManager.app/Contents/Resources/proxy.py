"""System proxy management: apply an account's proxy system-wide for a short
window (enough for Telegram to connect), then restore the original setting.

Depends only on state.py — no other repo-internal imports.
"""

import json
import os
import shlex
import subprocess
from datetime import datetime

import state


def get_active_network_service():
    """Return the macOS network service name for the current default-route interface."""
    try:
        r = subprocess.run(["route", "get", "default"], capture_output=True, text=True, timeout=15)
        iface = ""
        for line in r.stdout.split("\n"):
            if "interface:" in line:
                iface = line.split(":")[-1].strip()
                break
        if not iface:
            return "Wi-Fi"
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
    return "Wi-Fi"

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

    def scutil_get_proxy():
        cmd = "socks" if proxy_type.startswith("socks") else "http"
        try:
            r = subprocess.run(["networksetup", f"-get{cmd}firewallproxy", service],
                               capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            state._log.warning("scutil_get_proxy: networksetup query timed out")
            return False, "", "0"
        enabled = "Enabled: Yes" in r.stdout
        cur_host, cur_port = "", "0"
        for ln in r.stdout.split("\n"):
            if ln.startswith("Server:"): cur_host = ln.split(":", 1)[1].strip()
            if ln.startswith("Port:"):   cur_port = ln.split(":", 1)[1].strip()
        return enabled, cur_host, cur_port

    orig_enabled, orig_host, orig_port = scutil_get_proxy()

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
    channel = "socks" if proxy_type.startswith("socks") else "http"
    r_enabled, r_host, r_port = orig_enabled, orig_host, orig_port
    with state._proxy_state_lock:
        if not os.path.exists(state.PROXY_ORIGINAL_FILE):
            state._save_json_atomic(state.PROXY_ORIGINAL_FILE, {
                "service": service, "proxy_type": proxy_type,
                "enabled": orig_enabled, "host": orig_host, "port": orig_port,
                "saved_at": datetime.now().isoformat(),
            })
        try:
            with open(state.PROXY_ORIGINAL_FILE) as _pf:
                _base = json.load(_pf)
            _base_channel = "socks" if str(_base.get("proxy_type", "")).startswith("socks") else "http"
            # Only trust the file when it describes THIS proxy channel; a
            # different channel is independent and its live query is correct.
            if _base_channel == channel:
                r_enabled = bool(_base.get("enabled"))
                r_host    = str(_base.get("host", "") or "")
                r_port    = str(_base.get("port", "0") or "0")
        except Exception as _e:
            state._log.warning("apply_proxy: baseline read-back failed (%s); using live query", _e)

    # Build per-type command strings using shlex.quote() for all user-controlled values.
    # The initial set+enable pair runs via state._run_as_admin() (temp-file approach — no AppleScript
    # string embedding).  Restore uses sudo -n with cached credentials via bash -c directly.
    if proxy_type.startswith("socks"):
        set_cmd     = f"networksetup -setsocksfirewallproxy {shlex.quote(service)} {shlex.quote(host)} {shlex.quote(port)} off"
        on_cmd      = f"networksetup -setsocksfirewallproxystate {shlex.quote(service)} on"
        off_cmd     = f"networksetup -setsocksfirewallproxystate {shlex.quote(service)} off"
        restore_set = f"networksetup -setsocksfirewallproxy {shlex.quote(service)} {shlex.quote(r_host)} {shlex.quote(r_port)} off"
    else:
        set_cmd     = f"networksetup -sethttpproxy {shlex.quote(service)} {shlex.quote(host)} {shlex.quote(port)}"
        on_cmd      = f"networksetup -sethttpproxystate {shlex.quote(service)} on"
        off_cmd     = f"networksetup -sethttpproxystate {shlex.quote(service)} off"
        restore_set = f"networksetup -sethttpproxy {shlex.quote(service)} {shlex.quote(r_host)} {shlex.quote(r_port)}"

    result = state._run_as_admin(
        f"{set_cmd}\n{on_cmd}\n",
        "TelegramManager is setting a proxy for this Telegram account."
    )
    if result.returncode != 0:
        state._log.warning("apply_proxy: admin prompt rejected or failed (rc=%d)", result.returncode)
        return

    state._log.info("Proxy %s:%s active on service %r; will restore in 35 s", host, port, service)

    def schedule_restore():
        import tempfile, stat
        if r_enabled and r_host:
            restore_lines = [restore_set, on_cmd]
        else:
            restore_lines = [off_cmd]
        # Runs under `set -e`: only reached when the restore succeeded, so a
        # leftover file always means "system proxy may still be modified".
        restore_lines.append(f"rm -f {shlex.quote(state.PROXY_ORIGINAL_FILE)}")

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

            with os.fdopen(fd_wrapper, "w") as f:
                f.write("#!/bin/bash\n")
                f.write("sleep 35\n")
                f.write(f"osascript -e {shlex.quote(admin_script)} >/dev/null 2>&1 || true\n")
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


def _recover_stale_proxy():
    """Restore system proxy settings left modified by a previous run.

    state.PROXY_ORIGINAL_FILE existing at startup means apply_proxy() changed the
    system proxy but its scheduled restore never completed (server killed,
    admin prompt denied, crash). Without this, the whole machine keeps
    routing through the last account's proxy indefinitely.
    """
    try:
        with open(state.PROXY_ORIGINAL_FILE) as f:
            rec = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        state._log.error("stale proxy record unreadable (%s) — remove %s and check "
                   "System Settings → Network manually", e, state.PROXY_ORIGINAL_FILE)
        return

    service = rec.get("service", "")
    ptype   = str(rec.get("proxy_type", "socks5"))
    if not service:
        try:
            os.unlink(state.PROXY_ORIGINAL_FILE)
        except OSError:
            pass
        return

    state._log.warning("Previous proxy restore did not complete — restoring original "
                 "settings for service %r", service)
    q = shlex.quote
    if ptype.startswith("socks"):
        if rec.get("enabled") and rec.get("host"):
            lines = [
                f"networksetup -setsocksfirewallproxy {q(service)} {q(str(rec.get('host')))} {q(str(rec.get('port', '0')))} off",
                f"networksetup -setsocksfirewallproxystate {q(service)} on",
            ]
        else:
            lines = [f"networksetup -setsocksfirewallproxystate {q(service)} off"]
    else:
        if rec.get("enabled") and rec.get("host"):
            lines = [
                f"networksetup -sethttpproxy {q(service)} {q(str(rec.get('host')))} {q(str(rec.get('port', '0')))}",
                f"networksetup -sethttpproxystate {q(service)} on",
            ]
        else:
            lines = [f"networksetup -sethttpproxystate {q(service)} off"]

    r = state._run_as_admin(
        "\n".join(lines) + "\n",
        "TelegramManager needs to restore proxy settings left over from a previous run.",
    )
    if r.returncode == 0:
        try:
            os.unlink(state.PROXY_ORIGINAL_FILE)
        except OSError:
            pass
        state._log.info("Stale proxy state restored for service %r", service)
    else:
        state._log.error("Stale proxy restore failed (rc=%d) — the system proxy may "
                   "still be set; check System Settings → Network → %s",
                   r.returncode, service)
