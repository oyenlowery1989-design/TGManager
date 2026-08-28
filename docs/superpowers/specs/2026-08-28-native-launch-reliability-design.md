# Native Launch Reliability — Design Spec

_Date: 2026-08-28_

## Goal

Make the Swift `WKWebView` launcher reliably discover and own the Python
server it starts, with actionable diagnostics when startup fails.

## Scope

Included:

- A per-launch ready file that is written only after Python binds its HTTP
  listener.
- Swift startup based on that ready file rather than the mirrored config port.
- Scoped lifecycle ownership of the Python process started by the current
  launcher instance.
- Diagnostics for Python launch, bind, and readiness failures.
- Focused automated coverage for ready-file output and failure paths.

Excluded:

- macOS application/release signing, notarization, and certificate selection.
- Changes to API routing, session-token authorization, or account lifecycle.
- New dependencies or a separate supervisor process.

## Current Failure Modes

The launcher reads `manager_config.json` before launching Python, then polls
that port. A stale/missing port mirror can make Swift poll the wrong endpoint.
It also kills every process matching the manager server path before launch,
including a deliberately started standalone server, and discards Python stdout
and stderr. A failed bind therefore becomes a generic timeout page.

## Design

### Ready-file contract

Swift generates a unique path in `NSTemporaryDirectory()` and passes it as
`TG_READY_FILE` to its child Python server along with the existing
`TG_SESSION_TOKEN`.

After `ThreadedHTTPServer` successfully binds, Python atomically writes JSON:

```json
{"pid": 12345, "port": 8477, "session_token": "..."}
```

The file is written as `<path>.tmp` followed by `os.replace`. It is never
written if Python fails before binding. Python removes the ready file when it
shuts down normally, but the launcher always removes a stale file before a new
launch.

### Swift startup and ownership

Swift no longer uses `readPort()` or the path-wide `pgrep | kill` command.
It starts one `Process`, retains it in `serverProcess`, and waits for a valid
ready file whose session token equals the launcher’s token. The ready file
supplies the exact localhost URL that Swift loads.

On application termination, Swift terminates only `serverProcess` and removes
its own ready file. A separately started `server.py` is left alone.

### Diagnostics

Swift sends child stdout/stderr to `data/manager.log` (append mode). If the
child exits before readiness, or the timeout expires, the inline error page
shows whether Python could not launch, exited early, or never became ready,
plus the exact log location. It does not expose the session token.

### Error handling

- Malformed, missing, stale, or token-mismatched ready content is ignored
  until the deadline.
- A child that exits early is reported immediately rather than waiting for all
  polling attempts.
- Python logs ready-file write failures but continues serving; Swift then
  displays a specific readiness failure.

## Files Changed

| File | Change |
|---|---|
| `TelegramManager.app/Contents/Resources/server.py` | Write/remove the atomic ready file around the bound server lifecycle. |
| `TelegramManager.app/Contents/Resources/launcher.swift` | Create/pass/read/clean ready file; remove global server kill; retain launch diagnostics. |
| `tests/test_server_helpers.py` | Test ready-file JSON and atomic cleanup behavior through temporary paths. |

## Verification

- Python unit tests cover valid ready content, absent `TG_READY_FILE`, and a
  failed ready-file write without preventing the server from serving.
- Swift compiles with the project’s documented `swiftc` command.
- Manual smoke test: open `TelegramManager.app`, verify the UI appears, close
  it, then confirm only its child server exits.
