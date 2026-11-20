# Operations guide

Documentation version: `v1.61.2-codex.0`

## Bounded native resource details

`resource_details.json` is an optional, owner-private runtime overlay limited to
20,000 entries and 32 MiB. It accepts the original flat format and a compact
transport with SHA-keyed shared native source headers and conversation scopes.
Limits reject a whole oversized batch rather than silently truncating it.
It is applied after the existing secondary completion
timeline and before SQL search, pagination, source jumps, and CSV export. Each
entry pins the complete upstream row and exact account/group context, retaining
one independently authenticated Mac packet. Account binding and exact server ID,
type, sender, and source clock are replayed before applying the entry. An
upstream change or conflicting known field invalidates the overlay instead of
guessing a replacement.

This adapter can fill an absent author from an authenticated envelope mapping
and add existing versioned app-card title/source fields. It does not retain raw
media XML or attachment keys, change source IDs/body/clocks/direction, rewrite
the primary resource catalog, or reinterpret an XML media-owner attribute as a
message author. The source disclosure identifies the additional Mac evidence.
Registry appearance, changes and removal invalidate reader/contact caches and
stop an export whose pinned inputs changed. Review a bounded candidate and
retain a rollback before atomic publication under the global repair guard.

`scripts/repair_wechat_resource_details.py stage` takes explicit private packet
files, a new candidate and a new review report in the canonical workstation.
If capture manifests are present, their exact message IDs and upstream row hashes
constrain staging. A changed or no-longer-active row is never restored from a
stale base-catalog inventory. Native shadow replacements can already contain a
fully parsed reply, forward or text record; these are not resource-detail gaps.
Existing valid details are retained, quarantined conflicts are excluded, and
ambiguous or contradictory new witnesses are reported rather than selected.

Review the stage counters before using the separate `publish` command with
the report, an external rollback path, and a new result receipt. Publication
checks the candidate digest and all source identities again, preserves the old
registry, then atomically replaces only the small runtime overlay. Exact replay
does not rewrite the live registry or invalidate its caches. Stale prior entries
require explicit source revalidation rather than an unchecked row-hash rebase.
Source packet extraction remains separate and must authenticate the selected
account and shard before producing witnesses; readable pages alone do not prove
that account binding is sufficient.

`scripts/capture_wechat_resource_details.py --local-store local_store` inventories
historical app cards with retained native sender evidence and missing labels.
Use Python 3.14 or newer for compressed source cells. Add `--limit` and `--output`
to capture a bounded batch into a private workstation, optionally restricted by
`--target-session`. Resume with the returned cursor in `--after-message-id`.
The output records uncovered candidates and can be passed directly to `stage`
with `--packet`. Capture reads the source without changing the archive.

Transcript reads automatically reuse subtype and labels already present in an
active matching native witness. For a missing group text author, a manifest can
pin an independently authenticated original message by room, server ID, time,
and body. The author's native identifier determines the displayed name through
the same group-name lookup used for other messages.

## Media metadata migration

Preview canonical media metadata without reading cloud-only contents:

```bash
python3 scripts/migrate_media_sqlite.py --local-store /path/to/local_store \
  --report /path/to/private/media-preview.json
```

After the new runtime code is available, apply with a private rollback directory:

```bash
python3 scripts/migrate_media_sqlite.py --local-store /path/to/local_store \
  --report /path/to/private/media-migration.json --apply \
  --backup-directory /path/to/private/media-rollback
```

The default retains Parquet originals. Explicit `--retire-legacy` is permitted only
with `--apply`, after exact compressed rollback verification, source-drift checks,
SQLite integrity checks, and typed row/schema readback. The maintenance gate keeps
cache jobs out during publication; open source or target handles block migration.
Do not run an older writer against retained Parquet after adopting its SQLite
replacement. Preserve both versions if either has independent newer changes.

Only exact canonical names under `local_store/media` and `local_store/x` are
discovered. LLM roots, numbered copies, browser profiles, media payloads, and recovery
archives are excluded. Custom external cache roots need a separately reviewed
inventory; this command does not scan arbitrary settings-selected directories.
Logical bytes, resident allocation, rollback allocation, and net storage change
are separate measurements. A format change does not guarantee disk savings.

See [media SQLite storage](MEDIA_SQLITE_STORAGE.md) for benchmarking and rollback.

## WeChat group member snapshots

A group's Refresh chat history action can save a member snapshot when that account
has a verified local contact database. Group membership history displays the saved
observations. Capture uses the selected account's exact `db_storage/contact/contact.db`
and the configured key. The reader authenticates a stable main/WAL image in memory, verifies
SQLite integrity and the exact room scope, and requires agreement between the
native member relation and the room's embedded member list. The viewer and room
owner must both be present. Missing or inconsistent lists never become empty rosters.

Snapshots are normalized runtime data in
`local_store/messaging/wechat/group_rosters.sqlite3`; original archives and WeChat
databases remain read-only. The first observation establishes a baseline.
Subsequent observations compare stable native usernames within the same account,
room and contact-source stream. Name changes do not count as membership changes.
The displayed comparison covers the latest two comparable snapshots; older
observations remain preserved. A new source stream starts its own baseline.

Newly listed and No longer listed are observation differences with an interval
between captures, not exact join or departure times. Their cause stays unknown.
Existing explicit system notices remain a separate evidence log. A complete,
internally consistent committed local roster does not prove current server
synchronization. Source receipts retain WAL tail status: zero preallocation,
stale-generation tails and valid uncommitted frames preserve the committed view;
incomplete frame/header tails prevent capture.

The scoped refresh and the dedicated roster endpoint use loopback-only jobs with a
server-resolved conversation scope. Capture rejects cross-account reader results. Failed reads or saves preserve earlier snapshots. Desktop and
narrow views use the existing Collapse control; only the two group-history
summaries use 8 px block padding at every viewport.

## Mac WeChat conversation refresh

Private and group conversation pages show Refresh chat history when this computer
has an existing verified key for a message database belonging to the same native
account. The service discovers the existing owner-private registry under
`~/Library/Application Support/agenticContext/WeChat Recovery/Keys/`, using
`macos-*/verified-db-keys*.jsonl`. Keys, account-directory bindings and source paths
remain server-side. This feature does not capture new keys or control WeChat.

In Local resources → Text → WeChat, selecting a verified source account shows
**Refresh WeChat account** to the left of search. The button uses the existing
keys for that account and refreshes its eligible archived conversations across
conversation types. Search, sort, and conversation type stay selected when the
view reloads. The button is unavailable without verified keys for the selected
account. Account refresh shares the conversation refresh job guard and stages
one atomic recovery bank update for the selected account; other accounts and
original source databases remain intact.

On a Python 3.13 server, the task automatically selects a local Python 3.14 or newer
worker after probing both `compression.zstd` and the refresh pipeline. The optional
server environment variable `AGENTIC_CONTEXT_WECHAT_PYTHON` selects a specific
compatible interpreter. Only server-resolved archive scope enters its arguments;
the in-memory transport carries bounded public status. Cancellation and parent EOF
stop publication through the same final cancellation check as in-process execution.

Refresh status reports real preparation, history reading, comparison, update checking,
saving, and readback phases. Counters distinguish configured parts, completed parts,
unavailable or skipped parts, checked messages, and possible update candidates. Candidates
are not additions: added, upgraded, and reparsed describe committed new messages,
placeholder replacements, and same-source semantic repairs only after atomic publication.
No percentage or completion-time estimate is inferred from these counts.

The compatible worker sends changed enum/counter snapshots as newline-delimited progress
frames before its terminal result. Each frame is limited to 4 KiB and the complete stream to
256 KiB; counters must be exact, nonnegative JavaScript-safe integers. The parent validates
frames before updating the polling endpoint. Paths, account identifiers, message bodies,
keys, and exception text never enter this transport. Invalid output fails the task and requests
cooperative cancellation when the child is still running. The compatible worker allows 600
seconds for a single conversation, or 2,700 seconds if the recovery bank is at least 4 GiB.
An account refresh has its own 5,400-second limit. The browser reports an account timeout
separately from a failure to load the saved conversation list. A large bank still needs complete
validation before publication. Parent EOF, cancellation, and deadlines retain the final
publication guard.

Each explicit refresh reads a stable, authenticated main/WAL snapshot and updates
only the selected conversation or eligible conversations of the selected account. Source
databases remain read-only; plaintext and encrypted snapshots remain memory-only.
The only large temporary file is the bounded runtime bank candidate, which is either
atomically published or cleaned up by its owning job. No extraction workstation or
attachment copy is created for a routine refresh.
The candidate is staged below two owner-private directories, so a change to file status
time alone can be rechecked against the exact bytes without relaxing the publication guard.
The comparison and staging steps share the current bank's complete validation proof
when its input identities still match. Account staging compares only conversations
with candidate updates, one conversation at a time.
The candidate still receives full publication validation. The same worker can reuse
that validation for readback after checking the published file's digest and its inputs.
The web process may need to validate the new bank separately if it runs under a
different Python or SQLite version.

The reader accepts main files up to 2 GiB, WAL up to 64 MiB and SHM up to 16 MiB.
When the main file and possible WAL growth exceed 1 GiB, admission requires known
physical RAM and an estimated working set no greater than one quarter of it:
`4 * min(2 GiB, main + WAL) + 2 * WAL + SHM + 512 MiB`.
Reads cannot grow beyond the file sizes admitted before allocation. The memory
estimate is a conservative capacity policy, not a measurement of current free RAM;
memory pressure and competing jobs still need separate operational assessment.

SQLite reserves the fixed page containing byte offset 1 GiB. For this 4,096-byte
profile the reader accepts only an all-zero main-file page at number 262,145;
nonzero contents, arbitrary zero content pages and WAL writes to that page fail.
Receipts count the reserved page separately from HMAC-authenticated content pages,
and the complete committed snapshot must still pass full SQLite integrity checks.
This exception follows the [SQLite lock-byte page specification](https://www.sqlite.org/fileformat.html#the_lock_byte_page);
it is not a general zero-page or failed-authentication fallback.

A configured key does not prove every message shard is accessible. Missing keys,
unsupported source records and conflicting variants remain visible as partial
coverage. A changed source or competing archive publication can reject the refresh;
the existing archive stays intact. Retrying revalidates keys and source identities.
The action only reads records already stored by WeChat on this Mac. Messages that
are only on another device or in a shard without a verified key will not be added.

An authenticated shard lacking enough independent account witnesses is unavailable
for this refresh; other independently bound shards can still publish with partial
coverage. This does not substitute an owner proof. Cryptographic errors, invalid
timelines or owner mappings, source/configuration drift and cancellation still fail
the refresh before publication. If no shard has sufficient witnesses, a successful
partial result reports zero additions and the existing canonical archive count.

External maintenance processes that loaded an older publisher should finish before
using the new refresh action; only processes using the current canonical publisher
participate in its cross-process lock.

## iPhone

Open Agent > iPhone on the USB-connected Mac. Follow [iPhone control](IPHONE.md) for
Xcode and signing prerequisites, the seven setup steps, preview controls, and MCP use.
The local ChatGPT Tunnel phone switch is off by default and resets after an application
restart. It grants phone tools independently of project file access; turning it off blocks
new calls, while an already admitted operation may finish. Setup and recovery remain local.
The Gemini Tunnel and browser Agent do not acquire phone tools.

The default state directory is `~/.local/share/iphone-use`, outside the checkout. Keep its
configuration, owned WDA jobs, and signed builds when updating the application. The page
and standalone `scripts/iphone_mcp.sh` server share its operation lock. Normal app shutdown
closes only its preview/client and leaves the owned WDA start job available for reuse.

## Launch

### Refresh an existing native resource catalog

After the primary WeChat archive changes, use
`app.core.wechat_resource_catalog_refresh.stage_resource_catalog_refresh()` with the local
store root and a new candidate path in an authorized workstation. The function validates the
existing catalog's source receipts against the current primary and removes only proven
duplicate observations. It streams large scopes through temporary SQLite tables and reports
checked events, retained kinds, exclusions, and unchanged input identities. It never reads
media payloads or downloads preserved native databases.

The candidate is not automatically published. Keep a verified recoverable copy of the current
`resource_references.sqlite3`, confirm the primary and catalog identities still match the
candidate report, check candidate integrity and source counts, then publish with an atomic
replacement. Retain the report and rollback mapping. Do not merely edit the history pin or
overwrite the original catalog before validation. Keep the original chat archive unchanged.
This operation revalidates existing metadata; it does not establish full device-backup coverage.

For a separately identified native-source gap, retain a small metadata audit with exact source
snapshot, account, peer, table, local IDs, original clocks, directions, types, and source-file
receipts. `app.core.wechat_native_media_supplement.stage_native_media_supplement()` verifies
that audit against both independently retained primary receipts and the full resident source
database, then creates another unpublished candidate from the refreshed catalog. Its SQL
projection never selects the message payload column. Source databases must already be local;
this staging function does not hydrate cloud files. Preserve unavailable server IDs and local
status values without inventing delivery. Reuse the same rollback and explicit publication
checks, and report both the refreshed catalog scope and any remaining unindexed backups.

For an explicit inventory spanning many native databases, use
`app.core.wechat_native_resource_extension.stage_native_resource_catalog_extension()`.
Provide a current base catalog, a new candidate path, and the exact source snapshot,
original database path, and resident file for each database. Independently retained
primary locators or preservation receipts must prove each full-file SHA-256. The
function streams metadata and deduplication keys through SQLite; it never selects
`Message`, hydrates cloud files, or reads attachments. Exact native account and peer
identifiers define the scope. Unresolved peer hashes remain separate unclassified
conversations. Versioned source databases use hash-bearing record identifiers.

Each database spec may include `chat_tables`: a nonempty, unique allowlist of exact,
ordinary `Chat_<md5>` tables present in that database. Omit it to inspect all Chat tables;
never use an empty list or split a table into row slices. Every selected original database
still requires its full SHA-256. Deduplication seeds use the union of selected account/table
scopes across sources; out-of-scope primary observations may still supply database receipts.
Use `batch_size=1000` and a `progress` callback that checks actual free space and aborts at
the operation's deliberate reserve. Row callbacks count all native metadata rows, including
nonresource types. Check again at table and publication boundaries; a sampled disk guard is
not reserved storage or proof that a long validation cannot exhaust free space.

The candidate retains source occurrences separately from displayed events and records
every inspected table, including tables with no resources. A shared positive server ID
also requires the same account, peer, native clock, type, and direction; missing server
IDs require an exact native backup, database hash, table, and row. Keep alternate backup
receipts and conflicting clocks. Replaying the same inventory must add no events or
occurrences. Inspect coverage and unavailable-source gaps before publication. The legacy
catalog refresh rejects extended catalogs rather than dropping their membership tables.
Compare native direction fields separately from presentation uncertainty. An independently
proven unresolved group sender prefix must not make a new resource appear to be sent by
the archive owner; retain the original `Des` and the table-bound uncertainty receipt.

For audited HTML resources inside forwarded bundles, use
`app.core.wechat_html_forwarded_resources.stage_html_forwarded_resources()` against a
current media-event candidate. It reads source-bound audit metadata rather than archives
or attachments. Exact caption hashes, source ordinals, parent provenance, and member
receipts prove the nesting, including asynchronous JavaScript page members. Children stay
inside their original forwarding parent and reuse the existing embedded resource card.
An unavailable closing boundary remains an explicit source-quality flag; an unknown opening
boundary never supplies a guessed parent. Keep pending occurrences in the staging report.
The ordinary top-level event count and original message rows remain unchanged.

### Verify interrupted-backup restoration coverage

Appending decoded text or rebinding an existing catalog proves only that operation's scope;
neither establishes new native resource coverage. Retain the original source databases,
full-file SHA-256 receipts, exact account/table locators, and backup completion metadata.
Record checkpoint/WAL limitations and unmapped inputs. A successfully imported subset does
not make an interrupted backup complete.

Record device-backup completion separately from each cached profile's newest native clock.
A newer, finished iPad backup can contain older account caches and cannot replace newer
iPhone observations. Reprove each selected owner from that backup's native identity evidence;
display names and another device's LoginInfo are not fresh account proof. Declare every selected
database's original path and full SHA-256 in its source metadata, including databases with no
new text. Inventory `Brand/BrandMsg.db` and `ChatBot/msg.sqlite3` separately from ordinary
`DB/message_N.sqlite` shards; auxiliary text does not establish ordinary resource coverage.
Keep any explicit cloud-backup deferral in the run receipt and retain verified local sources.

Build a per-source, account, table, and native-type coverage matrix with `native_total`,
`decoded`, `metadata_only`, and `gap` counts. Keep those categories disjoint and reconcile
them to the source total. Pin the full SHA-256 and exact `_KINDS` mapping from
`app.core.wechat_resource_index`; derive resource counts from that mapping rather than a
handpicked subset. Report Type 1, each projector-supported type (including generic `unknown`
types 42/48/50/66/67), and nonresource/system types such as 10000/10002 separately. A resource
occurrence or placeholder proves metadata membership, not recovered attachment bytes or decoded
content.
Record missing compression dictionary IDs as explicit decode gaps; an equal dictionary ID
alone does not establish the correct dictionary bytes.

When investigating a missing dictionary, resolve the app version from that backup's retained
`iTunesMetadata`, including its external version identifier, and verify the downloaded app's
bundle identifier and build independently. Do not substitute the current App Store version.
The original WeChat `8.0.80.34` package inspected on `9 Nov 2026` contains `MsgDict.zstd`
and four brand dictionary resources, but `MsgDict.zstd` does not load as a standard Zstd dictionary.
The exact `MsgDict.zstd` resource SHA-256 is
`eb4665027d71302cdc74637155214d424813c5bacf0686e5a3ed7f4f5b9d8c80`.
Its name does not prove dictionary ID 5, and block alignment or high entropy alone does not
prove an encryption algorithm. Preserve the original resource and require a verified unwrapping
method followed by exact native-frame and independent text/reference checks before using it.

Budget an IPA download's original transport, personalized output, and any extraction separately.
An interrupted repack can leave a complete original transport alongside an invalid output;
verify ZIP completeness, every member CRC, version metadata, and full-file SHA-256 before
retaining either. Stream inspection rather than expanding the entire package. Finding a usable
dictionary also requires explicit decoder injection and replay of previously recorded decode
gaps: an unchanged row is absent from a new native delta. Resource-sender repair alone does
not recover quoted bodies or system notices.

Quoted replies, forwarded records, system notices, pat/tickle events, and revocations require
original decoded body evidence and source provenance. A type code or adjacent timestamp does
not prove those semantics. Preserve FTS-only text as secondary evidence; never manufacture
original XML or forwarded structure from it.

### Read authenticated WeChat for Mac sources

Mac SQLCipher database keys are scoped to the native account directory and database. Keep raw
keys outside the repository and disposable workstations, in a private local directory with
directory mode `0700` and file mode `0600`. Never put keys in Git, command arguments, ordinary
logs, runtime message stores, or an implicitly enabled cloud backup. Reauthenticate page one
before reuse; a previously verified key does not prove that a rebuilt or rekeyed database still
uses it. Database encryption keys do not supply an iOS WCDB compression dictionary.

Preserve a stable encrypted main/WAL/SHM/journal file group before reading. Validate the whole
group's identities and full hashes across the copy window. Authenticate every selected database
page and committed WAL frame; validate WAL salts, checksums, commit boundaries, and stale tails.
Run full SQLite integrity validation on the decrypted, memory-only committed snapshot. Record
the exact usable WAL prefix instead of calling an uncertain tail complete. Inventory open
databases separately from all physical database files; partial key coverage is not a full restore.

`wechat_macos_recovery.py` publishes small sealed recovery metadata, and
`wechat_macos_recovery_bank.py` stages an indexed additive SQLite bank beside the original
archive. Neither publisher discovers keys or independently decrypts a source: its caller supplies
the separately authenticated, hash-pinned native projection. Preserve the encrypted source and
private validation receipts. Account, peer, native table, sender mapping, server ID, source clock,
and original-body digest must remain independently traceable. Existing-scope recovery uses either
two exact local text anchors or explicit cross-peer source-account proof with an authenticated
native owner mapping. Names and local IDs alone do not bind a source account.

Native `Name2Id` row IDs vary between databases. Refresh requires exactly one
authenticated row whose username equals the archive owner; missing or duplicate
owner mappings remain unresolved. New account-binding schema 2 records that exact
row and revalidates two original cross-peer Type 1 witnesses, including a sent
message whose sender row and proof equal the owner row. Schema 1 retains its
original row-2 requirement. Two exact local text anchors remain valid for an
existing target conversation. Reused witness coordinates are always checked
against the current authenticated database and primary archive before adoption.

For the narrowly supported case where only `media_events.sqlite3` ctime differs
from a sealed schema-3 bank's input pin, use
`validate_mac_recovery_bank_revalidation()` for read-only semantic preflight.
`stage_mac_recovery_bank_revalidation()` additionally requires a new
`preserved_path`; it preserves an exact owner-read-only bank clone and creates a
separate candidate whose only logical metadata change is the input pin. It
revalidates current source, account, membership, deduplication and shadow evidence;
it does not establish historical byte equivalence of the changed input. The
ordinary reader remains strict. Publish a reviewed candidate through the existing
`publish_mac_recovery_bank()` lock and input-identity checks, and retain its
preservation and semantic receipts.

The bank stores text and inert resource metadata, not attachment bytes or opaque media XML.
Supported App message stubs can be upgraded under the same original event ID only after exact
source matching and empty-old-metadata checks. Retain the original catalog occurrence and its
hashes; unknown authors may be filled only from authenticated native sender mappings. Ordinary
body differences remain distinct observations and must not be overwritten or appended as
duplicate bubbles. Publish only after staged integrity, preservation, and replay checks pass.
Browsing, search, lookup, and export share the same pinned timeline and source validation.

Bank schema 4 is created lazily when a reviewed semantic repair completes a partial forwarded
record, an explicitly unavailable quoted reply, or a known native pat placeholder. The repair
requires the same authenticated raw body, native source coordinates, account, peer, type,
server ID, sender coordinate, and source clock. Retained forwarded items keep their original
paths, contents, and order. The canonical message ID and ordering stay unchanged. Every prior
full projection is retained in a bounded, digest-chained `projection_revisions` journal;
source packets, occurrences, and their original signatures are not rewritten. The reader
validates the complete chain, current head, and historical occurrence proofs. An old projection
replay cannot downgrade the current row, and another source's identical richer replay is a
verified no-op. Ordinary non-repair stages remain schema 3, and older schemas and writer 1.3
banks remain readable. Repairs use the existing full-input comparison and atomic publication
guards; `reparsed` counts published repairs separately from placeholder upgrades and additions.

For storage audits, distinguish logical size, allocated blocks, APFS shared extents, current
runtime stores, derived caches, and old hidden staging databases. A larger text metadata index is
not evidence of copied media. An older stage is not automatically disposable: prove preservation
and check active handles before any separately authorized cleanup. A moved file or lower folder
size does not prove physical space reclamation.

Reviewed derived-message retirement can leave export cross-references attached to a native
message from a different account observing the same group. Use
[`wechat_retained_evidence`](../app/core/wechat_retained_evidence.py) to verify the original
export envelope, source snapshot, import receipt, retirement alias, native scope, and body
digest before excluding that secondary reference from native SID matching. Preserve the
reference and its original account. Do not skip the entire conversation or treat the export
owner as the native owner. Unproved foreign evidence must still fail closed.

For old-row preservation proofs, match declared primary keys, including composite keys in
schema order, and reject NULL key components. Use implicit `rowid` identity only when no
primary key is declared. `INSERT OR REPLACE` can change an internal rowid while preserving
its declared key and protected columns; traversal order is separate from row identity.
If an outer phase fails after private validation, preserve validated private artifacts,
checkpoints, original rollbacks, and an actual-state journal for review. A checkpoint does
not prove publication or authorize automatic resume; recheck source and baseline boundaries
before another attempt.

Before publication, verify complete-table coverage, exact source occurrences, unchanged old
rows, and a same-scope replay that adds no events or occurrences. After publication, read back
canonical counts and representative records from each selected account and conversation,
including clocks, directions, boundary rows, and the requested quote/forward/system cases.
Verify the running UI or read API separately from offline tests, and mock host-side reveal
actions when checking controls. Keep rollback mappings and unresolved gaps explicit. Until
these checks and gap accounting are complete, report partial coverage rather than a full restore.

### Bind a new contact snapshot without inventing name history

Append a new snapshot's sender binding only from its verified native username and account
lineage to a unique, already verified canonical contact. Require exact namespace/realm,
source, snapshot, and native database receipts plus an applicable prior binding; room-only
proof does not establish a global binding. Hold ambiguous or unsupported identities. Keep
existing contact and name-observation rows unchanged.

Any real preferred-name observation in the current scope takes precedence. Otherwise, a
validated retained-source name context may reference an original observation only after
proving the same account, source, contact, native username, and applicable room, together
with the exact new binding and prior anchor. Preserve the original observation ID, source
snapshot, and provenance; do not copy old names into the new snapshot or treat backup dates
as rename times. Missing or conflicting proof remains unresolved. A reader relying on sealed
native-source receipts must disclose that basis and must not claim it reopens the native
contact database for every resolution.

New snapshot aliases must not become members of an older alias's immutable generic-name
anchor. Hash the original canonical generic bindings separately from later snapshot aliases,
while still rejecting conflicting identities. Recheck every retained resolution after a
later binding append; checking only the newest snapshot misses regressions in earlier names.

### Prepare and apply a subsequent device-backup increment

Use `scripts/inspect_wechat_backup_increment.py` with explicit `--backup`,
`--previous-workstation`, and a new `--workstation`. It preserves resident source files
through APFS clones, verifies complete hashes, distinguishes cached owners from current login,
and compares changed native Chat tables by their declared primary keys and complete cells.
It never uses the newest timestamp as a completeness cutoff. Late arrivals, edits, removals,
storage-class changes, and unavailable dictionaries stay explicit. The previous workstation
and its sources remain unchanged. An existing operation is not automatically resumed.
Require a verified primary publication boundary before using a previous workstation as a
skip base. Merely preserving or comparing sources does not prove their records were imported.
Inspect original manifest paths for WAL, SHM, and journal companions; checking siblings of
hashed backup filenames cannot establish that native SQLite sidecars are absent.

Use `scripts/prepare_wechat_backup_increment.py --workstation ... --local-store ...` to
project supported native text and prepare complete changed-table resource scopes. Private
acceptance literals may be supplied through a bounded local `--check-text-file`; do not
embed personal chat text in production code or operational documentation. A search-index
hit is secondary evidence and does not establish an original quote envelope or reference.
The bounded preparation/apply paths reject packages above 128 MiB or 100,000 rows; a larger
scope requires a separately reviewed streaming package rather than an unbounded memory load.

`scripts/apply_wechat_backup_increment.py --workstation ... --local-store ...` stages an
official preview, private append, exact source readback, zero-change replay, complete old-row
proofs, native resource extension and replay, media revalidation, and contact binding checks.
Only explicit `--publish` publishes its fully verified candidates. It uses a maintenance
boundary, preserved rollback clones, full input hashes, a durable intent and per-file actual
state. Keep all artifacts on failure; inspect the actual boundary before a separately reviewed
attempt. After primary publication, rebuild the forwarded-clock inventory and assessment
against the actual canonical file and prove the retained targets before publishing the clock
catalog. Validate the live API, rendered messages and bounded CSV separately.
The text stage checks actual free space before work, through SQLite progress callbacks, and
before private replacement. A disk-reserve interrupt must leave the canonical archive intact
and restore its temporary adapters. Preserve the first resource-stage receipt before replay.

Unchanged database bytes can inherit the previous verified coverage. For a changed database,
only a complete key/cell comparison establishes unchanged tables. Resource allowlists select
whole changed tables; do not report inherited scopes as newly scanned native resource rows.
Keep cloud backup deferred when the user has explicitly chosen local-only recovery.

### Retire proven derived text duplicates

Run `plan_derived_redundancy()` with explicit source/contact scopes and row budgets.
Keep its metadata-only result in a new OneDrive workstation. Do not equate an
unconfirmed export account with the matched native account or assign its timezone.
Only exact, independently identified group-message runs qualify; retain unsupported
and unique content. The planner reads stored text, never media or live WeChat.

Use `stage_retirement(..., apply=False)` first. Explicit application writes an APFS
candidate and a compressed, round-trip-verified rollback containing only removed
observations and affected metadata. Set a deliberate `minimum_free_bytes` in the
pinned plan. Each bounded write transaction reserves its next worst-case batch
and metadata budget against current free space; insufficient headroom discards
the entire candidate without changing the active archive. Never replace this with
a full byte-copy fallback or reduce the protection merely to force a run through.

`publish_retirement()` owns the maintenance boundary and atomic primary/resource
publication. It rejects any currently valid resource index that references a retired
ID; already-stale or cloud-only indexes stay unchanged. Keep the scoped rollback,
plan, and publication receipt outside runtime. Do not claim cloud preservation until
independently verified, and do not evict these records before that verification.
Logical row deletion does not establish reclaimed volume space; no full vacuum runs.

### Optional WeChat conversation body sharing

Inspect independently verified native conversations in the same group or the two reciprocal
sides of a private chat without modifying the archive:

```bash
python3 scripts/compact_wechat_group_storage.py --local-store /path/to/local_store \
  --conversation wechat:FIRST_CONVERSATION_ID --conversation wechat:SECOND_CONVERSATION_ID
```

Discover all unambiguous private chats between verified native account owners and inspect them
as one batch instead:

```bash
python3 scripts/compact_wechat_group_storage.py --local-store /path/to/local_store --all-private-chats
```

The legacy script filename remains supported for both conversation kinds. Private discovery
uses exact account/peer IDs, never names; one-sided and ambiguous pairs stay untouched.
The batch shares one backup, staging clone, and integrity check across all eligible pairs.

Add `--apply --backup-directory /path/to/new/rollback-directory` to publish an APFS-staged
snapshot after exact rollback-copy hashes and logical-row equality pass. The backup directory
must be new and on the same APFS volume. Preserve the receipt and rollback databases. Run this
explicitly again after later imports to share eligible new bodies; passive reads never compact.
Both accounts retain their own counts and every unique or conflicting record. Identical text
does not merge message events, source evidence, timestamps, or account direction.

`incremental_payload_bytes_saved` measures the actual added body/search-prefix reduction,
including reuse of an existing group pool. Its before/after values count selected physical
inline bytes and the complete pool. Legacy `payload_bytes_saved` is a theoretical comparison,
so do not sum it across repeated runs. The report also records database-file byte changes and
allocation separately from reclaimed disk blocks. SQLite can reuse
released pages; no VACUUM, backup deletion, media hydration, or cloud-space release occurs.
Cloud-only or already stale resource/media indexes are retained unchanged and reported as such.
Previously valid local pins are rebound only after the logical archive is verified unchanged.

Implementation: [shared storage](../app/core/wechat_shared_storage.py),
[publication](../app/core/wechat_shared_storage_publish.py), and
[CLI](../scripts/compact_wechat_group_storage.py). Compatibility updates cover
[native restore](../app/core/wechat_native_restore.py),
[export import](../app/core/wechat_export_merge.py), and
[MCP reads](../app/core/wechat_mcp_archive.py). Isolated coverage lives in
[storage tests](../tests/test_wechat_shared_storage.py) and
[publication tests](../tests/test_wechat_shared_storage_publish.py); the matching contracts
are in [Architecture](ARCHITECTURE.md#wechat-human-message-archives) and
[Testing](TESTING.md).

### WeChat storage maintenance boundaries

[SQLite preparation](../scripts/compact_wechat_sqlite.py) and
[atomic publication](../scripts/publish_wechat_compaction.py) require independent candidate
inodes. Candidates must have one hard link, including when adopting an older preparation
receipt or rebinding a sidecar. Publication also rejects shared source inodes across the
selected batch. The original `v1.2.0-codex.0` preparation receipts remain supported, subject
to these current safety checks.

External workstation fences compare existing ancestor directory identities as well as
resolved path spelling. Case aliases on a case-insensitive filesystem and alternative
filesystem paths to the same directory cannot place candidates, rollback artifacts, or
operation journals inside runtime storage. Distinct directories on a case-sensitive
filesystem remain independent.

[Workstation preservation](../scripts/preserve_wechat_workstations.py) rejects parent
traversal, symlinks, and unavailable or native reparse directories before writing or
descending into a subtree. Its exclusive rename backend supports macOS, Windows, and
Linux; unsupported platforms reject `--apply` before creating destination files.
Successive batches retain each earlier locator entry's workstation and receipt. A new
file at an already preserved relative path requires conflict review before any move;
it never silently replaces the earlier mapping. Legacy preservation receipts remain
readable.

[OneDrive inspection](../scripts/verify_wechat_onedrive.py) uses the current user's
Personal sync root, rejects traversal and symlink escapes, and leaves unavailable
subtrees explicitly unverified. Native offline, recall, and reparse attributes never
permit payload or transaction-control reads. Unavailable allocation is JSON `null`;
failed cache inspection is distinct from a known missing file. Local File Provider
flags remain preliminary evidence.
Require independent cloud-content verification and separate public/canonical-cache
allocation checks before releasing preserved local content or reporting disk reclamation.

### Recover HTML forwarded child clocks

Use [the recovery CLI](../scripts/recover_wechat_forwarded_clocks.py) with a stable,
locally available archive and fresh inventory/assessment paths. The default command
scans source provenance once, then assesses every HTML packet with unavailable
text-child clocks. It writes only derived inventory and proof metadata; it does not
publish runtime links or rewrite message bodies, contacts, media, or source clocks:

```bash
python3 scripts/recover_wechat_forwarded_clocks.py --local-store local_store \
  --inventory local_store/messaging/wechat/forwarded-clock-inventory-new.sqlite3 \
  --assessment local_store/messaging/wechat/forwarded-clock-assessment-new.sqlite3
```

Review the assessment's `assessments`, `native_validation`, and `info` tables before
publishing. A matching signature alone is insufficient: exact ordered child IDs,
authors, bodies, source identity, and packet rendering must pass validation, and
all valid iPad copies must agree on child clocks. Unknown native times contribute
no invented time; conflicting explicit clocks prevent automatic recovery.
Strong opaque-ID packets may cross receiving context under the complete agreement
receipt. Short or single-author packets retain receiving-context checks, and
ordinal-only IDs require the same independently verified parent event. Nested or
unsupported structures that cannot meet those proof rules remain unresolved.

Publish the completed assessment with the same paths and `--apply`:

```bash
python3 scripts/recover_wechat_forwarded_clocks.py --local-store local_store \
  --inventory local_store/messaging/wechat/forwarded-clock-inventory-new.sqlite3 \
  --assessment local_store/messaging/wechat/forwarded-clock-assessment-new.sqlite3 \
  --apply
```

Existing inventory and assessment files are reused rather than silently rescanned.
Use new paths after an archive publication; assessment and apply reject a changed
source identity. Apply stages and atomically replaces `forwarded_clock_links.sqlite3`,
records the assessment receipt path and SHA-256, checks catalog integrity, and reads
back every published target and recovered-child count. Preserve the assessment as
the candidate-agreement receipt. The SQLite catalog takes sole precedence over
`forwarded_clock_links.json`; deleting it re-enables legacy JSON lookup, so deletion
is not equivalent to withdrawing its links.

Conversation cards read the published pointers directly. Contact cache fingerprints
include the catalog digest; compatible caches revalidate their accepted references,
including restoring unknown child times after link withdrawal. Contact CSV uses the
same reference projection. Session CSV retains the original parent row and places
the proved packet's children immediately after it, preserving the three-column
schema and exact bodies. Markdown retains the parent source text and adds child
times, source clocks, and recovery proofs. Unlinked records keep their existing
export shape. Unknown children remain blank or `Unknown time`; naive clocks never
acquire an inferred timezone, and parent delivery times never become child times.
Session downloads stop if their pinned evidence changes during export.

The completed assessment on 4 Nov 2026 is
`local_store/messaging/wechat/forwarded-clock-assessment-4Nov2026-refresh2.sqlite3`:

| Measure | Source observations |
| --- | ---: |
| Retained HTML forwarding packets | 9,145 |
| Retained iPad forwarding packets | 11,162 |
| HTML packets with unavailable text-child times | 251 |
| Unavailable text-bearing children assessed | 3,226 |
| Packets with verified recovery | 190 |
| Child times recovered | 2,681 |
| Packets retained as unresolved | 61 |
| Child times retained as unknown | 545 |

No actual native-clock disagreement was found in that assessment. These are source
observations, not unique utterances: repeated packets across accounts or forwards
remain independently addressable. The 2,681 recovered children include the earlier
65-child recovery and must not be added to it again. These dated counts describe
that immutable assessment, not the result of a future archive import.
The refresh2 receipt followed a change to the archive's ctime identity field. A new
full inventory, assessment, and publication reproduced the same counts before
contact caches were rebuilt against the current identity; old proofs were not
accepted by merely replacing their fingerprints.

Prepare the supported local runtime:

```bash
./scripts/setup_python.sh
```

On Windows:

```powershell
.\scripts\setup_python.ps1
```

If the host interpreter is shared with an application that requires incompatible package versions,
use a project-local environment. On macOS, the launch script tries `.venv/bin/python` if the host
interpreters fail the runtime dependency checks. Point setup at the virtual environment explicitly:

```bash
python3 -m venv .venv
AGENTIC_CONTEXT_PYTHON="$PWD/.venv/bin/python" ./scripts/setup_python.sh
./scripts/run_app.sh
```

On Windows:

```powershell
py -3 -m venv .venv
$env:AGENTIC_CONTEXT_PYTHON = (Resolve-Path .venv\Scripts\python.exe)
.\scripts\setup_python.ps1
.\scripts\run_app.ps1
```

Start the Flask console:

```bash
./scripts/run_app.sh
```

On Windows:

```powershell
.\scripts\run_app.ps1
```

### Cloud storage and startup

On macOS, use Finder's `Keep Downloaded` for `app/` (including templates, static assets,
and fonts), `scripts/`, `main.py`, `requirements.txt`, and the selected project-local Python
environment, if any. These are runtime prerequisites and must remain available offline.
Keep required settings and the small prompt/deletion metadata catalogs local as well.
Media payloads under `local_store/` may remain cloud-only; do not pin the entire project
merely to make the console start.

Before importing the application, `main.py` redirects Python bytecode to the host's local
cache directory (`~/Library/Caches/agenticContext/pycache` on macOS). Old cloud-only
`__pycache__/` files cannot block imports even when their `.py` sources are already local.
An explicit `PYTHONPYCACHEPREFIX` is preserved. The cache is disposable; source and media
files remain in their existing locations.

Application construction starts cache tasks with empty in-memory snapshots. Each source
reads its saved inventory when its own page or status is requested. Passive Grok inventory
reads never rebuild a missing catalog, validate image headers, hash media, or rewrite the
catalog. Missing inventory falls back to file-name/type/size counts, explicitly awaiting
integrity and unique-total verification; it never treats cloud placeholders as absent.
The next explicit media sync retains the existing repair and validation pipeline.

A listening port alone does not prove readiness: the reload supervisor owns its socket
before the serving child finishes construction. Check an HTTP response from `/` and
`/api/cache/activity`. If file reads stall and `ls -lO` shows `dataless`, restore the runtime
prerequisites in Finder rather than downloading the entire media archive or resetting it.

### Debug mode and hot reload

`main.py` keeps Flask debug mode and Werkzeug's interactive debugger disabled. Explicit template
auto-reload and file watching remain enabled: template edits apply on the next request, static
files are read from disk, and a saved change to `main.py`, `app/`, or an installed dependency restarts
the serving process automatically. Edits under `tests/`, `scripts/`, `local_store/`, and `logs/`
never restart it.

The launcher runs two processes. A small supervisor owns the listening socket and restarts the
serving child; only the child builds services, owns browser sessions, and starts the ChatGPT
Tunnel. Requests that arrive during a restart wait in the socket backlog.

- A Python reload is a real process restart. The shared runtime shutdown callback runs first, so
  active cache tasks, Agent sessions, Jury runs, and Tunnel calls end. Do not save Python edits
  while a browser-owning job is active.
- Background WeChat contact indexing cancels cooperatively during host shutdown, including long
  SQLite scans and queued builds. An interrupted build discards only its unpublished temporary
  index; the last published index and original chat records remain intact. Opening the contact
  after reload can resume a needed rebuild.
- In-memory state resets on every reload: LAN sessions, Tunnel authorization grants, and queued
  work. The session secret is regenerated unless `AGENTIC_CONTEXT_SESSION_SECRET` is set.
- A child startup or import failure keeps the supervisor alive. It returns plain HTTP 503 while
  waiting for a Python source change under `main.py` or `app/`, then retries the child on the same
  listening socket. Adding, removing, or replacing a source file can trigger recovery; unchanged
  broken code never causes a restart loop. Runtime data, tests, tools, and linked directories are
  excluded from this recovery watcher. Tracebacks stay in Terminal rather than the 503 response.
- A supervisor-code change takes effect on the next complete launcher start; ordinary child hot
  reload cannot replace the already-running parent process.
- Stop the service with Ctrl+C in its terminal, or send `SIGTERM` to the supervisor. Either way the
  supervisor waits for the child to finish browser cleanup before exiting.
- Werkzeug's interactive debugger stays disabled on both loopback and LAN binds. Unexpected
  HTTP errors return a plain response; private traceback diagnostics stay in the server log.
- Set `DEBUG = False` in `main.py` to return to a single process without automatic restarts.

On macOS, the launcher prefers `python3` from `PATH`, then tries unversioned platform Python
installations. It skips an otherwise supported interpreter when required application modules are
missing, so an already prepared platform installation can still start the app without a manual
interpreter override.

Safari automation reuses one background AppleScript worker instead of starting `osascript` for
every API poll or media slice. The worker cannot activate or display a Dock item; it does not
open or close Terminal windows. Scripts and results travel through anonymous pipes, and each
exchange keeps the existing 20-second timeout. Media bytes return in slices of at most 1 MiB per
exchange, so a 2 MiB download range needs two exchanges; the fixed cost of an exchange, not its
size, dominates a transfer. A failed transport is reaped before another request can start. This execution-layer change requires a normal service restart when no
browser-owning job is active; a page reload alone does not update a running Python process.

If closing a stale Safari task window leaves a zero-tab shell, reconciliation records it as
idle before trying to reuse it. A shell that cannot accept a tab can then follow the existing
ownership-checked recovery path; an occupied or unverified window still blocks replacement.
Failure to prepare this temporary window means the account check did not run, not that the
user signed out. The user's authenticated browser windows remain outside this recovery path.

Browser-mode Agent and Settings directory controls use an in-page local folder browser on macOS and Windows.
The browser starts
from the current valid value or the nearest readable existing parent, supports breadcrumbs, Up,
absolute-path navigation, empty folders, and cancellation, and writes the canonical absolute path
only after `Select current folder`. It does not invoke Terminal, Finder, AppleScript, an OS-native
panel, `showDirectoryPicker()`, uploads, probe files, or same-name path guessing. Manual path entry
continues to use the existing validation route. Browsing is loopback-only and remains subject to
the application's Host, same-origin write, and LAN-session boundaries; it does not change Agent
workspace authority or persisted settings until the existing field save flow runs. A running Agent
task keeps its project control locked.

The Tunnel project folder control instead opens one host-native folder panel: macOS Standard
Additions `choose folder` or the Windows Shell folder dialog. A cancelled panel leaves the selection
unchanged. Its chosen path passes through local validation. An existing registered root becomes
current; a new nonoverlapping root is registered as writable and selected. The native endpoint is
loopback-only and accepts only the Tunnel project field.

The normal server address is `http://127.0.0.1:8666`, and the application binds only to loopback by
default. To opt in to trusted-LAN access, set `AGENTIC_CONTEXT_HOST=0.0.0.0` and set
`AGENTIC_CONTEXT_AGENT_PASSWORD` to exactly six ASCII digits before launch. There is no built-in
password. A successful unlock clears the previous session and creates an application-specific
signed browser session with `HttpOnly` and `SameSite=Strict`, matching Worthward's PIN-gate
semantics without sharing its cookie name or signing key. Neither the PIN nor the session cookie
is transport-encrypted over HTTP.

For persistent opt-in, place an owner-only `network-access.json` beside the resolved
`settings.json`, outside the repository. On macOS the default location is
`~/Library/Application Support/agenticContext/network-access.json`. Its JSON object contains
`host` (`0.0.0.0` for LAN or `127.0.0.1` for loopback) and `agent_password` (a string containing
exactly six ASCII digits). Keep POSIX permissions at `0600`, or restrict the Windows file ACL
to its owner. The file stores the PIN as a local credential, as Worthward's configuration does;
never add it to Git, logs, exported cache settings, or the shared synchronization ledger.
Both values must be valid, otherwise the file contributes no bind or credential. Missing,
malformed, oversized, unreadable, or symlinked files fail closed. Existing current and legacy
environment overrides still take precedence. A normal service restart applies the saved bind;
the default launcher requires no extra arguments. Reusing a Worthward PIN is an explicit
one-time local configuration step, not an ongoing dependency on its source tree or process.

When LAN access is enabled, every application route requires that signed session; static assets
and the unlock flow are the only pre-authentication exceptions. Unsafe private-network requests
also require a matching same-origin `Origin` header, and repeated failed unlocks are throttled per
client. Cross-site requests are rejected except for explicit, user-activated top-level page
navigation: GET with `Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`, and
`Sec-Fetch-User: ?1`. This permits external links and their document redirects without admitting
cross-site API or MCP reads, embedded resources, writes, mismatched Origins, or unauthenticated
LAN sessions. Responses vary by these Fetch Metadata headers so caches retain the request
boundary. Public, host-rebinding, and malformed-host requests remain rejected. Keep the console
on a trusted local network and do not expose it through router port forwarding or a
general-purpose public proxy. The only supported exception is the dedicated Gemini hostname in
[GEMINI_TUNNEL_SETUP.md](GEMINI_TUNNEL_SETUP.md): the application recognizes that exact Host and
admits only its OAuth metadata/endpoints and `/mcp/gemini`; every console path remains closed.

## Browser-session preconditions

- X caching begins from the currently signed-in Likes page in a supported host browser.
- Grok and ChatGPT syncing use their existing authenticated browser sessions.
- Microsoft Copilot Text uses the already authenticated `https://copilot.com/chat`
  session. Select Safari on macOS or an existing Chrome/Edge session. It reads the
  all-chats loader, authenticated continuation pages, and rendered transcripts
  without sending prompts. Preserve a supplied entry URL in `copilot_entry_url`,
  including its query: Microsoft's entry parameters can affect merged-history access.
  An explicitly empty continuation cursor proves exhaustion of the current catalog,
  not every legacy Copilot or Bing history surface. A short sidebar list does not
  prove catalog exhaustion. Legacy caches remain `Partial` until completion evidence
  has been persisted in `llm/copilot/sync-state.sqlite3`. A busy Safari
  task queues through the shared coordinator. Verification or sign-in pages stop
  collection; no automatic login repair is attempted. Review the independent
  `llm/copilot/history.sqlite3` cache through Local resources.

- Opening or restoring a blank Jury page, choosing another browser, changing a juror or model,
  and choosing `New session` do not launch a browser clone. `Check accounts` is the explicit
  readiness probe; Start performs the required server-side recheck before it sends a prompt.
  Safari account checks use one task-owned window and one tab per selected juror, send no prompt,
  and reject Claude. Edge and Chrome continue to admit Claude when it is explicitly checked.
- Jury remembers only the last browser, juror keys, and model-tier keys in same-origin local
  browser storage. It validates them against the current rendered options before reuse and never
  persists readiness, account diagnostics, prompts, or session content in that record.
- The current Jury page sends no turn count. It continues only while parsed material evidence is
  changing, records consensus or stable disagreement explicitly, and retains one-hour wall-clock
  and 6,500,000-byte durable-record safety boundaries. Cached legacy clients that explicitly send
  a valid two-to-six-round budget remain bounded. Do not replace these boundaries with an
  unbounded loop or admit another Jury before every owned browser context has closed.
- A standalone `JSON` renderer label immediately before a complete object is a supported provider
  response shape. Other malformed output remains invalid. Repeated invalid votes terminate as
  `structured_vote_stalled`, never as a factual `evidence_stalled` disagreement; inspect the
  preserved raw responses before retrying.
- Recent sessions allows deletion only for a `failed` Jury archive whose coordinator, provider
  workers, Safari window, and durable cleanup ownership have all finished. The local API rechecks
  those conditions before unlinking the exact owner-only JSON record; never remove Jury records
  directly while cleanup is pending.
- Both Zhihu workflows support only Chrome and Edge. They use an isolated temporary clone of the
  selected signed-in profile and perform credentialed same-origin API reads; Safari and an
  unauthenticated standalone HTTP client are not supported.
- A Safari-backed Cache task is macOS-only, opt-in, and owns one standard, visible background window
  with native window controls. It restores the user's previous frontmost application after
  every window-affecting operation, then closes and verifies that exact window at task end;
  it must not hide, minimize, move offscreen, or accumulate Safari windows.
  If a previous process left a recorded task window behind, Recheck closes that leftover window
  when its owner pid is gone or belongs to this process. If Safari refuses to close it, Recheck
  reuses that same task window instead of blocking Account status. A lease that points at a
  pre-existing window (the daily Safari login window) is cleared without closing or navigating it.
  Cache pages that support text/media use `/cache/<source>/<text|media>/<browser>`, for example
  `/cache/grok/text/safari`. The short `/cache/grok` path still renders.
- Chrome, Edge, and Safari support differs by source and automation engine; use the session probe
  in the console before a long sync.
- Passive Agent checks use a quiet, isolated Chromium context. ChatGPT source checks use a
  non-headless context because the provider's Cloudflare challenge rejects headless clones with
  HTTP 403. Windows probes retain offscreen/minimized launch arguments. Existing macOS silent
  probes retain their task-stage window policy and foreground-app restoration.
- Executing Chrome tasks on macOS use an isolated profile clone without offscreen or
  start-minimized launch arguments. macOS ChatGPT Edge Agent reuses Cache's daily Edge sign-in:
  probes, Recheck, source/history reads, and tasks clone the daily profile without CDP fallback.
  The configured ChatGPT Project currently opens a persistent verification page in the clone;
  task submission fails closed before the composer. This is recorded in
  [`CHATGPT_AGENT_CLOUDFLARE.md`](CHATGPT_AGENT_CLOUDFLARE.md). Other macOS Edge Agent providers
  retain their project debug profile.
  On Windows, a clone may be used before the project debug profile is initialized. After a
  successful debug-browser launch creates its marker, every later Windows Agent readiness,
  source, Project, and history probe or Agent task restarts or reuses that same persistent
  project profile over CDP, including after the user closes its window. Ordinary Cache
  probes and sync workers retain their existing clone-first behavior. The task selects or creates its provider page before
  normalizing that page's window. Windows uses CDP to request normal state and position
  `(80, 80)` at `1,280 × 900`, without
  requesting activation. A CDP window-control failure aborts before provider prompt submission.
  These fixed bounds are not a display-work-area guarantee; scaling and multiple monitors still
  require native Windows verification. One context does not guarantee one restored native window.
  Only macOS restores
  the previous foreground app; macOS decides Stage Manager grouping. Human verification reuses
  the same clone and retains the existing Resume gate. Automated execution never opens the user's
  original profile for writing. First-run, crash, notification, and repost prompts remain disabled.
- macOS Edge Jury shares that project debug profile. Its first
  `Check accounts` creates `<agent-runtime>/agent_browser_profile/edge` with owner-only directory
  permissions, starts one project Edge process, and opens one provider setup tab per selected
  juror. Complete the web-service sign-ins in those tabs and choose `Check accounts` again. Do not
  sign the Edge browser itself into a Microsoft account. The project Profile is never initialized
  from the daily Edge Profile and never copies `Local State`, Cookies, passwords, OneAuth data, or
  other browser identity material. Missing initialization and provider authentication both fail
  closed with actionable diagnostics; neither condition falls back to the daily Profile.
- Every macOS Edge Jury worker independently attaches over verified CDP to that one project process
  and creates its own Page in the persistent default context. Page leases are reference-counted in
  process; worker cleanup closes only its Page and connection. The last lease releases all task
  ownership but leaves the project Edge process and Profile available for reuse. A profile launch
  is serialized across threads and local service processes with an owner-only lock. The code does
  not use mock Keychain, basic password storage, disabled encryption, or Keychain mutation.
- Explicit login handoff opens the selected browser visibly. macOS ChatGPT Edge, Chrome, and
  Safari login, and Windows conversation handoff, use the normal resolved browser. Windows Edge
  or Chrome login and other macOS Edge Agent providers use the project-owned persistent debug
  profile so Recheck and later tasks read the same authentication state.
  Opening the page does not establish sign-in; the user must choose Recheck. Native Windows 11
  browser execution remains unverified on this macOS host.
- Windows Edge or Chrome login targets the project-owned debug browser instead of the user's
  daily profile. Because a running Edge or Chrome can keep its sign-in cookies under an exclusive
  OS lock, the Agent reuses a separate Chromium instance launched with
  `--remote-debugging-port` against a dedicated user-data directory under
  `local_store/agent_browser_profile/<browser>`. The login handoff navigates that debug browser to
  the provider home so the authenticated session lands in the debug profile the Agent reads over
  CDP. Once initialized, subsequent Windows Agent checks and tasks reattach to that same window
  while the user leaves it open. Playwright is not attached while `auth.openai.com` or a
  Cloudflare interstitial is visible, because enabling Runtime restarts Turnstile. Complete
  verification in the already-open window, then Recheck. The first sign-in on a fresh Windows
  debug profile is the only manual login the user performs there. One process-local lock owns each
  browser for the full CDP caller lifetime. Waiting is bounded to five seconds, and a busy
  operation fails clearly instead of blocking indefinitely. CDP-backed tasks admit one active
  caller per browser. Clone-backed macOS ChatGPT Edge and Chrome keep isolated-context behavior.
  Multiple application processes are not coordinated by this lock.
- A Cloudflare or CAPTCHA page on ChatGPT, Gemini, Grok, or Claude is not clicked, filled, or
  reloaded. ChatGPT's `auth.openai.com` authorize popup is the same fail-closed case: leave that
  window alone until it returns to chatgpt.com. The Agent aside Account row tells the user to
  complete verification now in the open browser, then Recheck. Repeating the probe, opening
  login again, or clicking Turnstile repeatedly restarts the challenge.
- Claude Agent readiness and source discovery reuse a Claude tab in the selected project debug
  browser instead of navigating its first tab, which may belong to ChatGPT. An existing `/new`
  tab is not reloaded. Challenge checks run before navigation, after navigation, and during
  composer hydration. A pre-attach or newly detected challenge produces the typed
  `human_verification` status, so passive status reads reuse it until an explicit Recheck.
  After updating this Python code, restart the console normally when no browser-owning job is
  active. Complete any remaining challenge manually in the same open Edge window, wait for the
  Claude page to return, and choose Recheck. These guards prevent application-triggered retries;
  offline tests do not establish that Claude will accept the browser or network. ChatGPT passing
  in the same Edge profile does not prove Claude clearance.
- Copying profile files and launching the clone do not prove provider authentication. The copy
  is not an atomic snapshot of a running browser. Check readiness in the clone; report copy or
  access errors without closing the user's browser or retrying against its writable profile.
  On Windows, when the host browser holds its `Network/Cookies` lock, the launcher falls back to
  the project-owned debug browser over CDP instead of failing the clone; no file is read from the
  locked profile.
- Normal clone-backed task exit closes the isolated context and removes its temporary profile. A
  CDP-backed Windows or macOS Edge exit closes only the Playwright connection and leaves the project
  browser and persistent login profile available. Each subsequent Chromium launch also removes only abandoned
  `cachelikes-edge-*` or `cachelikes-chrome-*` directories older than 24 hours; unrelated temporary
  paths are not touched.
- A failed temporary-profile removal reports its retained directory and remains protected from
  stale-profile cleanup in the current process. Cleanup errors become exception notes and log
  warnings when an earlier task, launch, copy, or close error already exists; otherwise they fail
  the operation. Inspect task-owned browser processes before manual cleanup. This protection is
  process-local, not a durable orphan-process tracker across service restarts.
- Do not add or repeatedly troubleshoot login flows as part of normal runtime operation. The
  application assumes an existing signed-in session.

### Copilot acceptance status, 21 Oct 2026

The earlier interrupted-window ownership failure has been recovered. The user
confirmed that its task window had been closed; the journal was preserved in
`local_store/recovery/copilot-safari-acknowledged-20261021T052346Z/ownership-before-recovery.json`.
Recovery held the global Safari lock and verified the former owner process was dead
before clearing stale ownership. A normal real `Start` on v1.43.1 then succeeded.
Host locking and that stale window are not the current coverage blocker.

The real current-catalog read traversed two pages to an empty cursor and deduplicated
17 sessions. The operator requested `Stop` after two sessions and four messages;
no completed backup of hundreds of sessions is claimed. The existing 17-session,
74-message archive retains SHA-256
`f352eb310dfc8fc0a4edd17b6a2bf0e67e831821f8f0b643940cda00ec83fb6e`.

The official exports are preserved under
`local_store/recovery/copilot-official-exports-20261021T052937Z`, with an integrity
manifest. The Copilot activity export has 74 data rows and 16 distinct conversation
titles; repeated titles are not stable session identities. The Windows-apps activity
export has zero data rows. Preserve the originals and backup bytes, and do not merge
or deduplicate sessions by title.

The older records have now been located in the existing macOS Edge Copilot sidebar.
Native accessibility inspection exposed an inner document at
`copilot.microsoft.com/chats/<short-id>` and 357 visible conversation links under
`Our conversations together`. Preserve the current signed-in sidebar: the ordinary
webpage does not expose the same history. The raw directory backup is
`local_store/recovery/edge-copilot-sidebar-20261021T054515Z/visible-history-catalog.json`,
42,468 bytes, SHA-256
`7eed63e2de8f6230c5fc638a52fd656568106d33e3cec47ff54bec07f1f51d3f`.
It explicitly records `discovery_complete=false`. It contains titles, ordinals, and
initial accessibility indexes, not message bodies or verified provider identities.
The indexes must be refreshed after UI changes. Raising the native window allowed
entry 1 to load its two-message transcript; its copied original assistant Markdown
is preserved as `recent-assistant-original.md` in the same recovery folder. Entries
18, 200, and 357 changed the conversation path but showed only the composer after
settling, with no body or error. Their scoped AX snapshots are preserved. This does
not prove deletion. Do not report directory preservation
as a transcript cache or a completed backup of 357 sessions. Body extraction and
verified exhaustion of this source remain unresolved.

Microsoft's [historical Bing export announcement](https://blogs.bing.com/search/2023/5/Bing-Preview-Release-Notes-Chat-History,-Charts,-Export,-and-more/)
documents conversation export and answer export. The current conversation menu has
no export action. The verified `Settings > Privacy > Export or delete history`
path opens `https://account.microsoft.com/privacy/copilot`. After user sign-in,
`Copilot apps > Export all activity history` produced the same 74-row, 96,622-byte CSV
and SHA-256 as the preserved original. Its title values match only entries 1 through
17; the repeated title at positions 10 and 14 cannot establish identity. No title
from entries 18 through 357 appears in that export. Microsoft's [macOS Edge shortcuts](https://support.microsoft.com/en-us/edge/keyboard-shortcuts-in-microsoft-edge)
define Command+S and Command+P for the current tab; neither establishes that sidebar
message bodies will be saved. Do not refresh or replace the sidebar merely to test
an unverified export path. Keep browser access within the granted target and origin;
a top-tab capability does not authorize cross-origin sidepane inspection.

The current-web catalog and official CSV exports do not establish coverage of this
separately observed sidebar directory.
The subsequent ordinary HTTP cache investigation preserved 19 native catalog
responses, 377 physical records, and 357 distinct provider IDs under
`local_store/recovery/edge-copilot-http-cache-20261021T061142Z`. Raw gzip, decoded JSON,
hash manifests, and a merged versioned catalog remain available. Two AX titles have
no exact cached-title match; do not reconcile them by similarity. Directory
`updatedAt` values describe metadata, not message dates. Native conversation URLs
are derived from the observed route and are labeled accordingly. A null cursor in
one cached response does not prove the complete live pagination chain.
The typed canonical directory is `local_store/llm/copilot/legacy-catalog.sqlite3`.
Application v1.43.2 uses it to keep current-web sync and reload Partial until legacy
coverage can be verified; it does not add title-only messages or stop collection of
available current-web messages. The existing 17-session, 74-message archive is
unchanged. Only one retained native history response is nonempty, for entry 1.
Eleven empty responses include sampled entries 18, 200, and 357; preserve their bytes
without interpreting emptiness as deletion.
Microsoft's [official migration guidance](https://support.microsoft.com/en-us/microsoft-365-copilot/troubleshoot-content-copilot-app)
describes possibilities rather than this account's cause; do not infer deletion.
The follow-up request-context alignment for `eligibility`, `hostContext`, and
`sessionID`, including `X-Client-Eligibility` and `x-host-context`, also awaits real
acceptance. It must consume only merged first-party route `store` and successful
query `storePatch` values, and must not read authentication tokens or cookies.

## Computer Use Agent

- Host-loopback requests continue directly. When trusted-LAN binding is explicitly enabled,
  RFC1918 private IPv4 and IPv6 ULA clients must unlock before any application page or API is
  served; public, cross-site, and host-rebinding requests remain rejected.
- Each task defaults to a new root-level ChatGPT, Gemini, Grok, or Claude Web conversation in the
  selected authenticated browser session. On macOS, Safari supports ChatGPT, Grok, and Claude
  Agent execution; Safari Gemini remains a valid source-only route for account, Recent sessions,
  and Project browsing, while its full execution uses Edge or Chrome. macOS Safari Jury runs
  ChatGPT, Grok, and Gemini in one owned window with one tab per juror, including the account
  check, and does not fall back to Edge. Safari Jury rejects Claude; Edge and Chrome still admit
  it when explicitly checked. ChatGPT model selection holds one short focus transaction, because its
  menu closes when Safari loses focus. Other model-menu and Send actions restore focus
  independently; response polling does not hold a focus transaction. Window operations
  preserve a later user app switch. A failed task-window close retains the Safari lease and blocks
  another Safari Jury until tracked cleanup succeeds. A durable ownership token, pre-creation
  window/tab inventory, and owned window ID survive an app restart: admission remains blocked while
  that window exists or creation is ambiguous, then clears automatically once absence is proven.
  An uncertain create waits through a 40-second persisted settle interval and two stable read-only
  inventories 0.5 seconds apart before the lease can clear. Cleanup closes only the exact owned
  window through Safari itself; it never clicks whichever Safari window happens to be frontmost.
  SIGINT, SIGTERM, and process exit share one idempotent cleanup callback. Shutdown first closes
  Jury admission, cancels and waits for active account checks, then waits for the Safari coordinator
  to close its owned window. Restoring the previous frontmost app remains best-effort and does not
  control Stage Manager grouping.
  Browser/provider/model/workspace preferences are queued as one revisioned snapshot and restored
  before the first status or source request after reload. A retired or wrong-provider model invalidates
  the pending snapshot without a POST or retry loop. A selected Safari route is retained rather than
  silently replaced with Edge. On execution-capable routes, the Agent sidebar can
  also join one of the 20 most recent root
  sessions, start a session in one of the 20 most recent projects, or join one of a project's
  20 most recent sessions.
- Gemini `New session in project` proves only a fresh transfer receipt on the selected Notebook
  route; it does not prove that Gemini created an independent provider-side subconversation.
- Settings → Agent controls the operating system, terminal permissions, context limit, turn limit,
  command timeout, and per-operating-system prompts. The operating-system setting detects the host and
  selects macOS or Windows automatically. The selected value must match the host; Windows uses
  Edge or Chrome and does not expose Safari.
- Chromium can attach the generated Markdown context directly. Safari falls back to compact
  on-demand reads because web content cannot programmatically assign a local file to a protected
  file input.
- Sending a task transmits the generated context and requested source excerpts to the selected Web
  account. Review that provider's data controls before using private or regulated source code.
  Safari Gemini source-only browsing never attaches local context or submits a provider prompt.
  Safari Claude execution also never uploads the context file; context is streamed on demand.
- Stop requests end current web generation and terminate the active local command process group.
  Each request binds both the selected session and its current run ID; a delayed Stop from an older
  run of the same session returns HTTP 409. Safari resolves one unique semantic Stop control on the
  exact page and sends one trusted native Return. An ambiguous control causes zero input, and an
  uncertain post-input result is confirmed only through generation-state reads, never a retry.
  On Windows, approved `.ps1` scripts run through the PowerShell controller, and process-tree
  cleanup uses `taskkill /T /F` where applicable. That mechanism is not an OS-level sandbox.
  Stop requests do not stop a detached durable compute job; use its dedicated `Stop job` control
  or `job_stop` with the exact `job_id`.
- Local Web UI acceptance uses the `browser_acceptance` controller action rather than a provider
  browser profile or an arbitrary `run` command. The controller serves only the selected safe
  workspace directory from a task-owned process bound to `127.0.0.1`, refuses port 8666 and any
  occupied requested port, launches clean unauthenticated Chromium with no user-data directory,
  blocks non-loopback/file/credentialed requests, and checks fixed desktop and narrow viewports.
  Screenshot and trace evidence is retained only below the Agent runtime root with size caps;
  incomplete task artifacts are removed. Projects may additionally reserve up to 64 ports in a
  workspace-root `.agenticContext-browser-acceptance.json` file using schema version 1 and a
  `protected_ports` integer array; malformed, linked, oversized, or invalid configuration fails
  closed. Stop, timeout, launch failure, assertion setup failure, and controller exceptions close
  only that task's context, browser, preview process tree, and temporary artifacts. The Action and
  bounded observation are recorded in the normal Agent event chain, and Activity labels the
  workspace root/target plus desktop+narrow Chromium coverage.
- Agent source discovery is cached in `local_store/agent/agent_source_catalog.parquet` for 15
  minutes per provider/browser/Project key. Fresh reads use process memory; the first read after a
  restart hydrates memory from Parquet. Expired passive reads retain the previous catalog and never
  launch a background browser collector. One initial Agent bootstrap cache miss performs a bounded
  check. ChatGPT model and effort discovery happens in its capability bootstrap; later task
  submissions verify their requested settings without a separate refresh button. Concurrent
  non-forced requests share one flight.
  A forced Recheck waiting on an older same-key flight runs a new serialized collector after the old
  flight exits and does not accept the older result as its refresh. The
  response's `cache.status` is `hit`, `miss`, `refreshed`, or `stale`; a stale response means the
  previous verified catalog was retained after an explicit check failed.
- Safari admission is bidirectional: a Cache start is rejected while a Safari Agent owns the
  browser, and an Agent start is rejected while any Safari Cache worker owns it. While a Windows
  Agent task owns Edge or Chrome, or a macOS Agent task owns Safari, source,
  Project-session, history, and bootstrap routes do not start another live collector for that
  browser. They return a cached entry without background refresh, return `unprobed` on a
  catalog/bootstrap miss, or return HTTP 409 on a history miss. The Windows rule is browser-wide
  because every provider shares that browser's CDP context; the Safari rule protects its serialized
  Apple Events context. macOS Agent Edge and Chrome retain their isolated-context behavior; macOS
  Edge Jury alone uses the project process and Page leases described above.
- ChatGPT, Gemini, Grok, and Claude on `/agent` use one agent-scoped browser bootstrap through Recent sessions:
  the same bounded initial check verifies readiness, collects the root session/project catalog,
  returns it to the selector, and seeds the memory/Parquet cache. Cache reuse and task completion
  do not add a browser launch. A later browser launch is reserved for an explicit refresh, task
  submission, or later Project-session selection; an expired keyed Project-session read may serve
  stale rows while one coalesced quiet refresh runs. Safari Grok uses the same visible-composer and
  authenticated-conversations checks through page-local requests without exporting cookies or
  cloning an Edge profile. Restricted Claude accounts remain unavailable and are not sent through
  a login-bypass flow. Safari Gemini stops after catalog discovery, reports execution as
  unsupported, and never enables Ask. Safari Claude enables Ask after the same catalog check.
- Claude.ai can show a Cloudflare `Just a moment...` page in a new Safari task window. The run
  pauses as human verification and surfaces that window; complete the check there, then resume.
  Safari Claude verifies the account's current model label read-only and does not switch models.

### ChatGPT Tunnel connection

Agent → **Tunnel**, with **ChatGPT** selected as the Web service, lets ChatGPT work on explicitly
registered local projects directly, through
the OpenAI Secure MCP Tunnel. No separate desktop app is involved: this service serves the MCP
endpoint at `/mcp` and supervises OpenAI's official `tunnel-client`.

For a local restart that should keep a manually disconnected Tunnel idle, launch with
`AGENTIC_CONTEXT_TUNNEL_START_DISCONNECTED=1`. The sidebar Connect action can still start it.

One-time setup:

1. In Agent → Tunnel, Step 1 contains four expandable visual guides for the OpenAI Platform flow:
   create a Tunnel with a name, short description, organization, and ChatGPT workspace; copy the
   resulting `tunnel_` ID; open API keys and create a key with Expiration set to Never and
   Permissions set to All; then copy the complete `sk-proj-` value immediately because the
   Platform displays it only once. The guides are lightweight inline SVG reconstructions with no
   macOS window chrome, a shared 10px card radius, and intentionally no real identifiers or
   secrets. On narrow screens, focus or swipe the guide body to inspect the full-width card without
   shrinking its labels below their readable size. A Never-expiring key is long-lived: store it
   securely and rotate or revoke it when needed.
2. Enter the Tunnel ID and API key in Agent → Tunnel, Step 2; a qualified pair saves
   automatically. The key is written to `tunnel-credentials.json` beside `settings.json` with
   owner-only permissions and is never rendered back into the page; a blank key field keeps the
   saved key. Save and format errors appear in the sidebar Tunnel status hint. Saving the same
   qualified pair is also an explicit reconnect request, so recovery never requires rotating or
   re-entering a working secret.
3. In ChatGPT, enable Developer mode (Settings → Security and login) and create an app with
   Connection **Tunnel** (the same Tunnel), Authentication **No auth**. After switching the server
   behind an existing Tunnel app, refresh that app under ChatGPT Settings → Apps so it lists the
   current tools.
4. Register the projects ChatGPT may use in `tunnel-projects.json` beside `settings.json`
   (`~/Library/Application Support/agenticContext/` on macOS). The file may be edited directly;
   registry edits apply on the next tool call without a restart. The local `Add project folder`
   control can also register a chosen folder:

   ```json
   {
     "schema_version": 1,
     "projects": [
       {"id": "agenticContext", "root": "/Users/<you>/Desktop/agenticContext", "writable": true},
       {"id": "worthward", "root": "/Users/<you>/Desktop/worthward", "writable": false},
       {"id": "neoMe", "root": "/Users/<you>/Desktop/neoMe", "writable": false},
       {"id": "shared-docs", "root": "/Users/<you>/Desktop/shared_docs", "writable": false}
     ]
   }
   ```

   `writable` defaults to `true`; set it to `false` for projects ChatGPT may only inspect. The
   registry is the maximum authorization boundary. Roots must be absolute canonical paths, must not overlap, and
   must not be the filesystem root or the home folder. A structurally invalid registry fails
   closed, but a valid entry whose directory is absent makes only that project unavailable; the
   other valid projects remain usable. Without this file, the Agent's selected workspace is offered
   as one unregistered compatibility project only when it is a Git repository root, so a parent
   folder such as the Desktop is never exposed as one project.
5. Return to Agent → Tunnel. Every registered project is checked for preferred discovery by
   default; an unavailable root remains visible but cannot be used until it is restored. The
   sidebar offers concise green checkboxes beside registered folders and labels read-only or
   unavailable entries. Checking or unchecking changes discovery preference without revoking a
   registered project's authority. In Step 4, use the inline project selector beside the task
   heading to choose one checked, available registered project for the precise
   kickoff prompt. This inline choice changes only the local prompt target; it does not change
   the service's current project. Background status updates preserve a valid local choice.
   A missing or unavailable current project requires another explicit choice; it does not silently
   redirect an existing task. Registry permissions remain unchanged.
6. The circular `Add project folder` action keeps the host-native folder chooser. On macOS it
   starts at the account's `Path.home() / "Desktop"` when available, otherwise at that account's
   home directory; the start folder itself grants no authority. Choosing an existing registered
   root makes it current. Choosing a new nonoverlapping root registers it as writable and makes it
   current. Once the service confirms the folder choice, the kickoff prompt adopts that project
   and its identity while retaining the task text. Choosing the already-current folder also
   returns the prompt to that project. An invalid or cancelled choice leaves the current project
   and permissions unchanged.
   Project updates and MCP registry reads share an owner-only file lock. If a registration
   cannot save the current selection, its mapping is rolled back before a tool can resolve it.

Runtime behavior:

- `python3 main.py` starts the Tunnel when credentials exist. Saving credentials requests a
  reconnect after a short debounce, and the ChatGPT status card also exposes Retry/Reconnect using
  the saved pair. Test and isolated app instances never start `tunnel-client`.
- State lives in `tunnel/` beside `settings.json`: the pinned client under `tools/`, the client log
  (`tunnel-client.log`, previous run in `.log.1`), its pid file, health URL, a per-start bearer
  token file, durable check jobs, and mutation request records. The bearer file and
  `tunnel-credentials.json` are owner-only on both hosts: mode `0600` on macOS and a protected DACL
  granting only the current Windows user on Windows. Both are verified before any secret byte is
  written, and a save that cannot establish the boundary is refused rather than stored more
  widely. The Tunnel status line reports that refusal, and any other storage or transport save
  failure, until the next credential edit; narrow layouts show it in the sidebar with the rest of
  the Tunnel status. A file saved before this boundary existed is narrowed when it is next read.
- A client left behind by a killed service is stopped before a new one starts, on macOS and Windows
  alike. Ownership requires a `tunnel-client run` command whose `--pid.file` and
  `--health.url-file` arguments are this state folder's files. Each candidate's process start time
  is re-read immediately before it is signaled; Windows additionally holds a process handle while
  confirming ownership, so a reused PID or an unrelated process is never stopped. A failed process
  scan is logged and does not block the new client. On Windows the scan uses one short PowerShell
  WMI query, plus a confirmation query only when a candidate exists.
- The API key reaches the child only as `CONTROL_PLANE_API_KEY`. The child receives a strict
  cross-platform allowlist of process, locale, temporary-directory, and CA-certificate variables;
  unrelated host credentials are not inherited. The control-plane proxy comes from
  `HTTPS_PROXY`/`HTTP_PROXY`, or otherwise from the manual system proxy: macOS System Settings or
  the Windows user Internet Options, each preferring the HTTPS entry over HTTP. Automatic proxy
  configuration (PAC or WPAD), SOCKS-only entries, and the machine-wide WinHTTP proxy are not
  evaluated; set `HTTPS_PROXY` for those networks. Loopback is always exempt so MCP traffic stays
  local.
- `/mcp` accepts only loopback callers that present the current bearer token. `GET /mcp` returns
  405 (no SSE stream). An `OAuth discovery failed` warning in the client log is expected: the app
  uses No auth, so no OAuth metadata is published and readiness does not depend on it.
  Bearer authority is rechecked atomically when each tool call enters the service, including
  each item in a JSON-RPC batch. Disconnecting, restarting, or clearing credentials refuses
  requests that passed the HTTP entry check but have not entered tool execution. Already admitted
  operations retain the existing uncertain-outcome handling and must be reconciled by reading
  their results rather than blindly replayed.
- Both authenticated MCP ingress routes cap JSON request bodies at 2 MiB. JSON-RPC batches are
  rejected before dispatch when they contain more than eight items, so an oversized batch cannot
  partially execute. Each accepted request therefore has a bounded parse cost, dispatch count, and
  aggregate response fan-out.
- Credentials configured, transport ready, a tool request observed, and a successful operation in
  the selected project are separate status facts. A configured, currently ready transport may
  start its first read-only check or normal task without any historical call. Historical success
  does not keep the card green after a status timeout, HTTP failure, stale response, or readiness
  loss. The page bounds and cancels status fetches, shows the last successful status time and the
  current redacted problem, and offers Retry/Reconnect with the saved credentials.
- Preflight, download, and transient network failures use bounded exponential retry; a stable
  ready interval resets the backoff. A client process that remains alive but unhealthy past its
  readiness deadline is recycled under its current generation. Superseded process or status
  results cannot overwrite a newer connection, and uncertain in-flight work is surfaced for
  reconciliation instead of being assumed successful. Closing the management page has no effect
  on the service-owned Tunnel process.
- `current_project` is the only project-less tool. It lets a direct natural-language request such
  as `@AgenticContext check the current project` discover the locally selected registered id,
  identity, permission, availability, selection revision, and registered project ids before calling
  `project_overview`. It also returns bounded records for all other registry-authorized projects,
  so one task may explicitly consult a read-only reference. The `selected_project_ids` response
  field contains the checked discovery subset, initially all registered ids. Host paths stay local
  to the management page and are not
  returned to the model.
- Every other tool names one resolved `project`; the normal page workflow requires a registered
  project. The no-registry compatibility fallback is explicitly unregistered and retains its
  existing writable authority when the selected workspace is a Git work-tree root; the page does
  not present it as a registered project or enable kickoff until explicit registration. Each id
  matches exactly and is never interpreted as a path. `project_overview` establishes the id and
  identity; every later project-scoped call must carry the returned `project_identity`, so changing
  the page selection cannot redirect an
  older request. The identity also binds the directory's native filesystem identity: replacing a
  root at the same path invalidates the old identity, and the failed request is never replayed
  against the replacement. Paths are relative to
  that project's root (absolute and `~` paths are refused), and every path goes through the Browser
  Agent's confinement, symlink, ignored-folder, and credential-file rules for that root. Read-only
  projects refuse every mutation or check before touching anything. Each project keeps its own read
  receipts, SHA-256 guards, edit generation, and verification evidence.
- The registry cache key includes the canonical registry path, device and file identity, modified
  and change timestamps, and byte size. Replacing a same-sized registry while preserving its
  modification time therefore invalidates cached authority. Queued project operations resolve and
  compare the project identity and permission again after acquiring the per-project lock; a
  revocation or remapping while queued is refused before the operation runs.
- If the registry file is absent, the compatibility fallback is available only when
  `git rev-parse --show-toplevel` confirms that the selected directory is itself the work-tree
  root. A `.git` placeholder or a nested directory is not sufficient. Choosing that same folder
  through `Add project folder` materializes its registration before the page enables kickoff.
- Availability is evaluated per registered project. A root that is temporarily absent remains
  visible as registered but unavailable and cannot be selected or opened; it does not suppress
  other usable projects. If that root later appears, discovery binds its native filesystem
  identity before any tool call can proceed.
- The project-tool catalog is exactly: `current_project`; `project_overview` (writability, root and nested
  instruction files of that project only, bounded Git status); `list_files`; `search_files`;
  `read_files` (1-8 files or ranges with SHA-256, truncation, and the next line and optional
  character offset to request);
  `apply_edits` (1-16 exact replacements with write-stage recovery); `write_file` (new UTF-8 files
  with optional project-local parent creation, or whole-file replacement only with the SHA-256
  from `read_files`); `delete_file` (the current SHA-256 from `read_files`); `run_check`;
  `start_check`; `observe_check`; `stop_check`; `show_changes` (status, staged/unstaged stats, and an
  optional bounded patch); and `review_changes` (bodycheck, the final gate). Arguments are
  validated against published closed schemas, so unknown fields fail. Removed compatibility names
  such as `read_file`, `replace_in_file`, `create_file`, `call_runtime_tool`, `list_projects`,
  `git_log`, and `git_diff_hunks` are not dispatchable. ChatGPT can additionally discover the
  fifteen phone tools only while the local [iPhone Tunnel grant](IPHONE.md#chatgpt-tunnel)
  is enabled. ChatGPT also receives four read-only WeChat archive tools
  (`wechat_archive_status`, `wechat_find_contacts`, `wechat_search_messages`, and
  `wechat_get_message_context`). They read only the configured `local_store/messaging/wechat`
  archive, accept no filesystem path, and do not modify it. Gemini retains only the project tools.
  A source change to those tools is loaded only when the serving process restarts. Then refresh
  ChatGPT's stored catalog (Settings → Apps → AgenticContext → Refresh/Scan Tools) before calling them.
  The archive interface reports its own version (`v1.1.0-codex.0`). Status coverage uses
  published history conversation summaries and indexed endpoint metadata, retaining the source
  timestamps, time bases, and endpoint quality flags. It distinguishes undated conversations and
  explicitly excludes optional resource/media overlays from those coverage counts; it does not
  invent unknown-time message or anomaly totals.
  Persistent history, contact, resource, and reference-index connections remain URI read-only.
  Validated timeline projections use connection-local memory tables before `query_only` is enabled.
  Context first locates the target's conversation, validates only that scope, and seeks chronological
  neighbors with a stable message-ID tie break. Unknown-time records remain a separate trailing
  range. SQL connections have a ten-second execution budget; interrupted work returns
  `query_timeout`, never an empty success. Scoped media proof limits return `index_incomplete`
  rather than silently omitting unverified events.
  Sender resolution keeps each reference's own ID and original, untruncated proof internally.
  Embedded HTML authors require the existing source/member/avatar proof; a native-looking sender
  hint alone remains unresolved. Contact IDs identify platform accounts, not assumed real-world
  people. Queries revalidate their publications, including reference-index identity and journals,
  before returning. Missing or stale derived contact indexes are explicit errors and are never
  built by these read-only tools.
- Batch reads distinguish `all_succeeded`, `partial`, and `all_failed`; a top-level success never
  hides an item failure. Their combined model-facing file content is bounded, and a single long
  line can be continued with the returned `next_start_line` and `next_start_character`. Every
  mutation requires a stable `request_id`. A completed retry returns the durable recorded result
  instead of applying it again; a record whose final outcome is unknown refuses blind replay and
  tells the caller to read and reconcile first. When a completed response ages out of the replay
  journal, a compact request-id tombstone remains so that the same id returns `outcome_unknown`
  rather than applying the mutation again. Tombstones are capped at 100,000 records; once that
  safety limit is exhausted, new mutations fail closed instead of discarding older ids. Preserve
  the `expired-ids.log` file beside the request records. Multi-file
  edits preflight the entire batch, verify every published file, and compare current content before
  rollback. A concurrent edit is preserved and reported as a recovery conflict instead of being
  overwritten. The result names committed, uncommitted, rolled-back, and conflicted files so it
  cannot claim all-or-nothing after a partial write.
- `show_changes` always reports path-scoped status and staged/unstaged stats. Set
  `include_patch=true` for a bounded unified patch; `staged=true` selects the staged patch, and
  `path` confines status, stats, and patch together. Credential and controller-internal names
  and patch segments are withheld from every model-visible Git summary. An ordinary untracked
  file appears in status but has no Git patch yet.
- `run_check` invokes only the controller's approved, shell-free command forms with a confined
  project working directory and bounded time, output, and process cleanup. For longer work,
  `start_check` launches the same allowlisted command in a durable bounded runner;
  `observe_check` retrieves status and a bounded output tail across MCP requests or a service
  restart, and `stop_check` cancels only the runner whose recorded process identity still matches.
  An identical active command or repeated idempotency key is deduplicated. A runner that vanished
  without a terminal result is reported as `unknown`, never left permanently `running`. Terminal
  reconciliation is durable and ordered, so querying an older failed job cannot revoke a newer
  pass; every reported successful verification is still rechecked against the current workspace
  fingerprint. The
  invoked test or build remains trusted project code: it runs as the AgenticContext service
  account and is not placed in a hermetic filesystem or network sandbox. Register only trusted
  projects and checks.
- Mutations, synchronous checks, and final review for one project are serialized; different
  projects do not block each other. Read-only file/Git/check-status observation does not wait
  behind a long synchronous check. Only a successful check whose captured workspace fingerprint
  still matches can satisfy `review_changes`; later edits invalidate it.
- Runtime selection is internal to AgenticContext. No public tool accepts or requires
  `runtime`, `runtime_name`, `execution_mode`, `runtime_gateway`, `adaptive_runtime`, or
  `full_operator_runtime`. Transport-added routing metadata is ignored before closed-schema
  validation and cannot select a project or action; every other undeclared argument is rejected.
- The project-tool definitions and dispatcher are static within one Python process. The
  optional iPhone catalog follows its current local grant on every discovery and call.
  `initialize` and `server/discover` advertise `tools.listChanged=false`; no list-changed
  notification is sent, so clients must explicitly refresh after changing that grant.
  Each layer refreshes only its own state:
  - Restarting the AgenticContext service (Python process) is the only step that loads a changed
    `TUNNEL_TOOLS` or dispatcher; `tools/list` and `tools/call` both read that one table.
  - A `tunnel-client` reconnect (a Step 2 credential save, or the automatic restart) renews forwarding and the
    bearer token only; it reloads no Python code and changes no tool.
  - An MCP client reconnect (`initialize` again) receives the running process's catalog, but a
    client may keep its cached tool snapshot.
  - ChatGPT Settings → Apps → AgenticContext → Refresh/Scan Tools replaces ChatGPT's stored
    snapshot. For a source-code catalog change, load the new service code first, then refresh
    the app. Toggling the local iPhone grant requires only a catalog refresh; restarting the
    service clears that grant, so enable it again locally before refreshing phone tools.
- Project tool calls are logged as
  `Tunnel tool <name> provider=<provider> ok=<bool> duration=<s> project=<id> target=<path or command>` in the
  application log, so a ChatGPT session can be audited after a restart.
  Phone calls use `Tunnel iPhone tool <name> provider=<provider> ok=<bool> duration=<s>`.
  Their activity records omit arguments, entered text, observed phone content, and image data.
- The status card exposes one `Tokens:` row. For ChatGPT Tunnel, its value accumulates estimated
  MCP request/response tool-text tokens across normal app server restarts. Only aggregate counts
  are saved in owner-only atomic JSON under the Tunnel runtime root. Independent service
  instances serialize updates through a neighboring owner-only lock file and merge only
  their unsaved calls with the validated saved count. The estimate does not
  decrease when an older call leaves the 20-call diagnostic history. An absent store begins at
  zero; an invalid, inaccessible, or unsaved store makes the value unavailable rather than
  silently restarting it at zero. A failed save does not block tool execution, and a later
  successful save restores the displayed total. The accessible label and tooltip state the
  saved scope. Calls whose tokens
  cannot be estimated are excluded from the cumulative number and make it a labeled lower bound;
  if none can be estimated yet, the value is unavailable. Gemini Tunnel continues to show a
  credential-generation-scoped estimate for retained calls. Neither number represents ChatGPT
  billing, account balance, remaining quota, or a model task total. The `o200k_base` tokenizer
  caches only successful loads. First loads and retries run in the background, with a 30-second
  cooldown after failure. Browser Agent tasks pin the available `o200k_base` tokenizer or the
  UTF-8 byte-count estimate at their first counting boundary. The chosen method survives
  interrupted-task continuation; older counts with unknown provenance are marked estimates.
  Corporate TLS interception can still prevent the initial tiktoken cache download. Provision
  the verified encoding file through the host's trusted network path if the retry cannot reach
  it. Once a background load succeeds, earlier unknown calls remain visible in the 20-call
  history, but `recent_usage` moves to a clearly labeled tokenizer-recovery window. That
  window remains unavailable until a complete new call finishes; it never treats earlier
  unknown counts as zero. ChatGPT `cumulative_usage` retains its already known total across that
  recovery and server restarts, with an unknown-call count so the UI can label the displayed
  lower bound. The shared
  tokenizer cache can be adopted without waiting for a
  second Tunnel cooldown. Restart the Python service to load changed application code;
  reconnecting the Tunnel alone cannot do that.
- Arbitrary shell commands, arbitrary executables, and general background processes are
  intentionally not exposed; only the approved verification commands run through `run_check` or
  the bounded durable-check lifecycle.
- WebCodex Desktop, its DMG, GUI helper processes, and desktop IPC are not part of this path and
  are not required at runtime.

Daily two-page workflow:

1. Open `http://localhost:8666/agent/tunnel/chatgpt`. All registered folders are checked for
   preferred discovery by default; use `Add project folder` and its host-native chooser to select
   an existing registered root or register a new writable root. Existing permissions remain
   unchanged. Choose one checked, available registered project from the inline selector in Step 4,
   then wait for the confirmed current-project revision and a Ready transport. The prompt layout
   remains visible during the switch, while Copy and Ask are disabled until the confirmed prompt
   is ready. Use Retry/Reconnect if the transport is stale or failed.
2. Open ChatGPT, choose `@AgenticContext`, and describe the task normally. Copying the local prompt
   is optional. Its single template pins the chosen project's exact id, identity, and selection
   revision, calls `current_project` to confirm that choice, and then calls `project_overview` for
   that id. A confirmed selector change updates those project fields while retaining edited task
   text. Explicitly named other registered projects may still be inspected as references within
   their registry permissions; read-only projects remain read-only.
3. A task with no supplied id calls `current_project`, pins the returned id/identity, then reads
   `project_overview` and the applicable instructions before any file operation. Every later call
   still sends that explicit id.
4. After changes, read back hashes or inspect changes, run an approved synchronous or durable
   check, and call `review_changes`. A successful check from older workspace content is rejected.
5. Ordinary project switching or transport recovery needs no plugin recreation. Adoption after a
   source-code or tool-definition change has three distinct stages: files updated on disk; the
   AgenticContext Python service restarted and serving that code; and ChatGPT's cached app catalog
   refreshed through Settings → Apps → AgenticContext → Refresh/Scan Tools. A Tunnel reconnect
   updates forwarding only. Confirm each stage separately, then start a new ChatGPT conversation.
   A local iPhone grant change needs a fresh tool catalog without a service restart. After an
   actual service restart, enable that grant again before refreshing phone tools.

Maintaining the client when OpenAI changes it:

1. The version and six archive/binary SHA-256 pairs are pinned in `app/core/tunnel_runtime.py`.
   Take new values only from the official `openai/tunnel-client` release. For a one-off trial,
   `AGENTIC_CONTEXT_TUNNEL_CLIENT_BIN` points the service at another binary.
2. Run `tests/test_tunnel_runtime.py`, `tests/test_tunnel_mcp.py`, and
   `tests/test_tunnel_credentials.py`.
3. Exercise the real forwarding path without ChatGPT: with the service running, start
   `tunnel-client dev proxy --mcp-server-url url=http://127.0.0.1:8666/mcp,channel=main` with
   `MCP_EXTRA_HEADERS="Authorization: file:<tunnel/mcp-authorization>"`, then send `initialize`,
   `tools/list`, and a read-only `tools/call` to the printed MCP URL.
4. Ablate before adopting a new flag, header, or protocol version: remove one change at a time and
   keep it only if its removal breaks readiness (`/readyz`), the `dev proxy` round trip, or a live
   ChatGPT call. Update `MCP_LEGACY_PROTOCOL_VERSIONS` and `MCP_STATELESS_PROTOCOL_VERSION` in
   `app/core/tunnel_mcp.py` the same way.

#### Recover ChatGPT Tunnel forwarding on macOS

Use this procedure when forwarded `tools/call` requests receive `upstream_status: 401` from
local `/mcp` after previously working. A folder missing the bearer, PID, and health URL files
can indicate that a running client outlived its supervised generation. Use the saved Tunnel ID
and API key.

1. In macOS Terminal, inspect the status and list client processes by executable name. Record
   `state`, `problem_code`, and `message`. Compare client PIDs with `tunnel-client.pid` and check
   their parent processes to identify any client left by an earlier service instance.

   ```bash
   curl -fsS 'http://127.0.0.1:8666/api/agent/tunnel/status?platform=chatgpt' |
     python3 -c '
   import json
   import sys

   status = json.load(sys.stdin)
   fields = ("state", "problem_code", "message", "configured", "generation")
   print(json.dumps({field: status.get(field) for field in fields}, indent=2))
   '
   ps -ax -o pid=,ppid=,lstart=,ucomm= | awk '$NF == "tunnel-client"'
   ```

2. Select **Retry/Reconnect** on the ChatGPT Tunnel card or run the following command in Terminal.
   The new generation stops clients owned by this runtime and issues a fresh bearer. The request
   can return while the client is still starting. Poll the status until it settles.

   ```bash
   curl -fsS -X POST 'http://127.0.0.1:8666/api/agent/tunnel/connect' -o /dev/null
   ```

3. If status reports `runtime_state_unwritable` or the client continues to fail or retry, stop the
   service cleanly and wait for it to exit. Use Ctrl+C in its launcher Terminal, or send SIGTERM
   to the supervisor. Start it again using the normal [launch command](#launch). If the same
   runtime-state error persists and points to a client log, stop the service again and move the
   affected `tunnel-client.log` files to a private backup location before relaunching.
4. Confirm that status is `ready` and `configured` is `true`. In
   `~/Library/Application Support/agenticContext/tunnel/`, check for `mcp-authorization`,
   `tunnel-client.pid`, and `tunnel-client-health.url`. The PID file should match the new
   supervised client, and any previous client PID should be gone. Check for a fresh
   `tunnel-client started` log entry followed by zero new upstream HTTP 401 warnings. Keep
   the bearer file and raw credential data out of diagnostic output.
5. In ChatGPT, go to Settings → Apps → AgenticContext → Refresh/Scan Tools. Start a new
   conversation and run a read-only tool call to verify forwarding from ChatGPT.

### Gemini Custom App connection

Select **Gemini** in Agent → Tunnel to configure the independent Gemini transport. It reuses the
same exact fourteen-tool catalog and project authority but does not reuse the OpenAI Tunnel ID, API key,
control plane, local bearer, or supervised process. Gemini requires a stable public HTTPS MCP URL,
so the operator supplies a dedicated reverse tunnel and AgenticContext supplies a static OAuth 2.1
client with PKCE S256.

Follow [GEMINI_TUNNEL_SETUP.md](GEMINI_TUNNEL_SETUP.md) for the complete setup, Cloudflare named-
tunnel example, current consumer-account prerequisites, Gemini Connected Apps steps, verification,
rotation, and troubleshooting. Operational invariants are:

- Keep AgenticContext bound to `127.0.0.1:8666`; the reverse tunnel forwards to loopback and
  sets the origin Host to the dedicated public hostname. The proxy itself must allowlist only the
  six documented OAuth/MCP paths and return its own 404 for every other path.
- Only the documented OAuth discovery, authorization, token, and `/mcp/gemini` paths are reachable
  on that Host. A public 404 for the console, local APIs, static files, ChatGPT `/mcp`, and every
  other path is expected.
- The private `gemini-tunnel-credentials.json` is independent of `tunnel-credentials.json`. POSIX
  hosts enforce mode `0600`; on Windows verify that the current user's settings-directory ACL is
  restricted to the intended account. Never put the Gemini client secret, signing key, access
  token, or refresh token in a URL, repository, reverse-proxy configuration, or support log.
- Configured means only that the local origin and OAuth material exist. Active requires an
  authenticated Gemini tool call observed by this process. Local tests do not prove a real Gemini
  connection, consumer eligibility, enterprise compatibility, or write-confirmation behavior.
- Clearing the public origin revokes the current local OAuth material. Stop or remove the external
  reverse tunnel and disconnect or remove the Custom App in Gemini separately.
- A process restart preserves the saved static client credentials but invalidates every in-memory
  authorization code, access token, and refresh token. Reconnect the Gemini Custom App after the
  service restarts.
- Copying the client secret never approves a callback. A complete valid authorization request is
  held in process memory for 10 minutes while the authenticated local Agent UI displays its exact
  callback URI. Approve or deny that exact URI there; a different redirect, state, PKCE challenge,
  client, resource, or scope cannot consume the approval. An opaque review identity also prevents
  a stale local page from approving a request that replaced the one it displayed.
- The OAuth adapter does not guess an undocumented consumer callback hostname. It pins the first
  approved complete callback URI for the current process. Deny an unexpected URI instead of
  broadening or bypassing the local review.

### Durable optimization jobs

Before `job_start`, create a reviewed workspace-root `.agenticContext-compute.json` file. It must pin
the exact approved entrypoint bytes, for example:

```json
{
  "schema_version": 1,
  "entrypoints": [
    {
      "id": "optimizer",
      "path": "scripts/optimizer.py",
      "sha256": "64-lowercase-hex-characters",
      "max_runtime_seconds": 43200
    }
  ]
}
```

The runtime must be between 43,200 and 86,400 seconds. Recalculate and deliberately review the
digest after every entrypoint change; a stale digest fails closed. The optimizer receives only
`--config <runtime-copy> --job-runtime <task-directory>` and, for an explicit resume,
`--resume <prior-checkpoint>`. Its config is a workspace-relative regular JSON file no larger than
1 MiB. Shell operators, redirects, executable names, environment enumeration, download commands,
and arbitrary argument vectors are not part of the action protocol.
On macOS, the worker also runs through `/usr/bin/sandbox-exec` with `network*` denied. The Windows
path does not currently apply an equivalent OS-level network-denying sandbox profile; the worker
runs with the current user's permissions. Optimizers must therefore use only local datasets and
must not depend on license servers, remote telemetry, distributed network workers, or localhost
sockets during the job.

Publish heartbeat data atomically to `progress.json`. Supported fields are generation or iteration,
completed and total evaluations, best objective, elapsed time, optional reliable ETA, and a bounded
summary. Publish the final export to `result.json`. Logs roll in place at 5 MiB; `job_status` returns
only the latest 4,000 characters.

Use `write_optimizer_checkpoint_atomic()` for `checkpoint.json`. Schema version 1 requires the
optimizer version, iteration, population or optimizer state, RNG state, seed, best objective, best
parameters, and evaluation count. A restart marks a missing worker interrupted. Resume is always an
explicit new `job_start` with a new idempotency key and the prior `resume_job_id`; the service never
duplicates an expensive run automatically.

Closing or refreshing the Agent page, finishing one provider turn, or reaching a Web Agent final
response does not stop the worker. Normal idle sleep is inhibited while it runs on macOS. The
project does not currently inhibit Windows idle sleep for the equivalent task. Closing the lid,
explicit Sleep, logout, reboot, power loss, service-host failure, or hardware failure can still
interrupt it, so the optimizer must checkpoint frequently enough for the workload.

## Local data

| Location | Contents |
| --- | --- |
| `local_store/x/` | X media and cache-catalog state |
| `local_store/media/grok/` | Grok media, catalog, manifest, and work queue |
| `local_store/media/chatgpt/<project-name>/` | ChatGPT images and catalog state |
| `local_store/llm/chatgpt/history.sqlite3` | ChatGPT typed text history |
| `local_store/llm/gemini/history.sqlite3` | Gemini typed text history |
| `local_store/llm/grok/history.sqlite3` | Grok typed text history |
| `local_store/llm/claude/history.sqlite3` | Claude typed text history |
| `local_store/llm/claude_code/history.sqlite3` | Local Claude Code text history |
| `local_store/llm/codex/history.sqlite3` | Local Codex text history |
| `local_store/llm/grok_build/history.sqlite3` | Local Grok Build text history |
| `local_store/llm/copilot/history.sqlite3` | Microsoft Copilot text history |
| `local_store/llm/<provider>_openclaw/history.sqlite3` | Provider-attributed preserved OpenClaw text |
| `local_store/llm/.cache_catalog.sqlite3` | LLM-root resource catalog; independent of non-LLM media catalogs |
| `local_store/llm/zhihu/history.sqlite3` | Formal Zhihu answer text, rich-text source, and source links |
| `local_store/prompt/prompts.parquet` | Saved prompt content snapshots and source pointers; prompts remain available if source history disappears. A prompt saved from a media card whose conversation has no cached text carries a `media-prompt:` content key instead of a message pointer. Removing a saved prompt deletes its row and remarks; only the running process can restore it, until it exits |
| `local_store/agent/agent_source_catalog.parquet` | Provider-neutral Agent session and Project discovery cache |
| `local_store/.cache_task.lock` | Gate that a starting cache task passes and a shadow backup or history repair holds |
| `local_store/.cache_task.<resource>.lock` | Advisory lock for one running cache task, exclusive browser, or shared store |
| `local_store/.browser-trash/` | Recoverable previews moved by the local-media browser |
| `local_store/.browser_deleted.json` | Browser deletion tombstones and exclusion identities |
| `logs/cachelikes.log.jsonl` | Structured local application log |
| Platform-native agenticContext settings path (`~/Library/Application Support/agenticContext/...` on macOS; `%APPDATA%\agenticContext\...` on Windows) | Device-local saved settings |

All cache and log paths are ignored by Git. Back up local media before using any destructive reset
operation.

### LLM history migration and rollback

Each provider now writes `local_store/llm/<source>/history.sqlite3`. Existing
Parquet sources remain readable only when the corresponding SQLite store is absent.
Migration is explicit; application reads do not create a database or retire a source.
New SQLite writes use storage format version 3: 32 stable SHA-256 conversation
buckets with Zstandard-compressed Arrow IPC payloads, explicit original row ordinals,
and the exact Arrow schema. Version 2 JSON payloads remain readable. Compressed JSON
summary version 2 contains metadata only; public reader and writer APIs are unchanged.
Finish Cache jobs before migration, use the canonical Terminal for any necessary
runtime shutdown, and leave unrelated browser and Terminal windows intact.

On macOS, preview the eligible canonical stores and retain the private report:

```bash
python3 scripts/migrate_llm_sqlite.py --report /private/var/tmp/llm-sqlite-review/report.json
```

After reviewing scope, apply with a new private rollback directory on durable storage:

```bash
python3 scripts/migrate_llm_sqlite.py \
  --report /private/var/tmp/llm-sqlite-review/applied.json \
  --backup-directory /path/to/private/llm-sqlite-rollback \
  --apply --retire-legacy
```

Use `py -3` with host-appropriate paths on Windows. The migration holds the cache
maintenance gate and checks open handles. It verifies exact schema, every row,
duplicate, original order, SQLite integrity, and source stability before publication.
It preserves source modification times for discovery overlays. Before retiring each
canonical Parquet file, it writes a gzip rollback and verifies the decompressed
bytes against the original SHA-256. Existing conflicting SQLite targets are not
overwritten. Numbered histories, imported evidence, media, other catalogs, and
browser profiles are outside its retirement scope. Keep failed or partial reports
and rollback receipts; resolve the named conflict before an explicit retry.

The private report separates initial and resident source allocation, logical sizes,
SQLite allocation, and rollback allocation. Cloud placeholders can initially have
zero allocated bytes; reading them can hydrate content. A smaller logical SQLite
archive does not prove disk space was reclaimed, particularly while backups or
numbered histories remain. Benchmarks use warm filesystem caches and disposable
write copies; report their scope and sample distributions instead of treating them
as cold-start or universal throughput results. Keep account-specific measurements
in the private report rather than repository documentation.

For rollback, finish all writers and preserve the current SQLite database first.
Decompress the receipt's gzip file to a new private candidate, verify its exact
restored hash and bytes, and validate it through the history reader in an isolated
root. Restore only to the receipt's explicit provider path after preserving the
SQLite target elsewhere; an existing SQLite file otherwise takes precedence over
Parquet. Never restore over an active database or infer that an unverified cloud copy
is a durable backup. Retain the report and reversible path mapping until rollback
and resumed application reads have been verified.

### Zhihu text cache

Open `GET /cache/zhihu`. Edge is selected by default; Chrome is the only alternative. The account
card calls the ordinary browser-session probe and enables Start only after
`https://www.zhihu.com/api/v4/me` identifies a signed-in account.

Leave `Answerer URL` blank to cache every unique answer found while paging that account's
`MEMBER_VOTEUP_ANSWER` activity. Enter a validated Zhihu people or Answers URL to cache every
answer exposed by that answerer's paginated API instead. Both modes recheck the newest page before
an atomic cumulative merge into `local_store/llm/zhihu/history.sqlite3`; a later run never removes
answers collected by an earlier mode. An activity target whose body is unavailable is retained as
an excerpt-plus-source-link record instead of aborting the complete liked-answer run. Review the
answers under Local resources with source `Zhihu`; the source-specific sidebar removes Clear
filters and adds a standard Answerer select populated from cached names. Its selection scopes the
metrics, sessions, search, ordering, and pagination. The list shows the literal answerer, omits its
redundant Source column and answer ID, and removes the inapplicable Projects metric. Detail tables
use the literal answerer name instead of Role and render the complete stored text without the
generic message-collapse limit. Original answer, question, profile, embedded anchor, and remote
image URLs remain explicit source links. The collector selects the canonical answer-body node,
preserves paragraphs, lists, headings, emphasis, links, rules, figures, and captions, and writes the
same structure as portable Markdown. Page controls are excluded. Rich text is normalized again and
sanitized at the final render boundary; duplicate fallback and lazy-loader copies become one local
`photo.badge.arrow.down.svg` placeholder per figure. The default `Export Markdown` action downloads
every cached answer for the selected answerer across all Local resources pages. No provider image URL
is mounted and no remote image binary is downloaded.

An answerer's Local resources detail page adds `Update latest answers from Zhihu` to its hover or
focus action drawer. It posts the cached profile link to `POST /api/browser/zhihu/answerer/refresh`,
which validates it as a Zhihu people URL and starts the ordinary Zhihu worker in newest-answer mode
with the saved Edge or Chrome selection, using the same signed-in browser clone as the Cache page.
Zhihu lists an answerer's answers newest first, so the worker reads pages until one whole page is
already cached, merges what it read cumulatively, and stops. It does not re-enumerate older answers or
verify a provider total: an answerer whose cache is partial (for example, from vote-up mode) keeps its
older gaps, and edits to older answers are not fetched, until a complete `Answerer URL` run on the
Cache page. That complete run reads the whole list and, when Zhihu's list has duplicate-backed gaps,
enumerates it twice, so a long answerer can take many minutes. The page polls
`GET /api/zhihu/status`, then reloads with a dismissible result banner naming the answers added and
already cached among the newest answers checked. Zhihu serves an answer's images from rotating
hosts, so an answer whose text is unchanged can still be rewritten; the banner counts those as
refreshed and never as new. The action is disabled when the cached
answers carry no profile link, is refused rather than queued while the Zhihu task or its
exclusive browser is busy, and is unavailable when external operations are disabled. Failures such as human verification are shown once and leave the
cache unchanged.

## Concurrency and local compute

Cache tasks run together. Each source and content mode is one task, so X Media and ChatGPT
Text, or the Text and Media modes of one source, can be active at once. A task that needs a
browser or store another task holds is accepted and waits in the queue, then starts by itself:

- Safari runs one cache task at a time. On Windows, Edge and Chrome do too. On macOS, Edge and
  Chrome tasks each clone the selected profile and run together.
- An X Text task and a Safari X Media task both write the liked-text history and take turns.
- Stop removes a queued task from the queue and asks a running task to stop after its current
  work item.
- The task entry in the lower right of every page lists running and queued tasks.
  `GET /api/cache/activity` returns the same list without scanning a cache or opening a browser.
- A manual shadow backup starts only when no cache task is active. With automatic backup
  enabled, the copy runs after a task when it is the only one, and otherwise once the last
  task finishes; a task started during the copy waits for it.
- The queue is held in memory. Restarting the console discards queued tasks.

The shared `Download workers` setting accepts values from `1` through `8`. Older settings files
remain readable; values below or above that range are normalized on load, and direct
`CrawlConfig` construction applies the same limit. Grok remains capped at four download workers
and ChatGPT at three isolated Chromium workers. Safari still owns one task window at a time.
Inside that window, ChatGPT direct project-index downloads use as many tabs as the setting allows,
up to three; set `Download workers` to `1` to keep them on a single tab. ChatGPT session scans
and prompt backfill, and every other Safari collector, stay on one tab.

ChatGPT visual-signature hydration is a separate local CPU stage. Small batches stay in the parent
process, while larger batches may use the automatically discovered conservative process budget.
The payload budget is enforced before reads; oversized files are decoded directly by the parent
instead of being queued or sent to a process worker.
The status snapshot exposes only numeric stage counts, durations, worker counts, queue depth, backend
name, and fallback counts. It never records image bytes, prompts, browser data, cookies, URLs, or
file paths. The base installation reports GPU unavailable because no optional GPU framework or
adapter is installed.

For a repeatable isolated measurement, run:

```bash
python3 scripts/benchmark_compute.py
```

On Windows:

```powershell
py -3 scripts/benchmark_compute.py
```

The benchmark creates a temporary synthetic fixture, performs one warmup and five measured runs,
and reports distributions for the legacy sequential path and the bounded CPU backend. It does not
read the local cache or launch a browser.

## Reset, deletion, and restoration

The local-media browser's Delete action moves a supported media file into recoverable browser trash
and records a tombstone so the same source resource is not immediately re-downloaded. Restore moves
the retained preview back to its original cache path.

The Grok and ChatGPT reset actions are more destructive: they remove their cached media and source
state for a complete future resync. Run them only while the corresponding task is idle and only when
you intend to discard that cache. Do not use reset operations as a routine troubleshooting step.

## Troubleshooting

- Missing Playwright Chromium: on macOS run `./scripts/setup_python.sh`, or run
  `python3 -m playwright install chromium` with the same Python 3.13 or newer interpreter
  used by the application. On Windows run `.\scripts\setup_python.ps1` or `py -3 -m playwright install chromium`.
- Missing downloader: install the project requirements so `yt-dlp` is available to the selected
  supported interpreter.
- Browser profile lock: on Windows a running Edge or Chrome locks `Network/Cookies`, so the
  Agent falls back to the project-owned debug browser over CDP and the first sign-in on that
  debug profile is the only manual step. On macOS, close duplicate normal browser windows, then
  retry the session probe.
- macOS Edge Jury setup: choose `Check accounts` once to open the owner-only project Profile, sign
  in to each selected provider tab, then choose `Check accounts` again. Do not close or alter the
  daily Edge Profile, do not approve or reset Keychain entries as a troubleshooting step, and do
  not add `--use-mock-keychain`, `--password-store=basic`, or encryption-disabling flags. If a fresh
  project Profile still produces a OneAuth prompt, stop and investigate Edge/OneAuth or Keychain
  ACL behavior; do not hide or automate the prompt.
- ChatGPT parallel sync: Cache Download workers retain their clone-first path and can use up to
  three isolated Edge contexts. If Windows profile copying reaches the existing Cookie-lock CDP
  fallback, that fallback is serialized through the single-owner project debug browser. Lower the
  shared Download workers setting only when the machine cannot sustain the browser load.
- ChatGPT Text history schema 3 keeps visible user prompts and completed final assistant replies.
  Tool recipients, non-final channels, hidden context, reasoning payloads, and incomplete replies
  are excluded using provider metadata, not wording or JSON detection. Ordinary legacy replies
  without channel/completion metadata remain supported. Older cached sessions are fetched again
  on the next Text sync even when their provider revision matches; each successful fetch replaces
  that session's rows while preserving retained messages' first-seen timestamps. Failed fetches
  leave existing rows intact. Let an active cache task finish before restarting into the new code
  and running Text sync; do not edit its live SQLite database or reset cached media to apply this change.
- Legacy ChatGPT tool traces can be reviewed offline with
  `python3 -m app.core.chatgpt_history_cleanup` (use `py -3 -m` on Windows).
  Add `--apply` to remove recognized traces preceding a later reply in the same turn.
  This includes multiline `fast|`, `slow|`, `open|`, and `find|` command batches with
  an optional trailing `length|short`, `length|medium`, or `length|long` option.
  Every line must match the protocol; mixed prose and fenced examples are retained.
  Ordinary prose is retained unless explicitly reviewed with `--remove-message chat-<id>`.
  The command holds the cache gate and refuses to run while any cache task is active. It backs up
  the original history file with its actual extension and a removal manifest
  under `local_store/recovery/`, and atomically writes the cleaned history. It preserves user
  messages, retained content, timestamps, and session IDs. Do not restore the backup over an
  active cache job. Channel-less future messages also reject standalone tool syntax; explicit
  final-channel JSON or code answers remain supported.
- Sync failure: inspect `logs/cachelikes.log.jsonl` for full structured diagnostics. The UI shows a
  bounded status message while retaining the detailed local log.
- Grok Text cache: the legacy text runtime follows all Grok conversation pages and response
  trees, but `/cache/grok` no longer renders a redundant sidebar action. Review the resulting
  history through Local resources, and see [CACHE_HANDOFF.md](CACHE_HANDOFF.md) for status
  routes, verified counts, and recovery commands.
- Gemini Text cache defaults to Edge on macOS and Windows. Safari is macOS-only; if it is explicitly
  selected there, preserve the saved Safari navigation interval. If Safari reaches `Failed to open
  page`, stop that run, close its single task window, and restart after confirming the window count
  returned to baseline. Never accelerate the run by reducing the saved interval and never open
  parallel Safari task windows.
- Gemini Text on Edge: use the headless Chromium history-RPC path. It follows the authenticated
  `MaZiqc` cursor, stores a 24-hour discovery checkpoint, and resumes by skipping cached session
  IDs. The current verified run exposed `740` sessions and cached `736` text-bearing sessions
  with `4,045` messages. A no-text session is an expected skip; a Google human-verification
  challenge must stop the task and be reported to the operator.
- Zhihu verification: HTTP 401/403, error code `40352`, `need_login`, or a visible human-check page
  is a terminal result for that run. Keep the existing archive, open the profile in the selected
  host browser, complete the provider's check manually, and explicitly retry. Do not copy cookies,
  add a CAPTCHA solver, switch to a raw HTTP scraper, or loop retries around the challenge.
- Zhihu count mismatch: do not edit the SQLite database or invent missing IDs. A short terminal result
  whose raw count is also below the reported total fails closed. A duplicate-backed provider gap is
  accepted only after two full enumerations reproduce the same ordered unique IDs and pagination
  counts; the gap is then persisted and shown as Not enumerable. Any drift preserves the earlier
  archive until a later explicit run succeeds.
- Cache inconsistency: use the local-media browser to inspect the affected source before choosing a
  source-specific reset. Avoid deleting catalog or manifest files by hand.

## Public errors and security review

HTTP routes return operation-specific public errors and retain documented validation messages
through closed application-owned allowlists. Arbitrary exception text, traceback strings, OS paths,
and provider diagnostics stay in server logs. Status adapters sanitize explicit diagnostic fields
without recursively modifying user prompts, assistant responses, or tool content. The shared boundary
is `app/web/public_errors.py`; adding a public error requires fixed actionable copy and a regression
that injects a private diagnostic into the affected route.

WeChat's native account and conversation directory names require their externally defined MD5
locator format. These locators do not authenticate a user, protect credentials, or provide content
integrity; exact source identities and separate SHA-256 evidence checks remain required. Reviewed
CodeQL false positives retain per-alert rationale and the reviewed commit on GitHub. Intentional
complete-directory selection and fixed same-origin legacy redirects likewise need individual flow
review. This classification never excludes a query, disables scanning, or excuses an arbitrary
exception string in a public response.

## Safe development checks

Run `./scripts/test.sh` or `./scripts/check.sh` (macOS/Linux) or `.\scripts\test.ps1` or
`.\scripts\check.ps1` (Windows) for offline validation. Pytest redirects all
default runtime paths into temporary directories; tests must never be pointed at the production
cache, Beta archive, log, settings, or browser-profile locations.

## Native forwarding, quoted replies, and pat notices

The `wechat_text` parser exposes `PARSER_VERSION` and retains typed source roles rather
than promoting arbitrary readable app fields. Native datatype-17 forwarding accepts
one unambiguous `recordinfo` holder or the observed `recordxml` wrapper containing
`recordinfo`. Inline, escaped, and CDATA documents share the existing cumulative XML
byte/node limits and eight-level forwarding budget. Other readable siblings retain
their original paths, clocks, and speakers when a nested holder cannot be read;
omission counts and partial flags remain explicit.

When a subtype-57 reply quotes another explicit subtype-57 app envelope, the quoted
message's literal `title` is its own body. Native compact copies may omit that
message's own inner reference. The parser retains the outer reference identifiers and the separate
current-reply/quoted-body roles; it does not borrow words from an adjacent record,
another account, or an app/file title without this envelope proof. Literal spacing,
line breaks, Unicode selectors, and embedded source punctuation remain unchanged.

The observed version-2 subtype-62 `patinfo` profile is a system notice. Its display
title stays literal, while actor, conversation, and patted participant usernames
remain separate native source assertions. `is_native_pat_projection()` validates
the closed projection and, when supplied, the exact conversation. Producers parse
this typed profile before applying the ordinary human-sender requirement: a pat's
internal sender row can be empty and differs from its action participant. The raw
sender coordinate is retained, while the projected notice has a neutral author and
unknown direction. Ordinary subtype-49 app messages retain their sender checks;
unsupported profiles and unavailable event identifiers are not fabricated.

An informative projection already stored in the Mac bank must not be overwritten as
an ordinary variant. A reviewed semantic reparse needs the same authenticated raw
body and exact account, peer, native row, message type, server ID, clock, and source
coordinates, with the prior projection and occurrence history retained. Parsing a
new envelope format alone does not authorize replacing an unrelated source event.
