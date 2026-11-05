# Cache handoff and operating runbook

Documentation version: `v1.34.0-codex.0`

This is the authoritative handoff document for the second Dock item, `Cache`.
Read it before changing Cache routes, source switching, Text/Media behavior, local
history persistence, or Grok synchronization. The third Dock item is `Local resources`;
it is the read-only review surface for cached media and text and must not be confused
with the Cache execution surface.

## Current media storage contract

The ChatGPT/Grok media catalogs, Grok download manifest/work queue, and X media
index now use native keyed SQLite tables. Ordinary updates commit dirty records;
hash, identity, and status reads use indexed queries. Queue claims are conditional
transactions, and catalog deduplication commits the replacement and deletion
together. Existing in-memory indexes retain their provider-specific semantics.

Legacy Parquet is read only when the SQLite target is absent and is retained by
ordinary first-write adoption. Do not rename old bytes to `.sqlite3` or point this
backend at an LLM history. Use the explicit migration script and verified rollback
receipt before retiring an old file. See [media SQLite storage](MEDIA_SQLITE_STORAGE.md).

## Current LLM storage contract

Provider text histories use `local_store/llm/<source>/history.sqlite3`, independently
of media, Agent discovery, saved prompts, and the separate WeChat archive. Copilot
coverage and directory evidence also use SQLite. The LLM-root `.cache_catalog.sqlite3`
is distinct from media catalogs outside that root. New callers use
`read_resource_rows` and `write_resource_rows_atomic`; their legacy Parquet-named
aliases remain compatible. Only an absent SQLite target permits a read-only Parquet
fallback. Do not point a provider at another provider's store or relabel database
bytes with a `.parquet` extension.

The backend stores Zstandard-compressed Arrow IPC payloads in 32 stable SHA-256
conversation buckets, retaining explicit original row ordinals, exact Arrow schemas,
and provider provenance. New writes use storage format version 3; version 2 JSON
payloads remain readable through the unchanged public APIs. Primary-key
conversation mappings support scoped reads; derived session summaries support the
overview without loading every body. Compressed JSON summary version 2 contains
metadata only and omits message-body previews. Search, complete exports, and the specialized
Zhihu path still process full history. Writes use a `FULL`-synchronous SQLite
transaction with a `DELETE` journal and update changed buckets. They retain the
provider's existing merge or replacement semantics; reordering global ordinals can
also change buckets whose message text is unchanged.

Use `scripts/migrate_llm_sqlite.py --report <private-report>` for a preview, then
explicit `--apply --retire-legacy` with a reviewed rollback destination. Each retired
source has a gzip backup verified against its original bytes. Preserve reports,
backups, and protected numbered histories. Logical savings, resident allocation,
rollback allocation, and any net disk change must be reported separately; neither
format choice nor cloud placeholder size establishes a measured benefit. See
[Operations](OPERATIONS.md#llm-history-migration-and-rollback) for the migration and
rollback procedure and [Architecture](ARCHITECTURE.md#llm-history-persistence)
for the storage boundaries. Historical acceptance hashes below identify their
original files and remain historical evidence after migration.

## 1. Product boundaries

The Dock order is:

1. Agent
2. Cache
3. Local resources
4. Settings

Cache is the execution surface. Its canonical source pages are:

| Source | Canonical page | Primary payload | Text history |
| --- | --- | --- | --- |
| X | `/cache/x` | Liked-post media | None |
| Grok Chat | `/cache/grok` | Browser Grok media assets | Grok Chat sessions and messages |
| Grok Build | `/cache/grok_build` | Local CLI transcripts | Visible user and assistant text, separate from Grok Chat |
| ChatGPT Chat | `/cache/chatgpt` | ChatGPT images | ChatGPT sessions and messages |
| Gemini | `/cache/gemini` | Rendered Gemini images in Safari | Gemini sessions and messages |
| Claude Chat | `/cache/claude` | Browser chat sessions and messages | Claude Chat sessions and messages |
| Claude Code | `/cache/claude_code` | Local Desktop or CLI transcripts | User and assistant text, separate from Chat |
| Codex | `/cache/codex` | Local app or CLI transcripts | User and assistant text, separate from ChatGPT Chat |
| Microsoft Copilot | `/cache/copilot/text/safari` | Authenticated rendered chat history | User and assistant messages, separate from every other provider |
| Zhihu | `/cache/zhihu` | Upvoted answers or one answerer's answers | One rich-text session per answer plus source links |
| OpenClaw provider archives | `/cache/chatgpt_openclaw` and each `<provider>_openclaw` source | Preserved local transcripts | Provider-attributed text, separate from native browser and CLI histories |

The OpenClaw archive's preservation, attribution, source keys, and uninstall-review
contract are defined in [OpenClaw migration](OPENCLAW_MIGRATION.md). Archive imports
do not require the OpenClaw runtime or a browser.

The OpenClaw archive's preservation, attribution, source keys, and uninstall-review
contract are defined in [OpenClaw migration](OPENCLAW_MIGRATION.md). Archive imports
do not require the OpenClaw runtime. Use the Dock's `Local resources` entry to
review, search, and export cached conversations.

The historical top-level paths `/grok`, `/chatgpt`, `/gemini`, `/claude`, and `/zhihu` are compatibility
redirects. Do not introduce new links to them. The legacy `/chatgpt` plan is not a
separate Agent route; it resolves into the Cache namespace.

`Local resources` owns `/browser`. It is the review and export surface, not the place
where a source sync is started. Its text source filter accepts `all`, `chatgpt`,
`claude`, `claude_code`, `codex`, `copilot`, `gemini`, `grok`, `grok_build`, `wechat`, and
`zhihu`, plus the preserved OpenClaw provider sources.

### WeChat text archives

Open `/browser?view=text&source=wechat` from Local resources. The account selector separates
verified native account IDs from unconfirmed export sources. Private chats, group chats, and
unclassified conversations have separate filters and presentation variants. A spreadsheet's
`Group Name` column alone does not prove that its conversations are groups. Matching sender
names or overlapping messages do not establish the export owner's account.

Import only a previously parsed text archive with `catalog.json`, `compression_manifest.json`,
and per-source `source.json` and `messages.jsonl.gz` files:

```bash
python3 scripts/import_wechat_archive.py /path/to/text-archive \
  --local-store /path/to/private/local_store \
  --report /path/to/private/local_store/messaging/wechat/import-report.json
```

The importer verifies compressed and decoded SHA-256 values and row counts, then atomically
replaces the complete SQLite snapshot. Failed imports retain the previous index. An optional
`--source-kind SOURCE=direct` or `SOURCE=group` requires independent evidence for that source;
otherwise uncertain exports stay unclassified. This is a complete-snapshot import, so retain
all intended sources in the input catalog. Counts remain per source, not deduplicated unique
original-message totals. The source clocks, record locators, and large message IDs remain
available in Source details and text exports.

User-directed source exclusions live in the private sidecar
`local_store/messaging/wechat/exclusions.json`. Its schema is
`{"schema_version": 1, "sources": [{"source_id": "curated-export", "reason": "User request"}]}`.
The importer reads this policy automatically and skips matching source IDs before opening their
metadata or compressed payloads, so excluded cloud inputs need not download again. Retained
message counts plus explicitly excluded catalog row counts must still reconcile with the full
catalog. Invalid policies fail without replacing the previous index. Recording an exclusion
prevents future imports; an already indexed source must also be removed from the current snapshot.
Keep user-specific decisions and removal receipts in private local storage, not tracked code.

The index stays local for offline browsing. Cloud input files can return to online-only after
successful verification; do not remove cloud originals or evict the index while using the page.
No original backup, attachment, media, OCR, or external service is read by passive browsing.
WeChat does not expose a Cache execution page or AI prompt actions. Focused coverage lives in
`tests/test_wechat_archive.py` and `tests/test_wechat_browser_e2e.py`, using synthetic archives
and an isolated application instance.

#### Contact identity and name observations

Every reviewed utterance also carries `messaging.contact`. The optional private
`messaging/wechat/contacts.sqlite3` sidecar stores canonical native usernames separately from
source-account remarks, snapshot-specific nicknames, mutable public aliases, and room display
names. Exact internal usernames share a contact across devices and source accounts; OpenIM
identities additionally require their application realm. A contact represents a platform identity,
not proof that two different accounts belong to one person. Source account partitions remain
independent. Case and original identifier bytes are preserved.

Only explicit native evidence authorizes a global identity. Export fields named `WeChat ID` may
have ambiguous semantics and must not be promoted to stable sender identities by name or syntax.
Database-local numeric keys require their owning lookup table; MD5 account directories and chat
table names are validated location hashes. Known exporter-generated placeholders remain unresolved.
Unknown utterances have separate provisional contact IDs, with their original source identifiers
retained for later review. System identity requires parsed system-message evidence.

Per-message corroboration is an overlay, never a rewrite of the original sender, name, timestamp,
body, or source account. A saved binding is accepted only when its source, message ID, UTF-8 body
SHA-256, exact timestamp string, and original record ID still match. Cross-source clock equality
alone does not establish an export timezone or owner. Name observations retain provenance, and
message times do not become asserted name-validity dates. The existing collapsed source metadata
shows the contact ID and identity status; Markdown export retains these references and the original
source display name. Missing or invalid sidecars leave text browsing available with provisional
objects. Complete historical name observations remain accessible through the paginated repository
API even when the current page's profile list is capped.

Contact regression coverage lives in `tests/test_wechat_contacts.py`,
`tests/test_wechat_contact_browser.py`, and `tests/test_wechat_contact_template.py`. All fixtures
are synthetic; production reconciliation audits and contact databases stay outside Git.

#### Local viewer display names

`wechat_display_names.py` reads private `messaging/wechat/display_names.json` once per
page. Schema version `1` contains a `contacts` object mapping canonical contact IDs to
literal, single-line viewing names. Only verified contacts receive this projection;
phone numbers, source labels, and unresolved export identifiers never establish a match.
The account projection also verifies the owner, account key, and native directory hash.
The MD5 directory check reproduces WeChat's native archive locator with
`usedforsecurity=False`. Canonical contact IDs use SHA-256 separately; the directory
digest supplies archive-format compatibility rather than cryptographic protection.

`viewer_display_name`, `viewer_account_label`, and account-option `viewer_name` are
separate from original source names and identity keys. Cards and Markdown headings can
show the viewing name while raw `author_label`, contact observations, source-account
labels, and export provenance retain their original values. Removing a preference restores
the source name on the next request. Missing, malformed, symbolic-link, or oversized
preferences safely fall back to source names. Preferences stay outside Git and do not
rewrite either the chat index or contact registry.

#### Native text decoding and additive reconciliation

The native helpers are library primitives, not a Finder backup restore tool. Select only textual
message cells and identity metadata; do not open media or attachment contents. The WCDB decoder
accepts plaintext on Python 3.13 or newer. Compressed cells require a runtime with
`compression.zstd`, available in Python 3.14, and the exact application dictionary when required.
Unavailable dictionaries, invalid frames, and unsupported content must remain deferred with
original database bytes, raw cells, locators, and reasons retained by the extraction caller.
Readable-text counts do not prove that every native row has decoded.

The bounded text parser recognizes explicit text types and selected app, forwarded, quoted, and
system envelopes. It never follows URLs or opens embedded resources. Preserve embedded author
evidence rather than treating a forwarded statement as the forwarding person's original speech.
Native identity parsing keeps stable usernames separate from mutable display names.

Use `reconcile_wechat_records` for an additive merge into an existing index; the archive-import
command above still replaces a complete snapshot. Reconciliation verifies account and conversation
hashes and honors source exclusions. It preserves existing message and partition evidence, stores
snapshot provenance, and publishes atomically only after integrity and preservation checks.
Matching requires a nonzero server ID in the same verified account and conversation, equal text,
and compatible known sender, direction, and comparable time. Missing IDs do not authorize fuzzy
deduplication. Conflicts remain separate variants, while exact replay is idempotent. Keep unknown
owners and clocks unresolved instead of inferring them from names or overlapping text.

Retain and verify source evidence separately from the searchable index. A text-only archive does
not preserve an iPhone's complete state or guarantee Finder restoration, full decoding, or recovery
of messages absent from the inputs. Backup deletion and cloud eviction require their own verified
retention decision; these helpers perform neither operation. Keep production reconciliation
receipts, account identifiers, and contact databases outside Git.

### Microsoft Copilot browser history

Choose `Microsoft Copilot` and the already signed-in Safari session, then press
`Start`. The text-only source also supports the existing Chrome and Edge transports.
The worker reads the initial catalog from Copilot's `/chat/all` loader and follows
every `GetConversationPageHistoryList` continuation through the authenticated
same-origin `/chat` action. An explicitly empty `syncState` proves exhaustion of
that current catalog. It does not establish coverage of every legacy Copilot or
Bing history surface. Sidebar links and a stationary viewport do not establish
catalog exhaustion.
Each `/chat/conversation/<id>` transcript is read from its rendered messages.
The configured entry URL's query is preserved throughout navigation and pagination
because it can affect Microsoft's merged-history eligibility. The worker sends no
prompt and does not change sign-in.
Safari participates in the shared Cache queue and owns only its temporary task window.

Complete sessions are atomically persisted to
`local_store/llm/copilot/history.sqlite3`. Stable provider message identities preserve
repeat-import idempotence, and unreadable, empty, interrupted, or shorter captures
preserve the prior session. Missing provider timestamps remain unknown; local capture
metadata is not presented as a message date. Catalog and transcript completion are
persisted separately in `llm/copilot/sync-state.sqlite3`. Legacy caches without this
evidence remain unverified after reload. The UI shows `Partial` without a full-history
percentage until discovery is verified; it shows `Scanning` during discovery.
After discovery, failed or stopped synchronization remains `Partial` even when
every session has been attempted. Only a verified successful finish can show 100%.
Reload and status reconciliation compare the completion manifest with the durable
session count; missing or inconsistent history cannot retain a completion claim.
Repeated cursors, stalled pagination, unsupported responses, omitted previously cached
sessions, and bounded traversal report incomplete collection and preserve the archive.
Success requires verified current-catalog exhaustion and every discovered transcript
to be processed without failure. A known page count is not an independently reported
cross-product account total. Media binaries and non-chat Copilot tasks are outside
this text source.

Local resources exposes the `Microsoft Copilot` filter, session search, saved prompts,
original conversation links, and Markdown export using the existing history surface.

#### Live acceptance and unresolved legacy coverage, 21 Oct 2026

Application v1.43.1 is the current acceptance baseline. After the user confirmed that
the interrupted Safari task window had been closed, its ownership journal was backed
up. Recovery held the global Safari lock, verified that the recorded owner process
was dead, and cleared the stale journal. A normal real `Start` then succeeded; the
former window-ownership failure is resolved.

The authenticated current catalog traversed two pages to an empty `syncState` and
contained 17 unique sessions. Two sessions were read, exposing four messages, before
the operator deliberately requested `Stop` to avoid presenting this as a completed
backup of the user's reported hundreds of sessions. The durable archive remains
17 sessions and 74 messages, with SHA-256
`f352eb310dfc8fc0a4edd17b6a2bf0e67e831821f8f0b643940cda00ec83fb6e`.
This acceptance proves the current catalog and normal worker lifecycle; legacy
coverage and a real complete transcript run remain unresolved.

The user's Microsoft Privacy Dashboard downloads were preserved byte-for-byte under
`local_store/recovery/copilot-official-exports-20261021T052937Z`, with hashes and row
counts in `manifest.json`. `copilot-activity-history.csv` contains 74 data rows and
16 distinct `Conversation` labels. Those labels are titles, including reused titles,
and must not be treated as stable session IDs. The Windows-apps export contains zero
data rows. These exports provide no additional evidence for hundreds of sessions.

The user subsequently located older records in the existing macOS Edge Copilot
sidebar, rather than the ordinary webpage. Native accessibility inspection identified
an inner document at `copilot.microsoft.com/chats/<short-id>` and 357 visible
conversation links under `Our conversations together`. The raw directory was
preserved at
`local_store/recovery/edge-copilot-sidebar-20261021T054515Z/visible-history-catalog.json`
(42,468 bytes; SHA-256
`7eed63e2de8f6230c5fc638a52fd656568106d33e3cec47ff54bec07f1f51d3f`).
Its `discovery_complete` value remains `false`: 357 is the observed directory size,
not a verified full-history total. Entries contain titles, ordinals, and the initial
accessibility indexes; those indexes are temporary UI references, not provider
conversation identities. Raising the native Edge window allowed the first entry's
two-message transcript to load. Its assistant answer was preserved through the
native `Copy message` control as `recent-assistant-original.md` in the same recovery
directory (879 characters, 1,577 bytes; SHA-256
`91810286b6d6d6f73367d04aea1c47ade16ed940fa24c6dadfb7c6e5e876dbbc`).
Entries 18, 200, and 357 changed the legacy conversation path but exposed only the
composer after settling, with no transcript or error; their scoped accessibility
snapshots are retained. This observation does not establish deletion. The directory
backup preserves titles, not 357 transcripts.
The legacy source's body extraction, catalog exhaustion, and full available-history
cache remain unfinished. The 17-session current-web catalog cannot represent this
separately observed sidebar history.

A bounded, read-only investigation of Edge's ordinary HTTP disk cache recovered
19 exact native catalog responses from `/c/api/conversations`. The raw gzip and
decoded JSON, source checks, and SHA-256 manifests are preserved under
`local_store/recovery/edge-copilot-http-cache-20261021T061142Z`. Their 377 physical
records contain 357 distinct native IDs; repeated IDs have identical records.
`merged-legacy-catalog.v1.json` retains original observations. Two current AX titles
have no exact match in this cached catalog, so no fuzzy identity reconciliation is
performed. Cached pagination order remains unproven.
The historical validated typed directory was preserved as
`local_store/llm/copilot/legacy-catalog.parquet` (SHA-256
`c934dc7297ff2859bdbd16afd4abe5f8da2760df72563833c6c2b8bab073241f`).
That hash identifies the original Parquet bytes; after migration, the canonical directory is
`local_store/llm/copilot/legacy-catalog.sqlite3` and the rollback receipt identifies the preserved original.
It is separate from message history. Application v1.43.2 retains Partial on sync
and reload whenever this legacy evidence remains unverified, while still caching
available current-web messages. Invalid directory evidence cannot permit a full
completion claim. The message archive hash is unchanged.
One retained native history response has two messages for entry 1. Eleven other
HTTP 200 responses have empty results, including exact native IDs for sampled
entries 18, 200, and 357. Their original bytes are preserved as negative observations,
not proof of deletion. No additional older message body has been recovered.

Final v1.43.2 verification passed the Copilot modules and web-app regression suite,
the legacy catalog's final 17 focused checks, Ruff, and scoped whitespace checks.
The complete gate remains blocked before its test stages by the existing
`streamlit 1.46.1` requirement `pillow<12` versus installed `pillow 12.3.0`; a fresh
dependency check reproduced that conflict. Live API and rendered UI readback show
357 discovered directory entries, 17 cached sessions, 74 cached messages, and Partial.
The Copilot message archive and all eight other provider history hashes stayed
unchanged. Final numbered-copy scans retained all 542 protected `local_store`/`logs`
files and found no candidates outside those boundaries; the shared-docs root had
none. The temporary localhost clipboard receiver and its agent-created tab were
closed. The existing Edge sidebar and signed-in official privacy page were preserved.

Microsoft's [Bing release notes from 19 May 2023](https://blogs.bing.com/search/2023/5/Bing-Preview-Release-Notes-Chat-History,-Charts,-Export,-and-more/)
documented per-conversation export controls and answer export to PDF, text, or Word.
The current per-conversation menu has no export command. The actual sidebar path
`Settings > Privacy > Export or delete history` opens Microsoft's privacy dashboard
at `https://account.microsoft.com/privacy/copilot`. After the user signed in, its
`Copilot apps > Export all activity history` downloaded a CSV with the same 74 rows,
96,622 bytes, and SHA-256 as the previously preserved export. Its title values match
only sidebar entries 1 through 17; entries 10 and 14 reuse one title, so title matching
does not prove session identity. No title from entries 18 through 357 occurs in the
CSV. Thus the verified bulk export does not preserve those 340 older transcripts.
Do not equate the ordinary
tab's save or print action with preserving sidebar message bodies. Preserve the
existing signed-in sidebar while inspecting its available controls.
Microsoft's [migration guidance](https://support.microsoft.com/en-us/microsoft-365-copilot/troubleshoot-content-copilot-app)
documents possible migration delays and in-app links to older content; it does not
diagnose this account or establish that its missing records were deleted.

The transport follow-up reuses the first-party context from merged route
`store` values and successful query `storePatch` values: `eligibility`, `hostContext`,
and `sessionID`. It aligns `X-Client-Eligibility` and `x-host-context` with the observed
frontend request, without reading authentication tokens or cookies. Fresh real
acceptance of that context/header addition is still required.

### Claude Code local import

Select `Claude Code` in the Cache source menu, choose `Local session folder`, and
press `Read local sessions`. The folder is also saved under Settings > LLM settings.
This text-only source needs no browser authorization and does not acquire a browser
lease. A remembered Media selection cannot change its import or status mode.

On macOS, an installed Desktop Code registry makes
`~/Library/Application Support/Claude` the default. Its `claude-code-sessions`
metadata maps explicit `cliSessionId` values to matching main transcripts under
`~/.claude/projects`. Choosing `~/.claude`, its `projects` folder, or one project
folder instead reads the main transcripts within that scope. The desktop registry
is not a web Chat export. Missing mapped transcripts and malformed records are
reported as incomplete; existing cached messages are retained.

The importer stores visible user and assistant text, timestamps, model labels,
and titles in `local_store/llm/claude_code/history.sqlite3`. It excludes tool
payloads, internal reasoning, subagents, and metadata-only messages. Source files
are never modified. Stable message identities support repeat imports without
duplicates, and an unchanged import leaves the cache file unchanged.

Local resources labels the legacy `claude` source `Claude Chat` and the new
`claude_code` source `Claude Code`. The latter supports sessions, search, saved
prompts, and Markdown export, with no fabricated web conversation link. Code
does not appear among Media sources.

### Codex local import

Select `Codex` in the Cache source menu and press `Read local sessions`.
`Local session folder` defaults to `CODEX_HOME` when configured, otherwise
`~/.codex`, and is also saved under Settings > LLM settings. Choosing the Codex
home reads its `sessions` and `archived_sessions` transcript trees; a selected
session tree narrows the scope. The source is text-only and acquires no browser
lease. ChatGPT Chat retains its existing browser Text and Media routes.

The importer reads explicit user and assistant message records, keeping session
identity, timestamps, model metadata, and source-file provenance. It excludes
system and developer instructions, tool payloads, internal reasoning, duplicate
event notifications, and subagent transcripts. Source files are read only.
Cached history is cumulative and independently stored in
`local_store/llm/codex/history.sqlite3`; repeat imports retain stable message
identities and leave unchanged cache bytes and modification time intact.
Malformed transcripts report an incomplete import while retaining prior data.

Local resources supports Codex session browsing, search, saved prompts, and
Markdown export. Local sessions have no fabricated web conversation links or
remote refresh action. The cached-page reload remains available. Codex does not
appear among Media sources.

### Grok Build local import

Select `Grok Build` in the Cache source menu, choose `Local session folder`, and
press `Read local sessions`. The folder defaults to `GROK_HOME` when configured,
otherwise `~/.grok`, and is also saved under Settings > LLM settings. A selected
sessions, workspace, or individual session folder narrows the import scope. The
source is text-only and acquires no browser lease. Grok Chat retains its browser
Text and Media routes.

The importer reads visible user and assistant messages from the native local
session records and uses session metadata for titles, model labels, and times.
It excludes internal reasoning, tool payloads, system context, and subagent
sessions. Source files stay read only. The independent typed cache is
`local_store/llm/grok_build/history.sqlite3`; repeat imports preserve stable
message identities, and partial or malformed inputs cannot delete cached history.

Local resources exposes Grok Chat and Grok Build separately for session browsing,
search, saved prompts, and Markdown export. Local transcripts have no fabricated
web conversation link. The Dock remembers both `/cache/grok_build` and the local
`source=grok_build` filter across visits to Agent and Settings.

## 2. Shared Text/Media contract

The Cache pages for ChatGPT, Grok, Gemini, and Claude render the shared blue sliding control at
the top of the Cache sidebar:

- Text is the first segment and the default for a new session.
- Media is the second segment and returns to the current canonical Cache page.
- The selected segment is remembered in `sessionStorage` under
  `cachelikes:browser-content-mode:v1`.
- Both segments stay on the selected `/cache/<source>` page. Text starts history
  synchronization for ChatGPT, Claude, Gemini, or Grok. ChatGPT Text remains
  independent of the selected Media project.
- The Cache source selector always stays in the Cache dock and opens the selected source's
  canonical `/cache/<source>` page. Local resources remains the read-only history
  viewer; selecting Text in Cache does not navigate there.
- Start, Stop, metrics, and status polling use the same explicit content mode.
  Grok Text displays session/message counts from its history worker and SQLite database,
  independently of Media asset counters. Responses for a previously selected mode
  are discarded after a mode change.
- The `all` source view is an aggregate view. It must include the ChatGPT, Gemini, Grok Chat, Grok Build,
  Claude Chat, Claude Code, Codex, Microsoft Copilot, and Zhihu history files.

The control is shared markup in `app/web/templates/_cache_page.html`, behavior in
`app/web/static/cache-page.js`, and styling in `app/web/static/style.css`. Changes to
this component also require reading `../SHARED_UI_SYNC.md` and
updating its `Cache-page Text/Media control` row. This repository is currently the
leading implementation and the sibling remains `Pending`.

## 3. Runtime split: media versus text

Local resources ChatGPT Media offers `All media` as the third view in `All sessions`.
The `media_view=all` query requires the ChatGPT Media session index and expands its
session covers into individual resources, using ordinary 24-resource pages. Actual
file sizes rule out impossible duplicates before bounded readers compute SHA-256 for
remaining candidates. Verified exact identities deduplicate across the filtered result
before pagination; similar images remain distinct. Hashes are cached by file stat
identity. Unreadable or unresolved resources keep separate entries, so a blocked
retained copy cannot hold the page open. Available files take precedence over matching
deletion tombstones. The mode reads files without modifying media, catalogs, or history.
List and Grid return to session covers; filters, pagination, and the Local resources
Dock preserve the explicit mode.

ChatGPT project Media follows the first-party gallery pagination contract: the
initial recent-image request uses `limit=25`, and each `after` cursor request uses
`limit=20`. A later failed or malformed page retains its verified candidates for
normal image processing. The run remains incomplete with `discovery_complete=False`;
it never treats that prefix as the complete image index or silently launches a full
legacy session scan. Optional prompt backfill is deferred until index discovery
finishes, so a second browser startup cannot obscure a partial run's image progress.
Existing images are reused by file ID. A later `Start` refreshes
discovery from the beginning; no pagination cursor or authentication header is persisted.

Safari accepts ChatGPT's observed same-project landing redirect between the bare
`/g/g-p-<32-hex-id>[-slug]` path and its `/project` path. This exception requires the
same complete project component and the same canonical HTTPS authority; different
projects, conversation paths, credentials, ports, and foreign hosts remain outside
the alias rule. It prevents a ready project landing page from causing a false
startup timeout during optional prompt backfill.

On 22 Oct 2026, a real Safari Media run under v1.43.4 completed discovery of 1,636
project sessions and 2,493 image candidates, processed every candidate, and reported
zero media or task failures. It reported 104 successful file registrations; normal
visual deduplication left 103 additional catalog entries, for 2,342 cached images
compared with 2,239 before the run. Every prior catalog entry and its content hash
was preserved. Prompt backfill honored its bounded time budget and deferred the remaining
metadata without losing image work. The later v1.43.5 landing-redirect fix has its
own isolated navigation checks; it does not require another full media download.

ChatGPT Text writes `local_store/llm/chatgpt/history.sqlite3`, independent of the
Media project directory. Schema v2 adds nullable `provider_revision` metadata.
Discovery captures `update_time`; only an equal non-empty persisted revision with
complete source timestamps permits skipping. Missing/changed revisions trigger a
fresh mapping request. Older schema files remain readable and refresh conservatively.
Text status (`?content_mode=text`) hydrates sessions/messages from this file without
opening the media catalog. Failed or rate-limited mapping fetches and empty discovery results are reported
as incomplete instead of a complete successful synchronization. Existing files in the former
`local_store/media/llm/chatgpt` location are preserved; no automatic migration occurs.
After a cooperative stop, unattempted sessions are pending rather than failed.

A recovery can install `local_store/llm/chatgpt/history-preservation.json` with
`{"version":1,"mode":"preserve-existing"}`. While present, normal Text sync preserves
all existing fields, appends missing messages, and stores changed bodies under a
deterministic `:recovered:online:<hash>` key. It also skips replacement of existing
titles. Invalid or unreadable guard data blocks writes rather than falling back to
replacement. Keep this guard with the recovered archive; removing it restores the
ordinary per-conversation replacement behavior. Recovery source/branch provenance and
raw provider/export mappings remain in the recovery manifest, not in message bodies.
Completed outbound voice transcripts with both typed voice metadata flags and only
`audio_transcription` parts directed `out` may be retained despite `end_turn=false`.
The ordinary hidden/channel/recipient restrictions still apply. Such nodes can be
earlier stages of one reply; separate node counts do not establish separate turns.
Official exports lacking these provider fields retain unverified visibility in the
recovery provenance rather than acquiring a claim of final-response status.
The failure counter includes only attempted mapping requests that failed or were
rate-limited; incomplete discovery and deferred work remain incomplete.
ChatGPT throttling (HTTP 429) outlasts the short in-request backoff, so a history-list
page or mapping request that stays rate-limited enters Stop-aware cooldowns of 60, 120,
240, and 480 seconds and retries the same item after each one. After any cooldown, the
remaining list pages or mapping requests of that phase are spaced at least 2 seconds apart,
doubling per throttle episode up to 64 seconds, and never below the configured scan wait.
A September 2026 Safari probe showed a burst of about ten mapping requests followed by
roughly one per minute, with no `Retry-After` header, so a full refresh of thousands of
sessions can span many hours and accumulates across runs. Only a request still
rate-limited after the final cooldown defers the remaining sessions to the next run; Safari
and Chromium page requests share this classification.

ChatGPT's interactive answers embed private-use tokens for citations, entity chips, map and chart
widgets, math, clarifying questions, and links. `app/core/chatgpt_markdown.py` renders the readable
parts as Markdown before the text is stored: entity names stay in sentences or become bold lines,
titled links stay as links when they use plain HTTP(S) addresses, map points become lists, charts
become tables, math becomes a fenced `latex` block, questions keep their options, and widgets whose
shape is not recognized, including known keys with changed inner fields, are preserved as fenced JSON. Citation, memory, image-reference, and placeholder markers are dropped
because they carry no readable text. Citation sources are not stored: the cached text does not
include the search-result URLs that citation markers refer to. Synchronization hashes the rendered
text. The schema version is unchanged, so an unchanged session is not refetched merely to pick up
the new rendering.

Local resources additionally adapts observed ASCII `box/row/text/title/caption`
components in ChatGPT assistant messages at display time. Two-cell rows become
Markdown tables, diagram text becomes labeled text, and captions keep their words.
It never executes JSX, SVG, or stored HTML. Unsupported component structures use
inert fenced source. Empty provider `Cite ref="turn..."` markers are hidden only
outside code; ordinary citations and code examples stay literal. This provider
adapter reuses existing Markdown surfaces without changing shared UI components,
stored message bodies, content hashes, or original Markdown exports. It does not
reconstruct the provider's interactive graphics.

Rows written before this rendering existed are repaired locally by
`python -m app.core.chatgpt_history_cleanup --normalize-markdown`. Without `--apply` the command
only reports the count. With `--apply` it takes the cache maintenance lock, writes
a backup with the original history file's actual extension under
`local_store/recovery/chatgpt-markdown-<timestamp>/` and `normalization.json`, and then
commits the repaired `history.sqlite3` transactionally. Rows are never removed: a message whose only content was a provider token keeps
an empty body, so turn and message indexes and each session's stored revision keep matching the skip logic.

Every source registers one runtime per content mode, each with its own task state, worker,
and Start/Stop. X, ChatGPT, Gemini, and Claude create one service instance per mode; a mode
that has not run in this process reads its counters from the store when its page is
requested. Status without `content_mode` keeps each source's original default: Media for X,
Grok, and ChatGPT, and Text for the others.

Grok's two runtimes are also two implementations. This split is intentional:

| Operation | UI action | Start route | Status route | Implementation |
| --- | --- | --- | --- | --- |
| Grok media | `Start` | `POST /cache/grok/start` | `GET /api/cache/grok/status` | `GrokDownloadService` and `grok_downloader.py` |
| Grok Text | Text, then `Start` | `POST /cache/grok/start` with `cache_content_mode=text` | `GET /api/cache/grok/status?content_mode=text` | `GrokHistoryService` and `grok_history.py` |

Text synchronization must not be implemented by scraping the currently visible Grok
sidebar or by extending the media downloader with unrelated counters. The retained Text
runtime uses the selected Edge profile in an isolated Chromium context, so it can read the
authenticated session without taking over the foreground Edge window. The shared
Stop form also submits `cache_content_mode=text` to stop this worker. The dedicated
`/cache/grok/text/start`, `/cache/grok/text/stop`, and `/api/cache/grok/text/status`
endpoints remain compatible aliases.
Security-verification HTML is reported as an actionable provider error without
retries or raw page markup. Existing history is retained.

Provider region and authentication errors remain blocking; a successful account
probe does not override a later sync failure. Idle Gemini session counts are
hydrated from the durable history file.

The shared media action occupies one right-aligned slot: idle shows `Start`, and a
running task replaces it in place with the existing red `Stop` form button.

The Text worker performs this sequence:

1. Fetch `/rest/app-chat/conversations?pageSize=60&excludeProjects=true`.
2. Follow `nextPageToken` by sending it back as the `pageToken` query parameter.
3. For every conversation, fetch
   `/rest/app-chat/conversations/<conversation-id>/response-node`.
4. Batch all response IDs into `POST
   /rest/app-chat/conversations/<conversation-id>/load-responses`.
5. Keep `human`/`user` and `assistant` responses with non-empty text.
6. Commit the normalized rows transactionally to the Grok Text SQLite database.

The response-node tree is important. A DOM scroll only exposes a partial, selected
branch and is not evidence that all history was discovered. The API list is paginated,
and `load-responses` is required to recover the complete text payload.

For Agent selected-session display, `load-responses` has an additional provider boundary.
Grok can return `cardAttachmentsJson` as an array of JSON-encoded card strings, while the
raw assistant message contains `<grok:render>` elements. The adapter preserves the raw message as
provenance and emits a structured citation only when a
`type="render_inline_citation"` element's `card_id` strictly matches an attachment with
`cardType="citation_card"` and an HTTP(S) URL. It must not infer citation order, accept a partial
identifier match, or turn malformed and non-HTTP(S) attachment data into a link.
When the raw message has one single-line prelude immediately before an exact bold
`「…」Session 更新` heading, the display adapter removes only that prelude to match Grok's native
final DOM; the provenance response remains unchanged, and the rule never crosses a line break.

Display Markdown remains a separate, HTML-disabled parse, and copy text is derived without provider
tags. Recognized citation tokens are replaced with escaped application markup after parsing;
legacy or incomplete cache data uses a safe
unlinked fallback rather than exposing provider tags or inventing a URL. The versioned contract is
part of the Grok Agent `session-history` cache identity. Advancing it invalidates only that source
kind for Grok; recent sessions, Projects, other providers, and
`local_store/llm/grok/history.sqlite3` remain intact. Third-level heading rhythm, citation chips,
and horizontally scroll-contained tables belong to the Agent answer surface and do not redefine a
shared Cache or Local resources style.

Claude history is a separate Cache runtime. It opens the authenticated `/chats` page,
discovers conversation links from rendered DOM, and opens each conversation. It does not
issue its own requests to private Claude endpoints, send prompts, or mutate the provider page.
The conversation transcript is virtualized: it mounts only rows near the viewport and hides
older messages behind a `Load earlier messages` control, so a single DOM read caches only part
of a long session. In Chromium browsers the runtime therefore passively records the conversation
response that the page itself loads (`/api/organizations/<org>/chat_conversations/<id>`) and stores
the displayed branch from it: text blocks as the provider's Markdown, citation URLs as source
links, the conversation model as the model label, and message creation times. Extracted text of
attachments, such as pasted text, is appended to its message after a `---` separator. Thinking,
tool, and injected prompt blocks, and uploaded files other than their extracted text, are not stored. Safari pages expose no response events, and a
Chromium page whose response is not seen within 30 seconds falls back to the rendered transcript:
the runtime presses `Load earlier messages` until it disappears, then scrolls from top to bottom
and merges rows by their transcript index. A session that renders nothing is retried and then
counted as failed instead of being skipped. A Cloudflare or other human verification challenge
stops the whole run immediately without evaluating, reloading, or retrying the page, because
those actions restart the challenge. The automation-controlled Edge window can be challenged on
`/chat/...` pages even when `/chats` loads. The runtime uses `POST /cache/claude/start`,
`POST /cache/claude/stop`, and `GET /api/cache/claude/status` (with compatibility alias
`GET /api/claude/status`).

An official Claude data export (claude.ai Settings > Privacy > Export data) is the
browser-independent backup. Keep its zips under `local_store/llm/claude/exports/<date>/` and import
`conversations-000.zip` with `python -m app.core.claude_export_import <zip>`. Without `--apply` the
command only reports counts. With `--apply` it takes the cache maintenance lock, writes
a backup with the original history file's actual extension under
`local_store/recovery/claude-export-<timestamp>/` and `import.json`, and then
commits `history.sqlite3` transactionally. Each exported session replaces the cached rows of that
session through the same conversion as the recorded payload; the export has no current-leaf
field, so the newest childless message ends the kept branch. Sessions missing from the export are
kept, and messages without text, such as an interrupted reply, are not stored.

Zhihu is a text-only Cache runtime and defaults to Edge. A blank `Answerer URL` first verifies the
selected browser through `GET https://www.zhihu.com/api/v4/me`, then follows the signed-in
account's paginated activity feed and retains only `MEMBER_VOTEUP_ANSWER` targets. An optional
validated `https://www.zhihu.com/people/<token>` URL switches to complete answerer pagination.
Both modes deduplicate by answer ID and recheck the newest page before committing. Rows are merged
cumulatively, so caching another answerer does not remove earlier answers. An answerer's Local
resources detail page starts a newest-answer run of that answerer mode (`latest_only`) through
`POST /api/browser/zhihu/answerer/refresh` with the cached profile URL, then follows
`GET /api/zhihu/status` until the shared runtime finishes. That run reads newest-first pages until one
is entirely cached and never claims completeness; the `Answerer URL` field remains the complete
run. Local resources renders the complete normalized answer text without its generic
message-collapse limit. A Zhihu-only
session list omits the redundant Source column, answer IDs, Projects metric, and Clear filters
action. Its standard Answerer select is populated from the cached author labels and scopes metrics,
sessions, search, ordering, and pagination to the selected name. The index shows each literal
answerer in its Answerer column, while a one-answer detail table uses that name instead of the
generic Role heading. The original answer, question, answerer, embedded anchor, and remote image
URLs are stored in `source_links`. The collector isolates the narrowest supported answer-body node,
stores canonical semantic HTML in `content_html`, and stores a structure-preserving Markdown
translation in `content_text`; reward, editing, voting, and other page controls are excluded. The
Web layer normalizes legacy rich rows again before applying its final sanitizer. Duplicate fallback
and lazy-loader copies inside one figure render as one local `photo.badge.arrow.down.svg`
placeholder, while captions remain visible. `Export Markdown` defaults to every answer in the
selected answerer group, independent of the current 100-row page. No remote media source is mounted
or downloaded.

## 4. Local compute boundary

ChatGPT visual signatures and decoded dimensions are pure local work. Startup catalog hydration
uses bounded immutable payload batches and may use conservative CPU processes only after the batch
is large enough to amortize process startup. The parent checks file size before reading, flushes
before a file would exceed the payload budget, and analyzes an oversized or concurrently changed
file directly in the parent. Workers return ordered analysis envelopes; the ChatGPT catalog remains
the only owner of metadata, duplicate removal, and atomic Parquet writes.
No browser context, authentication header, task lock, mutable catalog, or open file handle enters a
compute worker. The base install has no GPU adapter. Any future optional adapter must discard a
partial or failed GPU batch and recompute the complete batch on CPU before the catalog can observe it.

## 5. Durable local data

The current persistent cache layout is:

| Path | Owner | Meaning |
| --- | --- | --- |
| `local_store/media/grok/` | Grok media runtime | Media files, catalog, manifest, and work queue |
| `local_store/llm/.cache_catalog.sqlite3` | LLM-root resource catalog | Root-specific typed catalog, separate from non-LLM media indexes |
| `local_store/llm/chatgpt/history.sqlite3` | ChatGPT Text runtime | Typed ChatGPT messages |
| `local_store/llm/gemini/history.sqlite3` | Gemini Text runtime | Typed Gemini messages |
| `local_store/llm/grok/history.sqlite3` | Grok Text runtime | Typed Grok messages |
| `local_store/llm/grok_build/history.sqlite3` | Local Grok Build runtime | Typed local Build messages, independently cached |
| `local_store/llm/claude/history.sqlite3` | Claude Text runtime | Typed Claude messages |
| `local_store/llm/claude_code/history.sqlite3` | Local Claude Code runtime | Typed local Code messages, independently cached |
| `local_store/llm/codex/history.sqlite3` | Local Codex runtime | Typed local app and CLI messages, independently cached |
| `local_store/llm/zhihu/history.sqlite3` | Zhihu Text runtime | One answer per typed row; rich-text source and source links retained |
| `local_store/prompt/prompts.parquet` | Prompt manager | Saved prompt content snapshots plus source-message pointers, or a `media-prompt:` content key for a media prompt whose conversation has no cached text |
| `local_store/.cache_task.lock` | Cache runtimes | Gate a starting task passes and a backup or history repair holds |
| `local_store/.cache_task.<resource>.lock` | Cache runtimes | One advisory lock per running task, exclusive browser, or shared store |
| `logs/cachelikes.log.jsonl` | All runtimes | Structured diagnostics |

The formal text-history stores derive their ordered message fields from one schema builder in
`app/core/resource_persistence.py`. ChatGPT adds nullable `provider_revision`; every runtime still
has its own source-specific schema constant and file. Do not silently point one provider at another
provider's file merely because all filenames are `history.sqlite3`.

Grok Text rows contain a stable `message_key` formed as
`<conversation-id>:<response-id>`. The store replaces one conversation at a time,
preserves `first_seen_at`, updates `last_seen_at`, and commits changed storage buckets
in one SQLite transaction. Do not edit the database by hand during a sync.

## 6. Verified Grok baseline

On `13 Aug 2026`, a live Edge-authenticated run completed through the Text pipeline:

- API conversations discovered: `166`
- Non-empty cached sessions: `165`
- Cached messages: `1,537`
- User messages: `808`
- Assistant messages: `729`
- Failed sessions: `0`
- Empty sessions: `1`
- Output: `local_store/llm/grok/history.sqlite3`

The Local resources page was then opened with `source=grok` and showed `1,537`
messages and `165` sessions. This baseline is a diagnostic reference, not a hardcoded
expectation: Grok history changes whenever new conversations are created or removed.

## 7. Safari cache-window contract

Gemini browser refreshes preserve an existing conversation when the rendered snapshot is shorter,
conflicting, or cannot prove a complete matching prefix. Such a session is reported as incomplete;
an equal snapshot is a no-op, and a proven extension appends without changing existing keys.
For browser-independent recovery from an official My Activity export, see the
[Gemini Takeout import guide](GEMINI_TAKEOUT_IMPORT.md). Import apply requires the full maintenance
lock and keeps raw source evidence, backups, and explicitly marked uncertainty.

Gemini Text and Media dispatch independent pipelines through the same cooperative worker.
Media uses a task-owned Safari session to inspect rendered chat images from the observed
`lh3.googleusercontent.com` host, validates complete JPEG/PNG/GIF/WebP bytes, and commits
files and a catalog under `local_store/media/gemini/`. It honors size limits, known-file
hashes, and Local resources deletion exclusions. Videos, external search thumbnails,
blob URLs, and other attachments are outside this image-only mode. Failed images or
sessions produce an incomplete status; policy skips do not. Text keeps its SQLite store
and reports failed sessions as incomplete. Switching modes never displays text-message
counts as downloaded images. Gemini Media is selectable in Local resources.

Conversation timestamps decode the `hNvQHb` RPC envelope separately from the `MaZiqc`
history list. Reverse the complete newest-first RPC turn sequence before matching it to
oldest-first rendered messages, preserving entries with missing timestamps. Bot-check detection
excludes chat content, same-origin conversation navigation links, and hidden challenge elements;
the known `/unavailable` route cannot satisfy readiness. Media discovery uses block comments
because Safari's JavaScript adapter collapses whitespace before execution.

On 29 Sep 2026 Safari access recovered and a live Text run processed 44 of 269 discovered
sessions, leaving 763 cached sessions and 4,277 messages. That run was stopped after its
v1.38.0 timestamp mapping was found to reverse multi-turn chronology. A protected backup was
retained; a verified repair changed only 274 `last_seen_at` values in 25 sessions. Twelve
independent source re-reads exactly matched the correction, and every other message field and
the discovery checkpoint remained unchanged. This is partial live acceptance, not a claim that
all 269 sessions completed.

Source v1.38.1 passes 258 focused checks, including Safari adapter execution, independent
Text/Media dispatch, media file validation, and desktop/mobile browser checks. The last observed
8666 process still reported v1.38.0; final runtime adoption requires a native Terminal restart.
Real image-download acceptance remains pending: the next bounded Safari sample reached Google's
human-verification page before media discovery. Do not infer downloaded image bytes from Text
counts or offline tests. The full quality gate also remains blocked by the host's existing
Streamlit/Pillow dependency conflict; focused success does not make that gate green.

Safari-backed Cache tasks may own exactly one temporary Safari window at a time, and Safari
is never the default browser for a new Gemini task. The window must remain a standard visible
window with its native close, minimize, and full screen controls. It may stay behind the user's
foreground window, but must never be hidden, minimized, moved offscreen, or converted into a
reusable blank window shell. The shared Safari context captures and restores the user's
frontmost application whenever it changes Safari window state. A task may open more tabs inside
its one window, as ChatGPT Media does for direct project-index downloads, but never a second
window. The window is bound by its Safari window ID when it is created, so restoring the user's
front window cannot retarget the task to that window.

This Safari window lifecycle and its AppleScript/`osascript` controller are macOS-only. Windows
uses the PowerShell controller for approved `.ps1` paths and its Windows process-control path;
`taskkill /T /F` provides process-tree cleanup where applicable, not OS-level sandbox isolation.

The lifecycle is strict:

1. Record the existing Safari window count.
2. Create one task-owned standard window without reusing the user's current window.
3. Keep that one window render-active in the background for the task lifetime.
4. If the user closes it, stop with an explicit error; never spawn a replacement window.
5. On success, failure, stop, or exception, close the exact owned window through its
   native close button and verify that its Safari window ID disappeared.
6. The final Safari window count must equal the starting count.

Session probes, debugging helpers, and Cache collectors use this same lifecycle. They must not
embed a second raw `osascript` launch path or rely on a successful AppleScript return as their
only cleanup signal.

Safari admission is bidirectional across Cache and Agent. A Cache start returns `safari_agent_busy`
while a Safari Agent owns the browser; an Agent start returns `safari_cache_busy` while any Safari
Cache worker owns it. Neither workflow queues a second owned window behind the shared Apple Events
lock. Read-only Agent account, source, Project-session, and history probes use a verified cache or
return bounded busy/unprobed state while the Agent owns Safari.

Do not treat an AppleScript `close` return as proof of cleanup. Safari may retain
script-visible empty windows after returning success. A verified ID disappearance is
the cleanup criterion. Never leave `about:blank`, hidden, minimized, offscreen,
Gemini, Grok, or ChatGPT task windows behind after the task exits.

Gemini Text uses the saved `gemini_scroll_pause_seconds` value. Do not lower the live
setting merely to accelerate a manual run. Rapid navigation can put Safari into a
`Failed to open page` state after many sessions; keep the saved 5-second interval on
this Mac. The Gemini bot-check selector is one JavaScript expression: adjacent string
literals must be joined explicitly with `+`, because JavaScript does not concatenate
them like Python.

## 8. Edge Gemini Text contract

Edge is the preferred runtime for a long Gemini Text cache on this Mac. The worker
clones the authenticated Edge `Default` profile into one headless, task-owned context;
it must not open a visible Edge window, send foreground keyboard or mouse events, or
reuse the user's normal profile for writes.

The Angular Recents list is not a complete discovery source: it can render zero links
even while Gemini has history available. Chromium discovery therefore captures the
authenticated `MaZiqc` history RPC and follows its cursor:

1. Request a large page size (`1,000`); Gemini may cap each response at `100` entries.
2. Reuse the authenticated request fields and follow each returned cursor.
3. Stop only when the service closes the cursor, the configured maximum is reached, or
   the user requests a stop.
4. Persist the complete result atomically to
   `local_store/llm/gemini/discovery_checkpoint.json` before processing sessions.

The checkpoint is valid for 24 hours. The production service performs fresh discovery
and refreshes every session on each run. The optional low-level
`skip_cached_conversations=True` mode reuses the checkpoint and skips stored IDs,
but is not enabled by the service: ID existence does not establish freshness.
Do not enable this mode for routine synchronization without a revision-aware contract. A session with no non-empty text nodes is skipped as a
non-text session, not counted as a failed text cache.

On `14 Aug 2026`, the authenticated Edge history exposed `740` sessions through the
cursor. The completed text cache contains `736` text-bearing sessions and `4,045`
messages (`2,114` user and `1,931` assistant), with zero failed sessions, zero empty
messages, and zero duplicate message keys. The four remaining discovered sessions
exposed no cacheable text. These counts describe the current authenticated history,
not a hardcoded product limit.

The worker checks each page for Google human-verification markers. If a challenge is
detected, it stops and reports it to the operator; it must not attempt to solve a
captcha automatically. Transient tunnel and network failures are retried with a
cooldown, and the exact failure remains visible in the task event log.

## 9. Safe operator workflow

Use the local Terminal (macOS) or PowerShell (Windows) as the runtime control surface. The project
requires Python 3.13 or newer, without an upper-version cap. Use the setup, run, and
test wrappers to resolve the host interpreter; `AGENTIC_CONTEXT_PYTHON` selects an explicit executable.

### Before starting a sync

```bash
curl -sS 'http://localhost:8666/api/browser-session?platform=grok&browser=edge'
curl -sS 'http://localhost:8666/api/cache/grok/status'
curl -sS 'http://localhost:8666/api/cache/grok/text/status'
```

The Edge session probe must report `logged_in: true`. A mode that is already running or
queued cannot be started again, but every other source and mode can: see
[Running several cache tasks](#running-several-cache-tasks).

### Running several cache tasks

Each source and content mode is an independent task. Start X Media, then open ChatGPT Text
and start it as well; the Text and Media modes of one source are separate tasks too. What
happens next depends on what the tasks need:

- Different browsers run together. On macOS, two Edge or Chrome tasks also run together,
  because each clones the selected profile.
- Safari runs one cache task at a time. A second Safari task is accepted, shows the `queued`
  phase with the task it waits for, and starts by itself when Safari is free. Its page keeps
  the cached totals while it waits. On Windows, Edge and Chrome queue the same way.
- An X Text task and a Safari X Media task both write the liked-text history, so they queue
  even in different browsers.
- Stop on a queued task removes it from the queue. Stop on a running task is the usual
  cooperative stop. Each mode's Stop affects only that mode.
- While a Safari task runs, another Safari source's account row reads `Not checked` and
  explains that Start adds the task to the queue. The account is verified when the task runs.
- A targeted refresh from Local resources does not queue. If its browser or task is busy it
  is refused with the reason, because its page waits for the result.

The task entry in the lower right of every page lists running tasks first and queued tasks
after them. `GET /api/cache/activity` returns the same list.

ChatGPT Media downloads project-index images before its optional prompt-metadata pass. The
pass reports `enriching` with session counts, checks Stop during Safari response polling and
body transfer, and limits mapping reads to 60 seconds. Startup and authorization reads are
also bounded separately. Budget exhaustion saves completed metadata and leaves the image
sync successful; a later sync prioritizes catalog entries missing a prompt or title. The
browser lease remains held until the owned context closes, then queued Safari work starts.
Grok Safari navigation requires DOM readiness and checks the actual library file cards;
it does not request the unsupported `networkidle` state.

### Legacy Grok Text runtime

The legacy Grok Text runtime remains available through its status and start/stop routes
for compatibility, but `/cache/grok` no longer renders a separate Text sync action.
In remembered Text mode, the primary action reads `View text history`. The form
submits `cache_content_mode=text`, and `/cache/grok/start` redirects to the Grok
Local resources history without saving configuration or starting the Media worker.
Media mode retains the existing Start behavior.
Monitor `GET /api/cache/grok/text/status` until `running` is false. Use the runtime's
cooperative stop route when needed; do not delete the lock file while the worker process
is alive.

### Verify the resulting file

```bash
python3 - <<'PY'
from collections import Counter
from pathlib import Path

from app.core.resource_persistence import read_resource_rows

path = Path("local_store/llm/grok/history.sqlite3")
rows = read_resource_rows(path) or []
print({
    "path": str(path),
    "messages": len(rows),
    "sessions": len({row.get("conversation_id") for row in rows}),
    "roles": Counter(row.get("role") for row in rows),
    "empty_messages": sum(not str(row.get("content_text") or "").strip() for row in rows),
})
PY
```

On Windows:

```powershell
py -3 -c @'
from collections import Counter
from pathlib import Path

from app.core.resource_persistence import read_resource_rows

path = Path('local_store/llm/grok/history.sqlite3')
rows = read_resource_rows(path) or []
print({
    'path': str(path),
    'messages': len(rows),
    'sessions': len({row.get('conversation_id') for row in rows}),
    'roles': Counter(row.get('role') for row in rows),
    'empty_messages': sum(not str(row.get('content_text') or '').strip() for row in rows),
})
'@
```

Then open:

```text
http://localhost:8666/browser?view=text&source=grok&session_view=1&q=&sort=newest
```

The page should show Grok as the selected source, session rows, message counts, and
original Grok conversation links.

## 10. Troubleshooting decision tree

### Safari reports an unreadable JavaScript result (macOS-only)

Inspect the embedded JavaScript first. A Safari parse error can occur before the
wrapper's `try/catch`, causing AppleScript to return an empty value. In the Gemini bot
check, verify that split selector strings use an explicit `+`. Then run a minimal live
probe against `inspect_gemini_bot_check` before starting a full history sync.

### Safari shows `Failed to open page` (macOS-only)

Stop the current Cache task and close its exact owned window. Do not continue counting
each later session as an independent failure. Confirm the saved Gemini interval is 5
seconds, open one fresh standard task window, and resume only after the window count
contract passes. Do not compensate by opening multiple Safari windows.

### The Text action or status route returns `404`

The running Flask process predates the Grok Text integration. Check the listener and
restart the project from Terminal after confirming the current source tree:

```bash
lsof -nP -iTCP:8666 -sTCP:LISTEN
ps -axo pid,ppid,lstart,command | rg '[m]ain.py|[P]ython.*CacheLikes'
```

Do not kill an unrelated process and do not restart the sibling project. After restart,
`GET /api/cache/grok/text/status` must return JSON rather than the Flask `404` page.

### The Cache page opens but Text looks empty

Check the URL first. A Grok Text review must contain `source=grok`; `/browser` without
the source filter is an aggregate view and may be sorted or paginated differently.
Then check the SQLite history directly through `read_resource_rows`. If the file has rows but the page is empty,
inspect `app/core/chat_history_browser.py` for the Grok path mapping and the
`source_paths` tuple used by the `all` view.

### Only a few Grok chats are found

This is usually a DOM-scraping regression. The correct implementation follows
`nextPageToken`, uses `pageToken` on the next request, and loads response IDs through
the response-node and load-responses endpoints. Do not increase a sidebar scroll limit
as a substitute.

### Edge is not ready

Keep the existing signed-in Edge session intact and run the session probe again. Do not
add a login-repair flow, copy cookies, inspect storage, or switch to a foreground DOM
scrape. The worker clones the selected profile into an isolated temporary context.

### The task cannot start because another task owns the lock

A task in this process that needs a busy browser or store is queued, not refused. A refusal
that names another window means a second application process, a shadow backup, or a history
repair holds the gate or one of the task's resource locks. Query the active tasks and
inspect the lock metadata:

```bash
curl -sS 'http://localhost:8666/api/cache/activity'
cat local_store/.cache_task.lock
ls -a local_store | grep '^\.cache_task\.'
curl -sS 'http://localhost:8666/api/cache/grok/status'
curl -sS 'http://localhost:8666/api/cache/grok/text/status'
curl -sS 'http://localhost:8666/api/chatgpt/status'
curl -sS 'http://localhost:8666/api/gemini/status'
```

The JSON file is only metadata; the OS advisory lock is authoritative. Wait for the
active task to finish or stop it through its own UI. Never remove the file as a normal
fix. If the owning process is genuinely gone and a new process still cannot acquire
the lock, perform a read-only process check before taking any cleanup action.

### A full test run fails while importing Agent modules

That failure is outside the Grok Text boundary. Parallel Agent changes can remove or
replace the Agent runtime module set and related Agent files. Preserve those changes and
report the exact missing import; do not restore or overwrite them as part of a Cache task.
Run the focused checks first:

```bash
./scripts/test.sh tests/test_grok_history.py
./scripts/test.sh tests/test_web_app.py tests/test_style_tokens.py
```

On Windows:

```powershell
.\scripts\test.ps1 tests/test_grok_history.py
.\scripts\test.ps1 tests/test_web_app.py tests/test_style_tokens.py
```

The second command requires the Agent module set to be internally consistent.

## 11. Change map for future agents

Read these files together before changing Cache behavior:

- `app/web/templates/_cache_page.html`: shared Cache shell and Text/Media control.
- `app/web/templates/grok.html`: Grok media controls and Grok Text action.
- `app/web/templates/claude.html`: Claude rendered-history notice and metrics.
- `app/web/static/cache-page.js`: mode memory and Cache status polling.
- `app/web/cache_sources.py`: source registry and canonical page metadata.
- `app/web/app.py`: runtime registration, routes, status reconciliation, and redirects.
- `app/core/grok_history.py`: Grok API traversal, normalization, and SQLite persistence.
- `app/core/cache_task_coordinator.py`: task admission, the queue, and resource locks.
- `app/core/cache_service_support.py`: worker lifecycle shared by every cache service.
- `app/core/grok_history_service.py`: Grok Text worker lifecycle.
- `app/core/claude_history.py`: Claude discovery, recorded-payload and rendered-transcript extraction, and SQLite persistence.
- `app/core/claude_history_service.py`: Claude worker lifecycle, one worker per mode.
- `app/core/claude_export_import.py`: official Claude export import into Claude history.
- `app/core/chatgpt_markdown.py`: ChatGPT interactive-token rendering for Text synchronization and repair.
- `app/core/chatgpt_history_cleanup.py`: ChatGPT history repair (legacy traces and Markdown rendering).
- `app/core/chat_history_browser.py`: source path mapping and Local resources queries.
- `app/core/resource_persistence.py`: typed schemas, SQLite dispatch, and legacy Parquet compatibility.
- `app/core/llm_sqlite.py` and `app/core/llm_history_index.py`: compressed row buckets,
  scoped conversation mappings, transactions, and derived browser summaries.
- `scripts/migrate_llm_sqlite.py`: explicit migration, verified rollback, and private benchmarks.
- `tests/test_grok_history.py`: deterministic Grok Text regression coverage.
- `tests/test_cache_task_coordinator.py` and `tests/test_cache_concurrency_web.py`: concurrent
  and queued task coverage at the coordinator and route level.
- `../SHARED_UI_SYNC.md`: shared-component synchronization ledger.

When adding a new LLM source, update the source allowlist, source-specific path map,
aggregate `all` path tuple, source switcher, Cache Text link, persistence schema, status
runtime, and focused tests together. A source that appears in a dropdown but is absent
from one of those contracts is an incomplete integration.
