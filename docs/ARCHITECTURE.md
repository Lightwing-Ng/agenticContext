# Architecture guide

Documentation version: `v1.14.1-codex.1`

## Runtime flow

```text
scripts/run_app.sh or scripts/run_app.ps1
  -> Python 3.13 or newer runtime resolution
  -> main.py
  -> structured logging setup
  -> app.web.app.create_app()
  -> Flask routes and static UI
  -> app.core services, browser sessions, downloaders, and local catalogs
```

`main.py` is the only supported application entrypoint. The shell runtime resolver accepts Python
3.13 or newer and the module itself remains runtime-agnostic before Flask is imported.
`create_app()` builds independent state containers for the X, Grok, and ChatGPT workflows,
registers the local-media browser, and serves the Flask routes.

## Core package boundary

The application layer imports `app.core` through five domain façades instead of reaching into
every implementation module:

- `app/core/foundation/`: runtime configuration, logging, version, and task state.
- `app/core/compute_resources.py`, `app/core/compute_backend.py`,
  `app/core/compute_metrics.py`, and `app/core/compute_queue.py`: bounded local
  compute budgets, pure image-analysis execution, privacy-safe stage metrics, and
  deterministic backpressure/result publication. These modules do not receive
  browser contexts, authentication data, task locks, catalogs, or open file handles.
- `app/core/browser/`: browser descriptors, session probes, and provider-neutral X page identity.
- `app/core/storage/`: local media, chat history, and shadow-backup operations.
- `app/core/providers/`: X, Gemini, Grok, and ChatGPT workflows.
- `app/core/agent/`: Agent access control, source discovery, and Computer Use orchestration.

The original flat modules remain import-compatible during this migration. New application-layer
code should depend on the domain façades; provider and storage implementations may continue to
use their existing compatibility imports until each slice is moved. `browser.x_session` is a leaf
module: it owns X identity/readiness helpers so `browser_sessions` no longer needs an implicit
runtime import back into `scraper`.

The intended dependency direction is:

```text
app.web
  -> core domain façades
     -> core implementations
        -> foundation, browser leaves, and storage primitives
```

Core modules must not import `app.web`, templates, or frontend JavaScript. A domain façade should
export only the symbols needed by its caller and should not become a second implementation file.

## Application layers

- `app/core/config.py`: runtime defaults, persisted settings, paths, and input normalization.
- `app/core/state.py`: thread-safe task snapshots and cache-summary hydration.
- `app/core/service.py`: X Likes collection and yt-dlp orchestration.
- `app/core/grok_service.py` and `app/core/chatgpt_service.py`: background sync lifecycle,
  stop signaling, and shared cache-task exclusion.
- `app/core/grok_history.py` and `app/core/grok_history_service.py`: authenticated Grok Text
  API traversal, normalized message persistence, and the independent Grok Text worker.
- `app/core/scraper.py` and `app/core/browser_sessions.py`: X timeline discovery and browser
  session probing for Chrome, Edge, and Safari.
- `app/core/downloader.py`, `app/core/grok_downloader.py`, and
  `app/core/chatgpt_downloader.py`: source-specific cache acquisition, recovery state, and
  content validation.
- `app/core/chatgpt_agent_sources.py`: authenticated browser-mediated catalogs of the 20 most
  recent root ChatGPT sessions, projects, and project sessions for the Agent sidebar. Its Agent
  bootstrap combines ChatGPT readiness and root-catalog collection in one browser context.
- `app/core/agent_session_sources.py`: the provider-neutral Agent session and Project adapter;
  it maps ChatGPT Projects, Gemini Notebooks, Grok Projects, and Claude Projects into one URL and
  source contract. Claude source discovery reads rendered links only and shares the Chromium launch
  and Parquet cache boundary.
- `app/core/agent_source_cache.py`: the shared typed Parquet catalog for Agent recent sessions,
  Projects, and Project sessions. Its cache key isolates provider, browser, profile identity,
  source kind, and Project URL, while atomic replacement preserves the other providers' entries.
- `app/core/agent_access_security.py`: the Agent password resolver, constant-time password
  comparison, and loopback/private-network request boundary.
- `app/core/computer_use_agent.py`: selected ChatGPT, Gemini, Grok, or Claude Web session targets,
  runtime-discovered ChatGPT effort selection, bounded context packages, the local JSON action
  protocol, anchored read-receipt deletion, project path confinement, command policy, confirmed
  interrupted-session continuation, and mandatory bodycheck ordering for the optional Agent workspace.
- `app/core/cache_catalog.py` and `app/core/local_media_browser.py`: durable local indexes,
  media discovery, secure path resolution, deletion tombstones, and restoration.
- `app/core/logging_setup.py`: process-wide JSON-line logging.
- `app/web/`: Flask routes, templates, style tokens, and first-party browser JavaScript.

Web routes may orchestrate core services and present serialized state. Core modules must not
depend on templates or browser DOM details. Source-specific automation belongs at a browser or
transport boundary, while durable cache and state rules stay in core modules.

## OpenAI Site tools and Agent Optimization boundary

The top-level human pages include `_agent_optimization.html` through the shared sidebar bootstrap.
The project adapter renders one registry-derived, project-convention manifest; the byte-identical
shared runtime in `app/web/static/agent-optimization.js` validates the manifest and conditionally
registers three OpenAI Site tools through `document.modelContext.registerTool`.

The v1 tools expose only bounded capability metadata, bounded current-page metadata, and navigation
to a manifest-owned same-origin route. They do not read cached records or settings values, invoke a
Cache lifecycle route, submit an Agent prompt, authorize a terminal command, or mutate persisted
data. The Agent password unlock template is outside the registration surface. If the WebMCP API is
absent or the page is inside an iframe, registration is a no-op and the normal UI remains complete.

The cross-project naming, schema, result envelope, effects, security, evaluation, and promotion
rules live in `/Users/lightwing/Desktop/SHARED_AGENT_OPTIMIZATION.md`. Project-specific routes and
evidence live in [AGENT_OPTIMIZATION.md](AGENT_OPTIMIZATION.md).

## Agent capability and recovery boundary

`app/core/agent/capability_registry.py` is the single application registry for Agent Actions,
including their schemas and controller handler identifiers, bounded page observations, WebMCP
tools, and human-page navigation. Core execution resolves controller actions and observation names
through that registry, while the manifest adapter derives both public groups and WebMCP definitions
from it. This keeps browser discovery, local controller dispatch, and page-observation naming from
drifting into separate lists without granting WebMCP direct access to the Agent control plane.

`app/core/agent/event_chain.py` owns the durable run-local event chain. The Agent service creates a
new `run_id`, persists `run.started`, and appends ordered action, observation, verification,
bodycheck, lifecycle/page observations, interruption, recovery, and terminal events. It stores
bounded metadata rather than prompt, provider response, source, command, or page content.
`ComputerUseAgentService.doctor()` and the `/api/agent/doctor` routes consume the same chain summary
to offer an event timeline and explicit recovery actions. Recovery never retries the original
external prompt implicitly; a user-selected continuation may send only the fixed continuation
request after a persisted conversation-binding proof succeeds.

## Agent execution-session selection

The Agent execution selector persists a separate session ID for each browser/provider route.
An explicit `unknown_agent_session` status response recovers to `new` without submitting a prompt.
Route changes invalidate pending response epochs and restore only the target route's selection.
Unsent drafts remain in page memory, keyed by browser/provider scope and execution session ID;
changing routes restores that route's draft without sending it or copying it into another route.
Drafts are not persisted across a page reload.

`AgentSessionPool.catalog()` and atomic admission share the same capacity predicate. The frontend
uses `can_start` to gate Ask, while the locked backend remains authoritative if capacity changes
between polling and submission. A worker with a recorded run cannot be reassigned to a different
browser or provider. A stale status snapshot can still lead to a legitimate HTTP 409; it is not
proof that the admission rules differ.

## Durable compute-job boundary

`app/core/agent/compute_jobs.py` is a separate execution plane for 12-hour-class genetic,
evolutionary, Bayesian, and other local optimization workers. `job_start`, `job_status`, and
`job_stop` are registry-owned Agent Actions, but they do not pass through `run`, its 1,800-second
verification timeout, its allowlist, or its workspace fingerprint. The existing verification and
bodycheck gates remain authoritative for source edits.

A job starts only when `.agenticContext-compute.json` uniquely names a workspace-relative regular
Python entrypoint and pins its current SHA-256. The controller accepts no shell string or arbitrary
argument vector. It invokes the approved file through the fixed `--config`, `--job-runtime`, and
optional `--resume` protocol, copies the bounded JSON config into the task-owned runtime directory,
and strips the inherited environment to a small non-secret allowlist. Approval is therefore a code
review boundary: changing the entrypoint bytes invalidates approval before execution.
On macOS, the detached optimizer is also launched through `/usr/bin/sandbox-exec` with `network*`
denied, so even approved code cannot open a download or other network socket during the job.
The Windows path does not currently apply an equivalent OS-level network-denying sandbox profile;
the worker runs with the current user's permissions.

Runtime metadata, progress, checkpoints, results, and rolling logs live below the external Agent
runtime root in `compute-jobs/<workspace-hash>/<job-id>/`; they never live in the selected source
workspace. A 128-bit unpredictable `job_id`, stable idempotency key, request fingerprint, PID birth
identity, process-group ownership, and a one-active-job limit prevent duplicate submission and
PID-reuse termination. On startup or status inspection, active records are reconciled with the live
process identity. Missing workers become `interrupted`; they are never resubmitted automatically.

Compute JSON readers use a bounded read of the opened regular file. Windows readers explicitly
share deletion so status polling does not prevent the worker's atomic replacement of metadata or
progress. An existing reader consumes its original file while a subsequent reader sees the new
record. This sharing policy is specific to compute JSON; protected append and verified-deletion
handles retain their separate access restrictions. It does not guarantee publication against an
unrelated program holding an incompatible handle, nor make multiple writers transactional.

The detached worker owns the approved maximum runtime, capped at 24 hours, and remains alive after
the provider turn or browser session ends. On macOS, a job-scoped `caffeinate -i -w <worker-pid>`
assertion follows the worker rather than the Web Agent turn. It exits with the worker and is also
identity-checked during terminal-state reconciliation. The project does not currently inhibit
Windows idle sleep for the equivalent task. The detached Windows compute worker joins a named
Job Object before starting its optimizer. The job owns descendant termination, including abrupt
worker exit; native process birth identity protects against PID reuse. Stop is terminal only after
termination is verified. This is process containment, not an OS-level network sandbox. Ordinary
Agent inspection commands retain their separate best-effort `taskkill /T /F` fallback. Service
exit deliberately does not stop an active compute job.

## Responsive application-shell contract

The browser shell has two independent responsive boundaries. Content enters its compact phone
layout at `600 px` and below. The sidebar enters a fixed overlay at `900 px` and below, which
covers current iPad portrait widths without forcing tablet content into the phone layout.

`style.css` publishes both semantic values as CSS custom properties. `responsive.js` reads those
tokens and is the only first-party JavaScript module allowed to construct width-based media
queries. The templates load it before the sidebar bootstrap, which applies the stored
`cachelikes:sidebar-open` session state or defaults a new overlay session to collapsed. The main
sidebar controller owns the open and collapsed classes, `aria-expanded`, sidebar inertness, and
the backdrop's `hidden`, `aria-hidden`, inert, and tab-index states.

In overlay mode, safe-area-aware fixed geometry keeps the sidebar and toggle inside the viewport.
The backdrop sits below the sidebar, dock, and toggle. A global `[hidden]` rule makes hidden state
authoritative over responsive display rules, while closed sidebar and backdrop states also disable
pointer events explicitly.

## Chinese language presentation boundary

Every document shell loads `language-rendering.js`. The boundary annotator never rewrites text
or Unicode code points; it only marks untagged Han-containing controls and dynamic content as
`lang="zh-CN"` so macOS and browser font selection use the intended Simplified Chinese context.

All source values remain byte-for-byte stable: form controls, URLs, `data-*` attributes, JSON,
code-like content, and explicitly tagged `lang` boundaries are untouched. This is a display
context fix for macOS glyph selection, not a Simplified-to-Traditional conversion layer.

## Source flows

### X Likes cache

```text
authenticated X browser session
  -> scraper collects canonical liked-post URLs
  -> CacheLikesService schedules bounded workers
  -> yt-dlp downloads media using the selected browser cookies
  -> local_store/x/ and its durable cache catalog
```

The service deduplicates URLs, respects a cooperative stop request, shares one cross-workflow
task lock, and records the resulting summary in `TaskState`.

### Grok media cache

```text
authenticated Grok browser session
  -> GrokDownloadService
  -> catalog, manifest, and work queue
  -> validated media files in local_store/media/grok/
```

The Grok downloader persists catalog, resumable download-manifest, and work-queue state. A
rebuild verifies local media rather than trusting filenames alone. Snapshot and reset helpers
resolve their default cache directory at call time so tests can safely redirect it.

### Grok Text cache

```text
authenticated Edge session cloned into an isolated Chromium context
  -> paginated /rest/app-chat/conversations API
  -> response-node tree and batched load-responses API
  -> GrokHistoryStore
  -> local_store/llm/grok/history.parquet
```

Grok Text is deliberately separate from the Grok media runtime. Conversation pagination
uses `nextPageToken` as the next request's `pageToken`; the visible sidebar is not a
complete history source. Each response ID is stable within its conversation, so the
store uses `<conversation-id>:<response-id>` as the message key and atomically replaces
one conversation at a time. See [CACHE_HANDOFF.md](CACHE_HANDOFF.md) for the operator
workflow and recovery rules.

### ChatGPT image cache

```text
configured ChatGPT project or conversation URL
  -> ChatGPTDownloadService
  -> bounded parallel conversation workers with isolated Edge contexts
  -> original-image discovery, download claims, and catalog validation
  -> local_store/media/chatgpt/<project-name>/
```

Up to three workers scan conversations and download original image payloads concurrently. Each
worker owns its Playwright context and recycles its page after a bounded number of conversations;
recoverable page failures receive one retry. Catalog claims and atomic writes prevent duplicate
workers from corrupting the local index. Only image payloads that pass signature validation are
retained. The project name is sanitized before it becomes a cache path.

The local visual-signature and dimension stage is separate from browser and network work. At
ChatGPT sync startup, entries that need visual hydration are read into bounded immutable image
payload batches. File sizes are checked before each read, and the parent flushes an existing batch
before reading a file that would exceed the payload budget. A file larger than the budget, or one
that changes during the bounded read, is analyzed directly by the parent and never enters a payload
batch. This is a hard bound on queued input payload bytes, not a claim about decoded-image RSS.
Batches below 64 images use the existing synchronous CPU path; larger batches may use a conservative
process budget discovered from logical or reliable physical CPU counts and memory. Workers return
analysis envelopes only, and the catalog remains the sole durable commit owner. A GPU adapter is
optional and not installed by the base requirements; an injected adapter must return every expected
identity, otherwise the whole batch is discarded and recomputed on CPU. GPU workers never write
media, catalog, or Parquet state.

### Gemini Text cache (Safari path, macOS-only)

```text
selected authenticated Safari session
  -> one standard task-owned background window
  -> Gemini virtualized conversation navigation
  -> rendered user-query and model-response extraction
  -> atomic local_store/llm/gemini/history.parquet replacement
  -> native window close with Safari window-ID verification
```

The Safari-specific flow is macOS-only. Safari contexts are serialized across processes. The worker never reuses the user's
current window, never creates a replacement after the user closes the owned window,
and never leaves a hidden or blank reusable shell. Window creation, session probes, and
X likes collection share this context so failure paths still run exact-window cleanup.
The context restores the user's previous frontmost application after Safari window work,
and JavaScript execution is bounded by a macOS-only AppleScript timeout. Conversation rows are
replaced atomically per session, so a stopped run preserves every previously verified
session without duplicating messages.
The Windows path uses the Edge/Chromium controller and does not use a Safari window or
AppleScript. Its approved `.ps1` verification paths run through PowerShell, and
`taskkill /T /F` is used for process-tree cleanup where applicable.

### Local-media browser

`LocalMediaCatalog` reads the X, Grok, and ChatGPT cache trees and returns safe relative paths to
the Flask application. The browser route allows only readable supported media below the configured
cache root. Deleting an item moves it to a recoverable hidden browser-trash area and records a
tombstone; restoring it moves the retained preview back to its original safe path.

### Web Computer Use Agent

```text
selected local project
  -> provider-neutral recent session/Project catalog
  -> new or selected signed-in ChatGPT, Gemini, or Grok Web conversation
  -> bounded Markdown context package
  -> one JSON controller action at a time
  -> confined local read/change/check
  -> compact observation returned to the same conversation
  -> current bodycheck
  -> final Markdown result
  -> on an Edge and ChatGPT failure with an exact conversation URL:
       failed state retained
       explicit user handoff may open the same conversation in traditional Edge
       local file actions and bodycheck remain unfinished
  -> on a persisted interrupted Edge and ChatGPT run with binding proof:
       explicit Doctor continuation reuses the same conversation
       recorded permissions and effort policy are retained; context is not uploaded again
```

The web model never receives direct process or filesystem authority. The local controller resolves
every path below the selected project, separates explicit file actions from a restricted command
layer, bounds turns and output, and rejects final completion after an edit until bodycheck passes.
Controller actions travel in fenced `json` code blocks, and the browser reader prefers the literal
code-block text so Markdown rendering cannot consume source-code punctuation before parsing.
The provider adapter validates each official root session, Project, or Project session before the
task; the selection is run-scoped and the default remains a new root session.
Gemini 3.1 Pro and 3.8 Flash use the same controller, permissions, session binding,
verification, and bodycheck flow as ChatGPT. The shared provider model registry supplies both
local choices; Pro remains the default and Flash is an explicit selection. Before transferring
context or a prompt, the Gemini adapter verifies the exact versioned primary label in the
trigger-owned menu and both the selected class and a visible localized selected marker
(English, Simplified Chinese, or Traditional Chinese). Missing versions or incomplete proof
stop the run without substituting another model. The menu is closed after verification.
These are Gemini Web choices, so access depends on the signed-in browser account; no API key
or separate API execution path is introduced.

For Gemini, Project-new binds a fresh transfer receipt to the selected Notebook route but does not
claim an independent provider-side subconversation identity.
Source discovery is persisted separately from message history through a three-level read-through
path: process memory, the shared Parquet catalog, and the authenticated browser collector. The
`/agent` source routes reuse a 15-minute entry by default and coalesce concurrent explicit refreshes
by cache key. Expired passive reads retain the last known entry without starting a background
collector; a first Agent bootstrap cache miss performs one bounded check. Project-session selection
is an explicit later operation and may serve an expired keyed entry while one coalesced quiet refresh
runs. The visible Agent refresh control uses `refresh=1` for an explicit synchronous browser
re-check. A failed refresh falls back to the last known entry and marks the response as stale; no
remote conversation messages are written by this catalog.
The ChatGPT `/agent` status route performs the account probe, dynamic model and complete live effort discovery,
and root source collection together in one Chromium browser launch, returns both catalogs to the
page, and seeds the same source cache through its explicit `store` path. This keeps the status,
first-run effort selector, and Recent sessions selection on one browser opening; Project-session
loading remains isolated by its canonical Project URL key.
The Agent-scoped browser-session status route uses that same cache. Passive polling reuses the
cached bootstrap, including a bounded negative result; an explicit `refresh=1`, `true`, or `yes`
requests a synchronous fresh result and coalesces with an in-flight collector for the same key.
The fresh result is stored and supersedes any older in-flight browser response.
Agent bootstrap checks use quiet, task-independent Chromium contexts. ChatGPT source checks
remain non-headless because its Cloudflare challenge rejects headless clones with HTTP 403.
On macOS, silent probes and executing
Edge or Chrome task clones the selected profile into one normal, non-offscreen task-owned window so the user can
choose to inspect it through macOS window management without an automatic full-display takeover.
The launcher restores the prior foreground app if the browser took focus; macOS controls any Stage Manager
grouping. Chromium suppresses browser prompts and attempts to clean the task-owned profile on exit.
Stale profile names, age, and a missing owner process do not establish that browser children have
exited. Automatic stale-profile deletion therefore retains unknown directories for manual review;
normal lifecycle cleanup acts only on the clone owned by that launch.
On Windows, the controller uses PowerShell-compatible paths and trusted PowerShell execution for
approved `.ps1` scripts. The Windows path has no OS-level sandbox equivalent to macOS
`sandbox-exec`, and `taskkill /T /F` remains process-tree cleanup rather than sandbox isolation.
Traditional failure handoff does not reuse that writable clone. It records only the normalized
official conversation URL for the browser selected by the failed run; a normal Edge window opens
only after the user invokes the handoff action. That remote ChatGPT page never receives local
filesystem authority.
An interrupted continuation is eligible only when the persisted run records a confirmed provider
conversation binding in addition to a valid Edge and ChatGPT target, workspace, operating system,
permission state, and effort policy. A process failure before the first confirmed binding therefore
cannot cause Doctor to send a continuation message to a merely selected recent-session URL.

## Data ownership

| Location | Owner and purpose | Git policy |
| --- | --- | --- |
| `local_store/` | User media, source catalogs, queues, manifests, and deletion previews | Ignored except `.gitkeep` |
| `local_store/prompt/` | Snapshot-backed saved prompts retaining source pointers for traceability | Ignored except `.gitkeep` |
| `logs/` | Local structured JSON-line logs | Ignored except `.gitkeep` |
| Platform-native agenticContext settings path (`~/Library/Application Support/agenticContext/...` on macOS; `%APPDATA%\agenticContext\...` on Windows) | Device-local configuration | Outside the repository |
| `app/`, `tests/`, `docs/`, `scripts/` | Versioned source, contracts, and checks | Committed |

`AGENTIC_CONTEXT_RUNTIME_ROOT` and `AGENTIC_CONTEXT_SETTINGS_PATH` are the current runtime-injection inputs. The legacy `CACHELIKES_RUNTIME_ROOT` and `CACHELIKES_SETTINGS_PATH` aliases remain accepted for existing launchers and test environments.
Production startup leaves them unset. Pytest sets both before imports so tests cannot resolve the
user-owned locations above.

## Cross-workflow and safety invariants

- Only one cache job may own the shared `CacheTaskLock` at once, regardless of source.
- The shared `download_workers` setting is normalized to `1` through `8` at load, save, and direct
  configuration construction. Provider-specific browser limits remain stricter where required.
- Local compute process workers and in-flight image payload bytes are independently bounded; compute
  workers never share browser state or mutable persistence objects, and result queues apply backpressure.
- Account, project, and filename fragments are normalized before becoming local paths.
- File-serving routes reject traversal, symlink escape, hidden-state paths, unreadable files, and
  unsupported media extensions.
- Reset and browser deletion are data-changing operations. They must remain explicit user actions
  and must never be exercised against production data by the default suite.
- Browser automation starts from an already authenticated host session. It must not introduce a
  login-repair workflow without explicit product direction.
- Agent source context is an external data transfer to the selected ChatGPT account. The local UI
  discloses that boundary; tests never submit real project data or open authenticated profiles.
- The Flask server binds to the LAN by design. Cache and Local resources routes remain trusted-LAN
  surfaces. The Agent control plane accepts loopback directly and requires a signed session after
  a six-digit password unlock for RFC1918 or IPv6 ULA requests; public and host-rebinding requests
  are rejected.

## Testing boundary

Tests use pure functions, temporary directories, fakes, mocks, and Flask's `test_client()`.
They do not launch an authenticated browser, invoke yt-dlp, touch external services, or access
production cache, logs, settings, or browser profiles. See [TESTING.md](TESTING.md) for the
enforced quality gate and writing guidance.

### Execution session selection and admission

The Agent tab remembers execution IDs separately for each browser/provider route. Legacy unscoped
IDs are ignored. A typed `unknown_agent_session` status 404 resets only the current selection to
`new`; transport failures retain the selected worker and never retry a mutation. Route switches
invalidate in-flight status responses and restore only that route's remembered selection.
Existing workers reject starts for another browser/provider so their snapshots remain attributable.
The status catalog exposes `can_start` and `start_blocked_reason` from the pool's shared capacity
rule, including active workers outside the current route. This is advisory capacity, not a slot
reservation: atomic admission still validates capacity and conversation ownership at submission.

## Browser profile evidence and task identity

Chromium profile names must be one directory component; traversal, drives, absolute paths, and
links that escape the selected root are rejected before copying. On Windows the same executable
resolver supplies explicit handoff and Playwright launch. Each request captures its configuration;
each task also persists its selected browser, absolute data root, and profile. Conversation handoff
and interrupted continuation use that binding even after settings change. Legacy tasks without
profile proof cannot silently continue or hand off through the current profile.

Agent source-cache schema 2 partitions memory and Parquet entries by a hash of the resolved browser
profile identity. The browser receives the hash, not the data root. Readiness, provider catalogs,
and session history must match the current identity; profile changes invalidate pending responses
and cached provider choices. Browser execution-session identity remains separate so changing
configuration does not lose Stop for an already running task. Copying a running browser profile is
not an atomic snapshot or evidence that the clone is authenticated.

Browser cleanup failures keep the original task exception primary and preserve a structured
`cleanup_error` through Stop and restart. A standalone cleanup error fails the task. Retained
paths remain diagnostic evidence; neither context close nor task cancellation guarantees that
every native browser child or temporary directory has already been released.

Ask carries the profile identity used by its readiness evidence. The server compares it with the
same captured configuration passed to the worker and rejects missing or changed identities before
starting a task. Following up a recorded conversation also requires its original profile binding;
changing settings cannot silently continue that conversation through another profile.
