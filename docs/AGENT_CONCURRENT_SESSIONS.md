# Edge ChatGPT concurrent Agent sessions

Documentation version: v1.0.1-codex.1
Date: 6 Sep 2026

## Behavior

The Agent sidebar lists locally executed sessions for the selected browser, provider,
and workspace. Each row shows its title and Running, Paused, Completed, or failure
state. Select a row to inspect its response and Activity. New task creates a separate
session on submission. Unsent drafts survive switching within the page, and the
selected session is restored after a page refresh when it still belongs to the route.

The backend admits at most two active workers. Both must use ChatGPT in Edge;
other browser/provider combinations retain exclusive execution. Paused and stopping
workers occupy a slot until lifecycle cleanup completes. A third submission returns
HTTP 409 and is not queued or retried. Two workers cannot execute the same normalized
provider conversation concurrently. This is an application concurrency limit, not an
assurance about provider account enforcement.

Each session owns its worker, stop/resume signals, event chain, context bundle,
response history, and browser context. The existing Chromium launcher gives each
execution a unique temporary cloned profile. Stop, Resume, diagnostics, recovery,
open-conversation, and compute-job control resolve the selected session. The admission
guard also covers diagnostic continuation. Shutdown signals all workers before joining
any worker. Persisted metadata restores individual sessions; the existing privacy
policy still excludes response bodies and full in-memory history from disk snapshots.

## Implementation

- `app/core/agent/session_pool.py` v1.0.0-codex.1: session registry and atomic admission.
- `app/core/computer_use_agent.py` v3.57.0-codex.1: worker admission hook.
- `app/web/app.py` v1.65.0-codex.1: session-scoped control and status catalog.
- `app/web/static/computer-use-agent.js` v3.32.0-codex.1: session selection,
  capacity, draft isolation, reload restoration, and stale-response rejection.
- `app/web/templates/agent.html` v3.33.0-codex.1 and
  `app/web/static/agent-sessions.css` v1.0.0-codex.1: sidebar session rail using
  existing controls and theme tokens.
- `tests/test_agent_session_pool.py`, `tests/test_agent_sessions_e2e.py`, and
  `tests/test_web_app.py`: regression coverage and updated asset version expectations.

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
PID 42400 owns 8666 with cwd `/Users/lightwing/Desktop/agenticContext`. The live
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
remote tasks. Alpha is intentionally retained for continuation without resubmission.
The current test workspace remains demo_flight while that task is pending.

Nine focused session-pool and session-browser tests passed again after restart.
The ten application source/manifest files in demo_flight retained their pre-test
SHA-256 values. The tests requested no writes, commands, or compute jobs. The large
Blender binary was not rehashed for this read-only concurrency test.

## Shared UI and housekeeping

Worthward's stylesheet entrypoint and sidebar shell were inspected read-only. The
new product-specific rail reuses existing control classes, theme colors, and corner
radii. Shared review remains Pending in `../SHARED_UI_SYNC.md`; no sibling source
was changed.

The final numbered-copy inventory retains 15 pre-existing candidates: 12 protected
local-data/log files and three byte-distinct coverage files. No candidate was deleted
or moved, and no unrelated source change was present at the initial Git baseline.
