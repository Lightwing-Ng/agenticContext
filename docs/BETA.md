# Beta experiments

Documentation version: `v1.0.0-codex.1`

Application version: `v1.13.0`

Beta is an optional browser-local research playground. Its tools transform only the text the user
pastes or explicitly imports. They do not invoke a model, start a provider browser, call a server
API, or persist output beneath `local_store/`.

## Catalog

| Experiment | Route | Purpose |
| --- | --- | --- |
| Idea Collision | `/beta/idea-collision` | Cross two fragments and produce a testable research brief. |
| Context Capsule | `/beta/context-capsule` | Select evidence with line references within a character budget. |
| Question Radar | `/beta/question-radar` | Rank explicit questions, unknowns, TODOs, and blockers against an objective. |
| Memory Diff | `/beta/memory-diff` | Compare earlier and later notes before carrying context forward. |
| Decision Wind Tunnel | `/beta/decision-wind-tunnel` | Expose assumptions, counterarguments, failure signals, and reversible probes. |
| Mission Forge | `/beta/mission-forge` | Turn an open-ended ambition into a bounded Agent brief. |

`GET /beta` and `GET /beta/` select Idea Collision. Every experiment has its own canonical
`GET /beta/<experiment-id>` route. Unknown IDs return `404`.

## Runtime and persistence boundary

`app/web/beta.py` owns the immutable catalog and GET-only Flask blueprint.
`app/web/templates/beta.html` owns the shared page structure.
`app/web/static/beta/beta.js` owns input, draft, import, copy, and export behavior.
`app/web/static/beta/engines.mjs` contains deterministic transformations and is loaded only after
the user asks to run an experiment. `app/web/static/beta/beta.css` is scoped under `.beta-page`.

Draft fields use `sessionStorage` keys under:

```text
agenticcontext:beta:v1:draft:<experiment-id>
```

This storage is limited to the current browser tab. It is not copied into application caches,
server logs, Agent sessions, settings, or Local resources. Clearing one draft does not clear the
other experiments. Imported `.txt`, `.md`, and `.json` files are read in memory and never uploaded.

The page has no form action and all mutating HTTP methods return `405`. When JavaScript is disabled
or blocked, source material remains in the page and cannot be submitted to the server.

## Optional registration

`create_app(beta_enabled=False)` or `AGENTIC_CONTEXT_BETA_ENABLED=0` removes the Beta blueprint and
Dock entry. `create_app(beta_experiments=[...])` can select an ordered subset from the catalog. An
empty selection removes Beta; a string or unknown ID fails before service initialization.

The Beta Dock item appears immediately before Settings only when at least one experiment is
registered. Existing Cache destination memory remains independent.

## Verification

Run the focused route, browser, and engine contracts on macOS or Linux:

```bash
./scripts/test.sh tests/test_beta_routes.py tests/test_beta_e2e.py
node --test tests/test_beta_engines.mjs
```

On Windows:

```powershell
.\scripts\test.ps1 tests/test_beta_routes.py tests/test_beta_e2e.py
node --test tests/test_beta_engines.mjs
```

The tests use isolated application roots and a disposable browser. They verify the six-item
catalog, GET-only boundary, optional registration, browser-local draft isolation, file-import
limits, output escaping, responsive geometry, and absence of external requests.
