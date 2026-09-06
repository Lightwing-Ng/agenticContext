# Local documentation and test-system audit

Documentation version: `v1.0.0-codex.1`

Reviewed: 6 Sep 2026

## Scope

Reviewed the maintained root and `docs/` Markdown navigation, current setup and test examples,
Python resolution, both platform entrypoints, pytest selection and isolation, coverage handling,
and the GitHub quality workflow. Existing application, UI, and unrelated UI-test changes in the
checkout were preserved. Three outdated Cache test contracts were repaired after reproduction. No application service, authenticated profile, or sibling repository was changed.

## Repairs

| Finding | Result |
| --- | --- |
| Runnable guides still pinned a workstation-specific Python minor version | Current commands use project wrappers or unversioned host commands. Python 3.13 remains the minimum; historical commands retain their observed versions. |
| Current UI entrypoints mixed the historical sibling name with the active checkout | Current references now match the shared ledger and verified `worthward` paths; historical records retain their original names. |
| README still directed launches through PyCharm | The quick start now uses the native Terminal or PowerShell entrypoint. |
| Current testing guidance mixed operating rules with old pass counts and incident logs | Added a documentation index and moved dated evidence into `TEST_HISTORY.md`; expanded the behavior-to-test map. |
| Broken documentation links had no gate | Both gates now check repository-local Markdown targets, images, and ordinary heading anchors. Shared and remote references are explicitly outside the automatic check. |
| Windows test wrapper ignored its documented marker environment | Both wrappers use `not live` by default and support explicit selection. Direct pytest also defaults to `not live`. |
| Windows resolver could inherit stale launcher arguments | Each resolution clears prior resolved executable and argument state. |
| Windows setup could announce success after an installation failure | Every native installation step now propagates failure immediately. |
| Bash gate could announce success when Python executed no tests | Added the coverage freshness requirement already present on Windows. Missing and stale report cases were reproduced before repair and pass afterward. |
| Cache assertions still required the retired notice order, duplicate counters, and fixed columns | Matched the documented summary/run/activity hierarchy, checked unique metric ownership, and replaced fixed column counts with rendered containment and non-overlap while preserving touch, polling, and overflow assertions. |
| Tooling scripts were outside Ruff's gate scope | Both gates now include `scripts/`. |
| Windows CI could select a different Python through the launcher | The workflow explicitly supplies the interpreter installed by its setup step. Both pip caches track runtime and development requirements. |

The gate changes are version `v1.3.0-codex.1`; the documentation checker and POSIX regression
suite start at `v1.0.0-codex.1`; the Windows regression suite is `v1.1.0-codex.1`.

## Changed paths

- Guidance: `AGENTS.md`, `README.md`, `docs/README.md`, `docs/TESTING.md`,
  `docs/TEST_HISTORY.md`, `docs/TEST_COVERAGE.md`, `docs/ARCHITECTURE.md`,
  `docs/OPERATIONS.md`, `docs/CACHE_HANDOFF.md`, `docs/AGENT_OPTIMIZATION.md`,
  `docs/CI_FAILURE_PLAYBOOK.md`, `docs/SHARED_UI_WORKFLOW.md`, and `docs/STYLE_REFERENCE.md`.
- Tooling: `.github/workflows/quality.yml`, `pytest.ini`, `scripts/check.sh`,
  `scripts/check.ps1`, `scripts/check_docs.py`, `scripts/test.ps1`,
  `scripts/resolve_python.ps1`, and `scripts/setup_python.ps1`.
- Regression coverage: `tests/test_documentation.py`, `tests/test_test_entrypoints.py`,
  `tests/test_windows_entrypoints.py`, `tests/test_sidebar_e2e.py`, and
  `tests/test_style_token_registry.py`.

## Verification

- Before repair, both POSIX absent/stale-coverage regressions failed because the gate incorrectly
  printed `Quality gate passed.` without executing pytest.
- Focused tooling, startup, and runtime-isolation checks passed: 19 passed, 15 native Windows
  cases skipped on macOS. Those skips do not establish Windows execution correctness.
- A separate disposable direct-pytest probe passed its offline case and deselected its live case.
- Bash syntax checks and local documentation navigation passed.
- Initial full gate: 12 failed, 1,559 passed, 15 skipped, 560 subtests passed; coverage 71.25%.
  The same 12 failures reproduced in isolation. Source and the existing Cache layout record
  established outdated test assumptions; all 14 related cases passed after repair.
- Final complete gate passed: 1,571 passed, 15 native Windows skips, 560 subtests passed,
  71.25% combined statement-and-branch coverage, and 476.09 seconds of pytest execution.
  Ruff, JavaScript syntax, all 9 Node cases, and documentation navigation passed.
- Final documentation scan: 22 Markdown files, zero local navigation errors, two shared references
  reported outside the automatic check. `git diff --check` passed.
- The non-document source fingerprint remained unchanged during the final gate. The longest
  individual call was 8.73 seconds in the multi-source Cache browser contract; the duration
  report is diagnostic evidence, not a controlled before/after performance benchmark.

The final gate was invoked with UTC and duration reporting:

```bash
TZ=UTC PYTEST_ADDOPTS='--durations=15' AGENTIC_CONTEXT_PYTHON=/usr/local/bin/python3.13 ./scripts/check.sh
```

The version-specific path above records this audit's actual prepared interpreter, not a required
installation path. Focused checks used the same interpreter through `scripts/test.sh`.
Local logs are `/tmp/agenticcontext-docs-quality-final.log`,
`/tmp/agenticcontext-docs-ui-baseline.log`, and `/tmp/agenticcontext-docs-ui-fixed.log`.

The host-default Python met the minimum but lacked several project dependencies. Validation used
an explicitly selected, prepared interpreter; this did not change the resolver's default policy.

## Remaining boundaries

- Native Windows execution and a new GitHub Actions run were not performed locally.
- Complete gates still share `test-results/.coverage` and `test-results/coverage.json`; run one
  complete gate at a time per checkout. Freshness checks are not a concurrency lock.
- A minimum-version predicate is not evidence that every future Python/dependency combination has
  been exercised. CI intentionally verifies the minimum baseline.
- The documentation check validates navigation, not every semantic claim, raw HTML anchor, remote
  URL, or cross-repository contract. Dated production observations remain historical evidence.
- Final numbered-copy review retained 15 candidates: 12 protected runtime files and three coverage
  copies with different bytes. Metadata, hashes, Git state, and open-handle evidence are recorded in
  `/tmp/agenticcontext-docs-housekeeping.json`. No cleanup deletion was performed.
