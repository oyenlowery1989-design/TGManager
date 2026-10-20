# AyuGram startup measurements — 2026-10-20

The local preparation path and VPS transport were measured separately. No real
account was launched or closed. Local launch tests intercepted `open` and used
synthetic authentication/history in temporary directories.

| Stage | Before | After | Measurement |
|---|---:|---:|---|
| Local preparation of an existing custom clone | 1.694 s | 1.128 s | Median of three interleaved runs per version |
| Read-only VPS history catalog | 5.034–5.523 s | 2.785–2.801 s | Warm connection, actual configured VPS |
| First VPS catalog with connection warm-up | — | 4.124 s | One cold run; network timing varies |

The local fixture used an APFS clone of the installed 1.46 GB custom application,
synthetic session data and 6,400 empty cache files. Original measurements put
profile validation at about 0.02 s and the APFS app copy at about 0.008 s. Neither
was the main bottleneck.

These are preparation/transport timings, not a measured click-to-window time.
Actual history transfer, merge and application startup still add time.

Validation passed: 475 tests in the complete suite, plus the 30 remote-connection
tests using Manager's Python 3.9 runtime. Tests include process cleanup after
SIGTERM/SIGKILL, request failures, target changes, archive merging and session
preservation.

After restarting Manager normally, its private SSH master and socket were
confirmed active; both pre-existing client processes retained their PIDs and
executables. The separate full VPS health endpoint returned an active client
with no alerts, but took 6.665 s and 12.801 s in live checks. That endpoint runs
additional health checks and is not the pre-launch history-export request.
Connection reuse does not guarantee a fixed network or click-to-window time.

## Changes and boundaries

- Custom app opens reuse the master digest already verified during that launch,
  under the existing launch/update lock. Both the master and existing clone are
  still fully hashed on each launch; no persistent trust cache was introduced.
- Status and history requests may share one private foreground SSH connection.
  Google Cloud still resolves the target for every request. Socket names include
  the host-key alias and SSH connection identity; strict host-key checking stays
  enabled. Backup requests and the Screen Sharing tunnel use their existing path.
- Connection reuse expires after five minutes when no request holds a lease.
  Unready or failed warm-up falls back to a fresh connection. Failed requests are
  not replayed. A pipe supervisor removes the SSH processes and private socket
  directory when Manager exits, including an unexpected termination.
- History is fetched afresh and merged before launch as before. Account binding,
  closed-client checks, size limits, duplicate/conflict handling and recovery
  backups are unchanged. Authentication files are not synchronized.

The connection mechanism uses documented OpenSSH multiplexing and retains the
Google Cloud CLI target/host-key handling:
[OpenSSH ControlPath](https://man.openbsd.org/ssh_config#ControlPath) and
[gcloud compute ssh](https://docs.cloud.google.com/sdk/gcloud/reference/compute/ssh).
