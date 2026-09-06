# Historical test evidence

Documentation version: `v1.0.0-codex.1`

These dated snapshots preserve the commands, versions, and outcomes observed at the time.
They are not current runtime requirements or proof that the current checkout passes.
Use [TESTING.md](TESTING.md) for current commands and interpretation.

## Coverage baseline, 28 Aug 2026

Baseline remeasured on 28 Aug 2026 with Python 3.13.0, pytest 9.0.3,
pytest-cov 7.1.0, and Ruff 0.15.21:

- 1,119 tests passed, with 380 unittest subtests passed.
- Combined coverage for `app/` was 69.31% using branch coverage.
- All first-party JavaScript files passed syntax checks.
- All 9 shared Agent Optimization Node contract cases passed.

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
