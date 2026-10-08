# Safety fixes before AyuGram integration

The rollback tag `stable-before-ayugram` preserves commit
`87977d9bba9c779ba596fa22f09f0a89153b7550`. Test this branch in a separate
installation with disposable accounts before replacing the working Mac app.
A second installation still shares the default private metadata directory;
back up `~/Library/Application Support/TelegramManager/` as well as TG/ and data/.

## Changes

- Rename/move and config import keep shared state locks through commit and
  rollback, protecting concurrent metadata/workspace changes.
- HTTP and HTTPS system proxy baselines are captured/restored separately.
  Failed changes retain recovery records because a command may have partially
  changed system settings. Unknown/failed baseline queries and authenticated
  original proxies are left untouched. Recovery stops on command failure;
  restore epochs are scoped by network service and proxy channel.
- Signing and plist-update failures report failure rather than success.
- Readiness records containing the session token use private temporary files
  with mode 0600, independent of umask. Handler exception logs omit the bearer
  URL and exception text, reporting the exception class instead.
- Malformed/non-object JSON is rejected before dispatch, including destructive
  endpoints. Restore recognizes accounts in configured external scan roots
  while retaining managed-account and symlink validation.

The cloud test run includes 144 tests, Python syntax checks and real backend
startup/HTTP smoke checks. macOS command behavior is mocked where Linux cannot
execute it. This does not certify actual proxy changes, native launcher
behavior or preservation of real Telegram sessions.

## Mac checks before integration

1. Close all Telegram instances and preserve the full working installation
   and private Application Support directory. Use disposable accounts.
2. Rename/move an account while editing a different account's note; verify
   the note and workspace membership survive. Import a disposable config and
   confirm its metadata/workspaces persist after restart.
3. With system proxy application enabled in a controlled test, give HTTP and
   HTTPS distinct original settings; verify both are restored after the
   temporary proxy period and after a failed apply/restart. Do not use this
   feature to replace an authenticated original system proxy: it now declines
   that case because networksetup cannot expose its password for recovery.
4. Restore into an account in an extra scan folder; verify the session files
   and preserved tdata.bak directory. Confirm symlink destinations are refused.
5. Confirm a ready record is private, the dashboard starts and lock/token
   gating still works. Confirm a failed Dock-name/signing operation reports
   failure and does not launch a broken bundle.

## AyuGram release prerequisite

Source-level portable support is established, but integration is not enabled
in this branch. TGManager still accepts Telegram Desktop only.

On 2026-10-08 the official latest release resolved to v7.2.9 and listed
AyuGram.dmg. Homebrew's ayugram cask was at v7.0.9 and disabled with
`fails_gatekeeper_check`. That is evidence about the older listed package;
it does not establish the newer release's signing state or maliciousness.

Use an official current release, then verify on the target Mac:

```sh
codesign --verify --deep --strict /Applications/AyuGram.app
codesign -dvv /Applications/AyuGram.app
spctl --assess --type execute --verbose /Applications/AyuGram.app
```

Do not disable Gatekeeper or signature verification to make this pass. Record
the bundle ID, executable, signing team, architecture and release version.
AyuGram's development source uses `one.ayugram.AyuGramDesktop`, an executable
output name of `AyuGram`, and `TelegramForcePortable/tdata`. The actual released
bundle must be checked before creating its verification policy.

After that prerequisite, add explicit client profiles, separate Telegram and
AyuGram masters and account-level client selection. Validate independent
sessions, open/close process identity, watcher cleanup, backups, restores,
updates and restart persistence with fresh accounts on macOS. Avoid running
two different clients against the same live tdata directory.

Sources:

- https://github.com/AyuGram/AyuGramDesktop/releases/tag/v7.2.9
- https://raw.githubusercontent.com/Homebrew/homebrew-cask/master/Casks/a/ayugram.rb
