# Use Telegram Manager on another Mac

The `codex/ayugram-manager` branch contains the Manager source, tests and custom
AyuGram patches. Git does not transfer installed Telegram/AyuGram applications,
account sessions, saved history, backups or machine-specific configuration.

For installation without Git or compilation, use the
[1.2.0 release installation guide](RELEASE_1_2_0.md).

## Get the Manager

For a source checkout, have Git, Python 3.9 or newer, and Apple's Command Line
Tools available. Check `python3 --version` and `swiftc --version`; do not assume
Python is already installed. Full Xcode is needed to compile AyuGram itself,
not to run an existing compatible AyuGram build.

In a directory where you want to keep the Manager and its local accounts:

```sh
git clone --branch codex/ayugram-manager --single-branch \
  https://github.com/oyenlowery1989-design/TGManager.git
cd TGManager
mkdir -p TG data
open TelegramManager.app
```

Repository access may require your GitHub login. Create `TG` and `data` before
the first launch so profiles and runtime files use the intended directories.
The launcher compiles its native window on the first run.

For subsequent source updates, quit Manager normally, then run in the checkout:

```sh
git switch codex/ayugram-manager
git pull --ff-only
open TelegramManager.app
```

If Git reports local changes or a diverged branch, resolve those changes before
updating; do not reset or delete account directories to make an update work.

## Install the client applications separately

- Configure the shared **Telegram Desktop** application through Manager's Setup.
  The native macOS Telegram application is a different client and cannot serve
  as the portable master.
- Use the **custom AyuGram build** for deleted messages in conversations after
  restart and persistent local read state. Installing official AyuGram does not
  include these patches.
- Select a build matching the other Mac's processor. The existing verified
  custom macOS build is `arm64`; an Intel Mac needs a separately validated
  `x86_64` build.

The published channels use `manager-v1.2.0` and `ayugram-custom-v1.2.0`.
The older 1.1.0 drafts are not installation targets. Pushing source does not
publish or refresh binaries. Custom binary installation is a separate step;
cloning this branch alone does not finish it. See
[managed release packaging and installation](MANAGED_RELEASES.md) and
[custom AyuGram build instructions](../experimental/ayugram-inline/BUILD.md).

## Accounts and VPS history

Log in to separate local profiles on the other Mac. Any deliberate transfer of
an existing profile or backup must be handled separately with the relevant
client closed, never through Git.

VPS access also needs Google Cloud CLI authentication and the appropriate
project/SSH permissions on the other Mac. Configure its local VPS settings and
bind history synchronization to its own initialized AyuGram profile using
**Check Archive IDs**, the verified account ID and **Enable Sync**.
Do not copy absolute profile-path bindings from the first Mac.

The VPS history merge transfers supported saved messages and edits. It does
not transfer Telegram authentication or provide a complete conversation
backup. See [VPS setup and synchronization](REMOTE_AYUGRAM.md).
