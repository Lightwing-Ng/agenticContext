# Agent session audit challenge

Documentation version: `v1.1.0-codex.1`
Reviewed: 11 Sep 2026
Baseline commit: `0911288d80c85ac29595f8748c53ef3a50c52924`
Draft fix version: `v3.36.4-codex.1`; integrated working-tree version: `v3.36.5-codex.1`

## Assessment of the supplied report

The supplied report does not describe the current implementation. Its missing-fix and
missing-test claims are contradicted by the baseline commit, before the history-recovery
changes already present in the working tree. The report does not identify its own commit,
so the evidence cannot establish whether it accurately described an earlier snapshot.

| Claim | Current evidence | Disposition |
| --- | --- | --- |
| Execution selection uses one unscoped storage key | `executionStorageKey()` includes browser and provider; `syncAgentRoute()` restores the destination scope and invalidates request epochs. | Not reproducible on the reviewed baseline. |
| An expired UUID loops forever in Reconnecting | The status route returns a typed `unknown_agent_session` 404. `requestJson()` handles only that explicit error and switches to `new` without an Ask request. | Already addressed; generic transport failures retain the selection. |
| Switching routes silently overwrites the previous worker | Route changes select a scoped worker or `new`. `_admit()` rejects a recorded worker whose browser/provider differs, before replacing its snapshot. | Already addressed in both UI and backend. |
| Ask only checks the two-worker count | The UI consumes `can_start`; catalog and locked admission both use `_capacity_reason()`, including mixed-provider restrictions. | Already addressed. A later capacity change may still legitimately produce HTTP 409. |
| Unknown-ID and mixed-route regression cases are missing | `test_expired_execution_session_recovers_without_mutation`, `test_route_switch_isolates_worker_and_obeys_global_admission`, and `test_catalog_capacity_matches_atomic_admission` exist in the baseline. | The coverage claim is incorrect. |

Evidence owners:

- [Frontend selection, recovery, and submission](../app/web/static/computer-use-agent.js)
- [Status route and request boundaries](../app/web/app.py)
- [Atomic admission and catalog](../app/core/agent/session_pool.py)
- [Session E2E tests](../tests/test_agent_sessions_e2e.py)
- [Pool and API integration tests](../tests/test_agent_session_pool.py)

## Confirmed issue and correction

P2: Route switching discarded an unsent draft before the execution selector could save it.
Returning to the original browser/provider therefore displayed an empty composer. Four new
regression cases reproduced this on desktop and narrow viewports, through both provider and
browser changes. This is draft loss, not evidence of worker snapshot replacement.

The selector now saves drafts under the combination of browser/provider scope and execution
session ID. Route switching carries the previous scope into the save operation before restoring
the destination draft. The unconditional pre-switch clearing step was removed. Drafts remain
in page memory and are never automatically submitted or persisted across a reload.

The earlier history-recovery edits in the working tree were preserved. No backend admission,
protected data, browser profile, or running service was changed by this audit challenge.

## Verification

- Before the draft fix: all four new route-draft regression cases failed on return to the original route.
- After the fix: all four passed at 1,161px and 390px.
- Nine focused existing tests passed for unknown-ID recovery, route isolation, API targeting,
  direct-start protection, and both directions of mixed-provider admission.
- The combined run passed 30 tests and 26 subtests; two version-string assertions failed
  after concurrent configuration-restoration work advanced the script/template to v3.36.5.
  That work was preserved, and only the expected asset version was updated. The two affected
  Web checks then passed: 2 tests and 27 subtests.
- JavaScript syntax, `git diff --check`, and documentation validation passed (24 files, 0 errors).
- `PYTHONPATH=/tmp/agent-picker-test-deps TZ=UTC ./scripts/check.sh` stopped at step 1 because
  the selected host interpreter lacks `ruff`. No full-suite or coverage pass is claimed.

The temporary dependency path supplies existing Markdown dependencies missing from the default
host environment; it does not replace application behavior. Browser tests use disposable Chromium
and an isolated local server. This audit did not submit a real provider prompt or perform a native
Windows run. Unit/integration coverage and macOS Chromium do not prove Windows execution.

## Audit reporting improvements

Future reports should record a commit and dirty-tree boundary, link each finding to a current
function, distinguish verified defects from concurrency possibilities, and inspect existing
regression tests before claiming a coverage gap. Verification must separate static inspection,
focused tests, complete gates, authenticated-provider checks, and platform-specific execution.

## CDP instance identity verification

A later review confirmed that the Windows debug-browser reuse path treated any Chromium CDP
endpoint answering on the saved port as the project browser. Because the launcher also released a
transient free-port socket before Edge bound it, a recycled or concurrently claimed port could
attach the Agent to the wrong profile. The existing reuse test replaced the probe with a bare port
comparison and therefore did not exercise browser-process identity.

The `v1.23.0-codex.1` implementation records the browser GUID from
`webSocketDebuggerUrl` together with the product token and port. Reuse now requires the live GUID
and expected product to match. Fresh launches use Chromium's OS-selected port mode and accept only
a `DevToolsActivePort` marker whose GUID agrees with the live endpoint, closing the earlier
free-port selection race. Legacy bare-port records remain readable and are upgraded after a
successful matching probe. Login URL discovery applies the same identity checks.

The supplied patch was not applied verbatim. Microsoft documents the Edge product token as
`Edg/`, not `Edge/`; the latter would have rejected every valid Edge endpoint. Its proposed
`*.agent-backup-*.tmp` ignore rule was also excluded because recovery copies must remain visible
for manual review under the shared static-file housekeeping contract. Internal plan material and
the related ignore recommendation were not added to the repository.

Regression coverage verifies real `/json/version` parsing, matching-instance reuse,
wrong-instance relaunch, wrong-product failure, `DevToolsActivePort` parsing, legacy migration,
and durable identity recording. These source and macOS-hosted tests do not establish native
Windows browser or authenticated-provider acceptance; that boundary remains open.
