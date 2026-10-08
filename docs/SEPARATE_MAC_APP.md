# Install Telegram Manager Ayu alongside the old app

Unzip `Telegram-Manager-Ayu-macOS.zip` and drag **Telegram Manager Ayu.app**
into Applications. Keep your old Telegram Manager app where it is.
Open the new app with Finder. This source-built package requires Python 3;
Xcode command line tools provide Swift for the native window. Without Swift,
it uses Chrome, Brave or Edge with a separate temporary browser profile.
This cloud-built package is not Developer ID signed or notarized; macOS may
require approval in Privacy & Security. No Gatekeeper bypass is included.

The new app starts empty. It never automatically imports the old app's
settings, accounts, backups or metadata. Its bundle identifier is
`com.local.telegram-manager.ayugram`; all persistent manager state lives in:

`~/Library/Application Support/TelegramManagerAyu/`

Accounts are in `TG/`, settings/backups/logs/client masters in `data/`, and
notes/workspaces in the namespace root. It does not write config beside the
app or read the original TelegramManager support folder. Its default local
port is 8478 instead of the original 8477. If that port is occupied, launch
fails without stopping the old app; change only the new app's config port.
Moving or renaming the app does not change its storage namespace.

Set up Telegram and optionally AyuGram in the new app's Settings. Download
AyuGram separately; executable client bundles and real sessions are not
included in this package. See [AyuGram setup](OPEN_WITH_AYUGRAM.md).

Create a disposable account in the new app first. To test an existing account,
close it in the old app and copy its entire account folder into the new TG
folder; keep the original in place. Do not configure an external scan folder
pointing at old accounts or import the old configuration/workspaces: those
are explicit ways to share paths and defeat isolation. A copied session still
represents the same Telegram account on Telegram's servers; separate local
storage does not create a separate Telegram identity.

The operating system's network proxy remains system-wide. Leave “apply account
proxy system-wide” off (the default) if both managers are open. Each edition
has its own proxy records, but neither can make an OS proxy app-specific.

To remove the new edition, quit it and delete only Telegram Manager Ayu.app.
Its data remains in TelegramManagerAyu; deleting that folder separately removes
only this edition's local data. Never delete TelegramManager or the old TG/data
folders as part of uninstalling this edition.

Build from source with:

`python3 tools/build_isolated_app.py /tmp/tgmanager-ayu-build`

All 171 automated tests pass. A real HTTP startup check of the packaged app
confirms an empty account list and separate storage on port 8478.
The cloud checks verify packaging and storage isolation, not native macOS
compilation or client execution. Test on a disposable account before real use.
