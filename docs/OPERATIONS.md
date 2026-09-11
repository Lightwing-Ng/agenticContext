# Operations guide

Documentation version: `v1.14.2-codex.1`

## Launch

Prepare the supported local runtime:

```bash
./scripts/setup_python.sh
```

On Windows:

```powershell
.\scripts\setup_python.ps1
```

Start the Flask console:

```bash
./scripts/run_app.sh
```

On Windows:

```powershell
.\scripts\run_app.ps1
```

The normal server address is `http://127.0.0.1:8666`. The application also binds to `0.0.0.0`,
which permits access from trusted devices on the same LAN at `http://<host-lan-ip>:8666`.

Cache and Local resources routes have no login layer, so keep the console on a trusted local
network, do not expose it through router port forwarding, and do not publish it through a public
tunnel or reverse proxy. The Agent control plane is the exception: loopback requests continue
directly, while private-network requests to `/agent` and `/api/agent/*` require the six-digit
password gate. The default password is `195135`; set `AGENTIC_CONTEXT_AGENT_PASSWORD` before launch
to override it. A successful unlock is stored in the signed Flask session for that browser.

## Browser-session preconditions

- X caching begins from the currently signed-in Likes page in a supported host browser.
- Grok and ChatGPT syncing use their existing authenticated browser sessions.
- Both Zhihu workflows support only Chrome and Edge. They use an isolated temporary clone of the
  selected signed-in profile and perform credentialed same-origin API reads; Safari and an
  unauthenticated standalone HTTP client are not supported.
- A Safari-backed Cache task is macOS-only, opt-in, and owns one standard, visible background window
  with native window controls. It restores the user's previous frontmost application after
  every window-affecting operation, then closes and verifies that exact window at task end;
  it must not hide, minimize, move offscreen, reuse, or accumulate Safari windows.
- Chrome, Edge, and Safari support differs by source and automation engine; use the session probe
  in the console before a long sync.
- Passive Agent checks use a quiet, isolated Chromium context. ChatGPT source checks use a
  non-headless context because the provider's Cloudflare challenge rejects headless clones with
  HTTP 403. Windows probes retain offscreen/minimized launch arguments. Existing macOS silent
  probes retain their task-stage window policy and foreground-app restoration.
- Executing Edge or Chrome tasks on macOS use an isolated profile clone without offscreen or
  start-minimized launch arguments. On Windows, a clone may be used before the project debug
  profile is initialized. After a successful debug-browser launch creates its marker, every later
  Agent readiness, source, Project, and history probe or Agent task restarts or reuses that same
  persistent project profile over CDP, including after the user closes its window. Ordinary Cache
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
- Explicit login handoff opens the selected browser visibly. macOS login and Windows conversation
  handoff use the normal resolved browser. Windows login uses the project-owned persistent debug
  profile so Recheck and later tasks read the same authentication state. Opening the page does not
  establish sign-in; the user must choose Recheck. Native Windows 11 browser execution remains
  unverified on this macOS host.
- Windows login handoff targets the project-owned debug browser instead of the user's daily
  profile. Because a running Edge or Chrome keeps its sign-in cookies under an exclusive OS
  lock, the Agent reuses a separate Chromium instance launched with `--remote-debugging-port`
  against a dedicated user-data directory under `local_store/agent_browser_profile/<browser>`.
  The login handoff navigates that debug browser to the provider home so the authenticated
  session lands in the debug profile the Agent reads over CDP. Once initialized, subsequent Agent
  checks and tasks use it as the authoritative Windows Agent profile and restart it when necessary.
  The first sign-in on a fresh debug profile is the only manual login the user performs there.
  One process-local lock owns each browser for the full CDP caller lifetime. Waiting is bounded to
  five seconds, and a busy operation fails clearly instead of blocking indefinitely. The Windows
  Agent therefore advertises and admits one active task; macOS retains its existing concurrency.
  Multiple application processes are not coordinated by this lock.
- Copying profile files and launching the clone do not prove provider authentication. The copy
  is not an atomic snapshot of a running browser. Check readiness in the clone; report copy or
  access errors without closing the user's browser or retrying against its writable profile.
  On Windows, when the host browser holds its `Network/Cookies` lock, the launcher falls back to
  the project-owned debug browser over CDP instead of failing the clone; no file is read from the
  locked profile.
- Normal clone-backed task exit closes the isolated context and removes its temporary profile. A
  CDP-backed Windows exit closes only the Playwright connection and leaves the project browser and
  persistent login profile available. Each subsequent Chromium launch also removes only abandoned
  `cachelikes-edge-*` or `cachelikes-chrome-*` directories older than 24 hours; unrelated temporary
  paths are not touched.
- A failed temporary-profile removal reports its retained directory and remains protected from
  stale-profile cleanup in the current process. Cleanup errors become exception notes and log
  warnings when an earlier task, launch, copy, or close error already exists; otherwise they fail
  the operation. Inspect task-owned browser processes before manual cleanup. This protection is
  process-local, not a durable orphan-process tracker across service restarts.
- Do not add or repeatedly troubleshoot login flows as part of normal runtime operation. The
  application assumes an existing signed-in session.

## Computer Use Agent

- `/agent` and its `/api/agent/*` control routes accept host-loopback requests directly. Requests
  from RFC1918 private IPv4 or IPv6 ULA addresses show the password gate before the Agent page or
  API is served; public and host-rebinding addresses remain rejected.
- Each task defaults to a new root-level ChatGPT, Gemini, Grok, or Claude Web conversation in the
  selected authenticated browser session. Safari remains available only for ChatGPT; Claude uses
  Edge or Chrome. The Agent sidebar can also join one of the 20 most recent root
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
- Stop requests end current web generation and terminate the active local command process group.
  On Windows, approved `.ps1` scripts run through the PowerShell controller, and process-tree
  cleanup uses `taskkill /T /F` where applicable. That mechanism is not an OS-level sandbox.
  Stop requests do not stop a detached durable compute job; use its dedicated `Stop job` control
  or `job_stop` with the exact `job_id`.
- Agent source discovery is cached in `local_store/agent/agent_source_catalog.parquet` for 15
  minutes per provider/browser/Project key. Fresh reads use process memory; the first read after a
  restart hydrates memory from Parquet. Expired passive reads retain the previous catalog and never
  launch a background browser collector. One initial Agent bootstrap cache miss performs a bounded
  check; model and effort discovery happen in that check; later task submissions verify their requested settings without a separate refresh button. Concurrent requests share one flight. The
  response's `cache.status` is `hit`, `miss`, `refreshed`, or `stale`; a stale response means the
  previous verified catalog was retained after an explicit check failed.
- While a Windows Agent task owns Edge or Chrome, source, Project-session, history, and bootstrap
  routes do not start another live collector for that browser. They return a cached entry without
  background refresh, return `unprobed` on a catalog/bootstrap miss, or return HTTP 409 on a history
  miss. The rule is browser-wide because every provider shares that browser's CDP context. It is
  deliberately absent on macOS.
- ChatGPT and Claude on `/agent` use one agent-scoped browser bootstrap through Recent sessions:
  the same bounded initial check verifies readiness, collects the root session/project catalog,
  returns it to the selector, and seeds the memory/Parquet cache. Cache reuse and task completion
  do not add a browser launch. A later browser launch is reserved for an explicit refresh, task
  submission, or later Project-session selection; an expired keyed Project-session read may serve
  stale rows while one coalesced quiet refresh runs. Restricted Claude accounts remain unavailable
  and are not sent through a login-bypass flow.

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
| `local_store/llm/chatgpt/history.parquet` | ChatGPT typed text history |
| `local_store/llm/gemini/history.parquet` | Gemini typed text history |
| `local_store/llm/grok/history.parquet` | Grok typed text history |
| `local_store/llm/claude/history.parquet` | Claude typed text history |
| `local_store/llm/zhihu/history.parquet` | Formal Zhihu answer text and source links |
| `local_store/prompt/prompts.parquet` | Saved prompt content snapshots and source pointers; prompts remain available if source history disappears |
| `local_store/agent/agent_source_catalog.parquet` | Provider-neutral Agent session and Project discovery cache |
| `local_store/.cache_task.lock` | Cross-source advisory task lock |
| `local_store/.browser-trash/` | Recoverable previews moved by the local-media browser |
| `local_store/.browser_deleted.json` | Browser deletion tombstones and exclusion identities |
| `beta_store/zhihu/<token>/answers.parquet` | Verified Beta Zhihu answer snapshot; not a Local resources or ShadowBackup input |
| `logs/cachelikes.log.jsonl` | Structured local application log |
| Platform-native agenticContext settings path (`~/Library/Application Support/agenticContext/...` on macOS; `%APPDATA%\agenticContext\...` on Windows) | Device-local saved settings |

All cache and log paths are ignored by Git. Back up local media before using any destructive reset
operation. The Beta Zhihu archive remains outside `local_store/`; existing Cache reset actions do
not remove it, and ShadowBackup does not copy it.

### Zhihu text cache

Open `GET /cache/zhihu`. Edge is selected by default; Chrome is the only alternative. The account
card calls the ordinary browser-session probe and enables Start only after
`https://www.zhihu.com/api/v4/me` identifies a signed-in account.

Leave `Answerer URL` blank to cache every unique answer found while paging that account's
`MEMBER_VOTEUP_ANSWER` activity. Enter a validated Zhihu people or Answers URL to cache every
answer exposed by that answerer's paginated API instead. Both modes recheck the newest page before
an atomic cumulative merge into `local_store/llm/zhihu/history.parquet`; a later run never removes
answers collected by an earlier mode. An activity target whose body is unavailable is retained as
an excerpt-plus-source-link record instead of aborting the complete liked-answer run. Review the
answers under Local resources with source `Zhihu`; the source-specific sidebar removes Clear
filters and adds a standard Answerer select populated from cached names. Its selection scopes the
metrics, sessions, search, ordering, and pagination. The list shows the literal answerer, omits its
redundant Source column and answer ID, and removes the inapplicable Projects metric. Detail tables
use the literal answerer name instead of Role and render the complete stored text without the
generic message-collapse limit. Original answer, question, profile, embedded anchor, and remote
image URLs remain explicit source links; no provider HTML or image binary is mounted.

### Beta Zhihu Answers Cache

Open `GET /beta/zhihu-answers-cache`, enter a validated Zhihu people or Answers URL, select Chrome
or Edge, and use the explicit start control. Loading the page or polling
`GET /api/beta/zhihu-answers-cache/status` never launches a browser or writes data. Search the
persisted archive through `GET /api/beta/zhihu-answers-cache/answers`, then open one local body
through `GET /api/beta/zhihu-answers-cache/answers/<answer-id>`. These reads never contact Zhihu.
Start and Stop are separate POST operations at `/api/beta/zhihu-answers-cache/start` and
`/api/beta/zhihu-answers-cache/stop`.

Loopback access is direct. From a private-network address, open Agent and pass its six-digit access
gate in the same browser session before returning to this Beta page. The Zhihu status, archive,
start, and stop endpoints reject non-local hosts, remote source addresses, and cross-origin requests, and all
responses are marked `Cache-Control: no-store`.

Status polling projects only the archive metadata columns required for counts and recent items. The
service caches that bounded summary until the Parquet inode, modification time, or size changes, so
multiple open Beta pages do not repeatedly decompress stored answer bodies.

The Cached answers panel reads 20 summaries at a time and searches IDs, URLs, questions, excerpts,
and stored plain text. `View cached copy` performs a second exact-ID read and renders only text;
`Open on Zhihu` is the separate network destination. A missing body is reported as metadata-only,
which is distinct from an answer absent from the archive.

The start request competes for the same application-wide cache task lock as every ordinary Cache
worker. Wait for the current owner or stop it through its own UI; never remove
`local_store/.cache_task.lock` to force admission. Stop is cooperative. A Stop accepted before
`commit_pending`, or a failed, verification-blocked, or incomplete pre-commit run, leaves the
previous Parquet snapshot byte-for-byte available and does not publish collected partial rows.
After atomic `committing` begins, Stop is not accepted and the already verified snapshot finishes
publication.

Treat `completed` as valid only after the status reports a committed and read-back archive. A
terminal API page alone is not completion. The worker also rechecks the first API page before its
atomic commit. If duplicate-backed pagination exposes fewer unique IDs than the stable reported
total, it repeats the complete enumeration and commits only when both passes have identical IDs,
page/raw/duplicate counts, terminal state, and total. Read Reported, Cached, and Not enumerable as
separate values; the service never invents rows for hidden IDs. The archive stores content and
public metadata, including media URL references, but does not download image binaries.

## Concurrency and local compute

The shared `Download workers` setting accepts values from `1` through `8`. Older settings files
remain readable; values below or above that range are normalized on load, and direct
`CrawlConfig` construction applies the same limit. Grok remains capped at four download workers,
ChatGPT remains capped at three isolated Chromium workers, and Safari remains serialized.

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
  and running Text sync; do not edit its live Parquet file or reset cached media to apply this change.
- Legacy ChatGPT tool traces can be reviewed offline with
  `python3 -m app.core.chatgpt_history_cleanup` (use `py -3 -m` on Windows).
  Add `--apply` to remove recognized traces preceding a later reply in the same turn.
  This includes multiline `fast|`, `slow|`, `open|`, and `find|` command batches with
  an optional trailing `length|short`, `length|medium`, or `length|long` option.
  Every line must match the protocol; mixed prose and fenced examples are retained.
  Ordinary prose is retained unless explicitly reviewed with `--remove-message chat-<id>`.
  The command acquires the cache task lock, backs up the original Parquet and a removal manifest
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
- Zhihu count mismatch: do not edit the Parquet file or invent missing IDs. A short terminal result
  whose raw count is also below the reported total fails closed. A duplicate-backed provider gap is
  accepted only after two full enumerations reproduce the same ordered unique IDs and pagination
  counts; the gap is then persisted and shown as Not enumerable. Any drift preserves the earlier
  archive until a later explicit run succeeds.
- Cache inconsistency: use the local-media browser to inspect the affected source before choosing a
  source-specific reset. Avoid deleting catalog or manifest files by hand.

## Safe development checks

Run `./scripts/test.sh` or `./scripts/check.sh` (macOS/Linux) or `.\scripts\test.ps1` or
`.\scripts\check.ps1` (Windows) for offline validation. Pytest redirects all
default runtime paths into temporary directories; tests must never be pointed at the production
cache, Beta store, log, settings, or browser-profile locations.
