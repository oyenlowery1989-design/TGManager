# Remote AyuGram on the existing VPS

The remote client uses a new independent Linux profile. The deployment tools
do not import Mac sessions or authenticate a Telegram account; the user signs
in through the private remote desktop. Production validation is recorded below.

The intended host is `openclaw-gateway`, project
`project-64666d6d-e266-44ea-bfd`, zone `us-central1-a`, Debian 12 x86_64.
It has 4 GB RAM. The deployed unauthenticated desktop used approximately
305 MiB for the complete AyuGram slice, with a 398 MiB measured peak and around
700 MiB host memory available. These are pre-login measurements, not an account
capacity guarantee. Do not build AyuGram on this shared host.

## Linux build preparation

`python3 tools/prepare_remote_ayugram_build.py /absolute/new/context` creates an
allowlisted context containing only the complete source patch, build script and
hash receipt. It does not upload anything or start a paid build. Never submit
the whole Manager workspace to Cloud Build: it contains private account data.

Run `bash /absolute/new/context/build_linux.sh /absolute/new/workspace` on a
dedicated Linux x86_64 builder with Docker + buildx, Git, Python 3 + Jinja2, and
at least eight logical CPUs. Supply `TDESKTOP_API_ID` and `TDESKTOP_API_HASH` in
the private build environment, never in Git. The conservative preflight asks for
24 GB available RAM and 125 GB free disk; these are not verified minimums or a
promise of completion within a particular time or budget. The existing VPS is
for the finished app, not this build.

The script fetches the exact upstream commit, applies only the complete custom
patch, and builds the dependency image from that commit's Dockerfile generator.
It disables dependency debug/LTO and app LTO, bounds BuildKit parallel stages,
and limits compiler containers to eight CPUs and 24 GB. It does not use a moving
upstream dependency image. The generated recipe selects Rocky Linux's primary
HTTPS repository for BaseOS, AppStream, PowerTools and extras because a selected
mirror returned stale metadata and stalled the real build. Package GPG/TLS
verification remains enabled; the OS repository is still a changing repository,
not a package snapshot. Build output, which can contain API arguments, stays
in the private workspace's `build.log`; do not publish that log. A successful
build produces `ayugram-linux-x86_64.tar.gz`, a checksum, and a `release/` folder
with the executable, manifest, source reference, patch and upstream license.

No paid job or build timeout is configured by this script. Before submitting a
cloud build, agree its spending limit and enforce a corresponding provider-side
timeout with margin for disk/transfer costs. The recipe has been exercised with
synthetic tools and a complete Linux build. The full patch applied to the pinned
commit, and the resulting ELF started successfully on Debian 12 after installing
the runtime dependencies listed below. See the pinned upstream
[Linux build instructions](https://github.com/AyuGram/AyuGramDesktop/blob/d4b7546e77f93ac4c9bb53a3fcf7e9ffbbecf1b5/docs/building-linux.md).

## Deployment record — 2026-10-17

The user approved a temporary build capped at USD 10 and installation on the
existing Clawdbot VPS. The pinned Linux build completed and the new independent
profile is running, ready for the user's login. The temporary `e2-standard-8`
builder and its 200 GB boot disk were deleted after delivery; both absences were
verified. Runtime was approximately 3.5 hours, about USD 1.10 for compute, disk
and IPv4 at the planning rates, before transfer costs. This is an estimate, not
a billing invoice. There is no remaining paid builder.

Release provenance:

- Upstream commit: `d4b7546e77f93ac4c9bb53a3fcf7e9ffbbecf1b5`.
- Complete custom patch SHA-256: `8e6cd050250be6a2b2f7517c36266e166db286181759a457e9734950abee827a`.
- Linux binary SHA-256: `d2f296215dddf663afb7a47de1a72e6613edc3acbd37b1f6a962ea56eb435b5e`.
- Archive SHA-256: `b433b5fe13db290f4e0fa9b8cf096e0d2c2b4cad29f3a3e1e389f9ff9ed229d8`.

Completed deployment checks:

- All 393 Manager regression tests passed, along with Python and shell syntax
  checks and an independent review of the deployment changes.
- Normal Quit exited with status 0; automatic restart preserved all common
  preferences. Extra default fields added by AyuGram are allowed; every configured
  Ghost, history, translation, peer ID and transparency value was verified.
- Reinstallation preserved all 44 then-existing regular profile files byte for
  byte. The first-launch pending marker is gone. GTK 3 and PulseAudio runtime
  placement issues found on the actual host were corrected without weakening
  the profile validation.
- The real production backup service completed. Its encrypted offhost snapshot
  was restored into an empty disposable directory; a synthetic SQLite record
  still in WAL survived, and database integrity and `restic check --read-data`
  passed. Staging was removed and the client thawed.
- A real cgroup test killed only a synthetic backup worker with SIGKILL and
  confirmed that systemd cleanup automatically thawed the running client.
- The real Manager SSH tunnel bound loopback only, required VNC password
  authentication, reused its existing port/process and disconnected cleanly
  while AyuGram continued running. A 0.5-second VNC probe timeout caused false
  failures on the real connection; it is now 3 seconds with a regression test.
- All runtime units passed Debian `systemd-analyze verify`. VNC listens only on
  `127.0.0.1:5905`, and X11 has no TCP listener. No OOM events occurred during
  the blank-profile startup, restart and backup checks.
- A dedicated private GCS repository uses restic encryption and the existing
  VPS identity, with bucket-scoped object permissions and no new service-account
  key. A real isolation probe denied IPv4 metadata access while public DNS and
  HTTPS worked. The official IPv6 metadata address is denied by the unit rules;
  IPv6 enforcement was not separately exercised.
- Clawdbot remained active with zero service restarts and HTTP 200 health checks.
  Its service and host resolver were unchanged.

Authenticated message collection, memory use after login, native macOS Screen
Sharing interaction and a full host reboot are reserved for the user. No Mac
account or AUS 3 was opened. The daily backup timer and remote target are enabled
for boot; a live host reboot was deliberately avoided on the shared Clawdbot VPS.

## Boundaries and files

Only a new, independent login is allowed. The user completes Telegram login
inside Screen Sharing. Never upload AUS3, Mac `tdata`, Telegram settings, or any
existing local session. The installer has no session import option.

The dedicated `ayugram-remote` system user has no login shell. Runtime files:

- `/opt/ayugram-remote/releases/<binary-sha256>/AyuGram Inline Test`: immutable
  root-owned releases, with adjacent `release.json`.
- `/opt/ayugram-remote/trusted.json`: selected binary and patch hashes.
- `/var/lib/ayugram-remote/profile`: the sole user-owned login/profile directory.
- `/var/lib/ayugram-remote/backup-staging`: root-only, transient snapshot copy.
- `/etc/ayugram-remote/`: root-controlled VNC authentication and backup config.

The parent state directory is root-owned so the application cannot replace the
backup staging directory. A regular `.ayugram-inline-test` file contains exactly
`ayugram-inline-isolated-profile-v1\n`. The custom binary requires explicit
`-workdir /var/lib/ayugram-remote/profile`. Reinstalls validate and preserve all
existing profile bytes and preferences. An unmarked existing directory is
rejected rather than adopted, imported, reset or erased.

The release manifest must contain `schema: 1`, `platform: "linux-x86_64"`,
`binary_name: "AyuGram Inline Test"`, source commit
`d4b7546e77f93ac4c9bb53a3fcf7e9ffbbecf1b5`, `patch_sha256` of
`experimental/ayugram-inline/ayugram-inline.patch`, and `binary_sha256`.
The operator supplies the independently trusted binary digest to installation.
Matching a self-supplied manifest alone does not establish trust. An executable
Linux x86_64 ELF file is required. Checksum/provenance checking does not prove
that the app can run on Debian or stays within the memory budget.

## Installation, after the build is reviewed

Copy this repository's reviewed deployment files, common preference source,
patch and the verified Linux release to the VPS. Do not copy account directories.
The deployment record above identifies the verified binary already installed.
For another installation, review the release and these commands first. Creating
another paid builder requires budget approval; deployment scripts do not create
one automatically:

```sh
sudo apt-get install --no-install-recommends xvfb openbox x11vnc xauth x11-utils dbus-x11 restic rsync \
  libgtk-3-0 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
  libxcb-render-util0 libxcb-xinerama0 libxcb-xkb1 libxkbcommon-x11-0 \
  libegl1 libgl1 libpulse0 libasound2 libfontconfig1
sudo install -d -m 755 /etc/ayugram-remote
sudo x11vnc -storepasswd /etc/ayugram-remote/vnc-password
sudo chmod 600 /etc/ayugram-remote/vnc-password
sudo python3 tools/ayugram_remote/install.py /path/to/reviewed-linux-release --sha256 TRUSTED_BINARY_SHA256
```

`x11vnc -storepasswd` prompts interactively: never put the password in a command,
URL, config committed to Git, or log. Classic VNC authentication uses only the
first eight password characters; use a dedicated random password and the SSH
tunnel. Installation grants the runtime group read access to this obfuscated
VNC authentication file; no plaintext VNC password is stored by the tools.

For reinstalls, stop `ayugram-remote.target`, `ayugram-remote-backup.timer` and
`ayugram-remote-backup.service` first; the installer refuses running or transitional
services and never quits a client automatically. Existing code/configuration
destinations must be regular, singly linked, root-owned and unwritable by group
or others. Existing VNC/Xauthority files also require their specific owner and
private modes. Unsafe destinations are rejected before installation writes.

The installer checks the candidate before creating a user or profile and enables
units only after verification succeeds. It installs and enables, but does not
start, the runtime. Missing/bad binaries fail explicitly. No auto-updater is
installed; launches always include `-noupdate -ghost`. Dependency packages and
shared libraries required by the actual binary must also be checked on Debian
12 before starting. Do not run `ldd` against an untrusted binary.
GTK 3 is required even when `ldd` reports no missing libraries: this build loads
`libgdk-3.so.0` dynamically and aborts before showing a window when it is absent.

All runtime components and the backup service share `ayugram-remote.slice`:
`MemoryHigh=560M`, `MemoryMax=640M`, `MemorySwapMax=0`, `CPUQuota=50%`, and
`TasksMax=128`. Existing Clawdbot units and its project are not modified. Resource
limits can kill AyuGram or its backup when exceeded. The blank-profile checks
fit below the limit; authenticated load is not yet measured. Do not increase
limits on the shared host without reviewing Clawdbot headroom. Staging requires room for another copy of the non-cache profile.

The backup service uses `OOMScoreAdjust=1000` to prefer stopping a backup over
the collector during kernel memory pressure. This does not guarantee client
survival or control systemd-oomd; cleanup processes inherit the same adjustment.
The existing independent thaw cleanup still runs after a killed backup worker.

The pinned client defaults to 1 GiB / 31 days for each ordinary and media cache
(approximately 2 GiB combined targets). Automatic media downloads are enabled
by default; after the user's independent login, review Storage Usage and
Automatic Media Download in the client. These are native account settings, not
supported keys in the common AyuGram JSON profile. Do not edit encrypted session
settings to seed them. Cache targets are not a filesystem quota: saved history,
manual downloads and backup staging can still consume the shared root disk.
Monitor disk headroom before expanding beyond the initial account. See the
[pinned cache defaults](https://github.com/desktop-app/lib_storage/blob/ccdc72548a5065b5991b4e06e610d76bc4f6023e/storage/cache/storage_cache_types.h),
[cache initialization](https://github.com/AyuGram/AyuGramDesktop/blob/d4b7546e77f93ac4c9bb53a3fcf7e9ffbbecf1b5/Telegram/SourceFiles/storage/storage_account.cpp),
and [download defaults](https://github.com/AyuGram/AyuGramDesktop/blob/d4b7546e77f93ac4c9bb53a3fcf7e9ffbbecf1b5/Telegram/SourceFiles/data/data_auto_download.cpp).

Xvfb uses display `:95`, Xauthority authentication and no TCP listener. The
window manager and client use the same private display; the client has its own
D-Bus session. VNC binds only `127.0.0.1:5905`, requires the dedicated password,
and disables IPv6 listening. Do not add firewall rules opening VNC publicly.

The client uses a private systemd `RuntimeDirectory=ayugram-remote-client`
(mode `0700`) and `PULSE_RUNTIME_PATH=/run/ayugram-remote-client`. This keeps
PulseAudio's ephemeral runtime directory outside the backed-up profile; otherwise
its legacy runtime symlink makes the strict profile guard reject the profile.
No links are followed or allowed by the guard. See the pinned
[PulseAudio runtime path implementation](https://github.com/pulseaudio/pulseaudio/blob/v16.1/src/pulsecore/core-util.c#L1678).

The four runtime services deny traffic to metadata addresses
`169.254.169.254/32` and `fd20:ce::254/128`, preventing them from obtaining the
VM identity used by root restic. GCP also uses the IPv4 metadata address for DNS,
so these services bind a private root-owned `0644`
`/etc/ayugram-remote/runtime-resolv.conf` over their view of `/etc/resolv.conf`,
with Google public resolvers `8.8.8.8` and `8.8.4.4` and
`options timeout:2 attempts:2`. Host DNS is unchanged. The
installer validates the resolver destination before any writes and rejects
linked, incorrectly owned or writable destinations. The backup service and
shared slice have neither restriction, preserving ADC. Systemd 252 IP filtering
requires working cgroup eBPF support: verify actual metadata denial, public DNS
resolution, loopback VNC and root backup access on the host before relying on it;
a parsed unit directive alone is not proof of enforcement.

## Encrypted daily offhost backups

Use a separate GCS bucket/prefix approved for encrypted session backups; review
storage costs and access policy before provisioning. No bucket or credential is
created automatically. Create these files on the VPS outside Git, owned by root
with mode `0600`:

- `/etc/ayugram-remote/restic-password`: a strong unique repository password.
  Keep a separate secure recovery copy off the VPS.
- `/etc/ayugram-remote/gcs-credentials.json`: optional GCS service account key. Prefer the VPS
  existing instance identity with bucket-scoped access over creating a long-lived
  service account key; omit this file and GOOGLE_APPLICATION_CREDENTIALS when
  using that identity. Restrict access to the backup bucket/prefix.
- `/etc/ayugram-remote/backup.env`, systemd environment file:

```ini
RESTIC_REPOSITORY=gs:YOUR_APPROVED_BUCKET:/ayugram-remote
RESTIC_PASSWORD_FILE=/etc/ayugram-remote/restic-password
GOOGLE_APPLICATION_CREDENTIALS=/etc/ayugram-remote/gcs-credentials.json
```

Never commit real values or print the password/key. Initialize the repository
once, after checking the destination:

```sh
sudo systemd-run --wait --pipe --collect -p Slice=ayugram-remote.slice -p EnvironmentFile=/etc/ayugram-remote/backup.env /usr/bin/restic --no-cache init
sudo systemctl start ayugram-remote-backup.timer
```

The daily persistent timer adds up to 15 minutes jitter. It also runs a missed
backup after boot. A manual request queues the same service, never another copy
job. Configuration and password file permissions are validated before backup.
`GOGC=20`, one Go scheduler thread and `--no-cache` reduce backup memory pressure.

The script uses systemd's cgroup freezer for **only
`ayugram-remote-client.service`**, never the display, desktop, SSH, Clawdbot or
entire resource slice. Unlike asynchronous SIGSTOP, `systemctl freeze` waits for
the whole client tree. It times out after 10 seconds; copying times out after
45 seconds (with a further five-second kill grace). The EXIT/TERM/INT handlers
are armed before the freeze attempt. They always try to thaw, including when
freeze itself fails. `ExecStopPost` independently thaws after errors, timeout or
SIGKILL and removes the fixed staging copy. The complete backup service is
bounded to 15 minutes, with a 20-second stop timeout. The usual desktop pause is
only the local staging copy, not the upload. A root-owned `launch.lock` serializes
startup preference writes with snapshot preparation. An inactive/failed client
is copied without freezing; any concurrent startup waits for the same lock.
The startup lock closes atomically at exec, so an active client can then be
frozen as a complete cgroup. The backup releases this startup lock before upload, but retains a separate
exclusive backup-job lock through upload/cleanup so a second job cannot replace
its staging copy. A host/power failure ends processes;
the freezer state does not persist across a reboot.

The snapshot includes SQLite databases **and their WAL/SHM sidecars together**
while all application writers are frozen. Crash recovery is performed from the
copied files on restore. Only known regenerable `tdata/emoji`, `tdata/dumps`,
`tdata/user_data/cache` and `tdata/user_data/media_cache` are excluded. Numbered
account directories and every other directory remain included conservatively;
there is no broad `cache`, database, archive or history exclusion. Restic uploads
only the staging `profile` after thaw; the live profile is never its input.
Staging is removed on upload success and failure. No automatic `forget`, `prune`
or repository deletion is configured; interrupted uploads cannot delete the
last good snapshot. Retention and repository integrity checks remain an
operator responsibility; offhost storage grows until retention is reviewed.
Restic provides encryption; VNC's authentication file
is only obfuscated, and the live profile/staging are sensitive plaintext-at-rest
application files protected by filesystem permissions, not restic encryption.

Service stdout/stderr are suppressed to avoid account/credential leakage.
Inspect `systemctl show ayugram-remote-backup.service -p Result -p ExecMainStatus`
for safe failure information. If necessary, diagnose manually in a private root
session without shell tracing; never paste environment files or credentials.
A queued request is **not** a completed backup. Confirm the service finishes
successfully and inspect snapshots before relying on recovery:

```sh
sudo systemctl start ayugram-remote-backup.service
sudo systemctl show ayugram-remote-backup.service -p Result -p ExecMainStatus -p ExecMainExitTimestamp
sudo systemd-run --wait --pipe --collect -p Slice=ayugram-remote.slice -p EnvironmentFile=/etc/ayugram-remote/backup.env /usr/bin/restic --no-cache snapshots --tag ayugram-remote
```

For restore testing, first use `restic check` and restore a chosen snapshot into
an empty root-only **disposable directory**, never onto the active profile. Verify
the marker, expected history files, WAL/SHM and synthetic known records. Inspect
SQLite integrity with the appropriate database format/tool. Authenticated app
restore checks require an isolated offline test environment and explicit
operator coordination; never run duplicate live copies of the same restored
session. Broad restore/replacement is deliberately absent from Manager v1.

## First start and independent login

Configure and start the backup timer before declaring the desktop ready:

```sh
sudo systemctl start ayugram-remote.target
```

The deployed profile has already completed the initial normal Quit/restart; the
user may sign in directly. The following first-start instructions apply to a new
installation.

Common Ghost/history preferences come from the literal `COMMON_PROFILE` in
Manager's `ayugram.py`, plus `semiTransparentDeletedMessages=true`. The installer
reads this literal with Python AST, without importing Manager or reading account
state. The upstream first unauthenticated run may overwrite Ayu settings before
native core settings exist. **On the first display, do not log in. Quit AyuGram
normally using its menu, wait for the automatic restart (up to 30 seconds),
and log in only on this second launch. Refresh Manager and confirm its first-launch
warning has cleared before signing in.** While the pending marker exists, status
reports `needs_initialization:true` and `ready:false`; `viewer_ready:true` still
allows Screen Sharing for the required normal Quit. The
launcher reapplies the initial preferences once native settings exist, removes
the pending marker, and subsequently preserves custom preferences. It only
repairs a missing global Ghost entry when Global Ghost is enabled. Verify Ghost
Mode/history settings in the client before login and test them with disposable
messages. No authenticated behavior has been verified by these fixture tests.

`Restart=always` restarts a normally quit client, and the target uses Debian
12/systemd 252 `Upholds=` to recover dependencies if the display/window manager
exits. Each runtime service allows three starts per ten minutes to bound failure
loops. Stop `ayugram-remote.target` for deliberate maintenance, not just an
individual upheld service. After resolving a persistent failure, use
`systemctl reset-failed` for these units before restarting the target.

On macOS, create the loopback-only SSH tunnel:

```sh
gcloud compute ssh openclaw-gateway --project project-64666d6d-e266-44ea-bfd --zone us-central1-a -- -N -L 127.0.0.1:15905:127.0.0.1:5905
open vnc://127.0.0.1:15905
```

Screen Sharing prompts for the dedicated VNC password. Manager chooses an
available local port instead of the example `15905`; credentials never enter
the URL. Close the viewer/tunnel when finished. The remote client continues
running, subject to the resource limit and normal service state.

## Manager interface

The root-owned `/usr/local/sbin/ayugram-remote-control` accepts exactly one
argument, `status` or `backup`, and emits bounded JSON with `schema:1` and
`ok:true` on success. Other actions, extra arguments and arbitrary paths are
rejected. Manager uses fixed SSH commands:

```sh
sudo -n /usr/local/sbin/ayugram-remote-control status
sudo -n /usr/local/sbin/ayugram-remote-control backup
```

If the SSH user requires sudo delegation, authorize **only those two exact
commands** in a root-reviewed sudoers fragment (`visudo -cf`), never an arbitrary
shell, Python, systemctl wildcard, argument wildcard, or another user. The helper
and `/opt/ayugram-remote` scripts must remain root-owned and unwritable by that
user. Do not modify unrelated Clawdbot permissions.

Status includes `ready`, `client_active`, `vnc_port:5905`, `backup_configured`,
`backup_active`, `backup_last_result`, and `backup_last_finished`. `ready` requires
verified release/profile, all four runtime units active, a running backup timer,
and root-only backup configuration/password files. This verifies configuration,
not bucket reachability or an existing offhost snapshot. `backup_last_result` is
`never` before a completed run. `{ok:true,queued:true}` confirms queuing only.
Neither helper response contains a password, session content or credential path.

At actual deployment time, create Manager's machine-local
`data/remote_ayugram.json` as a regular user-owned `0600` file with **exactly**:

```json
{"schema":1,"project":"project-64666d6d-e266-44ea-bfd","zone":"us-central1-a","instance":"openclaw-gateway"}
```

This file contains no password or SSH command. Do not install it before the
remote service is reviewed and ready. The repository ignores `data/`.
The deployed Mac now has this private connection file. Reopen Manager to load
the updated code, then choose **Remote AyuGram → Open Remote Desktop**. The VNC
password and separate backup recovery password are in the local private
`data/Remote AyuGram Recovery.txt`; keep that file out of Git and shared logs.
The verified compressed Linux release is retained in
`data/Remote AyuGram Release/` for recovery.

## Focused verification and remaining checks

```sh
python3 -m unittest discover -s tests -p test_remote_deployment.py -q
python3 -m py_compile tools/ayugram_remote/runtime.py tools/ayugram_remote/install.py
bash -n tools/ayugram_remote/backup.sh tools/ayugram_remote/wait-display.sh tools/ayugram_remote/ayugram-remote-control
```

Fixtures prove profile preservation/isolation, release rejection, fixed helper
commands, pending preference repair, resource/listen directives, SQLite WAL
snapshot recovery, safe inactive-client copying, startup-lock exclusion, and thaw/cleanup on freeze
failure, thaw failure, copy timeout and upload
failure. They do not execute systemd on macOS. Actual unit validation, listener
inspection, killed-worker thaw and offhost restore passed on the VPS as recorded
above. After user login, measure the full slice under history and backup load
with `systemd-cgtop`/`systemctl show ayugram-remote.slice -p MemoryCurrent -p MemoryPeak`.
The strict profile guard rejects non-regular entries and must not be relaxed for
new account-generated files without checking their precise location and isolation.
Native Screen Sharing and host reboot persistence still require user validation
or a coordinated maintenance window. Continue checking Clawdbot health. If the 640 MB budget is insufficient,
stop this deployment and reassess capacity; do not raise it silently.

References: systemd documents freezer and failure cleanup behavior in
[systemctl](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html)
and [service units](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html).
Restic documents GCS authentication and repository setup in its
[repository preparation guide](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html).
