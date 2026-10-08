# Keep Telegram and optionally open an account with AyuGram

Telegram remains the default for Open, Open All, groups, workspaces, creation
and Session Keeper. The account card adds **Open with AyuGram** for existing
portable sessions. It does not replace Telegram or change an account's default
client permanently.

## Set up once on your Mac

1. Preserve the working version and account data before testing. The code
   checkpoint is `stable-before-ayugram`; it does not include your sessions.
2. Use a separate test installation with a disposable account first. Remember
   private metadata/workspaces are shared by default in
   `~/Library/Application Support/TelegramManager/`; preserve that folder too.
3. Obtain the official [AyuGram 7.2.9 macOS release](https://github.com/AyuGram/AyuGramDesktop/releases/tag/v7.2.9).
4. In Settings → Advanced → Optional AyuGram, choose its AyuGram.app.
5. Close Telegram for the test account, then click **Open with AyuGram**.
6. Close AyuGram using the existing Close action, then use the normal Open
   button to reopen the account in Telegram.

The AyuGram button is shown even before setup; clicking it then explains where
to install AyuGram. New empty login folders continue to use Telegram's normal
Open & Log In action. No bulk action or keeper pass switches clients to AyuGram.

## Session behavior

AyuGram uses the selected account's existing TelegramForcePortable/tdata.
The manager refuses to start one client while the other is detected for that
account, and serializes preparation/launch with other managed account
operations. It verifies the AyuGram app and saves a normal session backup
before launching. Failed verification or failed backup prevents launch.
Normal configured backup retention still applies to these backups.

The same Close, running status, backup/restore guards and idle cleanup recognize
either client. The optional app clone is kept separately at
`<account>/.ayugram-client.app`; it does not overwrite the account's Telegram
bundle or shared Telegram master. Cleanup removes app clones, not tdata.

The manager's concurrency protection cannot stop someone from independently
launching an external client against the same folder. Do not manually run two
clients against one live tdata directory.

## Verification policy

Telegram keeps its existing developer-team/signature verification. The exact
AyuGram release instead uses a separate full-payload SHA-256 manifest, derived
from the official DMG after checking its publisher-listed asset SHA-256:

`b634e44b647d189f656d54e970e95d8afdc8371dd4b4124933732d3b91907a5b`

All 29 payload files are checked, including the executable, libraries, plist
and resources. Missing files, modified files, added payload files and symbolic
links are rejected. Finder .DS_Store files and macOS extended attributes are
not executable payload and are excluded. The app is checked both during shared
installation and after copying the account clone immediately before launch.
No executable or DMG is committed to Git.

This verifies the publisher's exact release; it is not an Apple Developer ID
or notarization claim. The official package's signing limitations are recorded
in [the release assessment](AYUGRAM_RELEASE_ASSESSMENT.md). The integration
does not remove AyuGram quarantine, re-sign it, or disable Gatekeeper. macOS
can still refuse to launch it, in which case the manager reports failure.

Only 7.2.9's exact bundle is currently supported. Updates require a reviewed
new release manifest. Do not substitute an arbitrary signed/modified or newer
bundle. Per-account updater changes are discarded on the next closed-account
open when the manager prepares a fresh verified clone.

## Validation

All 169 automated tests pass in the cloud environment. JavaScript syntax and
the Git whitespace checks also pass.

Cloud checks cover the real full-release verification and shared installation,
the HTTP routes and password lock, rendered button behavior and routing,
mutual client exclusion, exact PID identity, backup/signature-failure handling,
watcher cleanup, launch timeout and preservation of Telegram/session files.
Actual client execution, macOS launch permission behavior and session-format
compatibility require a Mac. Passing cloud tests does not certify those cases.

Before using a real account, verify the disposable account can open in both
clients, close, restart, retain its login and notes, back up and restore, and
keep the original Telegram bundle/master unchanged. Try two different accounts
and confirm they remain independent. Keep the safety branch unmerged until
that native validation is complete.
