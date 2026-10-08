# AyuGram v7.2.9 macOS release assessment

Inspected 2026-10-08 without executing the downloaded client. The release was
downloaded from the official AyuGramDesktop GitHub repository, then its SHA-256
was checked against the digest published on GitHub's release asset listing.

## Exact package

- Release: v7.2.9
- Asset: AyuGram.dmg, 183,126,508 bytes
- SHA-256: `b634e44b647d189f656d54e970e95d8afdc8371dd4b4124933732d3b91907a5b`
- Bundle ID: `one.ayugram.AyuGramDesktop`
- Executable: `Contents/MacOS/AyuGram`
- Bundle version: 7.2.9
- Declared minimum macOS version: 10.13
- Architectures: Intel x86_64 and Apple Silicon arm64

The checksum verifies the downloaded bytes match the publisher's listed
asset. It does not provide Apple developer identity, notarization or a
guarantee of safe application behavior.

## Embedded signatures

Static parsing of the universal executable's Mach-O load commands found:

| Architecture | Embedded code signature | Developer team |
| --- | --- | --- |
| x86_64 | No LC_CODE_SIGNATURE command | None |
| arm64 | CodeDirectory with ad-hoc flag (flags 0x20002), no CMS signer | None |

No bundle `_CodeSignature/CodeResources` appeared in the disk-image file
listing. These observations establish this exact package has no embedded
Developer ID signature for either executable architecture. They do not claim
that macOS Gatekeeper was executed here or predict every user's first-launch
behavior. The Linux environment cannot perform an Apple trust assessment.

TGManager's current setup/update verification requires a valid macOS bundle
signature and the expected Telegram Desktop developer team. Accepting this
AyuGram package would require an explicit different trust policy; renaming
the app, changing only its bundle-ID allowlist or disabling checks would not
preserve the current guarantee. This branch does not relax those checks or
enable AyuGram launches.

## Next prerequisite

For integration under the existing developer-signature policy, obtain a
developer-signed AyuGram macOS build from a trusted publisher, then assess
that exact bundle on a Mac. A read-only diagnostic is available:

```sh
python3 tools/check_macos_client.py /Applications/AyuGram.app
```

It reads Info.plist, runs codesign verification/display, performs a Gatekeeper
assessment and reads the architecture list. It never launches the client,
modifies quarantine/signatures, changes network settings or reads account
data. JSON output includes public bundle/signing information and the local
app path. Exit 0 requires all checks to pass and a developer team to be
present; failure/unavailable tooling exits 1.

An alternative release-hash trust policy could be designed and reviewed
separately. It must authenticate the exact publisher artifact, verify every
executable/resource used, reject modified/unrecognized releases and handle
updates explicitly. It is a change in guarantees, not proof the current
package meets Apple's developer-signature policy.

Portable support remains feasible at source level: AyuGram uses
TelegramForcePortable/tdata. Once the trust prerequisite is resolved,
integration still needs client-specific process identity, separate shared
masters, per-account selection, backup coverage for Ayu settings and actual
macOS session-isolation tests.

## Sources

- https://github.com/AyuGram/AyuGramDesktop/releases/tag/v7.2.9
- https://github.com/AyuGram/AyuGramDesktop/releases/expanded_assets/v7.2.9
- https://github.com/AyuGram/AyuGramDesktop/releases/download/v7.2.9/AyuGram.dmg

The inspected package and JSON parsing results remain outside the source
checkout under /workspace/tgmanager-cloud/ayugram-release. No downloaded
binaries or account sessions are committed.
