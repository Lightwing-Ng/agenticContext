# Safari Cache acceptance

Document version: `v2.0.0-codex.0`
Application version under acceptance: `v1.45.0` (module revisions listed below)
Reviewed: 22 Oct 2026
Current acceptance status: Partial; eight bounded selections passed, three require external prerequisites

## Current scope

The current acceptance matrix covers six providers and eleven supported selections,
with Safari explicitly selected: X, Grok, ChatGPT, Gemini, and Claude each support
Text and Media; Microsoft Copilot supports Text only. Gemini is included in the
current scope. Its current regional restriction is an acceptance blocker, not a
reason to remove its supported routes from the matrix.

Reads use the existing authenticated Safari session. Acceptance must not send AI
prompts, create provider content, or replace the user's signed-in session. Each
result must distinguish an authorized production-cache run from a disposable-store
probe, and retain separate evidence for authorization, discovery, persistence,
terminal task status, and owned-window cleanup. A ready account, a working route,
an existing counter, or a successful isolated test does not establish a complete
live cache run.

The current record below is the acceptance authority for 22 Oct 2026. The dated
historical section retains earlier evidence and limitations; its four-provider,
eight-selection scope, Gemini exclusion, application versions, counts, and prior
acceptance conclusions do not describe the current run.

## Current capability boundaries

| Provider | Text | Media |
| --- | --- | --- |
| X | Text from the signed-in account's Likes timeline | Images and videos from liked posts |
| Grok | Conversation history | Generated library images and videos |
| ChatGPT | Account conversation history | Original assistant-generated images; uploaded files and video are outside this collector |
| Gemini | Supported conversation history; currently blocked by the provider's regional restriction | Supported first-party generated raster images; currently blocked by the same regional restriction |
| Claude | Rendered conversation messages | Supported first-party raster images rendered in message bodies; other attachments and video are excluded |
| Microsoft Copilot | Paginated all-chats catalog and complete rendered text sessions | Not supported; excluded from the eleven-selection count |

## Current repair coverage

The following repairs are part of this acceptance round. Their presence in source
and isolated checks remains separate from served-runtime and real Safari proof.

- Shared Safari acquisition waits within a scoped 60-second deadline and observes
  cooperative Stop while another operation owns Safari. Child workers inherit the
  scope. Contention must not replay an entire sync, steal an active window, or
  become a false authentication failure; an expired acquisition remains a failure.
- ChatGPT original-image downloads carry Stop through metadata resolution, the
  original and refreshed byte streams, and retry waits. Interrupted partial bytes
  remain resumable and do not become complete catalog entries. Account-history
  discovery requires explicit catalog exhaustion; unproven continuation preserves
  verified partial sessions while reporting incomplete coverage in Text and the
  all-session Media path. Project API and DOM pagination also reject unread or
  stalled continuations, while an explicitly empty project can complete. Real
  account pages report `total` as a page-relative hint: offsets 0, 100, and 200
  returned totals 101, 201, and 301; rereading offset 0 returned 101 again. The
  collector therefore validates consecutive offset/limit readback and explicit
  exhaustion rather than requiring a fixed total. An empty page that still
  advertises additional sessions remains incomplete.
- Grok Text validates conversation identities and pagination evidence. Repeated
  cursors or sessions, malformed pages, unread continuations, empty pages with a
  continuation token, and the page limit cannot become successful exhaustion.
  Verified partial sessions remain cacheable, while incomplete discovery prevents
  a successful service outcome even when those sessions have no download errors.
- Claude Media traverses each discovered conversation's virtualized transcript,
  expands earlier messages, and stops immediately at human verification. Chats
  discovery reads and scrolls the main list, excludes sidebar shortcuts, resets
  to its top, and requires a stable end. The existing 12-move/45-second discovery
  bounds remain; reaching a bound without exhaustion preserves verified sessions
  and produces incomplete Text and Media outcomes. Zero eligible images alone
  cannot prove an authenticated image download.

Current module revisions: `safari_automation.py` v2.18.0,
`cache_service_support.py` v1.2.0, `service.py` v1.11.2,
`chatgpt_downloader.py` v1.52.6, `grok_history.py` v1.5.3,
`grok_history_service.py` v1.1.1, `claude_history.py` v1.4.1,
`claude_media.py` v1.2.2, `claude_history_service.py` v1.2.1, and
`copilot_history_service.py` v1.2.1 (all with the `-codex.0` suffix).

## Current observations: 22 Oct 2026

Account checks reached ready for X, Grok, ChatGPT, Claude, and Microsoft Copilot.
These five provider checks establish authorization readiness only. Gemini's DOM
explicitly reports that the service is unavailable in the current region; its
current account and collection acceptance is blocked before caching. This is not
an observed logout and does not authorize repeated login or challenge attempts.

The originally requested, bounded Grok Media job discovered 100 of 100 candidates
and reported complete discovery. One remote HTTP 404 correctly caused a failed
terminal task outcome. That observed failure must remain visible; complete
candidate discovery does not imply that every requested media item was cached.
A later isolated Media acceptance successfully downloaded one image and one video.
That bounded success does not resolve the original job's unavailable asset.

The initial Microsoft Copilot check exposed 17 sessions. The later isolated
acceptance exhausted two catalog pages and persisted all 18 currently listed
sessions and 84 messages with no failures. A preserved legacy Edge
catalog contains 357 sessions whose transcript coverage has not been reconciled.
Exhausting the current Safari catalog does not establish a complete backup of
those legacy sessions. Production legacy coverage remains unreconciled; all
existing records were retained.

## Current live acceptance matrix

Results below separate bounded functional acceptance from account-wide backup
coverage. A Blocked entry records an observed provider restriction. A successful
traversal with zero eligible images does not establish a media transfer.

| Provider | Mode | Current readiness | Final live result | Required evidence or unresolved boundary |
| --- | --- | --- | --- | --- |
| X | Text | Ready | Passed, bounded | 92 liked posts persisted; finished with zero failures; user windows preserved and lease idle. An earlier two-round probe returned no URLs; the final eight-round rendered-page read succeeded. |
| X | Media | Ready | Passed, bounded | 96 post URLs discovered; one original image and two videos downloaded within the three-file budget. Original and thumbnail images decoded; both videos validated with ffprobe. Zero failures; user windows preserved and lease idle. |
| Grok | Text | Ready | Passed, bounded | Current non-project catalog returned 196 sessions; one selected session persisted 30 messages. Zero failures; user windows preserved and lease idle. |
| Grok | Media | Ready | Passed, bounded; production job partial | One image and one video downloaded and decoded from 100 visible candidates. The original 100-candidate production job retains its one remote HTTP 404 failure. User windows preserved and lease idle. |
| ChatGPT | Text | Ready | Passed, bounded persistence | Current conversation catalog exhausted at 3,135 sessions and an explicit empty page. One selected session and its message persisted; finished with zero failures. User windows preserved and lease idle. Existing production history remains separate. |
| ChatGPT | Media | Ready | Passed, supplied session | One existing session yielded 14 image references and seven deduplicated original PNG files. All seven decoded; finished with zero failures; user windows preserved and lease idle. |
| Gemini | Text | Blocked: provider regional restriction | Blocked | Real worker failed explicitly with the regional restriction before discovery; zero writes; user windows preserved and lease idle. |
| Gemini | Media | Blocked: provider regional restriction | Blocked | Real worker failed explicitly with the same regional restriction before discovery; zero writes; user windows preserved and lease idle. |
| Claude | Text | Ready | Passed, bounded | Final main-list discovery returned 12 sessions; one selected session persisted eight messages. Zero failures; user windows preserved and lease idle. |
| Claude | Media | Ready | Traversal passed; transfer unverified | Final scanner exhausted the current 12-session main list and inspected all 12 sessions. Zero eligible body images and zero failures; a supported existing image sample is still required. User windows preserved and lease idle. |
| Microsoft Copilot | Text | Ready | Passed, current catalog | Two catalog pages exhausted; all 18 sessions and 84 messages persisted. Zero failures; user windows preserved and lease idle. The 357-session legacy Edge catalog remains unreconciled in production. |

## Current verification and handoff status

| Verification layer | Result | Evidence and limitation |
| --- | --- | --- |
| Final consolidated Safari/provider regression | Passed | 1,125 tests and 10 subtests across 36 files; final product/test source unchanged during the run. Includes worker admission/Stop, browser probes, queues/concurrency, provider parsing/persistence, and Claude DOM regressions in isolated Chromium. |
| Static and JavaScript checks | Passed | Full Ruff; 30 documents with zero errors and three separately excluded shared references; syntax checks for 40 JavaScript files; 45 Node tests; final diff whitespace check. |
| Complete repository quality gate in the host environment | Failed at environment checks | Existing Streamlit 1.46.1 requires Pillow below 12, while the runtime has Pillow 12.3.0. The shared interpreter was not changed. |
| Complete repository quality gate in a clean environment | Failed at dependency audit | Clean Python 3.13 venv passes pip check. pip-audit reports 24 non-withdrawn Pillow 12.3.0 advisories; official PyPI/OSV metadata agrees. Fixed version 13.0.0 is excluded by current requirements. Audit was not skipped and the dependency contract was not changed. |
| Broad non-E2E Python selection | Failed | 4,705 passed, five failed, 23 skipped, two deselected, and 871 subtests passed. Combined line/branch coverage is 76.47%; branch coverage alone is 67.39%. The combined measure exceeds the 55% gate threshold, but the test selection is not green. |
| Complete Python selection | Interrupted | After 569 completed cases, two tunnel restoration assertions had failed. Both pass alone and with a short preceding workspace-switch sequence; the longer sequence cause remains unresolved. No retries, assertions, timeouts, or product behavior were changed to conceal this. |
| Served-runtime adoption | Verified | Python 3.13 serving child PID 30787 started on 22 Oct 2026 at 21:23:41 UTC+08, after the final Python edit. All 11 mode-specific status endpoints report v1.45.0. All 11 Safari pages return HTTP 200 with matching source/mode/browser. Served cache-page, browser-session-status, and browser-session-picker JavaScript bytes match the workspace. |
| Production history preservation | Verified | Final SHA-256 readback matches the initial files for all six canonical histories: X 2,512 rows; Grok 1,841; ChatGPT 83,374; Gemini 10,436; Claude 322; Copilot 74. Acceptance writes use isolated stores. |
| Numbered-copy housekeeping | Passed with retained conflicts | 558 candidates: 556 protected local_store/logs entries and two different-content source copies. All retained. No recoverable cleanup was applicable; no file was deleted. |

The five non-E2E failures reproduce at exact nodes in untouched Web facade imports
(`form_config.py` to `config`, `presentation.py` to `chatgpt_display`) and old asset
version assertions (`sidebar`, `style`, and `cache-page`). These failures remain
visible and are outside the backend Safari repair. A passing focused suite does
not imply a green complete repository gate.

Commands used the clean interpreter at
`/tmp/agenticcontext-quality-20261022-6l67yylq/venv/bin/python` with `TZ=UTC`.
The broad selection used `scripts/test.sh --ignore-glob='*e2e*'` and the normal
`not live` marker, with branch coverage enabled and the 55% threshold retained.
The full `scripts/check.sh` was attempted in both environments. The final focused
selection and exact failures are retained in that evidence directory alongside
`pytest-final-safari.log`, `non-e2e-summary.json`, `source-drift.json`, and
`audit-verification.json`. None of those isolated browser checks used Safari or a
user browser profile; real Safari evidence is recorded separately above.

This acceptance round's backend repairs touch `app/core/safari_automation.py`,
`cache_service_support.py`, `service.py`, `chatgpt_downloader.py`, `grok_history.py`,
`grok_history_service.py`, `claude_history.py`, `claude_media.py`,
`claude_history_service.py`, and `copilot_history_service.py`, their focused tests,
and this document. Existing unrelated dirty work is retained. No commit or sibling
project edit was made. The numbered source copies `app/core/downloader 2.py` and
`tests/test_downloader 2.py` have different content from their primaries and no open
handles at the audit; their contents are preserved for review.

Eight of eleven supported selections now have bounded real functional success
evidence. Acceptance remains partial: Gemini Text and Media are region-blocked,
and Claude Media still lacks a supported real image sample. Current-catalog success also does not reconcile
Copilot's legacy catalog or restore the unavailable Grok video.

Final native-state limitation: after all cache probes completed, both X and Grok
account refreshes returned ready. A subsequent extra Safari window inventory read
timed out. Computer Use then explicitly reported that macOS was locked and could
not be automatically unlocked. Further UI reads stopped and manual unlock was
requested. The per-worker preserved-window readbacks above remain valid; no final
post-lock window snapshot is claimed. The ownership journal is idle and all six
production history hashes still match. The original user window was not closed.

## Historical record: 26 Sep 2026

Historical document version: `v1.1.8-codex.0`
Historical application version: `v1.33.2`
Historical review date: 26 Sep 2026

Everything in this section records the earlier acceptance round. Its scope and
results are retained for provenance and are not current acceptance evidence for
22 Oct 2026.

### Scope

The requested matrix is ChatGPT, X, Grok, and Claude, each in Text and Media mode,
with Safari explicitly selected. Gemini is excluded. Provider reads use the
existing authenticated Safari session; probes must not send prompts or modify
provider content. Bounded acceptance writes use disposable local stores rather
than modifying the production cache.

### Changes

- X now exposes both modes, dispatches Text without a media download, and reads
  persisted text counters independently of media counters.
- X accepts the current `/i/history/likes` route only after checking the signed-in
  profile and selected Likes navigation. Safari Media binds photos and video
  presence to the primary rendered post, excluding quoted-post media.
- Original X photos have a bounded, validated download path. A durable completion
  marker separates partial progress from a complete post; completed video bindings
  survive interruption even when yt-dlp uses a media ID instead of the status ID.
  A temporary download archive prevents those completed IDs from consuming the
  next bounded retry's budget; replayed output does not double-count old files.
- Changing the source preserves the current content mode and supported browser,
  including after an in-page mode change.
- Cache exposes a manual Recheck when account verification fails, without
  automatically reopening or retrying a challenge. Safari verification messages
  correctly explain that the temporary check window has already been closed.
- Incomplete Grok response payloads preserve existing history. Partial Text or
  Media failures are reported as failures instead of successful completion.
- Safari ChatGPT media size limits are classified as configured skips.
- The completion audit found and corrected the same contract violation in Claude
  Media: configured size skips no longer retry or increment failure counters.
  Empty responses and real transfer failures still fail, existing files are
  preserved, and the skipped-image metric no longer labels every skip as known.
- Safari task-window reuse waits for an addressable tab before navigation and
  checks durable ownership before any recovery action. Navigation is ready only
  when the native address and the current DOM address agree.

### Capability boundaries

| Provider | Text | Media |
| --- | --- | --- |
| ChatGPT | Account conversation history | Original assistant-generated images; uploaded files and video are outside this collector |
| X | Text from the signed-in account's likes timeline | Images and videos from liked posts |
| Grok | Conversation history | Generated library images and videos |
| Claude | Rendered conversation messages | First-party raster images rendered in message bodies; other attachments and video are excluded |

### Verification record

Route and frontend acceptance covers all eight selections. Browser checks cover
914 × 791, 390 × 844 with touch, and 914 × 500 with touch, including source/mode
switching, preserved Safari selection, X counter visibility, and document overflow.

The combined provider, worker, route, and Safari suite passed 723 tests and
605 subtests after the Claude size-limit correction. The final
frontend selection passed nine browser tests in UTC: three Safari selection
viewports, four source-switching cases with session storage enabled or denied,
and two manual-verification recovery cases. These are focused results, not
certification of the complete repository quality gate.
The recovery cases verify that a cached verification requirement does
not cause a new probe on reload and that a manual Recheck sends exactly one
refresh request. Two lifecycle tests cover both Safari challenge exits.
One combined run exposed a short/touch test-readiness race. A controlled 750 ms
script delay reproduced menu clicks before deferred handlers were bound. The
test now installs its account stub before navigation, waits for DOMContentLoaded
after each provider change, and asserts the menu expanded before selection.
The final nine-case rerun passed without changes to timeouts, retries, or CSS.
The final 39-case X photo/video regression run, Ruff, JavaScript syntax,
documentation-link checks (29 documents, zero errors), and `git diff --check`
also passed. Three sibling-document references are reported separately by the
documentation checker. No native Windows run or full quality-gate result is claimed.

#### Real Safari probes

Claude Text discovery follow-up, 26 Sep 2026: the production job started at
16:40:26 HKT and failed before discovering any sessions. The production history
remained at one session and three messages last written on 6 Sep 2026. A
task-owned Safari tab reproduced an initially empty Chats document followed by
seven visible conversation links and seven main-list rows. The previous
discovery implementation waited 500 ms and stopped at its first unmoving scroll,
which could treat that initial document shell as an empty completed list.

`claude_history.py` v1.3.0-codex.0 now waits for visible main-list content, respects
loading indicators, and requires stable observations after scrolling. It uses a
monotonic deadline and cooperative cancellation; login and verification states
stop discovery. Claude Media v1.1.2-codex.0 passes the same cancellation callback.
The original link selectors and 12-scroll bound remain, so this fix does not
establish exhaustive account coverage. Thirty-nine focused tests passed,
including three real Chromium document-hydration regressions; Ruff and diff
checks passed. The diagnostic Safari tab was closed and the original four tabs
and active tab were restored.

The user restarted the service in Terminal. Listener PID 90055 started from this
repository at 16:53:06 HKT, after the discovery repair. Reloading the Claude Text
Safari page resolved a previous busy-check error and returned Signed in. Start
was activated on that page, and production job `80b14e187f8a` ran from 16:57:59
to 16:58:52 HKT on 26 Sep 2026. It inspected seven sessions and 37 messages,
updated five message rows, retained 32 unchanged messages, skipped three
unchanged sessions, and finished with zero failures. The official
`local_store/llm/claude/history.parquet` was atomically rewritten at 16:58:49 HKT
and contains seven distinct sessions and 37 rows. The live page displayed
Finished, 100%, Signed in, and an enabled Start button. This establishes an actual
production cache run through the requested UI with the repaired worker.

Claude session recency follow-up, 26 Sep 2026: some rendered messages omit source
timestamps, so their persisted `last_seen_at` equals their `first_seen_at` capture
time. Treating those values as conversation activity ranked older conversations
by collection order. `chat_history_browser.py` v1.20.1-codex.0 now applies the
existing ChatGPT capture-time exclusion to Claude as well. Sessions use their
latest available source message time; sessions without a known source time remain
last in both date sort directions. No production Parquet rewrite is needed.
Eighteen history-browser tests passed, including two regressions covering both
sort directions and adjacent-session navigation. An isolated Flask render using
a temporary copy of the seven-session production history confirmed that the
conversation with a latest message on 24 Sep 2026 is first under Newest first and
last under Oldest first, with all seven message bodies retained. After the user
restarted port 8666, listener PID 96122 began at 17:09:53 HKT. A subsequent live
Local resources readback confirmed that conversation is first in Newest first,
and its detail page has no previous session. The recency repair is adopted.

The earlier bounded probe writes below were isolated from the production cache. These reads do
not certify a complete account crawl or every supported file type.

| Provider | Text evidence | Media evidence |
| --- | --- | --- |
| ChatGPT | Three conversations, 13 persisted messages | Three live image-index candidates; one original downloaded, 1,469,531 bytes |
| Grok | One conversation, two persisted messages | 49 visible candidates; one asset downloaded, 2,040,233 bytes |
| Claude | Seven currently listed conversations, 37 persisted messages | No supported body-image candidates in those conversations; real download remains unverified |
| X | 16 liked posts persisted from the current authenticated Likes route | One original photo downloaded, 255,505 bytes; a separate planned-video run downloaded one video and its thumbnail, totaling 11,379,522 bytes |

All completed probes closed their owned contexts. X probes preserved the original
Safari window and its four tabs, leaving no task-owned window behind. X original
photos have both automated coverage and a real authenticated-page sample.
Existing production cache counters are not evidence of a new successful collection.

Live adoption of v1.33.1 is verified. The user restarted the service in native
Terminal after the UI tool refused Terminal control. Listener PID 69492 runs
Python 3.13 from this repository and started after the final production edits;
all eight provider/mode snapshots report `v1.33.1` and idle status.
All eight Safari pages return successfully, and the cache-busted `cache-page.js`
response matches the workspace SHA-256. Browser interaction confirmed X Text/Media
counter switching and preserved Safari/Media when navigating from X to Grok;
Grok Text, Claude Text/Media, and ChatGPT Text/Media also select their corresponding
route and counters. The X, Grok, and Claude account checks reached their verified
state with Start enabled.

An earlier final ChatGPT account check reported a human-verification requirement
after successful Text and image probes. Start was disabled, and automated retries
stopped. During the subsequent completion audit, the existing Safari ChatGPT tab
displayed its signed-in application and composer without a verification page.
One fresh Cache account check then returned Signed in. Both Text and Media showed
Start enabled with Safari selected. The earlier readiness blocker is resolved;
the prior bounded collection evidence remains separate from this account check.

The final Recheck recovery and Safari message corrections are v1.33.1. Their
isolated desktop and narrow-browser checks pass. While verification was pending,
reloading the live Cache page retained its cached requirement and showed Recheck
with Start disabled, preserving the no-retry rule.

#### Completion audit

The task is not fully accepted yet. Seven of eight requested provider/mode
combinations have bounded real Safari success evidence. Claude Media still needs
one existing, supported message-body image to verify an actual authenticated
download. The user confirmed that no such conversation is currently available.
Zero discovered images is not a successful download result. The full
repository quality gate has not been run; the focused checks above are the only
claimed automated verification. This audit reread all eight idle status endpoints
at v1.33.1 and confirmed the served JavaScript matches the workspace.

The audit also found that Claude's configured size limit was treated as a failed
download. The v1.33.2 correction has typed size skips, separate skip counts, final
service-state coverage, and preservation checks for already cached images. Its
three-file Claude regression group passed 41 tests. Live
adoption of that final patch is pending a v1.33.2 listener readback.

### Changed components

- Source and mode selection: `app/web/cache_sources.py`, `app/web/cache_routes.py`,
  `app/web/static/cache-page.js`, `app/web/templates/_cache_page.html`, and
  `app/web/templates/index.html`.
- Account recovery: `app/web/templates/_browser_session_status.html`,
  `app/web/templates/_cache_page_components.html`, and `app/core/browser_sessions.py`.
- Claude size-limit handling: `app/core/claude_media.py`,
  `app/core/claude_history_service.py`, and `app/web/templates/claude.html`.
- X collection and state: `app/core/scraper.py`, `app/core/service.py`, and
  `app/core/state.py`.
- X media persistence: `app/core/x_photo_downloader.py`, `app/core/downloader.py`,
  and `app/core/cache_catalog.py`.
- Safari navigation and task ownership: `app/core/safari_automation.py`.
- Provider preservation and completion reporting: `app/core/chatgpt_downloader.py`,
  `app/core/chatgpt_service.py`, `app/core/grok_history.py`,
  `app/core/grok_history_service.py`, `app/core/grok_service.py`,
  `app/core/claude_history.py`, and `app/core/claude_history_service.py`.
- Application release identifier: `app/core/version.py`.
- Regression coverage: `tests/test_cache_safari_modes.py`,
  `tests/test_cache_source_switching_e2e.py`,
  `tests/test_cache_media_completion.py`, `tests/test_x_cache_modes.py`,
  `tests/test_x_safari_likes_routes.py`, `tests/test_x_media_collection.py`,
  `tests/test_x_photo_downloader.py`,
  `tests/test_safari_idle_recovery.py`, `tests/test_safari_navigation_readiness.py`,
  and the existing provider, Safari, service, route, and sidebar test modules.
- Shared applicability record: `../shared_docs/SHARED_UI_SYNC.md`.

### Housekeeping and scope

Preserve authenticated windows, browser profiles, and production caches. Numbered
copies in those protected locations are not cleanup targets. No sibling project
source, user-owned service, or Git commit is modified by a focused test run.
The final numbered-copy scan found 519 regular files and four symlinks, all under
the protected Edge profile in `local_store/`, and zero candidates in
`../shared_docs`. All protected entries were retained; no cleanup was performed.
Worthward and neoMe applicability remains Pending in the shared ledger; their
source files were not changed.
