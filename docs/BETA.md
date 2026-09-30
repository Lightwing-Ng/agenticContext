# Beta experiments

Documentation version: `v1.1.0-codex.0`

Application version: `v1.14.0`

Beta module version: `v0.5.0`

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
| Echo Atlas | `/beta/echo-atlas` | Trace repeated vocabulary across distinct source lines and inspect its original context. |
| Curiosity Trail | `/beta/curiosity-trail` | Follow an objective through an anchor, a contrasting passage, and an observation prompt. |

`GET /beta` and `GET /beta/` select Idea Collision. Every experiment has its own canonical
`GET /beta/<experiment-id>` route. Unknown IDs return `404`.

## Guided experiments

All eight experiments use the existing shared Process List and native Collapse controls.
The first disclosure starts open for source material, an optional objective, examples, and the
run action. The following steps guide the user to inspect the result and challenge an apparent
connection before carrying it into another task. These are product-specific compositions of
the same controls used by Tunnel onboarding and Worthward Beta; they do not define new shared
component styles.

Start with a copied conversation, a saved Prompt, or personal notes. Use `Try example` to
explore a sample, or import an explicit text export. Review the source references in the result,
then copy or export the useful observations. A draft belongs only to its experiment and browser
tab; moving to another experiment preserves that experiment's independent draft.

### Echo Atlas

Echo Atlas counts recurring vocabulary across distinct source lines. It uses normalized English
words of 3–32 characters and adjacent pairs of Chinese characters, with a small stopword list.
Repeated occurrences in one line count once; lines equivalent after Unicode, case, and whitespace
normalization also count once. It shows up to eight motifs that each occur in at least two distinct
lines, with up to four source excerpts per motif. Each excerpt preserves a line reference and
identifies any prefix truncation. Insufficient or nonrecurring material produces an explicit
empty result.

An echo is a lexical recurrence, not proof of a shared meaning, importance, agreement, or
causality. Read the cited lines before naming a theme yourself. The optional objective helps
direct attention; changing it does not change the original source material. The inspiration is
deliberate context selection described in [Anthropic's context engineering article](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents),
not an implementation of its agent architecture.

### Curiosity Trail

Curiosity Trail selects up to three distinct source lines: an objective anchor and two further
passages chosen for low lexical overlap, with a preference for source-line distance. Each stop
retains its original line reference and adds a prompt for an observation or small experiment.
The same source and objective produce the same trail. Changing the objective can change the
anchor and reveal another route through the material. Fewer than three distinct lines produce
only the available stops and a request for more material; the engine does not invent passages.

Lexical contrast is a starting point for curiosity, not evidence that passages are semantically
unrelated or that the proposed observation will succeed. Line distance is not elapsed time.
The user supplies the interpretation
and decides whether to act. [Stanford d.school's Design Thinking Bootleg](https://dschool.stanford.edu/tools/design-thinking-bootleg)
provides inspiration for moving from exploration toward a small test; this experiment does not
reproduce or validate that method.

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

The tests use isolated application roots and a disposable browser. They verify the eight-item
catalog, GET-only boundary, optional registration, browser-local draft isolation, file-import
limits, output escaping, responsive geometry, and absence of external requests. Guided-flow
checks cover keyboard-operable disclosures and readable steps at desktop and narrow widths.

## Acceptance evidence, 30 Sep 2026

Beta v0.5.0 passed the following focused checks:

- `./scripts/test.sh -s tests/test_beta_routes.py tests/test_beta_e2e.py`: 63 passed
  (34 route cases and 29 isolated Chromium cases).
- `node --test tests/test_beta_engines.mjs`: 22 passed.
- `./scripts/test.sh tests/test_style_tokens.py -k 'process_list or shared_collapse or beta_dock'`:
  3 passed, 138 deselected.
- Ruff on the changed Python files, JavaScript syntax checks, `scripts/check_docs.py`,
  and `git diff --check`: passed.

Browser acceptance covers desktop/narrow Light and Dark, short touch reachability,
keyboard disclosures, source-safe rendering, independent drafts, imports, and exports.
The live service on port 8666 served v0.5.0 through its existing hot reload and ran
both new built-in examples without console errors. No service restart was performed.
The full repository quality gate was not run. The shared UI ledger records this as
an adapter using existing primitives, with no new sibling synchronization obligation.
