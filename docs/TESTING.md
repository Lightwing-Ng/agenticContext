# Testing guide

Documentation version: `v1.7.8-codex.1`

## Supported commands

Install the runtime and development dependencies with the supported Python 3.13/3.14 workflow:

```bash
./scripts/setup_python.sh
```

On Windows:

```powershell
.\scripts\setup_python.ps1
```

Run the ordinary offline suite:

```bash
./scripts/test.sh
```

On Windows:

```powershell
.\scripts\test.ps1
```

Run a focused test or marker selection:

```bash
./scripts/test.sh tests/test_local_media_browser.py
AGENTIC_CONTEXT_TEST_MARK_EXPRESSION=integration ./scripts/test.sh
```

On Windows:

```powershell
.\scripts\test.ps1 tests/test_local_media_browser.py
$env:AGENTIC_CONTEXT_TEST_MARK_EXPRESSION='integration'; .\scripts\test.ps1
```

The default marker expression is `not live`. Run an intentionally manual live check only with an
explicit override, for example `AGENTIC_CONTEXT_TEST_MARK_EXPRESSION=live ./scripts/test.sh`.

On Windows:

```powershell
$env:AGENTIC_CONTEXT_TEST_MARK_EXPRESSION='live'; .\scripts\test.ps1
```

Run the complete quality gate:

```bash
./scripts/check.sh
```

On Windows:

```powershell
.\scripts\check.ps1
```

Run the isolated local-compute benchmark:

```bash
/usr/local/bin/python3.13 scripts/benchmark_compute.py
```

On Windows:

```powershell
py -3.13 scripts/benchmark_compute.py
```

Run the durable optimization-job contract tests without a long-lived workload:

```bash
./scripts/test.sh tests/test_compute_jobs.py tests/test_agent_capability_registry.py
```

On Windows:

```powershell
.\scripts\test.ps1 tests/test_compute_jobs.py tests/test_agent_capability_registry.py
```

When the quality gate fails, read the [CI Failure Playbook](CI_FAILURE_PLAYBOOK.md) before changing
production code, tests, platform branches, or retry behavior.

Run the responsive contract and sidebar browser layers independently with:

```bash
./scripts/test.sh tests/test_responsive_contract.py
./scripts/test.sh tests/test_sidebar_e2e.py
```

On Windows:

```powershell
.\scripts\test.ps1 tests/test_responsive_contract.py
.\scripts\test.ps1 tests/test_sidebar_e2e.py
```

Run the OpenAI Site tools contract and disposable-browser layers independently with:

```bash
node --test tests/test_agent_optimization.mjs
./scripts/test.sh \
  tests/test_agent_optimization.py \
  tests/test_agent_optimization_browser.py
```

On Windows:

```powershell
node --test tests/test_agent_optimization.mjs
.\scripts\test.ps1 `
  tests/test_agent_optimization.py `
  tests/test_agent_optimization_browser.py
```

`AGENTIC_CONTEXT_PYTHON` may override the interpreter only when it resolves to Python 3.13 or 3.14.
The resolver prefers a supported host `python3`, `python`, or Windows `py -3.13` launcher, then
falls back to known platform-specific Python
installations.

## Quality gate

The canonical local and CI quality gate is `scripts/check.sh` on macOS/Linux and
`scripts/check.ps1` on Windows. The gates run, in order:

1. Ruff static checks over `main.py`, `app/`, and `tests/`.
2. `node --check` for every first-party JavaScript file in `app/web/static/`.
3. Node unit tests for the shared Agent Optimization and Site tools runtime contract.
4. The full pytest suite with branch coverage for `app/`, including the disposable-browser
   responsive sidebar E2E flow.

The coverage report is written to `test-results/coverage.json`; all generated test artifacts are
ignored by Git. The gate currently enforces a 55% combined statement-and-branch coverage floor.
Override `AGENTIC_CONTEXT_COVERAGE_MINIMUM` only for an intentional local diagnostic, never to make a
regression pass.

Baseline remeasured on 28 Aug 2026 with Python 3.13.0, pytest 9.0.3,
pytest-cov 7.1.0, and Ruff 0.15.21:

- 1,119 tests passed, with 380 unittest subtests passed.
- Combined coverage for `app/` was 69.31% using branch coverage.
- All first-party JavaScript files passed syntax checks.
- All 9 shared Agent Optimization Node contract cases passed.

Raise the coverage floor only after adding behavior-level tests. Do not exclude production modules
or lower the threshold to mask a gap.

## CI portability contract

GitHub Actions is the canonical clean-room gate. It runs the quality contract on `ubuntu-latest`
and `windows-latest` with Python 3.13, Node.js 22, UTC, and a freshly installed Playwright
Chromium. A test that passes only on the developer's macOS workstation is not evidence of a
passing project contract.

Use this command to reproduce the CI timezone locally:

```bash
TZ=UTC AGENTIC_CONTEXT_PYTHON=/usr/local/bin/python3.13 ./scripts/check.sh
```

On Windows:

```powershell
$env:TZ='UTC'; .\scripts\check.ps1
```

Tests must follow these rules:

- Do not hard-code local-time output such as `13:00`. Use UTC fixtures, the production formatter,
  or an explicit timezone in the assertion.
- Settings read/write helpers must resolve `AGENTIC_CONTEXT_SETTINGS_PATH` at call time. Do not use a
  module-import snapshot as a function default, because pytest and clean-room CI inject the
  process boundary before application startup.
- Do not hard-code `Finder`, `open -R`, or macOS permission messages in a host-neutral test. Test
  each platform through explicit platform arguments or monkeypatch the host predicate when the
  test is specifically for macOS or Windows.
- The yt-dlp cookie-source mapper may return the explicit `safari` backend on every CI host;
  browser automation remains host-aware and must still reject Safari where the runtime registry
  does not expose it.
- Overlay sidebars must use an explicit viewport-bounded height with internal scrolling. Do not
  rely on intrinsic fixed-position height on touch viewports, because Chromium implementations
  can report a transient bottom edge outside `window.innerHeight`.
- When `prefers-reduced-motion: reduce` is active, the sidebar shell, overlay, and toggle must
  disable their transitions entirely. The JavaScript state timer may remain short for bookkeeping,
  but the geometry used by touch hit testing must already be final when the state attribute changes.
- DOM observers must not continuously observe attributes that their callbacks write on every pass.
  Observer-backed controls must make attribute, class, and style updates idempotent, and a static
  contract test must pin the observed attribute set.
- Keep filesystem, browser-profile, and subprocess tests inside `tmp_path` or an explicit fake
  platform boundary. Never inspect the runner's real browser profile or file manager.
- Browser E2E tests must use a clean context and local Flask server only. Disable or stub unrelated
  background polling when the test injects a DOM fixture; otherwise the application may replace
  the fixture during the assertion.
- For responsive geometry and hit testing, wait for the relevant rectangle to become stable before
  asserting `document.elementFromPoint()`. Reduced motion shortens transitions but is not a promise
  that a DOM update is synchronous.
- Keep live, authenticated, and remote-service checks under `@pytest.mark.live`; they do not belong
  in the default quality gate.
- CPU compute tests use synthetic image bytes and temporary paths. GPU tests inject a fake adapter
  and verify that partial, duplicate, unknown, OOM, failed, or initialization-failed batches are
  discarded and recomputed in full on CPU. Payload-budget tests verify that the parent flushes before
  oversized files and analyzes them without queueing their bytes. No test treats a fake adapter,
  static inspection, or an HTTP response as GPU evidence.
- Durable compute-job tests use short approved fake workers and temporary runtime roots. They pin
  12-hour configuration without waiting 12 hours, verify provider-turn-independent survival,
  idempotent start, atomic checkpoint and explicit resume, restart reconciliation, PID-reuse
  refusal, owned process-tree stop, rolling logs, path and symlink rejection, and unchanged
  workspace fingerprints. A test must never launch a long optimization or write below the
  production Agent runtime root.

## Local-compute benchmark contract

The benchmark uses a deterministic `1,024 × 1,024` PNG fixture, one warmup run, five measured runs,
and reports the full wall-time and parent CPU-time samples plus medians, together with numeric
backend metrics from the final optimized run. On this host, the 32-image
fixture measured a legacy sequential wall-time median of approximately `0.310 s`, while the
bounded CPU path measured approximately `0.307 s`; the process
backend was intentionally not selected for that size because process startup made it slower. With
the same fixture expanded to 128 images, the legacy median was approximately `1.246 s` and the
bounded CPU backend median was approximately `0.510 s`. These latest recorded values are local
synthetic evidence,
not a claim about browser, network, or GPU throughput. Remote discovery, browser automation,
downloads, Safari, Parquet commit, and backup stages remain outside GPU acceleration unless a
separate live profile proves otherwise.

When a gate fails on GitHub, reproduce the exact failing node first with `TZ=UTC`, then run the
complete platform gate: `./scripts/check.sh` on macOS/Linux or `.\scripts\check.ps1` on Windows.
Do not weaken an assertion, skip a platform branch, lower coverage, or add a retry until the
failure has been classified as a real product regression or a test environment assumption.

## Test organization

- Pure unit tests cover URL normalization, source parsing, media classification, state
  transitions, durable queue behavior, retries, and path validation.
- Filesystem regression tests use `tmp_path` or `TemporaryDirectory` for catalogs, manifests,
  media files, deleted previews, and settings.
- Flask integration tests use `create_app()` plus `test_client()` and assert route contracts
  without starting a web server.
- Computer Use Agent tests build context packages in temporary projects, execute the controller
  through deterministic actions, and replace the signed-in browser runner with a fake.
- Durable compute-job tests construct a temporary approval manifest whose entrypoint SHA-256
  matches a short fake optimizer. Source, config, checkpoint, progress, result, metadata, log, and
  process identities are asserted through public job operations; tests do not weaken the existing
  verification-only `run` contract.
- Style-token, template, and responsive-contract tests protect durable UI boundaries directly.
- Sidebar E2E tests start an isolated local Flask server and a clean headless Chromium context.
  They cover touch input, backdrop dismissal, viewport transitions, real hit testing through
  `document.elementFromPoint()`, and the shared Chinese language boundary across startup and
  dynamic DOM mutations. The language test checks source-text preservation, `:lang(zh-CN)`
  matching, and the macOS-oriented glyph fixture without converting Unicode text.
- Agent Optimization tests validate manifest bounds, schema closure, read/write annotations,
  same-origin navigation, unsupported-browser fallback, iframe exclusion, registration idempotence,
  partial registration failure, protected-page exclusion, and a real random-port Chromium lifecycle.

The current detailed module-to-behavior map is maintained in [TEST_COVERAGE.md](TEST_COVERAGE.md).

## Isolation contract

`tests/conftest.py` runs before application test modules are imported. It redirects all default
runtime locations to process-scoped temporary directories:

- `HOME` keeps settings and browser-profile defaults away from the user account.
- `AGENTIC_CONTEXT_RUNTIME_ROOT` moves default local caches and logs away from the repository.
- `AGENTIC_CONTEXT_SETTINGS_PATH` redirects persisted settings.

Default tests must not:

- open an authenticated Chrome, Edge, Safari, or Playwright profile;
- make X, Grok, ChatGPT, yt-dlp, or general network requests;
- read, copy, delete, reset, or restore a user-owned cache, log, setting, or browser profile;
- submit a real background cache job.

Mock external boundaries at the module that invokes them. Existing patterns mock
`sync_playwright`, `launch_chromium_context`, `subprocess.run`, `urlopen`, the scraper, and
source download functions. Do not replace a lower-level implementation when the route or service
boundary is the behavior being tested.

## Markers

- `integration`: Flask tests that cross module boundaries and remain fully offline.
- `slow`: Tests materially slower than the unit-test median.
- `live`: Explicit manual checks that require a signed-in browser or a remote service. These never
  belong in CI or the default quality gate.

The sidebar E2E layer may use Playwright-managed Chromium or an installed Chrome or Edge binary,
but always launches a clean browser context without a user-data directory. Its Flask server uses
the same process-scoped temporary runtime as the rest of pytest. Browser tests must retain both
isolation properties and must never navigate to an external service.

## Writing a new test

1. Put the test beside the behavior it protects under `tests/test_<area>.py`.
2. Prefer a public behavior or invariant over assertions about incidental implementation details.
3. Use a temporary filesystem location for every test-owned file.
4. Use the `client` fixture for route contracts and preserve the injected runtime boundary.
5. Add a marker only when it accurately describes the test's cost or boundary.
6. Run the focused test, then `./scripts/check.sh` on macOS/Linux or `.\scripts\check.ps1` on
   Windows before handoff.

## Gemini Web Agent model verification, 6 Sep 2026

Controller version: `v3.56.0-codex.1`.

- `python3.13 -m pytest tests/test_computer_use_agent.py -q`: 448 passed.
  Includes 3.8 Flash settings validation, the shared action loop through bodycheck/final,
  and refusal to transfer project context when model verification fails.
- `python3.13 -m pytest tests/test_sidebar_e2e.py -q -k gemini_model_dom`:
  64 passed. Before the fix, the six positive exact-proof cases produced five failures:
  Flash was unsupported and Chinese selected markers were rejected.
- `python3.13 -m pytest tests/test_sidebar_e2e.py -q -k agent_recent_provider_sessions_submit_agentic_task_target`:
  Three passed, including both Gemini versions and the existing Grok path. Requests are
  intercepted; this verifies local selection and task serialization, not a remote response.
- `python3.13 -m pytest tests/test_sidebar_e2e.py -q -k gemini_model_picker_keeps_both_versions_selectable`:
  Two passed at 1,138px and 390px, with both choices selectable and no horizontal overflow.
- Ruff and `git diff --check` passed.

Live Edge inspection confirmed versioned 3.1 Pro and 3.8 Flash menu entries and successfully
selected each, reopening the menu to read back the selected marker. The original Flash choice
was restored and the menu closed. No project context or test prompt was sent to Gemini.
The existing 8666 listener belongs to this checkout but reports a running Agent task and still
serves the prior Pro-only catalog. It was not restarted. Normal service restart after that task
finishes, followed by a real Gemini Agent task, remains the production acceptance step.

Numbered-copy housekeeping retained 12 protected log/provider-state files and three coverage
copies; no files were removed. Detailed metadata is in the task-local
`/tmp/agenticcontext-gemini-housekeeping.json` report.

The complete `./scripts/check.sh` run passed Ruff, JavaScript syntax, all nine JavaScript
unit tests, and the 55% coverage threshold (71.17% observed). Pytest completed with 1,525
passed, 13 failed, and 560 subtests passed. The two additional narrow/desktop model-picker
cases were run separately after the full gate had collected its tests.

The unresolved full-gate failures are:

- Four `test_activity_preserves_collapse_and_tracks_current` cases: the test expects
  `checkmark.circle.fill.svg`, while current CSS uses `checkmark.circle.svg`. The exact
  `reduce-390` case also failed independently under `TZ=UTC`.
- `test_cache_sidebars_reuse_the_chatgpt_base_contract`: duplicate matching elements.
- Two `test_agent_response_pagination_is_immersed_but_keeps_interactive_effects` cases:
  an effect-clearance assertion observes 10px where at least 23px is expected.
- Three `test_chatgpt_effort_footer_keeps_the_fifteen_pixel_label_on_one_line` cases:
  the rendered label is `Latest`, while the test expects `Best available`.
- `test_cache_action_row_switches_stop_visibility_with_running_state`: `View text history`
  is rendered where `Start` is expected.
- `test_successful_agent_completion_collapses_activity_without_erasing_a_new_draft`:
  the existing prompt-scroll assertion is `128 > 140`.
- `test_cache_summary_metrics_reuse_the_foundation_metric_contract`: 14 metric-card
  occurrences versus 10 expected.

These failures were not changed as part of the Gemini model integration. The complete
local log is `/tmp/agenticcontext-gemini-quality.log`; the full gate is not green.

## CI contract repair, 6 Sep 2026

This follow-up supersedes the unresolved gate status above. The latest failed Quality gate,
[run 33983592896](https://github.com/Lightwing-Ng/agenticContext/actions/runs/33983592896),
tested `ccb0e40` and reported 14 failures. Thirteen reproduced locally under UTC. The remaining
action-rail failure reproduced independently on Linux in
[run 34010968208](https://github.com/Lightwing-Ng/agenticContext/actions/runs/34010968208).

The repair updates existing tests to the current product contracts:

- Completed Activity rows use the outlined checkmark. The Activity heading remains visible with
  the account's green completion asset; the answer status retains its filled checkmark.
- Gemini submits browser and content-mode hidden fields. Grok's Start/Stop test explicitly selects
  media mode and intercepts the status URL with its content-mode query parameter.
- ChatGPT retains four text-mode and nine media-mode metric cards, plus the shared elapsed metric.
  The automatic model label is Latest.
- Pagination clears the immersed composer by 10px. The question text owns its scrolling; the
  answer extends behind the composer and reserves bottom padding for its full height.
- Resizing a touch viewport changes the theme button from 44px to 36px. Linux can report the
  fixed container's final right edge before its child button catches up. Diagnostic snapshots
  showed a correct 1,139px container edge and a transient 1,131px button edge, with matching root,
  layout, and visual viewport widths. Wait for the child to fit its container, then retain the
  original one-pixel alignment assertions. No production CSS, breakpoint, or tolerance changed.

Verification:

- The three complete related files passed 279 tests under UTC.
- The final resize test passed on Linux in
  [run 34011171889](https://github.com/Lightwing-Ng/agenticContext/actions/runs/34011171889).
  This temporary diagnostic workflow runs only that test and is not a complete gate.
- The complete local `./scripts/check.sh` passed 1,540 Python tests, 560 subtests, nine JavaScript
  tests, Ruff, and JavaScript syntax checks. Branch coverage was 71.14%, above the unchanged 55%
  threshold. Runtime: macOS 27, Python 3.13.0, Node.js 22.23.1, UTC, and an isolated Playwright
  1.62.0 / Chromium 151.0.7922.34 installation matching the failed CI browser versions.
- Final source versions: Activity tests `v1.0.5-codex.1`, sidebar tests `v1.30.2-codex.1`,
  and metric registry tests `v1.3.2-codex.1`.

The working tree's production files and user-owned 8666 service were preserved. Numbered-copy
review retained 12 protected log/provider-state files and three coverage files with different
bytes; no cleanup was authorized by the evidence. The local audit is
`/tmp/agentic-ci-housekeeping.json`.

### Windows execution proof

The repaired Linux complete gate passed 1,537 tests, three platform skips, and 560 subtests in
[run 34011318221](https://github.com/Lightwing-Ng/agenticContext/actions/runs/34011318221).
Reading that run's Windows logs exposed a separate false-success condition: Python stages
returned immediately without pytest output or a coverage artifact.

PowerShell unwrapped the launcher's single `-3.13` argument into `System.String`. Splatting that
scalar returned exit code zero without executing the requested Python module. On the same runner,
an explicit `string[]` executed the module correctly. All four Windows Python entrypoints now
preserve the argument array. The quality gate also requires a coverage report written by the
current pytest invocation, so an absent or stale report cannot produce a successful gate.

Four native Windows regression cases passed in
[run 34011631232](https://github.com/Lightwing-Ng/agenticContext/actions/runs/34011631232):
the test entrypoint executes both passing and failing pytest probes with the correct exit codes,
and the gate rejects a no-op interpreter with either absent or stale coverage. These tests require
the Windows launcher and PowerShell and are explicitly platform-scoped; they do not replace any
existing cross-platform tests. The native regression workflow is a focused check, not a full gate.

Windows gate version: `v1.2.1-codex.1`. Other Windows Python entrypoints: `v1.0.2-codex.1`.
The application was not launched and no dependency installation was run through the updated
user-facing setup entrypoint during this repair.

Once pytest actually executed on Windows, run 34011737226 exposed 29 collection errors sharing
one cause: `ZoneInfoNotFoundError` for `Asia/Hong_Kong`. Runtime requirements now declare the
Windows-only `tzdata` dependency, as recommended by the
[Python zoneinfo data-source contract](https://docs.python.org/3/library/zoneinfo.html#data-sources).
This supplies the missing IANA database without changing application timezones or formatting.
An isolated Python 3.13 probe with the system timezone search disabled reproduced the exception
without the package and resolved `Asia/Hong_Kong` to UTC+08:00 with the package installed.
