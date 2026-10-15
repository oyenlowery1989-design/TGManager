# Custom AyuGram build

The patches target [AyuGram Desktop v7.2.9](https://github.com/AyuGram/AyuGramDesktop/tree/v7.2.9),
commit `d4b7546e77f93ac4c9bb53a3fcf7e9ffbbecf1b5`. They add restoration of locally
archived deleted messages inside ordinary chats after restart, plus persistent
local read progress. The existing deleted-message opacity setting is reused.
See `LOCAL_READ.md` for read-state behavior and limits.

- `ayugram-inline.patch` is the complete application patch from that commit.
- `build-preparation.patch` contains the dependency preparation adjustments.
- `local-read.patch` is an incremental reference for the earlier inline build.
  It is already included in the complete patch; do not apply it again.

## Build

Use full Xcode and a dedicated workspace outside Manager account scan roots.
Its absolute path must not contain spaces because upstream dependency scripts
use unquoted paths. Use a separate mounted disk image if the external volume's
name contains spaces. Follow the upstream macOS prerequisites and allow enough
space for dependencies, compiler caches and output; do not remove account data.

The verified configuration is Xcode 27.0, arm64, Debug, targeting macOS 12.
This is a local experimental build, not a notarized distribution package.
The executable is approximately 1.43 GB. Dependency preparation omits disabled
crash-reporting dependencies and adapts their obsolete deployment targets;
the application patch uses range-v3's existing libc++ compatibility definition.

From a new, empty workspace, set the paths and API credentials yourself:

```sh
export DEVELOPER_DIR=/path/to/Xcode.app/Contents/Developer
export PATCH_DIR=/absolute/path/to/TelegramManager/experimental/ayugram-inline
export TMPDIR="$PWD/tmp/" CCACHE_DIR="$PWD/cache/ccache"
export CLANG_MODULE_CACHE_PATH="$PWD/cache/clang"
export CMAKE_BUILD_PARALLEL_LEVEL=4 CARGO_BUILD_JOBS=4
mkdir -p "$TMPDIR" "$CCACHE_DIR" "$CLANG_MODULE_CACHE_PATH"
git clone --branch v7.2.9 --depth 1 https://github.com/AyuGram/AyuGramDesktop.git tdesktop
cd tdesktop
test "$(git rev-parse HEAD)" = d4b7546e77f93ac4c9bb53a3fcf7e9ffbbecf1b5
git apply --check "$PATCH_DIR/ayugram-inline.patch"
git apply "$PATCH_DIR/ayugram-inline.patch"
git apply --check "$PATCH_DIR/build-preparation.patch"
git apply "$PATCH_DIR/build-preparation.patch"
git submodule update --init --recursive
cd ..
./tdesktop/Telegram/build/prepare/mac.sh skip-release silent
cd tdesktop/Telegram
./configure.sh -D TDESKTOP_API_ID="$TDESKTOP_API_ID" -D TDESKTOP_API_HASH="$TDESKTOP_API_HASH" -D CMAKE_OSX_ARCHITECTURES=arm64 -D DESKTOP_APP_MAC_ARCH=arm64 -D CMAKE_CONFIGURATION_TYPES=Debug -D CMAKE_OSX_DEPLOYMENT_TARGET=12.0 -D CMAKE_XCODE_ATTRIBUTE_CODE_SIGN_IDENTITY=-
cmake --build ../out --config Debug --target Telegram --parallel 4
codesign --verify --deep --strict '../out/Debug/AyuGram Inline Test.app'
```

`TDESKTOP_API_ID` and `TDESKTOP_API_HASH` must be supplied through your local
environment. Do not commit credentials, built applications, profiles or backups.
Ad-hoc signature verification checks bundle integrity; it does not establish
Apple Developer ID identity or notarization.

## Synthetic checks

From the patched source root:

```sh
test_dir=$(mktemp -d)
xcrun clang++ -std=c++20 -Wall -Wextra -Werror -pedantic \
  -fsanitize=address,undefined -I Telegram/SourceFiles \
  Telegram/SourceFiles/test/deleted_message_restore_test.cpp \
  -lsqlite3 -o "$test_dir/restore-test"
"$test_dir/restore-test" "$test_dir/history.sqlite"
xcrun clang++ -std=c++20 -Wall -Wextra -Werror -pedantic \
  -fsanitize=address,undefined -I Telegram/SourceFiles \
  Telegram/SourceFiles/test/local_read_state_test.cpp -o "$test_dir/read-test"
"$test_dir/read-test" write "$test_dir/read.fixture"
"$test_dir/read-test" read "$test_dir/read.fixture"
```

These tests use synthetic records. They cover the production restoration and
local-read policies, malformed input, isolation and persistence serialization.
They do not replace authenticated UI testing or test the encrypted storage
engine itself. The full application build, two empty-profile normal Quit cycles,
and 316 Manager unit tests passed for this source revision.

## Isolation and Manager integration

The output has a separate bundle ID, `one.telegrammanager.AyuGramInlineTest`,
and requires an explicit `-workdir`. That directory must contain a regular
`.ayugram-inline-test` file with exactly
`ayugram-inline-isolated-profile-v1` followed by one newline. Canonical-path and
recursive symlink checks run before profile logs and settings. Use a fresh empty
profile for automated smoke checks. Never mark an original account as a test
profile. The guard is not an operating-system sandbox.

Automatic updates, crash uploads and global Telegram URL/document handlers are
disabled in this custom build. The original session encryption and TDF version
checks remain active: it cannot import a Telegram 7.3.1 session. Use an independent
AyuGram login when the session is incompatible; do not edit session headers.

Manager validates the shared custom bundle against its pinned fingerprint and
provenance, then opens each account's separate `.ayugram/TelegramForcePortable`
profile. Custom Dock names are supplied without modifying the signed executable
or sealed plist. The source commit alone does not install the compiled master.
The online updater currently reads official Telegram and AyuGram releases;
distribution and automatic updates for custom releases still need a separate
release channel and installation policy.

Forum/topic/reply lists, monoforums and Saved Messages sublists are excluded.
Old archives may lack attachment or sender information. Messages never received
or saved locally cannot be reconstructed. Independent profiles do not synchronize
their local history or read state.

The upstream GPL license continues to apply. Preserve the upstream license and
provide the corresponding source, including these changes, with distribution
of a custom binary.
