# Managed Manager and custom AyuGram releases

The release channel is fixed to [`oyenlowery1989-design/TGManager`](https://github.com/oyenlowery1989-design/TGManager/releases).
Manager and the custom AyuGram build are independent components. Official
Telegram and official AyuGram releases are separate channels.

A release tag is exactly `manager-vX.Y.Z` or `ayugram-custom-vX.Y.Z`. Versions
have three numeric parts, with no prefix, suffix or leading zeroes. Attach the
matching pair of assets:

| Component | Assets | Bundle | Data compatibility |
| --- | --- | --- | --- |
| Manager | `manager.json`, `manager.tar.gz` | `TelegramManager.app` | `manager-state-v1` |
| Custom AyuGram | `ayugram-custom.json`, `ayugram-custom.tar.gz` | `AyuGram.app` | `ayugram-7.2.9-inline-v1` |

## Build local assets

Use Python 3.9 or newer and macOS developer tools (`swiftc`, `lipo`, `codesign`).
The packager uses only the Python standard library. It does not upload, publish,
launch apps, open profiles or alter the installed Manager bundle. Choose a new
output directory for each component/version; existing assets are never overwritten.

For Manager, first commit the reviewed updater source. Supply the full commit
SHA, not a branch name. The tool reads only tracked bundle blobs from that Git
commit into temporary staging. The selected commit must contain both
`release_runtime.py` and `managed_releases.py`; untracked files and current working-tree edits
are excluded. It compiles the staged Swift launcher for the selected architecture
and macOS 12, writes both plist version fields and a small installed-release
marker, then ad-hoc signs the staged app. It never signs or edits the live app.

```sh
python3 tools/build_managed_release.py manager \
  --version 1.1.0 \
  --source-commit FULL_40_CHARACTER_MANAGER_COMMIT \
  --architecture arm64 \
  --output /path/to/releases/manager-1.1.0
```

For custom AyuGram, reproduce the source and build described in
[`experimental/ayugram-inline/BUILD.md`](../experimental/ayugram-inline/BUILD.md).
Use its complete `ayugram-inline.patch`, not the incremental reference patch.
The source commit is the Manager repository revision containing the distributed
patch and build instructions; `upstream_commit` is the patched upstream source
base. The current compatibility domain targets upstream
`d4b7546e77f93ac4c9bb53a3fcf7e9ffbbecf1b5` (AyuGram 7.2.9).

```sh
python3 tools/build_managed_release.py ayugram-custom \
  --version 1.1.0 \
  --source-commit FULL_40_CHARACTER_MANAGER_COMMIT \
  --upstream-commit d4b7546e77f93ac4c9bb53a3fcf7e9ffbbecf1b5 \
  --patch experimental/ayugram-inline/ayugram-inline.patch \
  --app '/path/to/build/AyuGram Inline Test.app' \
  --architecture arm64 \
  --output /path/to/releases/ayugram-custom-1.1.0
```

Custom input is checked before copying. Profiles, `tdata`,
`TelegramForcePortable`, Manager state files, Python caches, absolute or escaping
symlinks, hardlinks and special files are rejected. Relative framework symlinks
must resolve within the bundle. The archived root is normalized to `AyuGram.app`;
the executable and signed plist are preserved. The bundle ID must be
`one.telegrammanager.AyuGramInlineTest`. The tool checks executable architecture
and runs `codesign --verify --deep --strict` before accepting the staged bundle.
Ad-hoc signatures provide integrity checks, not Apple Developer ID identity or
notarization. The manifest records declared source provenance; the packager
cannot prove a supplied executable was compiled from that source.

API credentials belong in the build environment (`TDESKTOP_API_ID` and
`TDESKTOP_API_HASH`), never in the manifest or release notes. Preserve upstream
licenses and distribute corresponding source, patches and build instructions
with custom binaries as required by the upstream GPL license.

## Manifest and archive contract

Each JSON manifest uses schema `1` and records `component`, strict release
`version`, signed bundle `app_version`, `bundle_id`, `bundle`, `archive`, the
whole-bundle `fingerprint`, full lowercase 40-hex `source_commit`, one
`architecture` (`arm64` or `x86_64`), `min_macos` (`12.0`), and
`data_compatibility`. It also records archive `sha256` and byte `size`. Custom
manifests additionally record `patch_sha256` and full 40-hex `upstream_commit`.
The patch hash covers the exact bytes of the supplied complete patch.

The fingerprint uses the same sorted path/content/symlink-target algorithm as
Manager's release runtime and AyuGram verification. It includes signing metadata.
Manager's staged `Contents/Resources/managed-release.json` stores schema,
component, version and compatibility only; it cannot contain its own fingerprint.
The compressed tar contains one bundle root and only regular files, directories
and validated relative symlinks. No profile or runtime state belongs in an asset.

Review the local manifest and archive before manually creating a GitHub Release
with the exact component tag and uploading both assets. This script has no
publish command. GitHub release assets and checksums are trusted through the
fixed repository channel; SHA-256 is an integrity check, not a separate release
publisher signature.

## Check, approve, schedule, install

Manager checks the release channel weekly and can also check on request. A check
does not authorize a download or installation. The user approves the specific
version before downloading and scheduling it. The staged manifest, GitHub asset
digest/size, archive digest, bundle fingerprint, identity, architecture and
compatibility must all agree before installation.

The helper waits until Manager and all managed clients are closed. It does not
force-close processes or relaunch Manager or clients. Quit them normally when
ready; scheduling does not replace normal Quit. Installation replaces application
bundles only, preserving account folders, sessions, profiles, backups and Manager
state. A failed replacement can restore the previous application bundle.

Rollback restores the previous app, never a profile backup. Manager rollback is
limited to the same `manager-state-v1` compatibility domain. Custom AyuGram
rollback is blocked after the first launch of the newly installed version,
because that launch may have changed profile data. Retain the old app until the
update is accepted, and do not work around the launch guard by restoring or
editing session files.

The first managed update also retains the existing local app and its exact
installation metadata. This local rollback snapshot has a byte fingerprint and
bundle identity; it is not represented as a published release. An unsigned
developer Manager (including the Chrome fallback) can be restored to those
exact saved bytes. Downloaded releases and custom AyuGram always require valid
code signatures. At most one previous bundle per component is retained.

If Manager cannot start after an update, close Manager and the clients, then run
the helper saved outside the app (adjust the absolute project path):

```sh
python3 '/absolute/project/data/ReleaseUpdates/worker.py' rollback \
  '/absolute/project/TelegramManager.app' manager
```

The helper applies the same rollback checks as the UI and waits for normal
closure. Open Manager yourself afterward. If the project has no `data` folder,
`ReleaseUpdates` lives beside the Manager bundle. Do not move either directory
while an update is queued. Launch clients through Manager: direct launches of
account clones outside Manager cannot participate in its lifetime locks or its
first-launch rollback guard.

## Synthetic validation

```sh
python3 -m unittest discover -s tests -p test_release_packaging.py -q
python3 -m py_compile tools/build_managed_release.py
```

The tests create synthetic bundles and mock compiler, architecture and signing
commands. They check archive boundaries, forbidden filesystem entries, metadata,
provenance hashes, selected-commit staging and source immutability. They do not
launch real apps, inspect account data, authenticate a Telegram session, or
substitute for verifying an actual release on its target macOS architecture.
