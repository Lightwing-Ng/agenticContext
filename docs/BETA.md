# Beta experiments

Documentation version: `v0.5.4`
Application version: `v1.12.0`

Beta is an optional research workspace in the Dock immediately before Settings. Its seven
experiments reuse the existing sidebar, theme, typography, controls, and local assets. Open
`/beta` to start with the first enabled experiment; with the default catalog this remains Idea
Collision. Each sidebar entry has its own URL.

As of application `v1.10.0`, the everyday text-first Zhihu workflow is promoted to
`/cache/zhihu` and `local_store/llm/zhihu/history.parquet`. This document continues to govern the
separate Beta snapshot and its provider-gap research evidence; the Beta archive stays isolated
unless its rows are explicitly migrated through the formal text-history schema.

As of application `v1.11.0`, Local resources presents the promoted archive as an answerer-first
hierarchy: the Zhihu index groups cached answers under each literal answerer name, and opening an
answerer shows that person's complete cached answer collection. The optional Beta experiment below
continues to describe capture research rather than the everyday browsing interface.

As of application `v1.12.0`, the Beta snapshot is namespaced under
`local_store/beta/zhihu/<token>/answers.parquet`. It retains its richer completeness-research
schema and remains excluded from Local resources indexing, while sharing the canonical
`local_store/` root and its next enabled ShadowBackup pass.

## Experiments

| Experiment | Route | Working prototype |
| --- | --- | --- |
| Idea Collision | `/beta/idea-collision` | Combine two supplied ideas into a structured research brief and testable next step. |
| Context Capsule | `/beta/context-capsule` | Select original source lines with references within a total character budget for a fresh agent session. |
| Question Radar | `/beta/question-radar` | Extract explicit questions, unknowns, TODOs, and blockers from supplied excerpts, ranked against the objective. |
| Memory Diff | `/beta/memory-diff` | Compare two supplied notes and surface additions, removals, and retained lines. |
| Decision Wind Tunnel | `/beta/decision-wind-tunnel` | Build a pre-mortem with assumptions, counterarguments, failure signals, and a reversible trial. |
| Mission Forge | `/beta/mission-forge` | Turn an ambition into a bounded mission with checkpoints and acceptance criteria. |
| Zhihu Answers Cache | `/beta/zhihu-answers-cache` | Capture every answer record exposed by one answerer's authenticated pagination, then search and read the local archive with explicit provider-gap accounting. |

The first six experiments are deterministic browser tools. They do not call a model, conduct semantic reasoning,
discover new facts, or verify the generated suggestions. Their outputs are structured drafts for
human review or for a separate agent session. Context Capsule's budget counts UTF-16 code units,
including the export structure and references; it is not a token budget. Question Radar uses
text heuristics and lexical objective matching; it is not a GraphRAG index, semantic search
engine, or automatic research agent.

Zhihu Answers Cache is the one network-backed Beta experiment. It runs only after an explicit
start request, uses a selected authenticated host browser, and persists one verified snapshot in
the namespaced Beta archive. It does not call a model or feed the collected content to an Agent.

## Resource and execution boundary

For the first six experiments, bring an excerpt from Local resources, a saved prompt, a cached
conversation, or your own notes by explicitly copying it into a Beta input or selecting a local
text file. Imported `.txt`, `.md`, and `.json` files are treated as literal text, with a maximum
file size of 240,000 bytes and a maximum decoded length of 60,000 UTF-16 code units. These local
experiment runtimes never read cached messages, source catalogs, Agent sessions, settings,
cookies, or local files automatically. They never start a cache task, submit an Agent prompt,
execute a command, schedule a job, or write to `local_store/`.

Experiment inputs, drafts, and results stay in the browser. Only input fields are saved in
`sessionStorage`, under `agenticcontext:beta:v1:draft:<experiment-id>`. Reloading or revisiting an
experiment in the same tab restores its own draft; results are not persisted. Clear draft removes
only that experiment's key. The Beta runtime and styles load only on Beta pages; the pure recipe
engine is imported lazily when one of those six experiments runs. Their routes remain GET-only
and have no API, background worker, polling loop, or model credential. Existing page routes and
service instances retain their current owners and behavior.

The Zhihu page is also a side-effect-free GET. Its separately registered same-origin API is the
only Beta mutation surface:

| Operation | Method and route | Contract |
| --- | --- | --- |
| Read status | `GET /api/beta/zhihu-answers-cache/status` | Read task state and a bounded archive summary; never launch a browser or create a directory. |
| Browse archive | `GET /api/beta/zhihu-answers-cache/answers` | Search answer ID, URL, question, excerpt, or body text and return one bounded page of summaries without answer bodies. |
| Read cached answer | `GET /api/beta/zhihu-answers-cache/answers/<answer-id>` | Return one stored plain-text body and record digest by stable answer ID; never contact Zhihu. |
| Start capture | `POST /api/beta/zhihu-answers-cache/start` | Validate `profile_url` and `browser`, acquire the shared cache-task lock, then start one worker. |
| Stop capture | `POST /api/beta/zhihu-answers-cache/stop` | Request cooperative stop before the commit boundary; never publish an unverified partial snapshot. |

Disabling Beta, selecting a subset that excludes Zhihu Answers Cache, or selecting no experiments
also removes this API. No other Beta experiment may call it.

The API accepts loopback requests directly. A browser using the private-network address must first
unlock the Agent password gate in the same session, then return to the Beta page. Host, source
address, and any supplied Origin must remain local and same-origin. Every API response uses
`Cache-Control: no-store`.

Start accepts a JSON object with `profile_url` and a `browser` ID, which the server normalizes. A
valid start returns HTTP 202; malformed input returns HTTP 400, and shared-lock contention returns
HTTP 409. Status may
receive `profile_url` as a query parameter to read that profile's existing archive while idle; an
active task always reports its own profile. Stop returns HTTP 202 when accepted. It returns HTTP
200 with `stop_requested: false` when no task is running or the verified atomic commit has already
begun. Status responses expose bounded counts and recent-answer metadata, never stored answer
content, raw provider responses, or browser credentials. Status reads only the Parquet metadata
columns needed for that response and reuses the result until the archive file identity changes.
Archive browsing is separate: its query is limited to 500 characters, each response is capped at
100 summaries, and bodies are returned only by exact numeric Answer ID. The browser defaults to
20 summaries per page. Both read endpoints validate the same profile and storage-root boundaries,
return `Cache-Control: no-store`, and expose plain text rather than rendering stored provider HTML.

The shared application shell retains its ordinary saved Agent navigation, theme, and sidebar
preferences. Those shared presentation helpers are not experiment inputs or execution authority.

The Run control stays disabled until the local handler initializes. Native form submission is
also suppressed, so a failed module load cannot send pasted material through a GET query.

Use the copy/export controls to move a reviewed result elsewhere. Moving a draft into an actual
Agent session remains a separate explicit action in that workspace. Research source links are
references; following one opens the referenced website.

## Zhihu Answers Cache contract

The input accepts only HTTPS `zhihu.com` or `www.zhihu.com` people URLs whose path is
`/people/<token>` or `/people/<token>/answers`. Credentials, explicit ports, other hosts, extra
path segments, and tokens outside the bounded ASCII allowlist are rejected before browser launch.
A query such as `?page=2` is accepted as a reference but never controls collection: the worker
normalizes the profile identity and starts the API snapshot at offset 0. This validation is also
the server-side SSRF boundary.

Only Chrome and Edge are supported. The worker clones the selected browser's existing profile into
an isolated temporary Chromium context, opens the canonical Answers page, and performs credentialed
same-origin JSON reads from Zhihu's member-answers API. It sends no extracted cookie or
authorization value through a separate HTTP client and never writes into the user's browser
profile. The temporary context is closed at task exit.

Collection requests a bounded 20-answer offset sequence ordered by creation time. Zhihu may return
more than 20 records for one cursor, so the worker validates the host, path, limit, and offset from
`paging.next`, discards its current HTTP scheme, and constructs the next HTTPS request itself. It
never advances by response length. Every row is keyed and deduplicated by the stable answer ID.
Repeated pages, malformed entries, an absent or changing provider total, a premature empty page,
or a safety-limit breach make the snapshot incomplete. `paging.is_end` is necessary but not
sufficient for completion. Before publication, all of these conditions must hold:

1. The API has reported its terminal page.
2. One `paging.totals` value remains stable across every page.
3. The unique count equals that total, or duplicate-backed pagination leaves a smaller enumerable
   set that a second complete pass reproduces with the same ordered IDs, page count, raw count,
   duplicate count, terminal cursor, and total.
4. A new offset-0 read has the same total and ordered answer-ID fingerprint as the original first
   page.
5. The atomically written Parquet file can be read back with the same row count and capture metadata.

Only a snapshot that passes all five checks replaces
`local_store/beta/zhihu/<token>/answers.parquet`. The path is rooted beneath the configured
Local resources store, the token is validated before it becomes a directory name, and the archive
remains in a dedicated Beta namespace. It is not indexed by Local resources or included in any
source-specific Cache reset. An enabled ShadowBackup copies it on the next backup pass, although
the Beta capture itself does not start that backup. The worker still acquires the application-wide cache task lock, whose
advisory metadata remains at `local_store/.cache_task.lock`, so a Zhihu capture cannot overlap an
X, Grok, ChatGPT, Gemini, or Claude cache job.

Rows retain the normalized author, question, answer URL, timestamps, public counters, answer HTML,
plain text, content hash, state flags, and deduplicated HTTPS Zhihu image references. An explicitly
collapsed record with no provider body is retained with `content_available=false`; its excerpt is
not promoted into invented body text. Provider fields that are absent, including `is_normal` or a
vote count, remain null rather than becoming false facts. Provider
relationship, viewer-vote, cookie, and raw-response fields are excluded. Media URLs are metadata
only in this version; image binaries are not downloaded for offline use.

The Parquet metadata records the provider-reported total, enumerable unique count, provider gap,
page count, raw record count, duplicate occurrences, and whether one or two stability passes were
required. The UI labels these separately as Reported, Enumerated, Cached, and Not enumerable. It
never creates placeholder rows for IDs that Zhihu does not reveal.

An accepted cooperative stop before `commit_pending`, authentication failure, human-verification
challenge, unstable pagination, parse failure, network exhaustion, or pre-commit write validation
failure preserves the previous Parquet file. Once the verified snapshot enters the atomic
`committing` phase, Stop is no longer accepted and that commit is allowed to finish; it never
publishes only the rows collected before verification. HTTP 401/403, Zhihu error code `40352`,
`need_login`, and the provider's human-verification page are terminal verification states, not
transient retries. The application does not solve, bypass, or automate a CAPTCHA. The operator
must complete any requested verification manually in the selected host browser and explicitly
start a new run. Only bounded transient transport, HTTP 408/425/429, or server-error retries are permitted.

## Enable, disable, and select experiments

Beta is enabled by default. Before the next normal application launch, set
`AGENTIC_CONTEXT_BETA_ENABLED=0` to remove both its routes and its Dock entry. The environment
values `1`, `true`, `yes`, and `on` enable it, ignoring case and surrounding whitespace; all other
values disable it. This flag is resolved when the application is created. Changing it does not
alter a running service.

Code that embeds the application may override the environment explicitly:

```python
# Code version: v0.3.0
from app.web.app import create_app

application = create_app(beta_enabled=False)
```

The factory can also install a subset without editing the shared navigation:

```python
# Code version: v0.3.0
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
./scripts/test.sh tests/test_beta_routes.py tests/test_beta_e2e.py tests/test_zhihu_answers.py
node --test tests/test_beta_engines.mjs
```

On Windows:

```powershell
.\scripts\test.ps1 tests/test_beta_routes.py tests/test_beta_e2e.py tests/test_zhihu_answers.py
node --test tests/test_beta_engines.mjs
```

These checks cover all seven page routes, unchanged default selection, disabled mode, partial
installation, Dock order, existing-page asset isolation, and the production content and service
boundary. `tests/test_zhihu_answers.py` uses fixtures and temporary directories to cover strict URL
normalization, answer parsing, cursor validation, collapsed/null fields, ID deduplication, stable
provider-gap and first-page verification, atomic replacement and metadata readback, stop/failure
preservation, and mocked worker lifecycle behavior. Flask and browser
checks exercise the same-origin start/status/stop contract without opening an authenticated
profile or contacting Zhihu. The six local experiments retain their bounded import, browser-only
storage, deterministic engine, export, and responsive coverage. A fixture-backed macOS check is
not live provider evidence or native Windows evidence.

### Verification record: 10 Sep 2026

Application `v1.9.0`, Beta `v0.2.0`:

- The focused Python contract passed 108 cases. The isolated Chromium suite passed all 28 cases at
  desktop and narrow widths, including the seventh navigation entry, disabled/subset isolation,
  same-origin API controls, progress and provider-gap labels, completion, stop, and error states.
  All 13 Node engine cases passed. Ruff, Python compilation, JavaScript syntax, documentation
  checks, and `git diff --check` passed after the final archive-publication and status-recovery
  regressions.
- An authenticated Edge clone completed a live capture for `feifeimao` without changing the
  user's browser profile or automating a human-verification challenge. Zhihu reported 333 answers;
  17 pages yielded 426 raw occurrences, 328 stable unique IDs, 98 duplicate occurrences, and a
  provider gap of five. A second complete pass reproduced the ordered IDs and pagination metrics
  exactly before publication.
- Independent Parquet readback verified 328 unique rows, canonical answer URLs, every content
  hash, and 583 HTTPS Zhihu/Zhimg media references. One provider-collapsed answer was retained as
  metadata with `content_available=false`; the other 327 records include provider body content.
  The verified archive was originally captured at `beta_store/zhihu/feifeimao/answers.parquet`
  and now resides at `local_store/beta/zhihu/feifeimao/answers.parquet`. No candidate temporary
  archive remains.
- The complete repository gate finished with 2,074 passed, 29 failed, 18 skipped, and 457 passing
  subtests; branch coverage was 70.16%. All then-collected Beta and Zhihu cases passed. The gate is
  not green: its failures are existing Agent, provider-wait, architecture, Demo Flight,
  responsive, account, style-token, timestamp, and pagination-contract baselines in files
  unchanged by this feature. The final provider-gap and idle-status refinements passed the focused
  checks above after that complete-gate run.
- No process was listening on the user-owned port 8666, so this task did not start or restart that
  service. The source and isolated app are verified; the next normal application launch will load
  the feature. No native Windows or live Windows-provider run is claimed.

### Verification record: 11 Sep 2026

Application `v1.9.1`, Beta `v0.3.0` adds the missing local readback surface. The archive list now
searches the complete persisted corpus with bounded pagination, and `View cached copy` reads one
plain-text body by stable Answer ID. The original Zhihu link remains a separate explicit action.
The read APIs reuse the existing local-origin and LAN-unlock boundary and never contact the
provider. Focused and live-readback evidence is recorded in the task handoff rather than changing
the prior 10 Sep 2026 capture record.
- Worthward source was not changed. The shared synchronization ledger records this Beta surface as
  a product-specific adaptation pending sibling review; no cross-project parity is claimed.

### Historical verification record: 7 Sep 2026

This record predates Zhihu Answers Cache and therefore verifies only the original six local
experiments. Do not use it as evidence that the seventh experiment or a live Zhihu capture passed.

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
