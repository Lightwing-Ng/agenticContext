# Beta experiments

Documentation version: `v0.1.1`
Application version: `v1.8.0`

Beta is an optional research workspace in the Dock immediately before Settings. Its six
experiments reuse the existing sidebar, theme, typography, controls, and local assets. Open
`/beta` to start with the first enabled experiment; each sidebar entry has its own URL.

## Experiments

| Experiment | Route | Working prototype |
| --- | --- | --- |
| Idea Collision | `/beta/idea-collision` | Combine two supplied ideas into a structured research brief and testable next step. |
| Context Capsule | `/beta/context-capsule` | Select original source lines with references within a total character budget for a fresh agent session. |
| Question Radar | `/beta/question-radar` | Extract explicit questions, unknowns, TODOs, and blockers from supplied excerpts, ranked against the objective. |
| Memory Diff | `/beta/memory-diff` | Compare two supplied notes and surface additions, removals, and retained lines. |
| Decision Wind Tunnel | `/beta/decision-wind-tunnel` | Build a pre-mortem with assumptions, counterarguments, failure signals, and a reversible trial. |
| Mission Forge | `/beta/mission-forge` | Turn an ambition into a bounded mission with checkpoints and acceptance criteria. |

These are deterministic browser tools. They do not call a model, conduct semantic reasoning,
discover new facts, or verify the generated suggestions. Their outputs are structured drafts for
human review or for a separate agent session. Context Capsule's budget counts UTF-16 code units,
including the export structure and references; it is not a token budget. Question Radar uses
text heuristics and lexical objective matching; it is not a GraphRAG index, semantic search
engine, or automatic research agent.

## Resource and execution boundary

Bring an excerpt from Local resources, a saved prompt, a cached conversation, or your own notes
by explicitly copying it into a Beta input or selecting a local text file. Imported `.txt`,
`.md`, and `.json` files are treated as literal text, with a maximum file size of 240,000 bytes
and a maximum decoded length of 60,000 UTF-16 code units. Beta's experiment runtime never reads cached messages, source catalogs,
Agent sessions, settings, cookies, or local files automatically. It never starts a cache task,
submits an Agent prompt, executes a command, schedules a job, or writes to `local_store/`.

Experiment inputs, drafts, and results stay in the browser. Only input fields are saved in
`sessionStorage`, under `agenticcontext:beta:v1:draft:<experiment-id>`. Reloading or revisiting an
experiment in the same tab restores its own draft; results are not persisted. Clear draft removes
only that experiment's key. The Beta runtime and styles load only on Beta pages; the pure recipe
engine is imported lazily when an experiment runs. No Beta API, background worker, polling loop, or model credential is added. The
optional Flask Blueprint serves GET pages from immutable experiment metadata; POST, PUT, PATCH,
and DELETE are not accepted. Existing page routes and service instances retain their current
owners and behavior.

The shared application shell retains its ordinary saved Agent navigation, theme, and sidebar
preferences. Those shared presentation helpers are not experiment inputs or execution authority.

The Run control stays disabled until the local handler initializes. Native form submission is
also suppressed, so a failed module load cannot send pasted material through a GET query.

Use the copy/export controls to move a reviewed result elsewhere. Moving a draft into an actual
Agent session remains a separate explicit action in that workspace. Research source links are
references; following one opens the referenced website.

## Enable, disable, and select experiments

Beta is enabled by default. Before the next normal application launch, set
`AGENTIC_CONTEXT_BETA_ENABLED=0` to remove both its routes and its Dock entry. The environment
values `1`, `true`, `yes`, and `on` enable it, ignoring case and surrounding whitespace; all other
values disable it. This flag is resolved when the application is created. Changing it does not
alter a running service.

Code that embeds the application may override the environment explicitly:

```python
# Code version: v0.1.0
from app.web.app import create_app

application = create_app(beta_enabled=False)
```

The factory can also install a subset without editing the shared navigation:

```python
# Code version: v0.1.0
from app.web.app import create_app

application = create_app(
    beta_enabled=True,
    beta_experiments=("context-capsule", "mission-forge"),
)
```

The subset preserves catalog order. Duplicate IDs are deduplicated; an empty collection removes
the entire Beta Blueprint and Dock entry. Unknown IDs or a string in place of a collection raise
`ValueError` before existing services are initialized. A disabled Beta ignores the subset.
Unknown or disabled experiment URLs return HTTP 404.

This is modular application isolation, not a separate operating-system process or security
origin. Beta shares the existing site shell and browser origin; its code must preserve the
explicit data and execution boundaries above. Removing the Blueprint does not remove static
source assets from the application's static directory.

## Research references

The experiments adapt workflow ideas from these primary sources. They do not implement the
systems described by the sources or claim equivalent capabilities.

- [Google DeepMind: AI co-scientist](https://deepmind.google/blog/co-scientist-a-multi-agent-ai-partner-to-accelerate-research/)
  informs Idea Collision's hypothesis and test structure.
- [Anthropic: Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
  informs Context Capsule and Memory Diff's explicit context handoff.
- [Microsoft GraphRAG: Query overview](https://microsoft.github.io/graphrag/query/overview/)
  informs Question Radar's distinction between narrow questions and broader corpus questions.
- [Anthropic: Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
  informs Decision Wind Tunnel and Mission Forge's explicit evidence and success criteria.

## Verification

Run the focused route and isolation contract:

```bash
./scripts/test.sh tests/test_beta_routes.py tests/test_beta_e2e.py
node --test tests/test_beta_engines.mjs
```

On Windows:

```powershell
.\scripts\test.ps1 tests/test_beta_routes.py tests/test_beta_e2e.py
node --test tests/test_beta_engines.mjs
```

These checks cover the six routes, default selection, disabled mode, partial installation,
GET-only behavior, Dock order, existing-page asset isolation, and the production content and
service boundary. Browser checks verify experiment interactions, storage behavior, bounded
imports, export, static assets, responsive geometry, and navigation. Node checks exercise the
pure engines, adversarial source text, deterministic behavior, and complete-export character
budgets. Both quality-gate entrypoints include the engine tests. A source or macOS check is not
native Windows evidence.

### Verification record: 7 Sep 2026

Application `v1.8.0`, Beta `v0.1.0`:

- Focused checks passed: 25 route/isolation cases, 13 engine cases, and 21 distinct Chromium
  cases across the focused runs. The browser coverage includes all six experiments, light/dark
  themes at 1,024px and 390px, exact restoration of two 60,000-character escaped drafts, failure
  handling, and preservation of unrelated browser state.
- The complete macOS gate finished with 1,761 passed, 39 failed, 15 skipped, and 443 passing
  subtests. All collected Beta cases passed. The final large-draft regression was added after
  gate collection and passed in a separate focused run. Ruff, documentation checks, JavaScript
  syntax, and all 22 Node cases passed. The fresh branch-inclusive coverage report was 71.93%,
  above the 55% threshold. The repository gate is not green.
- Existing failures concern Agent activity/session selectors and hydration, account probe and
  shared-layout expectations, source-token assertions, and a stale Cache script assertion.
  Representative failures were reproduced with `AGENTIC_CONTEXT_BETA_ENABLED=0`; the relevant
  existing implementations were compared against the starting Git revision. These findings
  remain separate follow-up work and were not repaired as part of Beta.
- The user-owned service on port 8666 was preserved and still returns HTTP 404 for `/beta`.
  A normal service restart is required to load the new Blueprint and templates; isolated test
  servers verified the new routes. No native Windows run is claimed.
- Numbered-copy housekeeping retained 18 existing candidates: 15 protected files in
  `local_store/` and `logs/`, and three differing coverage copies. No candidate was deleted.
- Sibling UI sync pending: the conditional Dock slot and Beta consumers remain a candidate
  for review against Worthward in the shared synchronization ledger.

The complete gate used this host's dependency-complete Python interpreter through the supported
override; the application requirement remains Python 3.13 or newer:

```bash
TZ=UTC AGENTIC_CONTEXT_PYTHON=/usr/local/bin/python3.13 ./scripts/check.sh
```
