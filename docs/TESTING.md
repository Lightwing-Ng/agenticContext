# Testing guide

Documentation version: `v1.43.1-codex.0`

## Supported commands

CI runs eight deterministic file shards on each existing Linux and Windows platform.
The partition balances end-to-end browser files and other files separately by source
size, adding a fixed 64 KiB planning allowance per browser file for its disposable
browser/server fixture setup. Source size alone put one browser file in one shard
and 28 in another; remote Linux and Windows jobs reached their existing time limits.
The allowance accounts for repeated setup work rather than estimating seconds.
Every discovered test file belongs to exactly one shard. Each shard retains the
ordinary static checks, default `not live` marker, and browser setup. The paired
`AGENTIC_CONTEXT_TEST_SHARD` (1 through 8) and
`AGENTIC_CONTEXT_TEST_SHARD_COUNT=8` variables enable this CI-only mode and permit
partial coverage below 55%; incomplete shard settings or additional test filters fail.
Without these variables, the launchers still run the complete suite with the existing
55% branch-coverage threshold.

The required Linux and Windows checks are the final aggregate jobs. Each requires all
eight successful shard jobs and exactly eight artifacts bound to the current commit,
run, attempt, platform, file inventory, selected files, and coverage hashes. Raw branch
coverage uses relative application paths and is combined before enforcing the same
55% global threshold. Missing, stale, duplicate, altered, or non-branch artifacts fail
the aggregate; shard percentages are never averaged. Job timeouts, test markers,
platform coverage, and coverage requirements are unchanged. One unusually large test
module remains indivisible, so capacity improvements still require live CI evidence.
Re-run the complete workflow when retrying: artifacts are named by run attempt, and
successful shards from an earlier attempt cannot fill gaps in a newer attempt.

Install the project dependencies with the supported Python 3.13 or newer workflow:

```bash
./scripts/setup_python.sh
```

On Windows:

```powershell
.\scripts\setup_python.ps1
```

Use an isolated project environment when the host interpreter has dependencies owned by another
application:

```bash
python3 -m venv .venv
export AGENTIC_CONTEXT_PYTHON="$PWD/.venv/bin/python"
./scripts/setup_python.sh
```

On Windows:

```powershell
py -3 -m venv .venv
$env:AGENTIC_CONTEXT_PYTHON = (Resolve-Path .venv\Scripts\python.exe)
.\scripts\setup_python.ps1
```

Run the ordinary offline suite:

```bash
./scripts/test.sh
```

On Windows:

```powershell
.\scripts\test.ps1
```

Run a focused test or marker selection:

```bash
./scripts/test.sh tests/test_local_media_browser.py
AGENTIC_CONTEXT_TEST_MARK_EXPRESSION=integration ./scripts/test.sh
```

On Windows:

```powershell
.\scripts\test.ps1 tests/test_local_media_browser.py
$env:AGENTIC_CONTEXT_TEST_MARK_EXPRESSION='integration'; .\scripts\test.ps1
```

`tests/test_chatgpt_display.py` and `tests/test_chatgpt_display_web.py` cover the
display-only adapter for observed ChatGPT component markup and empty citation
pointers. Known component rows become Markdown tables, diagram text remains
readable, and unsupported structures keep inert fenced source. Inline, fenced,
and indented code stay literal. Route checks preserve user messages, other
providers, stored Parquet bytes, and original Markdown exports.

The default marker expression is `not live`. Run an intentionally manual live check only with an
explicit override, for example `AGENTIC_CONTEXT_TEST_MARK_EXPRESSION=live ./scripts/test.sh`.

ChatGPT media pagination checks in `tests/test_chatgpt_media_partial_index.py`
verify the first-party `25`/`20` page sizes and processing of verified candidates
after a later HTTP 500, without launching legacy session scans or reporting complete
discovery. Temporary catalogs prove preservation of existing image bytes and new-file
readback. Stop and unrelated-error cases remain distinct. These offline fakes do not
establish that ChatGPT's live pagination or image transport has recovered.
`tests/test_safari_project_redirect.py` covers the observed bare-project to
`/project` landing redirect in both directions, ready-state navigation, and rejection
of changed authorities, projects, slugs, conversation paths, and noncanonical aliases.
It never opens Safari or a live provider page.

`tests/test_chatgpt_history_merge.py` and `tests/test_chatgpt_history_preservation.py`
cover append-only reconciliation, deterministic conflicting-body variants, source
identity preservation, idempotence, and invalid-guard failures. Guarded normal sync
must retain every existing row even when a later provider response omits it or changes
its body. These isolated fixtures never read the production archive or open a browser.

`tests/test_chatgpt_voice_history.py` verifies the narrow exception for completed,
typed outbound voice transcripts whose provider node has `end_turn=false`. Hidden
nodes, tool recipients, internal channels, incomplete audio, and ordinary unfinished
text remain excluded. Voice stages keep separate source identities even when an
earlier transcript is a prefix of the final reply.

`tests/test_chatgpt_display.py` and `tests/test_chatgpt_display_web.py` cover the
display-only adapter for observed ChatGPT component markup and empty citation
pointers. Known component rows become Markdown tables, diagram text remains
readable, and unsupported structures keep inert fenced source. Inline, fenced,
and indented code stay literal. Route checks preserve user messages, other
providers, stored Parquet bytes, and original Markdown exports.

Microsoft Copilot's focused offline checks are `tests/test_copilot_history.py`,
`tests/test_copilot_catalog.py`, `tests/test_copilot_catalog_transport.py`,
`tests/test_copilot_sync_state.py`, `tests/test_copilot_history_store.py`,
`tests/test_copilot_history_service.py`, `tests/test_copilot_legacy_catalog.py`,
`tests/test_copilot_legacy_recovery.py`, and
the `test_copilot_*web*` and `test_copilot_*e2e*` modules. They use isolated stores,
semantic page fakes, and disposable browser fixtures. They never open authenticated
Safari, Chrome, or Edge sessions. A separate manual Safari check must establish
current provider selectors, current-catalog cursor exhaustion, task-window cleanup,
and persisted readback before claiming live collection. An empty cursor proves only
that catalog's endpoint; a separate legacy Copilot or Bing surface needs its own
source-specific acceptance. Fixtures that traverse hundreds
of synthetic sessions prove the pagination contract, not a real account's coverage.
For a real account, retain the supplied entry query and compare discovered sessions,
processed sessions, failures, manifest flags, and archive readback. Reload tests must
keep legacy or interrupted caches partial and suppress an unverified 100% indicator.

The v1.43.1 real check on 21 Oct 2026 followed recovery of a user-acknowledged closed
task window: its journal was backed up, and recovery verified the old owner was dead
while holding the global Safari lock. Normal `Start` then traversed two current
catalog pages, deduplicated 17 sessions, and read two sessions with four messages.
An explicit operator `Stop` ended the check. The 17-session, 74-message archive hash
remained unchanged; this is a successful lifecycle and current-catalog check, not a
completed backup of the user's reported hundreds of sessions.

The preserved official exports in
`local_store/recovery/copilot-official-exports-20261021T052937Z` contain 74 activity
rows and zero Windows-apps rows. The first export's 16 distinct `Conversation`
labels are titles with reuse, so tests must not treat them as stable conversation
IDs or infer a session total from their cardinality. No export supplied additional
message-body coverage for hundreds of sessions. The actual legacy source is the
existing macOS Edge sidebar. Its 357 visible titles and 19 verified HTTP catalog
responses are preserved in recovery; the latter contain 377 physical records and
357 distinct native IDs. Tests verify exact response hashes, gzip decoding, strict
catalog fields, duplicate identity preservation, typed Parquet readback, and invalid
catalog handling. A complete current-web sync must remain partial across repeat
runs and reload whenever this legacy evidence remains unverified. Existing legacy
message rows must not prevent collection of available current-web sessions.
Catalog entries never become synthetic message rows. The one nonempty retained
history response belongs to entry 1; eleven empty responses, including entries 18,
200, and 357, do not establish deletion. Full older-body recovery remains unfinished.

The transport follow-up must test merged route `store` and successful query
`storePatch` context for exactly `eligibility`, `hostContext`, and `sessionID`, with
the frontend's `X-Client-Eligibility` and `x-host-context` headers. Fixtures must
exclude authentication-token or cookie reads. Passing those offline tests cannot
substitute for fresh authenticated acceptance of the header/context addition.

On Windows:

```powershell
$env:AGENTIC_CONTEXT_TEST_MARK_EXPRESSION='live'; .\scripts\test.ps1
```

Run the complete quality gate:

```bash
./scripts/check.sh
```

On Windows:

```powershell
.\scripts\check.ps1
```

Run the WeChat contact and native-text checks with synthetic fixtures:

```bash
./scripts/test.sh tests/test_wechat_contacts.py tests/test_wechat_contact_browser.py \
  tests/test_wechat_contact_template.py tests/test_wechat_provenance.py \
  tests/test_wechat_identity.py tests/test_wechat_wcdb.py tests/test_wechat_text.py \
  tests/test_wechat_reconcile.py
```

They cover exact native identity, OpenIM realm separation, scoped name observations, unidentified
speaker isolation, and per-message body/time/record fingerprints. Native checks cover bounded
metadata parsing, strict plaintext decoding, explicit missing-backend/dictionary failures,
supported text envelopes, account/hash boundaries, replay, conflicting observations, exclusions,
and preservation of existing message evidence during additive publication. Real Zstd cases run
when `compression.zstd` is available; Python 3.13 still exercises the explicit unavailable-backend
contract. These checks do not read production backups, cloud exports, media, or the local index,
and passing them does not establish complete recovery of a particular backup.

WeChat disclosure motion checks use a synthetic archive and disposable browser profiles:

```bash
./scripts/test.sh tests/test_wechat_disclosure_e2e.py tests/test_wechat_contact_disclosure_e2e.py tests/test_wechat_browser_e2e.py tests/test_browser_chat_effects_e2e.py
```

They verify measured width/height spring motion and opening overshoot, interrupted reversal,
keyboard activation, arrow removal, shadow alignment, final geometry cleanup, Reduced Motion
including a midflight preference change, and disclosure without ScrollTimeline support.
The desktop/narrow Light/Dark checks never read the production archive or authenticated profiles.
Contact-source disclosures reuse the same spring with height/body motion only. Their checks cover
stable table columns, native keyboard activation, continuous reversal, Reduced Motion, restored
interaction state, and idempotent initialization after asynchronous content replacement.

Shared conversation-storage checks use isolated synthetic accounts with partial overlap:

```bash
./scripts/test.sh tests/test_wechat_shared_storage.py tests/test_wechat_shared_storage_publish.py
```

They verify unchanged logical rows and account counts, complementary gaps, exact-byte body
sharing, search suffixes, conflicting bodies, independent directions, late imports, replay,
legacy SQL writers, read-only/attached database queries, append guards, and strict MCP format
validation. Publication checks replace APFS cloning with a test-only file copy and exercise
verified rollback evidence, sidecar pin handling, changed-input refusal, and failed-publication
recovery. They never read production history, hydrate cloud archives, or launch browsers.

Storage-maintenance regressions use disposable SQLite files and synthetic provider metadata:

```bash
./scripts/test.sh tests/test_compact_wechat_sqlite.py tests/test_publish_wechat_compaction.py tests/test_preserve_wechat_workstations.py tests/test_verify_wechat_onedrive.py tests/test_archive_wechat_rollbacks.py tests/test_wechat_rollback_location.py
```

They assert source-byte preservation when candidate hard links, repeated source inodes,
late candidate links, parent traversal, or actual runtime-directory aliases are refused.
Case checks follow the filesystem's real identity semantics rather than lowercasing paths.
Phased relocation tests retain prior workstation/receipt mappings and reject conflicting
relative paths before moving new files. Relocation unit tests replace native rename/durability
primitives; unsupported-platform apply checks verify that no output is created. Missing
allocation metadata stays unknown. File Provider success never certifies remote content or
authorizes local release. Native macOS compression and atomic-swap checks retain their
explicit filesystem/platform requirements; no Windows execution is inferred from local results.

Private-chat checks require reciprocal verified native owner/peer IDs and retain separate
sent/received directions, sender labels, sources, gaps, and conflicting bodies. Invalid
one-sided, extra-account, mixed-kind, self-chat, and spoofed identity scopes fail before mutation.
Batch cases prove cross-pair and existing-group-pool reuse with exact marginal byte accounting,
one staging/integrity pass, zero-cost replay, overlap rejection, and all-or-nothing failure.

WeChat contact checks use synthetic message and contact sidecars:

```bash
./scripts/test.sh tests/test_wechat_contacts.py tests/test_wechat_contact_browser.py tests/test_wechat_contact_template.py tests/test_wechat_provenance.py
```

They cover exact native identity, OpenIM realm separation, account/room/snapshot-specific remarks,
complete observation pagination, unidentified-speaker isolation, and per-message body/time/record
fingerprint checks. These tests do not open real backups, cloud exports, or the production index.

Payment extraction and presentation use synthetic source envelopes in
`tests/test_wechat_payments.py`, `tests/test_wechat_payment_presentation.py`, and
`tests/test_wechat_payment_presentation_e2e.py`. Coverage includes ordinary, forwarded,
and quoted Type 49 cards, exact source-text validation, CNY/HKD and unknown/conflicting
currency, the observed regional subtype-2001 transfer tuple, separate owner accounts,
literal escaping, unchanged raw CSV/Markdown/database bytes, and four standard currency
glyphs with neutral fallbacks. Disposable Light/Dark browsers at desktop/narrow widths
verify the existing sender/timestamp and message layout without active payment or media links.
`tests/test_wechat_payment_copy.py` covers the keyed locale catalog, exact field-scoped
Simplified/Traditional provider aliases and parameterized notifications. Integration and
browser cases verify shared ordinary/forwarded/quoted localization, literal memo/greeting
preservation, semantic language fragments, unchanged account partitions and raw exports,
and rejection of payment-looking ordinary prose or mismatched source metadata.

Contact analysis adds verified cross-account aggregation and same-tab navigation checks:

`tests/test_wechat_source_precedence.py` and
`tests/test_wechat_global_native_preference.py` cover the shared native-over-HTML
preference across contact and conversation views, preferred/default exports and
explicit source occurrences. Cases include exact same-second device permutations,
independent intervals after an ordering anomaly, conflicting server events,
ambiguous same-second duplicates, unverified owners, HTML-only messages and a
complete v1.14 cache replay without an author rescan. Raw source bytes and every
retained reference remain unchanged.

```bash
./scripts/test.sh tests/test_wechat_contact_analysis.py tests/test_wechat_contact_analysis_routes.py tests/test_wechat_contact_analysis_e2e.py
```

These cover scoped identifiers, namesake exclusion, exact message proofs, unknown timestamps,
source-page boundaries and ties, journal rejection, cache invalidation, and immutable source bytes.
Desktop/narrow Light/Dark cases exercise mouse and keyboard contact links, the standard table,
search, and browser Back restoration of URL, scroll, disclosures, focus, and unrelated history state.
Existing source-disclosure tests activate the summary through its native keyboard boundary;
clicking a verified speaker name now intentionally navigates to Contact analysis.

Responsive contact-table checks use long synthetic statements and source labels:

```bash
./scripts/test.sh tests/test_wechat_contact_responsive_e2e.py
```

Desktop, 831px, 700px, and 390px Light/Dark cases verify that all five columns fit
without horizontal scrolling, long text wraps without truncation, and the No. heading
and values remain centered. They measure body/header alignment after resizing and
require the exact 10/20/40/15/15% allocation. Early and
late pages cover both short and five-digit source-occurrence numbers.
Explicit traditional-scrollbar cases reserve a real 15px gutter and verify that
the detached header still matches the body's narrower column grid.
The exact two-row last page additionally checks 80,501 and 80,502 with the reported
timestamps at 855px and 831px. These checks measure rendered text-line counts and
intrinsic text fit, not just centered bounding boxes, to reject wrapped values.

Contact conversation-kind badge checks compare rendered contact-table badges with
the existing chat-history badge at 823px and 390px in Light/Dark. They cover the
shared material, font, spacing, and label semantics, plus narrow-column wrapping
without collision with the source account. Both pages load the same badge asset.

```bash
./scripts/test.sh tests/test_wechat_contact_kind_badge_e2e.py
```

Stable contact-loading checks hold a synthetic index pending across real JSON polls:

```bash
./scripts/test.sh tests/test_wechat_contact_loading_e2e.py tests/test_wechat_contact_analysis_routes.py
```

They verify an initially open shared modal, one document/history entry, stable centered geometry,
the canonical inline SVG under normal/reduced motion, a single result update, browser Back
cancellation, stale-response rejection, error recovery, and search-draft/focus preservation after
dismissal. The shared modal's default hidden state and escaped optional copy remain covered.

Run the isolated local-compute benchmark:

```bash
python3 scripts/benchmark_compute.py
```

On Windows:

```powershell
py -3 scripts/benchmark_compute.py
```

Run the durable optimization-job contract tests without a long-lived workload:

```bash
./scripts/test.sh tests/test_compute_jobs.py tests/test_agent_capability_registry.py
```

On Windows:

```powershell
.\scripts\test.ps1 tests/test_compute_jobs.py tests/test_agent_capability_registry.py
```

When the quality gate fails, read the [CI Failure Playbook](CI_FAILURE_PLAYBOOK.md) before changing
production code, tests, platform branches, or retry behavior.

Run the responsive contract and sidebar browser layers independently with:

```bash
./scripts/test.sh tests/test_responsive_contract.py
./scripts/test.sh tests/test_sidebar_e2e.py
```

On Windows:

```powershell
.\scripts\test.ps1 tests/test_responsive_contract.py
.\scripts\test.ps1 tests/test_sidebar_e2e.py
```

Run the six-experiment Beta contract and fixture-backed formal Zhihu checks with:

```bash
./scripts/test.sh tests/test_beta_routes.py tests/test_beta_e2e.py tests/test_zhihu_answer_collection.py tests/test_zhihu_history.py tests/test_zhihu_web.py
node --test tests/test_beta_engines.mjs
```

On Windows:

```powershell
.\scripts\test.ps1 tests/test_beta_routes.py tests/test_beta_e2e.py tests/test_zhihu_answer_collection.py tests/test_zhihu_history.py tests/test_zhihu_web.py
node --test tests/test_beta_engines.mjs
```

These commands remain offline. They do not establish that a signed-in Chrome or Edge profile can
currently complete Zhihu's live provider flow.

On macOS, run the complete offline iPhone contract, including disposable-browser cases, with:

```bash
./scripts/test.sh tests/test_iphone_*.py
```

For a focused page/service/Tunnel check or the browser layer alone:

```bash
./scripts/test.sh tests/test_iphone_routes.py tests/test_iphone_service.py tests/test_iphone_tunnel.py tests/test_iphone_tunnel_service.py
./scripts/test.sh tests/test_iphone_e2e.py
```

The runtime tests use temporary private state, fake WDA HTTP servers, and mocked subprocess
boundaries. They cover transport retries, uncertain-action refusal, setup ownership, preview
pause/recovery, and controller behavior. The browser cases mock every iPhone API response and
keep the isolated service's external operations disabled. They verify navigation, setup gating,
screen coordinates, typing drafts, narrow layouts, and explicit Tunnel opt-in. Unix runtime
modules skip on hosts without `fcntl`; page/service tests still cover the unsupported-platform
response without loading those modules.

These checks cannot prove Apple signing, USB pairing, Developer Mode, WDA installation, real
phone readiness, or a live ChatGPT phone call. Verify those separately using the explicit local
setup and readiness steps in [iPhone control](IPHONE.md), with an attached unlocked device and
the user's own signing account. Do not turn an offline fixture into a live device test.

Run the Secure MCP Tunnel contract independently with:

```bash
./scripts/test.sh \
  tests/test_tunnel_mcp.py \
  tests/test_iphone_tunnel.py \
  tests/test_tunnel_checks.py \
  tests/test_tunnel_runtime.py \
  tests/test_tunnel_credentials.py \
  tests/test_owner_only_files.py \
  tests/test_tunnel_credential_errors_e2e.py \
  tests/test_gemini_tunnel.py \
  tests/test_tunnel_workflow_e2e.py
```

The iPhone Tunnel tests inject an offline phone service and verify explicit local opt-in,
external-operation and platform gates, ChatGPT-only discovery, native screenshot image blocks,
closed phone schemas, provider revocation before admission, local revocation before dispatch,
private activity records, and unchanged project-tool boundaries. They do not connect a phone,
install WDA, or start the Tunnel. Real phone readiness and live ChatGPT calls require separate
manual verification after local setup and explicit user enablement.

Host-parity tests cover both operating systems from either host. Simulated Windows tests model the
protected DACL, the WinINet proxy setting, and handle-held orphan confirmation, including ACL
refusal, inherited or shared DACLs, a PID reused before either signal, and a failed process scan.
The macOS run also stops a real orphaned process while sparing a lookalike. Tests marked for native
Windows read the real DACL, compare the proxy with `urllib.request.getproxies_registry()`, and
terminate a real owned process through WMI and a process handle; they run only on the Windows
quality gate. The credential-error browser test proves at desktop, narrow, and short viewports that
a refused save is announced in the Tunnel status line without horizontal overflow, and that the
next edit retracts it while rejected field values remain field markers only.

Workspace fingerprint regressions cover the exact `forPrompts/` and `docs/forPrompts/` reference
directories: only ignored, wholly untracked, non-linked directories may be excluded. Tracked
content, ordinary ignored fixtures, non-Git workspaces, failed Git queries, and a tracking change
during the scan retain coverage or refuse an incomplete snapshot. The excluded materials are
never opened, so cloud hydration cannot block a code check.

ChatGPT kickoff browser tests cover single-project identity pinning, multi-project discovery and
permission reporting, target clarification, per-project overview, and task-body preservation.
They switch the confirmed selection in both directions, reject an uncommitted change, and verify
that copying uses the updated prompt at desktop and narrow viewports. Initial server-rendered
multi-project instructions match the client-generated prefix.

The Tunnel tests also revoke ChatGPT bearer authority between HTTP authentication and tool
admission, including later calls in a batch, while preserving already admitted work and its
uncertain-outcome reporting.

The Tunnel tests pin the exact fourteen-tool public catalog, closed-schema enforcement,
discovery/dispatcher agreement, rejection of removed compatibility names, direct invocation
without runtime selectors, current-project discovery, durable compare-and-swap selection
revisions, preferred project sets that do not narrow registry authority, explicit project
identities, ingress byte and JSON-RPC batch limits, same-size registry replacement with preserved
modification time, exact Git-root fallback admission, and permission revalidation after a queued
write acquires its project lock. They cover prevalidated and write-failure batch recovery,
concurrent-edit rollback conflicts, duplicate-safe mutation requests, three-state batch reads,
stale-write and delete SHA protection, truncation continuation, workspace confinement, bounded
status/patch inspection, `git status`, approved checks, durable check deduplication/cancellation/restart query,
runner-loss `unknown` recovery, bounded output and retained-job counts, stale-verification
rejection, request-id replay tombstones after journal pruning, fail-closed tombstone capacity,
credential-clear bearer revocation, destructive-command refusal, current-evidence bodycheck, and
loopback bearer authentication. The isolated workflow test uses one application instance to save a
current project and preferred set, discover both through authenticated `/mcp`, and
create/read/edit/re-read/delete a file, confirm absence, run a check, finish `review_changes`, and
reject a write to the read-only reference project. It also proves that an explicit registered reference remains discoverable after
it is unchecked locally. A second local-page case keeps one missing registered root visible as
unavailable, permits removing that stale preference, and proves that another registered project
remains usable and may become the deterministic current fallback.

The project registry cases use temporary writable and read-only roots and cover unknown and
path-like ids, cross-project and absolute-path escapes, same-name files, symlinks,
project-scoped instruction discovery, per-project SHA guards across switching, re-registration,
same-path root replacement, admission-time replacement races, invalid registries failing closed,
per-project handling of temporarily missing roots, schema-1 selection migration, and a dynamic
`Path.home() / "Desktop"` picker start that never becomes a Desktop-wide authority fallback.
Runtime cases cover transient
preflight recovery, ready-then-lost connectivity, a live but persistently unhealthy client,
bounded retry, HTTP status failures, status-fetch timeout/cancellation, and superseded-generation
results. Browser cases prove the first-use path has no historical-activity gate, a failed
selection save cannot change the shown project, the generated prompt updates its exact id without
discarding the user's task text, and stale or failed polling revokes a previous green state.
Browser assertions exercise keyboard-operable project checkboxes, revision-conflict rollback,
exact registered-root matching from the circular folder action, and refusal of an unregistered path
without changing permissions. A successful ChatGPT selection that adds a selected project or changes
the current project, and a new folder registration, start a new qualified local Tunnel client
generation only while their saved revision remains current. The revision check and reconnect startup
are atomic under the runtime lock; readiness is checked asynchronously. Repeated saves, removal of
a noncurrent selected project, unregistration, rejected or superseded saves, Gemini selection, and
missing credentials do not restart it. The local `Ready` state reports client health; it does not
establish ChatGPT app authorization or catalog refresh. Read and write access still depends on the
registered project's writable flag and MCP write guards. If local reconnect startup fails, the
saved selection remains committed and the Tunnel reports `Unavailable` on subsequent status polls
without exposing the underlying exception. The Tunnel usage card must contain exactly one visible
`Tokens:` row whose accessible description identifies it as an estimate; total-call, active-call,
recent-call, and billing rows must be absent. The recovery regression exercises an early
uncounted call, tokenizer readiness, the first complete post-recovery call, the status API,
and initial HTML. It verifies that history retains the early record while the token window
identifies excluded calls, stays unavailable before a complete new call, and never sums an
unknown estimate as zero. The Browser Agent tests separately verify asynchronous first
load, per-task pinned counting method, and continuation provenance.
These tests are offline and use temporary projects, registries, runtime roots, and disposable
browser contexts; live ChatGPT and Gemini round trips remain separate provider-specific evidence.

Catalog adoption is verified as three separate states rather than one green claim. Source tests
prove the disk catalog. An isolated restarted service must return the expected `tools/list` and
complete a read-only authenticated call. ChatGPT discovery is separate provider evidence and is
complete only after Refresh/Scan Tools shows the new catalog in a new conversation; reconnecting
the Tunnel alone proves neither service-code adoption nor ChatGPT catalog refresh.

The Gemini transport tests independently cover strict public-origin validation, atomic private
credential storage with POSIX owner-only mode checks, safe snapshots, protected-resource and
authorization-server metadata, exact
client/resource/scope/redirect binding, mandatory PKCE S256, one-time authorization codes,
short-lived access tokens, refresh tokens, signature and expiry failure, public-Host path
confinement, exact pending-callback review and single-use local approval, bearer challenges, credential-
generation-scoped Active evidence, provider-attributed activity, and identical fourteen-tool
discovery. Rotation tests also assert the generation-scoped `recent_usage` and cumulative
call count after the bounded history evicts older calls, reject a previously authenticated
request if its credential rotates before tool admission, and exclude older in-flight calls.
Batch rotation preserves completed item responses, refuses every remaining valid request ID
without running those items, answers malformed remaining items with `-32600`, skips valid
notifications, and keeps the bearer challenge when no item answered. A post-authentication
rotation must also reject `tools/list`, `initialize`, `ping`, and a notification before
returning a response; metadata dispatch must remain atomic with the authority generation.
Post-authentication rotation also turns malformed JSON, oversized bodies, empty or
oversized batches, and the GET method's 405 response into 401 bearer challenges; valid
credentials retain the original error or method response. They must also prove that the client secret, signing key, and bearer values do not appear in HTML,
initial JSON, status payloads, URLs, or logs; copying a secret does not approve a callback; pending,
approved, denied, stale-review-id, empty-state, changed-state, and changed-callback cases fail closed;
exact first-use callback
pinning is enforced without assuming an undocumented consumer callback hostname; and body ceilings
still apply without Content-Length. These tests use a loopback request with the configured
public Host and do not open a real reverse tunnel or establish provider eligibility.

The ChatGPT and Gemini Tunnel onboarding UI runs in the disposable Chromium layer:

```bash
./scripts/test.sh tests/test_agent_sessions_e2e.py -k "tunnel or connection_mode"
```

The local project directory browser has a separate observable-behavior slice:

```bash
./scripts/test.sh \
  tests/test_shadow_backup.py \
  tests/test_web_app.py \
  tests/test_style_tokens.py
./scripts/test.sh tests/test_agent_sessions_e2e.py -k agent_directory_browser
```

The source and route cases cover real canonical paths, empty and Unicode folders, spaces and
quotes, same-name-safe exact paths, valid and broken symlinks, invalid-path recovery, relative-path
rejection, registered fields, and the loopback boundary. Disposable-browser cases cover desktop
and 390 px narrow geometry, one-click dialog visibility, child and absolute-path navigation,
selection plus the existing Agent preference save, cancellation without a project change, focus
restoration, duplicate-click suppression, abort cleanup, and rejection of a simulated late
response. Directory browsing intentionally has no fixed session timeout; cancellation owns request
cleanup. These tests do not replace a macOS foreground-app acceptance: before handoff, an actual
host browser must be foregrounded and checked for one-click dialog visibility, correct absolute
path selection, cancellation, and no Terminal or Dock interaction. Run that acceptance on an
isolated loopback service, never by restarting the user-owned 8666 service.

It measures the rendered page at 1,280, 876, and 390 px wide, including a 420 px short viewport: the
Agent workspace is the only content scrollport; the outer `Step 1` through `Step 4` markers align
with their headings and share the same text color; body copy uses `--font-ui-md` under
`--font-ui-lg` headings; the project path, credential values, and masked credential placeholder use
the scoped technical monospace and the two credential displays share one size. Step 1 contains four
independent native disclosure rows, marked ➊ through ➍, and Step 3 contains two more rows for the
ChatGPT plugin flow. They are closed on first render and after reload, support mouse, Enter, and
Space, and reveal six responsive inline SVG guides. Each disclosure summary keeps one concise
sentence-case title while the diagrams retain the detailed choices. The disclosure adapter follows
Worthward's canonical 12×8 px
chevron, 8 px icon gap, `10px 0` summary padding, `0 10px 10px` body padding, and 180 ms standard
rotation. The vectors contain no macOS window chrome, every rectangular surface uses a 10 px
radius, and the API-key diagram selects Expiration: Never and Permissions: All. The contract
rejects raster images, SVG `<image>` or `<foreignObject>` nodes, embedded data images, and
credential-shaped literal values. Every guide uses a 640 px logical viewBox, remains no wider than
640 CSS pixels, and shrinks with its disclosure body without introducing a nested horizontal
scrollport. The document itself remains free of horizontal overflow. Every instruction
package places the Style tokens Secondary
button below its complete copy and against the package's lower-right edge. Step 2 has no manual
disconnect or reconnect control; Step 4 keeps an editable gray prompt surface, copies its current
value, and changes the button label to `Copied` before inviting the user to open ChatGPT. Further
edits reset the copy state and resize the field without creating another scroll owner. All four
outer steps are divider-free, the focused
action keeps the shared 48 px physical-effect bleed inside the scrollport, the title clears the
global quick actions, routine Tunnel message/activity copy is absent, and credential errors still
reach the sidebar hint. Switching the Web service in place must replace the entire provider panel,
status snapshot, labels, links, and copy actions without navigation or stale-response bleed.
Gemini acceptance also asserts that no secret is present before the explicit copy response, the
public origin and copied MCP URL remain distinct, ChatGPT credentials never appear in the Gemini
panel, an exact pending callback can be approved or denied without stale-provider bleed, and
unsupported providers stay unsupported.

Run the OpenAI Site tools contract and disposable-browser layers independently with:

```bash
node --test tests/test_agent_optimization.mjs
./scripts/test.sh \
  tests/test_agent_optimization.py \
  tests/test_agent_optimization_browser.py
```

On Windows:

```powershell
node --test tests/test_agent_optimization.mjs
.\scripts\test.ps1 `
  tests/test_agent_optimization.py `
  tests/test_agent_optimization_browser.py
```

`AGENTIC_CONTEXT_PYTHON` may override the interpreter only when it resolves to Python 3.13 or newer.
The resolver prefers a supported host `python3`, `python`, or Windows `py -3` launcher, then
falls back to unversioned platform-specific Python installations.

## Quality gate

Jury behavior and UI coverage run with `./scripts/test.sh tests/test_jury.py
tests/test_jury_browser.py tests/test_jury_routes.py tests/test_jury_e2e.py`. These checks use
fake provider boundaries; rendered acceptance uses disposable browser contexts and isolated
local stores. Real signed-in provider/model acceptance remains a separately authorized manual
check, as described in [Jury](JURY.md).

The canonical local and CI quality gate is `scripts/check.sh` on macOS/Linux and
`scripts/check.ps1` on Windows. The gates run, in order:

1. Installed requirement-version validation, `pip check`, and a local-environment `pip-audit` scan.
2. Ruff static checks over `main.py`, `app/`, `tests/`, and `scripts/`. The enforced rule set is
   pinned in `ruff.toml` so a Ruff upgrade cannot change it; Ruff 0.16 replaced the built-in
   default selection that the gate previously relied on.
3. Offline Markdown file, image, and heading links in the repository documentation.
4. `node --check` for every first-party JavaScript file in `app/web/static/`.
5. Node unit tests for the shared Agent Optimization and Site tools runtime contract.
6. The full pytest suite with branch coverage for `app/`, including the disposable-browser
   responsive sidebar E2E flow.

The coverage report is written to `test-results/coverage.json`; all generated test artifacts are
ignored by Git. The gate currently enforces a 55% combined statement-and-branch coverage floor.
Override `AGENTIC_CONTEXT_COVERAGE_MINIMUM` only for an intentional local diagnostic, never to make a
regression pass.

Historical coverage counts are preserved in [TEST_HISTORY.md](TEST_HISTORY.md).

Raise the coverage floor only after adding behavior-level tests. Do not exclude production modules
or lower the threshold to mask a gap.

## CI portability contract

GitHub Actions is the canonical clean-room gate. It runs the quality contract on `ubuntu-latest`
and `windows-latest` with Python 3.13 as the minimum-version baseline, Node.js 22, UTC, and a freshly installed Playwright
Chromium. A test that passes only on the developer's macOS workstation is not evidence of a
passing project contract.

Use this command to reproduce the CI timezone locally:

```bash
TZ=UTC ./scripts/check.sh
```

On Windows:

```powershell
$env:TZ='UTC'; .\scripts\check.ps1
```

Tests must follow these rules:

- Do not hard-code local-time output such as `13:00`. Use UTC fixtures, the production formatter,
  or an explicit timezone in the assertion.
- Settings read/write helpers must resolve `AGENTIC_CONTEXT_SETTINGS_PATH` at call time. Do not use a
  module-import snapshot as a function default, because pytest and clean-room CI inject the
  process boundary before application startup.
- Do not hard-code `Finder`, `open -R`, or macOS permission messages in a host-neutral test. Test
  each platform through explicit platform arguments or monkeypatch the host predicate when the
  test is specifically for macOS or Windows.
- The yt-dlp cookie-source mapper may return the explicit `safari` backend on every CI host;
  browser automation remains host-aware and must still reject Safari where the runtime registry
  does not expose it.
- Overlay sidebars must use an explicit viewport-bounded height with internal scrolling. Do not
  rely on intrinsic fixed-position height on touch viewports, because Chromium implementations
  can report a transient bottom edge outside `window.innerHeight`.
- When `prefers-reduced-motion: reduce` is active, the sidebar shell, overlay, and toggle must
  disable their transitions entirely. The JavaScript state timer may remain short for bookkeeping,
  but the geometry used by touch hit testing must already be final when the state attribute changes.
- DOM observers must not continuously observe attributes that their callbacks write on every pass.
  Observer-backed controls must make attribute, class, and style updates idempotent, and a static
  contract test must pin the observed attribute set.
- Keep filesystem, browser-profile, and subprocess tests inside `tmp_path` or an explicit fake
  platform boundary. Never inspect the runner's real browser profile or file manager.
- Browser E2E tests must use a clean context and local Flask server only. Disable or stub unrelated
  background polling when the test injects a DOM fixture; otherwise the application may replace
  the fixture during the assertion.
- Agent `browser_acceptance` coverage must pin the stronger controller boundary: an owned loopback
  preview process, an unauthenticated Chromium context with no user profile, fixed desktop and narrow
  viewports, blocked external/file/credentialed/unowned-port requests, protected/occupied-port
  rejection, bounded assertion/console/page-error evidence, bounded screenshot/trace artifacts, and
  deterministic cleanup on success, assertion failure, timeout/Stop, launch failure, and controller
  exceptions. Portable tests may exercise the real disposable Chromium installed by the quality gate;
  host-specific process assertions must not claim native Windows evidence when run on macOS.
- For responsive geometry and hit testing, wait for the relevant rectangle to become stable before
  asserting `document.elementFromPoint()`. Reduced motion shortens transitions but is not a promise
  that a DOM update is synchronous.
- Keep live, authenticated, and remote-service checks under `@pytest.mark.live`; they do not belong
  in the default quality gate.
- CPU compute tests use synthetic image bytes and temporary paths. GPU tests inject a fake adapter
  and verify that partial, duplicate, unknown, OOM, failed, or initialization-failed batches are
  discarded and recomputed in full on CPU. Payload-budget tests verify that the parent flushes before
  oversized files and analyzes them without queueing their bytes. No test treats a fake adapter,
  static inspection, or an HTTP response as GPU evidence.
- Durable compute-job tests use short approved fake workers and temporary runtime roots. They pin
  12-hour configuration without waiting 12 hours, verify provider-turn-independent survival,
  idempotent start, atomic checkpoint and explicit resume, restart reconciliation, PID-reuse
  refusal, owned process-tree stop, rolling logs, path and symlink rejection, and unchanged
  workspace fingerprints. A test must never launch a long optimization or write below the
  production Agent runtime root.

## Local-compute benchmark contract

The benchmark uses a deterministic `1,024 × 1,024` PNG fixture, one warmup run, five measured runs,
and reports the full wall-time and parent CPU-time samples plus medians, together with numeric
backend metrics from the final optimized run. On this host, the 32-image
fixture measured a legacy sequential wall-time median of approximately `0.310 s`, while the
bounded CPU path measured approximately `0.307 s`; the process
backend was intentionally not selected for that size because process startup made it slower. With
the same fixture expanded to 128 images, the legacy median was approximately `1.246 s` and the
bounded CPU backend median was approximately `0.510 s`. These latest recorded values are local
synthetic evidence,
not a claim about browser, network, or GPU throughput. Remote discovery, browser automation,
downloads, Safari, Parquet commit, and backup stages remain outside GPU acceleration unless a
separate live profile proves otherwise.

When a gate fails on GitHub, reproduce the exact failing node first with `TZ=UTC`, then run the
complete platform gate: `./scripts/check.sh` on macOS/Linux or `.\scripts\check.ps1` on Windows.
Do not weaken an assertion, skip a platform branch, lower coverage, or add a retry until the
failure has been classified as a real product regression or a test environment assumption.

## Test organization

### Windows Agent browser verification

Offline regressions in `test_computer_use_agent.py`, `test_scraper_and_browser_sessions.py`, and
`test_web_app.py` cover configured Windows login/conversation arguments, saved-config forwarding,
provider-page window selection (including empty contexts), pre-submission window-control failure,
debug-profile restart, full CDP caller-lifetime exclusion, single-worker admission, active-worker
probe suppression, macOS non-interference, passive launch modes, and cleanup failure/error
precedence using disposable fixtures. They do not
read authenticated profiles or prove native window or provider behavior.

Before claiming native Windows 11 support for the complete workflow, record the commit, Python,
Playwright and Edge/Chrome versions, display layout, scaling, and profile selection, then perform
the following explicit manual checks with the user's authorized test account. Keep credentials,
cookies, and conversation content out of test artifacts. Do not add these checks to the default
suite or CI; an automated equivalent must be marked `live`.

| Scenario | Required evidence |
| --- | --- |
| Edge and Chrome project debug profiles | Windows login handoff, Recheck, window close/restart, and task retain the same project-owned authentication state; copying and launching alone do not establish authentication. macOS Edge Agent clones daily Edge like Cache ChatGPT and must not use the empty project debug profile. |
| Concurrent requests | A second Windows Agent is rejected immediately; source/history probes return cached, `unprobed`, or busy responses without driving the active browser. |
| Source browser open during clone creation | Record copy success or an actionable access/copy error; do not silently switch to writable automation of the real profile. |
| Passive readiness/source checks | Windows probes remain offscreen/minimized and do not activate the browser. |
| Existing provider tab, unrelated first tab, and empty clone | The selected or newly created provider page owns the normalized task window; count native windows separately from contexts. |
| Single display, high scaling, and multiple displays | Verify the actual provider window is usable; fixed CDP geometry is not proof of work-area fit. |
| Human verification | The same controlled clone and turn survive manual verification and the existing Resume gate. |
| Normal completion, Stop, closed provider window, and launch failure | The context exits; inspect remaining task-owned processes and temporary paths without disturbing the user's browser. |
| Failed profile removal | A retained-path diagnostic is present; an existing task/launch error remains primary; no successful cleanup is claimed. |

Run the native offline gate separately with `.\scripts\check.ps1`. A passing macOS mock run or a
Windows CI run without the authenticated manual checks is not live Windows provider evidence.

### General test layers

- Pure unit tests cover URL normalization, source parsing, media classification, state
  transitions, durable queue behavior, retries, and path validation.
- Filesystem regression tests use `tmp_path` or `TemporaryDirectory` for catalogs, manifests,
  media files, deleted previews, and settings.
- Flask integration tests use `create_app()` plus `test_client()` and assert route contracts
  without starting a web server.
- Formal Zhihu collector tests cover URL, host, and cursor rejection; answer normalization;
  answer-ID deduplication; stable provider-gap verification; and human-verification errors without
  contacting Zhihu or opening a host profile. Workflow tests inject current-account, vote-up
  activity, and author-answer pages into a
  temporary `local_store` root. They verify exact activity filtering, cursor rejection,
  cumulative Parquet writes, unavailable-body retention, source-link extraction, empty provider
  HTML, complete Local resources answer rendering, source-specific Answerer filtering, query-state
  preservation, removed answer IDs/Projects/Clear filters, literal detail headings, default Edge
  controls, login-probe routing, and the single-character vector logo.
- Concurrent cache task tests never open a browser or contact a provider. Coordinator tests use
  a temporary lock directory and recorded callbacks for admission by browser and store, queue
  order, cancellation, a second process, and the deferred post-task backup; one case runs the
  real X and ChatGPT workers on their own threads with injected collectors. Route tests build
  the application with idle worker threads, release every admitted task on teardown, and cover
  per-mode Start, Stop, status, the queue, targeted-refresh refusal, and the Safari account
  check. `test_cache_activity_e2e.py` and `test_cache_queue_e2e.py` use synthetic activity and
  status responses in a disposable browser for the task entry on every application page and
  the queued state on a Cache page. Run them with
  `./scripts/test.sh tests/test_cache_task_coordinator.py tests/test_cache_concurrency_web.py tests/test_cache_activity.py tests/test_job_lock.py`
  and `./scripts/test.sh tests/test_cache_activity_e2e.py tests/test_cache_queue_e2e.py`.
- Zhihu answerer update tests cover the drawer action's rendering, disabled state without a cached
  profile link, route URL validation, busy and disabled-operation refusals, and the script-to-route
  contract. Collector tests cover the newest-answer read: stopping at the first fully cached page,
  reading to the terminal page when nothing is cached, ignoring provider totals and duplicates,
  fail-closed cursor and repeated-page handling, and cooperative stop. Sync tests cover its merge
  metrics and the answerer-URL requirement. `test_zhihu_answerer_refresh_e2e.py` drives the real
  Zhihu service, collector, and Parquet merge in a disposable browser against injected provider
  pages: added, unchanged, and provider-failure results, wait-modal release, and the four-action
  drawer inside 1,138, 1,024, and 390 px viewports. No test contacts Zhihu or opens a host browser
  profile.
- ChatGPT Media Sessions index tests seed an isolated ChatGPT media catalog. Unit tests cover cover
  selection (newest image, tracked before deleted, image before video), title and time ordering,
  page clamping, `detail_page` agreement with Session View, and the `session_index` filter's
  restriction to ChatGPT media. Route tests cover the card, link, toggle, sort-label, hidden-field,
  and pagination markup in every Media state. `test_chatgpt_media_sessions_e2e.py` drives a
  disposable browser through 26 sessions: the shown cover's natural width identifies the newest
  image. Landscape, 4:5, tall, and size-less covers must render uncropped: rendered ratio equal to
  the natural ratio, image as wide as its cover and inside it, and `object-fit: contain`. The grid is
  four columns whose covers and titles align across a row; the list uses a 112px column. The layout
  choice persists across reloads, and a sidebar filter, pagination, and dock round trip keep the
  index. Phone cases at 390 px in light and dark themes assert two uncropped grid columns, an 88px
  uncropped list column, and no document-level horizontal overflow. No test contacts ChatGPT.
- `test_chatgpt_flat_media.py`, `test_chatgpt_flat_media_web.py`, and
  `test_chatgpt_flat_media_e2e.py` cover the All sessions `All media` view: every
  session resource expands, exact bytes deduplicate globally before 24-item paging,
  stale catalog hashes cannot hide distinct files, and unreadable resources remain
  separate. Tests verify size-based read avoidance, bounded blocked-file handling,
  hash-cache invalidation, active-file precedence, filter
  boundaries, stored-byte preservation, accessible dock switching, previews, history
  navigation, mode persistence, and desktop/phone layouts with synthetic media only.
- `test_select_keyboard_e2e.py` opens the Local resources Media type and Sort order menus at
  1,280 and 390 px in light and dark themes and asserts an opaque theme-background base, a menu
  that covers the next field at its overlap, option hit targets, and no horizontal overflow. The
  Settings operating-system and catalog cases in the same file keep asserting the `62%` Period
  material for every other standard select.
- Local resources reader tests seed an isolated ChatGPT history and verify that LLM text renders as
  full-height conversation cards with wrapped source-link chips, a one-row header timestamp at
  1280, 715, and 390 px, and no document-level horizontal overflow, while Zhihu keeps its table.
- Computer Use Agent tests build context packages in temporary projects, execute the controller
  through deterministic actions, and replace the signed-in browser runner with a fake.
- Durable compute-job tests construct a temporary approval manifest whose entrypoint SHA-256
  matches a short fake optimizer. Source, config, checkpoint, progress, result, metadata, log, and
  process identities are asserted through public job operations; tests do not weaken the existing
  verification-only `run` contract.
- Style-token, template, and responsive-contract tests protect durable UI boundaries directly.
- Sidebar E2E tests start an isolated local Flask server and a clean headless Chromium context.
  They cover touch input, backdrop dismissal, viewport transitions, real hit testing through
  `document.elementFromPoint()`, and the shared Chinese language boundary across startup and
  dynamic DOM mutations. The language test checks source-text preservation, `:lang(zh-CN)`
  matching, and the macOS-oriented glyph fixture without converting Unicode text.
- Beta E2E tests intercept the Zhihu start/status/stop and local archive-read APIs on the isolated
  local origin. They exercise progress rendering, searchable summaries, exact-ID plain-text body
  readback, and request payloads, but must abort every external request and must
  not substitute a live provider response for a fixture.
- Agent Optimization tests validate manifest bounds, schema closure, read/write annotations,
  same-origin navigation, unsupported-browser fallback, iframe exclusion, registration idempotence,
  partial registration failure, protected-page exclusion, and a real random-port Chromium lifecycle.

The current detailed module-to-behavior map is maintained in [TEST_COVERAGE.md](TEST_COVERAGE.md).

## Isolation contract

`tests/conftest.py` runs before application test modules are imported. It redirects all default
runtime locations to process-scoped temporary directories:

- `HOME` keeps settings and browser-profile defaults away from the user account.
- `AGENTIC_CONTEXT_RUNTIME_ROOT` moves default local caches, the Beta archive, and logs away from the
  repository.
- `AGENTIC_CONTEXT_SETTINGS_PATH` redirects persisted settings.

Default tests must not:

- open an authenticated Chrome, Edge, Safari, or Playwright profile;
- make X, Zhihu, Grok, ChatGPT, yt-dlp, or general network requests;
- read, copy, delete, reset, or restore a user-owned cache, Beta archive, log, setting, or browser profile;
- submit a real background cache job.

Mock external boundaries at the module that invokes them. Existing patterns mock
`sync_playwright`, `launch_chromium_context`, `subprocess.run`, `urlopen`, the scraper, injected
Zhihu page fetches, and source download functions. Do not replace a lower-level implementation
when the route or service boundary is the behavior being tested.

## Markers

- `integration`: Flask tests that cross module boundaries and remain fully offline.
- `slow`: Tests materially slower than the unit-test median.
- `live`: Explicit manual checks that require a signed-in browser or a remote service. These never
  belong in CI or the default quality gate.

The sidebar E2E layer may use Playwright-managed Chromium or an installed Chrome or Edge binary,
but always launches a clean browser context without a user-data directory. Its Flask server uses
the same process-scoped temporary runtime as the rest of pytest. Browser tests must retain both
isolation properties and must never navigate to an external service.

## Writing a new test

1. Put the test beside the behavior it protects under `tests/test_<area>.py`.
2. Prefer a public behavior or invariant over assertions about incidental implementation details.
3. Use a temporary filesystem location for every test-owned file.
4. Use the `client` fixture for route contracts and preserve the injected runtime boundary.
5. Add a marker only when it accurately describes the test's cost or boundary.
6. Run the focused test, then `./scripts/check.sh` on macOS/Linux or `.\scripts\check.ps1` on
   Windows before handoff.

## Entrypoint and documentation checks

Both test wrappers select `not live` by default, honor the current marker override and its legacy
alias, disable bytecode and pytest cache output, and preserve pytest's exit code. Direct pytest
also defaults to `not live` through `pytest.ini`; an explicit `-m live` remains a manual opt-in.
Neither the default suite nor the default quality gate is a fast unit-only command: both include
disposable browser tests. For a shorter iteration, select a file or use
`AGENTIC_CONTEXT_TEST_MARK_EXPRESSION='not live and not slow' ./scripts/test.sh`.

Both quality gates reject absent or stale coverage after a nominally successful pytest command.
Run only one complete gate at a time in a checkout: the coverage database and JSON report are
shared under `test-results/`. Independent focused tests do not produce that report by default.
The Windows setup entrypoint stops at the first failed dependency command. Windows CI explicitly
selects the interpreter installed by its setup step, so a different launcher default cannot bypass
the minimum-version baseline.

Run the tooling regressions with:

```bash
./scripts/test.sh tests/test_test_entrypoints.py tests/test_windows_entrypoints.py tests/test_documentation.py
```

On Windows:

```powershell
.\scripts\test.ps1 tests/test_test_entrypoints.py tests/test_windows_entrypoints.py tests/test_documentation.py
```
 Native Windows cases are skipped on other hosts and must pass on Windows CI;
POSIX launcher cases are skipped on Windows. A skip is not platform execution evidence.

Run `python3 scripts/check_docs.py` on macOS/Linux or `py -3 scripts/check_docs.py` on Windows
with the prepared interpreter for the documentation-only check.
It parses Markdown links, reference links, images, and ordinary heading anchors, including repeated
headings. Fenced examples are not links. Remote URLs and references outside the checkout are not
validated; shared references are printed separately because sibling files are absent in clean CI.
The check does not claim to validate prose semantics, HTML anchors, or remote availability.

The resolver checks the Python minimum version and the modules required by its runtime, test, or
quality mode. Run the setup wrapper with the same interpreter used for tests. If the host default
lacks dependencies, the POSIX resolver tries unversioned platform installations; set
`AGENTIC_CONTEXT_PYTHON` only when a specific prepared Python 3.13 or newer executable is required.
A resolver pass does not establish compatibility of every future dependency release.

Pinned native packet clocks are covered by `tests/test_wechat_forwarded_clocks.py`,
`tests/test_wechat_forwarded_clock_integration.py`, and
`tests/test_wechat_forwarded_clock_exports.py`:

```bash
./scripts/test.sh tests/test_wechat_forwarded_clocks.py tests/test_wechat_forwarded_clock_integration.py tests/test_wechat_forwarded_clock_exports.py
```

These synthetic checks cover exact complete-packet child IDs, authors and bodies;
independent receiving accounts; verified direct participant pairs; strict ordinal
IDs; short and single-author opaque-ID packets; explicit nontext resource gaps;
missing HTML times; naive native clocks; contradictory clocks; and source mutation.
Indexed catalog cases check sole SQLite precedence over legacy JSON, per-target
failure isolation, archive-identity pins, bounded reads, and assessment-receipt gates
for strong packets crossing receiving context. A changed or invalid catalog must
not revive a legacy JSON link while the SQLite authority still exists.

Integration cases verify reversible contact-cache upgrades, contact CSV, and
completion trees whose positions differ from the source packet. Session export
cases exercise actual CSV and Markdown download routes, exact parent-body and
delivery-time preservation, full packet-child output, nested paths, preview
isolation, unknown child times, original-clock disclosure, and evidence withdrawal.
The session CSV schema remains `Time`, `Speaker ID`, `Statement`; child rows follow
their source parent rather than claiming a new globally sorted conversation.
Restored child times must never replace parent times or literal HTML source clocks,
and a naive child time must never acquire a guessed timezone.

The full census and assessment workflow is documented in
[Operations](OPERATIONS.md#recover-html-forwarded-child-clocks). Discovery hints are
not sufficient validation: assessment must compare all valid corresponding iPad
copies, retain explicit unresolved outcomes, and publish only against the same
archive identity. Production assessment counts measure source observations, not
deduplicated utterances or a test-suite pass count. A full scan or source publication
is an explicit operator action, never part of the ordinary synthetic test suite.

Dated browser, platform, and CI observations are preserved in [TEST_HISTORY.md](TEST_HISTORY.md).
Do not reuse those counts or version-specific commands as evidence for a new checkout.
