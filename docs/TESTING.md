# Testing guide

Documentation version: `v1.8.6-codex.1`

## Supported commands

Install the runtime and development dependencies with the supported Python 3.13 or newer workflow:

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

Set `AGENTIC_CONTEXT_TEST_REPORT_FAILURES=1` to flush each failed test or collection report before
the final pytest summary. Windows CI enables this diagnostic and checks the native core boundaries
before running the complete gate. The diagnostic preserves the original test outcomes, selection,
and exit codes; the preliminary checks do not replace the full coverage run.

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
python3 scripts/benchmark_compute.py
```

On Windows:

```powershell
py -3 scripts/benchmark_compute.py
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

`AGENTIC_CONTEXT_PYTHON` may override the interpreter only when it resolves to Python 3.13 or newer.
The resolver prefers a supported host `python3`, `python`, or Windows `py -3` launcher, then
falls back to unversioned platform-specific Python installations.

## Quality gate

The canonical local and CI quality gate is `scripts/check.sh` on macOS/Linux and
`scripts/check.ps1` on Windows. The gates run, in order:

1. Ruff static checks over `main.py`, `app/`, `tests/`, and `scripts/`.
2. Offline Markdown file, image, and heading links in the repository documentation.
3. `node --check` for every first-party JavaScript file in `app/web/static/`.
4. Node unit tests for the shared Agent Optimization and Site tools runtime contract.
5. The full pytest suite with branch coverage for `app/`, including the disposable-browser
   responsive sidebar E2E flow.

The coverage report is written to `test-results/coverage.json`; all generated test artifacts are
ignored by Git. The gate currently enforces a 55% combined statement-and-branch coverage floor.
Override `AGENTIC_CONTEXT_COVERAGE_MINIMUM` only for an intentional local diagnostic, never to make a
regression pass.

Historical coverage counts are preserved in [TEST_HISTORY.md](TEST_HISTORY.md).

Raise the coverage floor only after adding behavior-level tests. Do not exclude production modules
or lower the threshold to mask a gap.

## CI portability contract

GitHub Actions is the canonical clean-room gate. It runs the quality contract on `ubuntu-latest`
and `windows-latest` with Python 3.13 as the minimum-version baseline, Node.js 22, UTC, and a freshly installed Playwright
Chromium. A test that passes only on the developer's macOS workstation is not evidence of a
passing project contract.

Use this command to reproduce the CI timezone locally:

```bash
TZ=UTC ./scripts/check.sh
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
- Compute JSON concurrency tests hold the actual reader open across a real atomic write, then
  check the original reader and the replacement path independently. Native Windows also checks
  that an ordinary reader is an incompatible negative control. File-size, decoding, regular-file,
  and handle-cleanup checks remain separate from that concurrency evidence. The owned-tree Stop
  fixture preserves its readiness and termination deadlines; readiness failures report bounded
  start/current state and log evidence before cleaning only the fixture's job. A macOS pass does
  not establish the cause of a Windows readiness failure.

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

### Windows Agent browser verification

Offline regressions in `test_computer_use_agent.py`, `test_scraper_and_browser_sessions.py`, and
`test_web_app.py` cover configured Windows login/conversation arguments, saved-config forwarding,
provider-page window selection (including empty contexts), pre-submission window-control failure,
passive launch modes, and cleanup failure/error precedence using disposable fixtures. They do not
read authenticated profiles or prove native window or provider behavior.

Before claiming native Windows 11 support for the complete workflow, record the commit, Python,
Playwright and Edge/Chrome versions, display layout, scaling, and profile selection, then perform
the following explicit manual checks with the user's authorized test account. Keep credentials,
cookies, and conversation content out of test artifacts. Do not add these checks to the default
suite or CI; an automated equivalent must be marked `live`.

| Scenario | Required evidence |
| --- | --- |
| Edge `Default` and a configured non-default Chrome profile | Login handoff, Recheck, and task select the same data root/profile; copying and launching alone do not establish authentication. |
| Source browser open during clone creation | Record copy success or an actionable access/copy error; do not silently switch to writable automation of the real profile. |
| Passive readiness/source checks | Windows probes remain offscreen/minimized and do not activate the browser. |
| Existing provider tab, unrelated first tab, and empty clone | The selected or newly created provider page owns the normalized task window; count native windows separately from contexts. |
| Single display, high scaling, and multiple displays | Verify the actual provider window is usable; fixed CDP geometry is not proof of work-area fit. |
| Human verification | The same controlled clone and turn survive manual verification and the existing Resume gate. |
| Normal completion, Stop, closed provider window, and launch failure | The context exits; inspect remaining task-owned processes and temporary paths without disturbing the user's browser. |
| Failed profile removal | A retained-path diagnostic is present; an existing task/launch error remains primary; no successful cleanup is claimed. |

Run the native offline gate separately with `.\scripts\check.ps1`. A passing macOS mock run or a
Windows CI run without the authenticated manual checks is not live Windows provider evidence.

### General test layers

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

- `HOME`, `USERPROFILE`, `HOMEDRIVE`, `HOMEPATH`, `LOCALAPPDATA`, and `APPDATA` keep both
  POSIX and Windows settings/browser-profile defaults away from the user account.
- `TMPDIR`, `TEMP`, `TMP`, and Python's cached temporary directory use a private test root.
- `AGENTIC_CONTEXT_RUNTIME_ROOT` moves default local caches and logs away from the repository.
- `AGENTIC_CONTEXT_SETTINGS_PATH` redirects persisted settings.

Default tests must not:

- open an authenticated Chrome, Edge, Safari, or Playwright profile;
- make X, Grok, ChatGPT, yt-dlp, or general network requests;
- read, copy, delete, reset, or restore a user-owned cache, log, setting, or browser profile;
- submit a real background cache job.

The test audit hook denies non-loopback Python network dispatch, port 8666, native browser
launches, and native authorization prompts. Playwright profile attachment and persistent contexts
are denied. Real browser tests must request `disposable_browser_launch`, launch headless with no
user-data directory, and retain the context route guard that blocks external requests. These are
test isolation guards, not an OS sandbox for arbitrary subprocess code.

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

## Entrypoint and documentation checks

Both test wrappers select `not live` by default, honor the current marker override and its legacy
alias, disable bytecode and pytest cache output, and preserve pytest's exit code. Direct pytest
also defaults to `not live` through `pytest.ini`; an explicit `-m live` remains a manual opt-in.
Neither the default suite nor the default quality gate is a fast unit-only command: both include
disposable browser tests. For a shorter iteration, select a file or use
`AGENTIC_CONTEXT_TEST_MARK_EXPRESSION='not live and not slow' ./scripts/test.sh`.

Both quality gates reject absent or stale coverage after a nominally successful pytest command.
Run only one complete gate at a time in a checkout: the coverage database and JSON report are
shared under `test-results/`. Independent focused tests do not produce that report by default.
The Windows setup entrypoint stops at the first failed dependency command. Windows CI explicitly
selects the interpreter installed by its setup step, so a different launcher default cannot bypass
the minimum-version baseline.

Run the tooling regressions with:

```bash
./scripts/test.sh tests/test_test_entrypoints.py tests/test_windows_entrypoints.py tests/test_documentation.py
```

On Windows:

```powershell
.\scripts\test.ps1 tests/test_test_entrypoints.py tests/test_windows_entrypoints.py tests/test_documentation.py
```
 Native Windows cases are skipped on other hosts and must pass on Windows CI;
POSIX launcher cases are skipped on Windows. A skip is not platform execution evidence.

Run `python3 scripts/check_docs.py` on macOS/Linux or `py -3 scripts/check_docs.py` on Windows
with the prepared interpreter for the documentation-only check.
It parses Markdown links, reference links, images, and ordinary heading anchors, including repeated
headings. Fenced examples are not links. Remote URLs and references outside the checkout are not
validated; shared references are printed separately because sibling files are absent in clean CI.
The check does not claim to validate prose semantics, HTML anchors, or remote availability.

The resolver checks the Python minimum version, not dependency availability. Run the setup wrapper
with the same interpreter used for tests. If the host default lacks dependencies, set
`AGENTIC_CONTEXT_PYTHON` to the path of a prepared Python 3.13 or newer executable. A resolver pass
does not establish compatibility of every future dependency release.

Dated browser, platform, and CI observations are preserved in [TEST_HISTORY.md](TEST_HISTORY.md).
Do not reuse those counts or version-specific commands as evidence for a new checkout.

## Windows audit regression coverage

`test_chromium_profile_boundaries.py` covers path containment, shared executable resolution,
retained stale profiles, and cleanup precedence with synthetic roots. `test_agent_profile_binding.py`
covers configuration changes, in-flight cache partitioning, persisted task binding, and legacy
profile-proof refusal. `test_agent_profile_browser.py` uses clean local browser fixtures to reject
missing or mismatched profile evidence and stale in-flight replies. `test_agent_browser_cleanup.py`
checks that Stop and restart preserve a retained-profile error.

`test_windows_anchored_delete.py` checks the native ABI/handle algorithm with injected file APIs on
other hosts and real temporary Windows files on Windows. Native cases include successful receipt
deletion, changed content/root, sharing conflicts, and replacement attempts. `test_compute_jobs.py`
exercises portable output collection, native identity, job ownership, and verified stopping
on the host where it runs. Abrupt worker-exit containment still requires the native acceptance
check; a normal Stop test does not establish that separate lifecycle. A macOS fake is not native Windows evidence.

`test_windows_workspace_fingerprint.py` reproduces Windows directory-entry metadata with missing
identity fields. Fingerprinting must obtain authoritative file metadata, keep link and size limits,
and observe content changes. `test_owner_only_permissions.py` verifies private artifact creation and
ACL failure handling; Windows cases inspect the actual owner and protected DACL instead of POSIX
mode bits. Native Stop checks wait for the identified worker handle to signal as well as checking
the Job Object's active process count.

Event-chain and rotating-log tests verify native privacy before content is appended, preservation
of existing records, and failures without a successful write. Windows assertions inspect the owner
and protected DACL; POSIX assertions retain the `0600` file and `0700` directory contract. A FIFO
fixture exercises nonregular rejection on POSIX; Windows uses an actual directory at the same
JSONL leaf and asserts that no read is attempted.

Host-neutral Agent browser fixtures must report the same operating system as their saved settings.
Explicit mismatch cases must still disable Ask and refuse the source catalog. Filesystem assertions
compare native paths, URL/catalog fields compare POSIX paths, and disposable Markdown fixtures use
UTF-8. Child pytest runs pin their own root and configuration so Windows temporary files on another
drive retain diagnostic node IDs and do not inherit the parent's coverage arguments.
`test_static_asset_delivery.py` checks the actual Flask responses for the search module and its
Fuse dependency, including exact bytes and executable JavaScript MIME types on the running host.
It also injects incorrect host MIME mappings without changing global types, and preserves HEAD,
range, cache validation, error responses, and nonstatic routes. Search entry and dependency URLs
carry a new cache version so an old incorrectly typed response cannot be reused after upgrading.

CI installs managed Chromium in a job-owned directory under `RUNNER_TEMP` and preserves its
absolute `PLAYWRIGHT_BROWSERS_PATH` through test isolation. A discovery check imports the isolation
fixture before checking the installed executable, so redirected home/profile paths cannot silently
substitute a host browser. The Windows preflight also runs the model-view DOM contract separately
with installed Chrome and Edge. Their exit codes remain failures even when managed Chromium passes;
frame/timer diagnostics do not change the original click deadline or retry a failed click.

Run related suites first and then one complete platform gate at a time; the coverage files are
shared within each checkout. A Windows CI pass verifies disposable local files/processes/browser
fixtures, not real account login, profile authentication transfer, native visible window usability,
scaling, or provider prompt submission. Those still require the explicit manual matrix above.

Ask profile identity checks compare configuration snapshots; the identity is not a server-issued
provider authentication credential. Route and browser tests must check mismatch rejection before
worker start and preserve the real-provider authentication boundary.
