# Persistent local read state

The custom AyuGram build remembers the latest locally read message ID and the
time of that read for each ordinary conversation. It also preserves an explicit
local read/unread marker. It stores metadata only, without duplicating message
text, attachments, or the deleted-message archive.

Use the existing **Read on Local** menu action, **Mark as Read** while Ghost Mode
has read receipts disabled, or read messages normally under that setting. The
cursor is saved as you advance; a normal Quit flushes pending preference writes.
This applies from the first read with this build; old in-memory progress from a
previous build cannot be recovered after it has exited.

The record lives in AyuGram's existing encrypted account preferences under its
own `tdata`, with a versioned key containing the authenticated user and full typed
peer ID. Standard Manager AyuGram backups include these preferences; Delete Cache
does not remove them. No original Telegram session is involved. Other clients and
separate copies of a profile do not receive this local state.

On restart, the saved cursor is restored before messages arrive. If newer
messages exist, a read-only `messages.getHistory` request counts actual incoming
messages after that cursor. It does not invoke a Telegram read-receipt method.
Responses use pages of 100, with a maximum of 20 pages per reconciliation. Failed,
unknown, stale or incomplete results retain a conservative unread count rather
than hiding new messages. Very large or unavailable ranges can therefore show
the server's larger count until a subsequent refresh or local read.

Generation checks cancel superseded work; ordinary chat view unloading does not
cancel an independent recount. Server counts cannot advance the saved local
cursor. Explicit **Read on Server** and **Read Message** remain network actions
chosen by the user. Automatic mark-after-action and mention/reaction content-read
paths respect disabled read receipts in this build.

Forum topics, discussion reply lists, monoforums, Saved Messages sublists, and
media-playback/mention/reaction badge persistence are outside this feature. They
have separate state models and are not treated as whole ordinary chats.

## Verification

`Telegram/SourceFiles/test/local_read_state_test.cpp` compiles the production
serialization, merge and pagination policy with strict warnings and address /
undefined-behavior sanitizers. Two separate processes write/read a synthetic
record. Cases cover malformed records, timestamps, account and typed-peer
isolation, partial reads, ID gaps, deleted/outgoing/new messages, stale dialog
ordering and bounded pagination. This tests policy/persistence serialization,
not an authenticated UI or the encrypted storage engine itself.

The full application build validates integration and the existing encrypted KV
specialization. The existing deleted-history synthetic regression suite and
Telegram Manager suite are also run. Source review covers lifecycle, request
dispatch, read receipts and asynchronous stale responses.

No existing account is opened for automated verification. Authenticated UI
verification remains a manual acceptance step. Machine-local build and deployment
receipts are excluded from the public source repository. Reproduction commands
are in `BUILD.md`.
