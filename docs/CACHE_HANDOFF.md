# Cache handoff and operating runbook

Documentation version: `v1.10.0-codex.1`

This is the authoritative handoff document for the second Dock item, `Cache`.
Read it before changing Cache routes, source switching, Text/Media behavior, local
history persistence, or Grok synchronization. The third Dock item is `Local resources`;
it is the read-only review surface for cached media and text and must not be confused
with the Cache execution surface.

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
| Grok | `/cache/grok` | Grok media assets | Grok sessions and messages |
| ChatGPT | `/cache/chatgpt` | ChatGPT images | ChatGPT sessions and messages |
| Gemini | `/cache/gemini` | Gemini sessions and messages | Gemini sessions and messages |
| Claude | `/cache/claude` | Claude sessions and messages | Claude sessions and messages |
| Zhihu | `/cache/zhihu` | Upvoted answers or one answerer's answers | One text session per answer plus source links |

The historical top-level paths `/grok`, `/chatgpt`, `/gemini`, `/claude`, and `/zhihu` are compatibility
redirects. Do not introduce new links to them. The legacy `/chatgpt` plan is not a
separate Agent route; it resolves into the Cache namespace.

`Local resources` owns `/browser`. It is the review and export surface, not the place
where a source sync is started. Its text source filter accepts `all`, `chatgpt`,
`claude`, `gemini`, `grok`, and `zhihu`.

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
  Grok Text displays session/message counts from its history worker and Parquet file,
  independently of Media asset counters. Responses for a previously selected mode
  are discarded after a mode change.
- The `all` source view is an aggregate view. It must include the ChatGPT, Gemini, Grok,
  Claude, and Zhihu history files.

The control is shared markup in `app/web/templates/_cache_page.html`, behavior in
`app/web/static/cache-page.js`, and styling in `app/web/static/style.css`. Changes to
this component also require reading `../SHARED_UI_SYNC.md` and
updating its `Cache-page Text/Media control` row. This repository is currently the
leading implementation and the sibling remains `Pending`.

## 3. Runtime split: media versus text

ChatGPT Text writes `local_store/llm/chatgpt/history.parquet`, independent of the
Media project directory. Schema v2 adds nullable `provider_revision` metadata.
Discovery captures `update_time`; only an equal non-empty persisted revision with
complete source timestamps permits skipping. Missing/changed revisions trigger a
fresh mapping request. Older schema files remain readable and refresh conservatively.
Text status (`?content_mode=text`) hydrates sessions/messages from this file without
opening the media catalog. Failed or rate-limited mapping fetches and empty discovery results are reported
as incomplete instead of a complete successful synchronization. Existing files in the former
`local_store/media/llm/chatgpt` location are preserved; no automatic migration occurs.
After a cooperative stop, unattempted sessions are pending rather than failed.
The failure counter includes only attempted mapping requests that failed or were
rate-limited; incomplete discovery and deferred work remain incomplete.

Grok has two independent runtimes. This split is intentional:

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
6. Persist the normalized rows atomically to the Grok Text Parquet file.

The response-node tree is important. A DOM scroll only exposes a partial, selected
branch and is not evidence that all history was discovered. The API list is paginated,
and `load-responses` is required to recover the complete text payload.

Claude history is a separate Cache runtime. It opens the authenticated `/chats` page,
discovers conversation links from rendered DOM, opens each conversation, and stores only
rendered user/assistant message content, source links, and visible model labels. It does
not call private Claude endpoints, send prompts, or mutate the provider page. The runtime
uses `POST /cache/claude/start`, `POST /cache/claude/stop`, and
`GET /api/cache/claude/status` (with compatibility alias `GET /api/claude/status`).

Zhihu is a text-only Cache runtime and defaults to Edge. A blank `Answerer URL` first verifies the
selected browser through `GET https://www.zhihu.com/api/v4/me`, then follows the signed-in
account's paginated activity feed and retains only `MEMBER_VOTEUP_ANSWER` targets. An optional
validated `https://www.zhihu.com/people/<token>` URL switches to complete answerer pagination.
Both modes deduplicate by answer ID and recheck the newest page before committing. Rows are merged
cumulatively, so caching another answerer does not remove earlier answers. Local resources renders
the complete normalized answer text without its generic message-collapse limit. A Zhihu-only
session list omits the redundant Source column, answer IDs, Projects metric, and Clear filters
action. Its standard Answerer select is populated from the cached author labels and scopes metrics,
sessions, search, ordering, and pagination to the selected name. The index shows each literal
answerer in its Answerer column, while a one-answer detail table uses that name instead of the
generic Role heading. The original answer, question, answerer, embedded anchor, and remote image
URLs are stored in `source_links`. Provider HTML is empty and remote media is never mounted.

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
| `local_store/llm/chatgpt/history.parquet` | ChatGPT Text runtime | Typed ChatGPT messages |
| `local_store/llm/gemini/history.parquet` | Gemini Text runtime | Typed Gemini messages |
| `local_store/llm/grok/history.parquet` | Grok Text runtime | Typed Grok messages |
| `local_store/llm/claude/history.parquet` | Claude Text runtime | Typed Claude messages |
| `local_store/llm/zhihu/history.parquet` | Zhihu Text runtime | One answer per typed row; provider HTML omitted and source links retained |
| `local_store/prompt/prompts.parquet` | Prompt manager | Saved prompt content snapshots plus source-message pointers |
| `local_store/.cache_task.lock` | Cache runtimes | Cross-process advisory task lock |
| `logs/cachelikes.log.jsonl` | All runtimes | Structured diagnostics |

The five formal text-history files use the same logical fields, while each provider runtime has
its own source-specific schema constant in `app/core/resource_persistence.py`. Do not
silently point one provider at another provider's file merely because all filenames
are `history.parquet`.

Grok Text rows contain a stable `message_key` formed as
`<conversation-id>:<response-id>`. The store replaces one conversation at a time,
preserves `first_seen_at`, updates `last_seen_at`, and writes through an atomic
temporary Parquet file. Do not edit the Parquet file by hand during a sync.

## 6. Verified Grok baseline

On `13 Aug 2026`, a live Edge-authenticated run completed through the Text pipeline:

- API conversations discovered: `166`
- Non-empty cached sessions: `165`
- Cached messages: `1,537`
- User messages: `808`
- Assistant messages: `729`
- Failed sessions: `0`
- Empty sessions: `1`
- Output: `local_store/llm/grok/history.parquet`

The Local resources page was then opened with `source=grok` and showed `1,537`
messages and `165` sessions. This baseline is a diagnostic reference, not a hardcoded
expectation: Grok history changes whenever new conversations are created or removed.

## 7. Safari cache-window contract

Safari-backed Cache tasks may own exactly one temporary Safari window at a time, and Safari
is never the default browser for a new Gemini task. The window must remain a standard visible
window with its native close, minimize, and full screen controls. It may stay behind the user's
foreground window, but must never be hidden, minimized, moved offscreen, or converted into a
reusable blank window shell. The shared Safari context captures and restores the user's
frontmost application whenever it changes Safari window state.

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

The Edge session probe must report `logged_in: true`. The media and Text runtimes must
be idle before starting another task. Only one cache task may hold
`local_store/.cache_task.lock` across the entire application.
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

from app.core.resource_persistence import read_parquet_rows

path = Path("local_store/llm/grok/history.parquet")
rows = read_parquet_rows(path) or []
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

from app.core.resource_persistence import read_parquet_rows

path = Path('local_store/llm/grok/history.parquet')
rows = read_parquet_rows(path) or []
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
Then check the Parquet file directly. If the file has rows but the page is empty,
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

Query all source status endpoints and inspect the lock metadata:

```bash
cat local_store/.cache_task.lock
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
- `app/core/grok_history.py`: Grok API traversal, normalization, and Parquet persistence.
- `app/core/grok_history_service.py`: worker lifecycle and shared task lock.
- `app/core/claude_history.py`: rendered Claude discovery, extraction, and Parquet persistence.
- `app/core/claude_history_service.py`: Claude worker lifecycle and shared task lock.
- `app/core/chat_history_browser.py`: source path mapping and Local resources queries.
- `app/core/resource_persistence.py`: typed Parquet schemas and atomic writes.
- `tests/test_grok_history.py`: deterministic Grok Text regression coverage.
- `../SHARED_UI_SYNC.md`: shared-component synchronization ledger.

When adding a new LLM source, update the source allowlist, source-specific path map,
aggregate `all` path tuple, source switcher, Cache Text link, persistence schema, status
runtime, and focused tests together. A source that appears in a dropdown but is absent
from one of those contracts is an incomplete integration.
