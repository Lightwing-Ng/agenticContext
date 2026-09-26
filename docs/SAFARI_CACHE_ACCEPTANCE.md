# Safari Cache acceptance

Document version: `v1.1.8-codex.0`
Application version: `v1.33.2`
Reviewed: 26 Sep 2026

## Scope

The requested matrix is ChatGPT, X, Grok, and Claude, each in Text and Media mode,
with Safari explicitly selected. Gemini is excluded. Provider reads use the
existing authenticated Safari session; probes must not send prompts or modify
provider content. Bounded acceptance writes use disposable local stores rather
than modifying the production cache.

## Changes

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

## Capability boundaries

| Provider | Text | Media |
| --- | --- | --- |
| ChatGPT | Account conversation history | Original assistant-generated images; uploaded files and video are outside this collector |
| X | Text from the signed-in account's likes timeline | Images and videos from liked posts |
| Grok | Conversation history | Generated library images and videos |
| Claude | Rendered conversation messages | First-party raster images rendered in message bodies; other attachments and video are excluded |

## Verification record

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

### Real Safari probes

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

### Completion audit

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

## Changed components

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

## Housekeeping and scope

Preserve authenticated windows, browser profiles, and production caches. Numbered
copies in those protected locations are not cleanup targets. No sibling project
source, user-owned service, or Git commit is modified by a focused test run.
The final numbered-copy scan found 519 regular files and four symlinks, all under
the protected Edge profile in `local_store/`, and zero candidates in
`../shared_docs`. All protected entries were retained; no cleanup was performed.
Worthward and neoMe applicability remains Pending in the shared ledger; their
source files were not changed.
