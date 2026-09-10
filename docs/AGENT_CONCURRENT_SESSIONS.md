# Edge ChatGPT concurrent Agent sessions

Documentation version: v1.3.1-codex.1
Date: 9 Sep 2026

## Behavior

The Agent sidebar lists locally executed sessions across all workspaces for the
selected browser and provider. Each row shows its title and Running, Paused, Completed, or failure
state. Select a row to inspect its response and Activity. New task creates a separate
session on submission. Unsent drafts survive switching within the page, and the
selected session and its recorded workspace are restored after a page refresh.
Selecting a row updates the local project fields before requesting that task state;
it does not rewrite global preferences or move the running worker.

On macOS, the backend admits at most two active workers. Both must use ChatGPT in
Edge; other browser/provider combinations retain exclusive execution. On Windows,
the initialized project debug browser has one rendered context, so the backend
advertises and admits one Agent worker. Paused and stopping workers occupy a slot
until lifecycle cleanup completes. An over-capacity submission returns HTTP 409 and
is not queued or retried. Two workers cannot execute the same normalized provider
conversation concurrently. These are application concurrency limits, not assurances
about provider account enforcement.

New session remains available as a draft surface when admission is temporarily blocked. The Agent
status shows the backend `start_blocked_reason`, the disabled Ask control exposes that explanation,
and polling enables Ask without discarding the draft after the active workspace lease or other
capacity constraint clears. The UI never bypasses the locked admission check.

Each session owns its worker, stop/resume signals, event chain, context bundle,
response history, and browser context. macOS Chromium execution retains a unique
temporary cloned profile. Windows uses one project-owned persistent debug profile
and therefore admits one worker. Stop, Resume, diagnostics, recovery,
open-conversation, and compute-job control resolve the selected session. The admission
guard also covers diagnostic continuation. Shutdown signals all workers before joining
any worker. Persisted metadata restores individual sessions; the existing privacy
policy still excludes response bodies and full in-memory history from disk snapshots.

## Implementation

- `app/core/agent/session_pool.py` v1.0.0-codex.1: session registry and atomic admission.
- `app/core/computer_use_agent.py` v3.57.2-codex.1: worker admission hook and
  message-pair attribution for repeated ChatGPT replies.
- `app/web/app.py` v1.65.0-codex.1: session-scoped control and status catalog.
- `app/web/static/computer-use-agent.js` v3.32.0-codex.1: session selection,
  capacity, draft isolation, reload restoration, and stale-response rejection.
- `app/web/templates/agent.html` v3.33.0-codex.1 and
  `app/web/static/agent-sessions.css` v1.0.0-codex.1: sidebar session rail using
  existing controls and theme tokens.
- `tests/test_agent_session_pool.py`, `tests/test_agent_sessions_e2e.py`, and
  `tests/test_web_app.py`: regression coverage and updated asset version expectations.

The 9 Sep 2026 Windows extension adds browser-wide active-worker detection, a
process-local per-browser CDP lock with a five-second acquisition bound, and
single-worker Windows admission. Source and history routes do not start a competing
collector while that browser is active. This leaves the macOS two-worker behavior
and the historical macOS acceptance evidence below unchanged.

The session header is `X-CacheLikes-Agent-Session`. Omission retains the legacy
`primary` worker; `new` represents a draft and allocates a new session only on Ask.
Unknown identifiers cannot fall back to stopping the primary worker. Status lists
filter session metadata by browser, provider, and workspace, while the active count
covers the whole application. The limit is enforced by one application process, as
used by the repository's local server; a multi-process WSGI deployment would need
shared admission coordination.

## Verification

Command prefix: `AGENTIC_CONTEXT_PYTHON=/usr/local/bin/python3.13`.
The default Homebrew Python lacked `markdown_it`, so the existing supported Python
installation with project dependencies was used instead.

```bash
./scripts/test.sh tests/test_agent_session_pool.py tests/test_agent_sessions_e2e.py tests/test_agent_activity_e2e.py tests/test_web_app.py tests/test_computer_use_agent.py tests/test_agent_controller_hardening.py
```

Result: 651 tests and 541 subtests passed. Browser checks use disposable Chromium
and an isolated local server at desktop 1,138px and narrow 390px widths. Coverage
includes a simultaneous three-request admission race, context and stop isolation,
paused capacity, duplicate conversations, startup failure, shutdown, API selection,
sidebar switching, draft retention, selected Stop, capacity availability, late
responses, and refresh restoration. Ruff, JavaScript syntax, and diff checks passed.
This is focused acceptance, not an uninterrupted full quality gate or a real-provider
simultaneous-send acceptance run.

## Authorized production restart and real-provider acceptance

On 6 Sep 2026 the user explicitly authorized a force restart and testing. The old
8666 listener, PID 47088, was killed and replaced through macOS Terminal using
`AGENTIC_CONTEXT_PYTHON=/usr/local/bin/python3.13 ./scripts/run_app.sh`.
PID 42400 initially owned 8666 with cwd `/Users/lightwing/Desktop/agenticContext`. The live
status API exposes `concurrency_limit: 2`, and the Edge page loads
`computer-use-agent-v3.32.0-codex.1` and the Sessions sidebar.

The saved workspace was changed to `/Users/lightwing/Desktop/demo_flight`, saved,
reloaded, and verified before submission. Two read-only acceptance tasks were
submitted using the dynamically available Latest model and Extra High effort:

- Parallel Alpha: session `17e50a26fde54bc1bf99712e63bf0129`,
  run `run-c5abe7653359408f898a0d98015519ed`.
- Parallel Beta: session `99fa304057c74bc68f54f960a91154eb`,
  run `run-93236ae727374c92865c8b7a2336059e`.

Both starts returned HTTP 202 with running=true. A third request returned HTTP 409
with the two-slot capacity message. Both real providers verified Latest / Extra High
and bound distinct ChatGPT conversation URLs after submission. Edge displayed both
sessions and `2 of 2 active`; switching changed the selected session and its provider
link without stopping either worker.

Both tasks then paused before the first local controller action because macOS
reported `CGSSessionScreenIsLocked=Yes`. The user was asked to unlock the Mac;
no login, lock-screen, or interruption protection was bypassed. Selecting Beta and
clicking Stop in the actual Edge UI stopped only Beta, cleared its context file,
recorded its terminal event, and reduced the live active count to one. Alpha remained
paused with its own conversation and context intact. Full action-loop/bodycheck
completion is still unverified pending unlock; this is not a claim of two completed
remote tasks. Alpha was retained for continuation after unlock.

Nine focused session-pool and session-browser tests passed again after restart.
The ten application source/manifest files in demo_flight retained their pre-test
SHA-256 values. The tests requested no writes, commands, or compute jobs. The large
Blender binary was not rehashed for this read-only concurrency test.

## Unlocked-host follow-up

After unlock, Alpha reached three controller turns, including a successful README
read. Continuation exposed a response-attribution defect: ChatGPT returned the same
`bodycheck` text as its preceding response, while the visible turn count could stay
unchanged as earlier messages were virtualized. The controller waited despite a new
user/assistant pair. Version v3.57.1 adds both message IDs to the atomic snapshot and
accepts this case only when both IDs change, the latest user text matches the
submitted message, and the assistant follows that user. Existing conversation,
generation, stability, and Stop checks still apply. Unit regressions reject stale
IDs, mismatched user text, incorrect ordering, and a foreign conversation; disposable
Chromium also covers replacement of a same-count, same-text pair.

The interrupted continuation had not persisted a confirmed binding, so recovery
correctly refused another direct continuation. Acceptance restarted through the
normal existing-conversation path, without changing persisted safety metadata. One
attempt stopped before upload because the effort slider was unavailable. Direct
inspection confirmed the current combined model/effort menu; one bounded retry
verified Latest and Extra High before submission. The service uses supported Python
3.13 through the repository launch wrapper. Current user preferences and the other
active worker were preserved.

The retry `run-75c98751ddce42178accb6e145840d44` sent successfully, but its repeated
`bodycheck` exposed a second boundary: attachment labels and the collapsed message's
Show more control polluted the user text. Version v3.57.2 extracts the actual ChatGPT
message body before exact matching. Disposable Chromium covers both plain messages
and messages with an attachment and collapse control. The final focused command
below passed 542 tests; Ruff and diff checks passed.

```bash
./scripts/test.sh tests/test_computer_use_agent.py tests/test_agent_controller_hardening.py tests/test_agent_session_pool.py tests/test_agent_sessions_e2e.py
```

Alpha was stopped normally, its context path was cleared, and its terminal event was
recorded. Beta remains stopped. The global active count is one because an unrelated
Worthward task is running. PID 22398 serves 8666 with v3.57.1 loaded; another force
restart was deferred to preserve that active task. Therefore v3.57.2 still needs a
safe runtime restart and real-provider bodycheck/final verification. This follow-up
does not claim remote completion. All ten checked demo_flight source/manifest hashes
remain unchanged. Concurrent session-rail edits from another task were preserved.

## Shared UI and housekeeping

Worthward's stylesheet entrypoint and sidebar shell were inspected read-only. The
new product-specific rail reuses existing control classes, theme colors, and corner
radii. Shared review remains Pending in `../SHARED_UI_SYNC.md`; no sibling source
was changed.

The final numbered-copy inventory retains 15 pre-existing candidates: 12 protected
local-data/log files and three byte-distinct coverage files. No candidate was deleted
or moved, and no unrelated source change was present at the initial Git baseline.

## Connection recovery, 6 Sep 2026

Code versions: computer_use_agent.py v3.58.0-codex.1 and
computer-use-agent.js v3.34.0-codex.1.

The browser status request has an eight-second timeout. A failed status read shows
Reconnecting and retains the selected run, execution state, and stop target. The
next successful poll restores the authoritative state. Refresh restores the selected
execution ID before its first request and does not submit or stop a task.

After submission, a verified provider conversation binding is persisted before
waiting for the first complete controller action. Zero controller turns therefore
means no complete action has been received; it does not imply that provider
inference has stopped. The UI distinguishes provider generation from connection
recovery. Read-only transport failures retry at half-second intervals, up to sixty
retries per read; the provider prompt is never resent. Navigation reads retain a
separate twenty-retry bound. Explicit Stop remains authoritative. Session identity
and receipt failures are not treated as network failures.

A closed browser transport or exhausted read recovery becomes Interrupted. If the
verified binding and recorded permissions exist, the existing Doctor Continue
operation remains available even at zero turns. Continuation is explicit and uses
the recorded conversation and workspace. A closed browser is not silently reopened,
and this change does not replay local actions or restore full response bodies from
persisted metadata.

Validation: 464 Agent/session checks passed before the final polling-wait hardening;
17 focused recovery/session checks passed after that hardening. These include a
single send across transport retries, early binding, cancellation, identity rejection,
zero-turn continuation, status disconnect/recovery, and selected-run refresh.
The existing 8666 service was not restarted: Computer Use rejected Terminal access.
Backend deployment remains pending a canonical Terminal restart.

## Cross-project session visibility, 6 Sep 2026

session_pool.py v1.2.0-codex.1 includes workspace_path in every catalog row and
removes workspace filtering within the selected browser/provider. Frontend
v3.35.0-codex.1 follows the selected session workspace. Fifteen focused session
checks passed, including desktop/narrow cross-project selection and refresh.
The live service still has two active workers; this backend catalog change is pending
restart after those workers finish. No live worker was stopped for this UI change.
