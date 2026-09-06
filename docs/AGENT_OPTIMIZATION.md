# OpenAI Site tools and Agent Optimization

Documentation version: `v1.3.3-codex.1`

This project implements the shared Agent Optimization contract at
`/Users/lightwing/Desktop/SHARED_AGENT_OPTIMIZATION.md`. That file owns the cross-project naming,
schema, result, security, lifecycle, evaluation, and promotion rules. This document owns only the
agenticContext adapter and its verification evidence.

## Runtime adapter

| Boundary | Project implementation |
| --- | --- |
| Shared runtime | `app/web/static/agent-optimization.js` |
| Project manifest | `app/web/templates/_agent_optimization.html` |
| Capability registry | `app/core/agent/capability_registry.py` |
| Agent event chain | `app/core/agent/event_chain.py` |
| Doctor routes and UI | `app/web/app.py`, `app/web/templates/agent.html`, `app/web/static/computer-use-agent.js` |
| Shared page bootstrap | `app/web/templates/_sidebar_bootstrap.html` |
| Node contract tests | `tests/test_agent_optimization.mjs` |
| Flask render tests | `tests/test_agent_optimization.py` |
| Disposable-browser tests | `tests/test_agent_optimization_browser.py` |

The runtime is byte-identical to the sibling antigravity copy and registers tools only when the
top-level document exposes `document.modelContext.registerTool`. Unsupported browsers receive the
normal interface without an error or behavioral fork.

The Agent password unlock template does not load Site tools. Normal loopback Agent pages load the
same safe v1 inventory as Cache, Local resources, and Settings pages. The manifest is generated at
render time from the capability registry; the shared runtime remains byte-identical and still
registers only three WebMCP tools.

The registry is the application-side source of truth for the Agent Action protocol, including
closed input schemas, controller handler identifiers, prompt examples, and read-only task rules;
bounded page observations; WebMCP tool metadata; and human-page navigation. The rendered manifest
publishes registry-derived WebMCP names, descriptions, schemas, and `readOnlyHint` values. It keeps
the public manifest small by publishing three aggregate groups for the internal Agent actions, page
observations, and WebMCP tools rather than exposing executable Agent Action schemas to the browser
tool surface.

The closed schema is also an execution boundary. `WorkspaceController` validates every registered
non-final Action against its registry-owned schema before dispatch, including the action constant,
required fields, types, bounds, and undeclared fields. The Web loop validates `final` against the
same schema before verification or bodycheck gates, rendering, and publication.

The internal registry now also owns `job_start`, `job_status`, and `job_stop`. These durable local
compute actions remain behind the Agent controller and password boundary; they are summarized in
the public aggregate count but are not registered as WebMCP tools. Site tools therefore cannot
approve an optimizer, start paid compute, inspect job logs, resume a checkpoint, or stop a process.

## v1 tools

| Tool | Result | Data boundary |
| --- | --- | --- |
| `get_site_capabilities` | Seven registry-derived capability groups and seven allowlisted destinations | Does not read cached records, settings values, or Agent context |
| `get_page_context` | Site ID, title, language, route, and matching destination | Reads zero page-content fields |
| `navigate_to_site_target` | Same-origin destination and scheduling evidence | Navigates only; does not submit Cache or Agent actions |

The allowlisted destinations are `/cache/x`, `/cache/grok`, `/cache/chatgpt`, `/cache/gemini`,
`/browser`, `/agent`, and `/settings`.

## Explicit exclusions

The v1 adapter does not expose:

- Cache start, stop, status probes, reset, refresh, or background browser launch.
- Local resource search results, saved prompts, cached messages, media paths, delete, restore,
  export, reveal, or file-manager actions.
- Agent prompt submission, source discovery, session content, recursive task execution, or terminal
  authorization.
- Settings values or writes.
- Agent event records, recovery actions, or doctor state through WebMCP. Those remain local
  human-interface diagnostics behind the existing Agent request boundary.

These exclusions prevent Site tools from widening the existing trusted-LAN, password, authenticated
browser, local storage, and user-data boundaries. Future read or write tools must follow the shared
promotion workflow and reuse existing core services and route authorization.

## Agent event chain and doctor recovery

Each new Agent run receives a `run-<hex>` identifier and appends an owner-only JSONL chain
under the runtime root at `events/<run_id>.jsonl`. The chain starts with `run.started`, then links
each parsed Agent Action to an `action.requested` event, a bounded controller `observation`, and,
when applicable, a `verification` or `bodycheck` event before one terminal run event. The real loop
also records registered `agent_status`, `browser_session`, `provider_turn`, `browser_interruption`,
and `agent_response` page observations. Browser and provider interruptions are page-observation
events; Resume, Continue, and cleanup are recovery events. Event payloads are bounded and strip prompt,
response, source, command, output, and page-content fields. The persisted snapshot stores bounded
run metadata documented in `COMPUTER_USE_AGENT.md` alongside the chain health summary and the last
action/event identifiers; it does not store prompt or provider-content text.

The root event records workspace identity only as `{device,inode}`. Successful read observations and
delete audit data can retain the SHA-256 read-receipt digest, generation, and file identity; delete
also retains that digest as `delete_digest`. The browser-session event records a SHA-256 identity derived from the platform
and canonical conversation URL. The chain does not record an absolute workspace path, raw provider
URL, or file/page/provider content; ordinary action summaries may retain a bounded
workspace-relative path.

`GET /api/agent/doctor` exposes the same lifecycle, chain, verification, bodycheck, and temporary
context-cleanup checks used by the Agent page. The Doctor panel appears when attention is needed or
when a completed run has public event metadata to inspect. It renders the bounded event timeline
and offers Resume, context cleanup, provider handoff, a bound-session continuation, or a new-task
affordance as appropriate. `POST /api/agent/doctor/recover` never implicitly resubmits the original
provider prompt. Its explicit `continue` action may send one fixed generic continuation request only
when persisted evidence proves that an interrupted Edge and ChatGPT run had already bound its
conversation; it preserves the recorded permission and effort policy and does not upload context.

## Automated verification

Run the focused contract, rendering, and disposable-browser layers with:

```bash
node --test tests/test_agent_optimization.mjs
./scripts/test.sh \
  tests/test_agent_capability_registry.py \
  tests/test_agent_event_chain.py \
  tests/test_agent_doctor.py \
  tests/test_agent_optimization.py \
  tests/test_agent_optimization_browser.py
```

On Windows:

```powershell
node --test tests/test_agent_optimization.mjs
.\scripts\test.ps1 `
  tests/test_agent_capability_registry.py `
  tests/test_agent_event_chain.py `
  tests/test_agent_doctor.py `
  tests/test_agent_optimization.py `
  tests/test_agent_optimization_browser.py
```

Run the local controller acceptance separately:

```bash
./scripts/test.sh \
  tests/test_demo_flight_agentic_crud.py
```

On Windows:

```powershell
.\scripts\test.ps1 `
  tests/test_demo_flight_agentic_crud.py
```

It snapshots the SHA-256 digest of every `DEMO_FLIGHT_FILES` member from the local `demo_flight`
source, copies only that allowlist into an ephemeral workspace, exercises CRUD and cold verification
there, then rechecks the complete original allowlist after teardown. The original Demo is read-only
test input.

Run the complete project gate with:

```bash
TZ=UTC ./scripts/check.sh
```

On Windows:

```powershell
$env:TZ='UTC'; .\scripts\check.ps1
```

The suite uses pytest temporary runtime paths and disposable browser contexts where applicable. The
local Demo acceptance additionally reads its named source project as immutable input. Tests do not
open an authenticated profile, call a remote service, or touch the user-owned cache, logs, or
settings.

Current automated evidence from 30 Aug 2026: all 9 shared Node contract cases and the focused
registry, event-chain, Doctor, manifest, rendering, disposable-browser, and controller checks
passed. The agenticContext complete gate passed 1,177 Python cases and 413 subtests with
70.04% combined branch coverage. The sibling antigravity complete gate passed 954 Python tests,
293 Node tests, and 267 Chromium tests. Both projects render one manifest, register exactly the
same three WebMCP tools, and serve byte-identical shared runtime copies (SHA-256
`bffacd17ddfd40c0febd57e6ecb53022b7f5f68ba0238808433a09011214ed5c`).

## OpenAI built-in Browser smoke test

This manual check depends on current OpenAI rollout and is not a CI gate:

1. Update the ChatGPT desktop app and enable Site tools under Browser permissions.
2. Open a disposable local test instance in the built-in Browser.
3. Inspect Available Site tools and confirm exactly three tools, with two read tools and one
   navigation tool.
4. Run `get_site_capabilities` and verify seven capability groups plus seven destinations.
5. Run `navigate_to_site_target` with `local_resources`; verify the visible URL and page.
6. Review the invocation under Recently used.
7. Confirm the unsupported-browser path still preserves the ordinary UI.

Use GPT-5.6 Sol or GPT-5.6 Terra for the current OpenAI compatibility check. GPT-5.6 Luna currently
has WebMCP disabled. Treat this as client compatibility, not application logic.

Last recorded manual evidence from 28 Aug 2026: the ChatGPT built-in Browser discovered exactly
the three v1 tools on an isolated port 8677 Settings page, returned the four-capability and
seven-destination inventory, returned bounded Settings context, navigated through the Site tool to
`/browser`, and discovered a fresh three-tool set whose context matched `local_resources`. The
isolated runtime was stopped and removed after the check. The 30 Aug 2026 completion work did not
rerun this rollout-dependent smoke test.

Official reference: [OpenAI Site tools](https://learn.chatgpt.com/docs/webmcp).
