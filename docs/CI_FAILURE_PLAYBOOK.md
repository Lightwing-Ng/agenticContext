# CI Failure Playbook

Documentation version: `v1.0.4-codex.1`

Established: 21 Aug 2026

## Purpose

This playbook defines the evidence-first procedure for GitHub Actions and local quality-gate
failures. It exists to prevent speculative test edits, platform-specific patches, and repeated
fixes for symptoms that share one deeper cause.

## Non-negotiable triage order

Follow this order for every new gate failure:

1. Retrieve the latest failed workflow run. Do not diagnose from an old pasted log or from a
   previous commit.
2. Extract the exact failing test, assertion, exception, browser, URL, and first relevant server
   log entry.
3. Reproduce the same node locally with the CI timezone and the project-supported Python runtime.
4. Inspect the current source and its call sites before changing a test expectation.
5. Classify the failure as a product regression, test assumption, platform difference, dependency
   incompatibility, test-isolation defect, or CI infrastructure failure.
6. Run the focused test, the complete related suite, and the full quality gate.
7. Push the verified change and read the new GitHub Quality gate result before declaring success.

Treat an automated repair suggestion, including an LLM-generated suggestion, as a hypothesis. It
must not override current logs, source code, or a deterministic reproduction.

## Prohibited shortcuts

Do not perform any of the following without evidence that the proposed change is the actual fix:

- change a responsive breakpoint only to match a failing assertion;
- skip Safari, iPad, Windows, or another platform because the current runner is different;
- weaken an assertion, add an unconditional retry, or increase a timeout to hide a hang;
- hard-code macOS output in a host-neutral test;
- add a platform special case without testing the explicit platform boundary;
- assume an HTTP `200` response means that the browser reached `DOMContentLoaded`;
- accept a focused test pass as proof that the full quality gate is healthy.

## Reproduction contract

Use the CI timezone and the required local interpreter:

```bash
TZ=UTC ./scripts/check.sh
```

On Windows:

```powershell
$env:TZ='UTC'; .\scripts\check.ps1
```

For a browser failure, run the exact test first:

```bash
TZ=UTC ./scripts/test.sh tests/test_sidebar_e2e.py -k "exact_test_name"
```

On Windows:

```powershell
$env:TZ='UTC'; .\scripts\test.ps1 tests/test_sidebar_e2e.py -k "exact_test_name"
```

Record at least:

- Python, Playwright, Chromium, Node.js, and operating-system versions;
- timezone and quality-gate command;
- exact test name and first failing assertion or timeout;
- request URL and `wait_until` condition;
- whether the Flask server returned a response;
- whether all static assets returned successfully;
- whether the browser reached `DOMContentLoaded`;
- the first captured application or browser-console error.

The CI environment used for the 20 Aug 2026 incident was Ubuntu 24.04, Python 3.13.15,
Playwright 1.62.0, Chromium 151.0.7922.108, Node.js 22, and UTC.

## Frontend observer contract

Every `MutationObserver` callback must converge after one synchronization pass. A callback must
not continuously observe attributes that it writes on every pass.

Observer-backed controls must make attribute, class, and style updates idempotent:

```javascript
if (element.getAttribute(name) !== value) {
    element.setAttribute(name, value);
}
```

The observer's `attributeFilter` must exclude attributes that are internal synchronization output,
unless the callback has a proven fixed-point guard. Add a static contract test for the filter and
the idempotent update path.

An HTTP `200` response followed by a `DOMContentLoaded` timeout is compatible with a main-thread
JavaScript loop. Inspect observers, synchronous loops, event handlers, and scripts loaded by the
page before changing the Flask route or increasing the navigation timeout.

## Responsive and motion contract

When `prefers-reduced-motion: reduce` is active:

- sidebar geometry must already be final when touch hit testing runs;
- sidebar shell, overlay, and toggle transitions must be disabled;
- JavaScript timers may remain only for state bookkeeping;
- tests must verify rendered rectangles and `document.elementFromPoint()`, not only ARIA state;
- a stable-rectangle wait is still required when the DOM update itself is asynchronous.

## Incident record: 20 Aug 2026

### iPad portrait geometry failures

The first failures reported `sidebarInsideViewport == false` for iPad portrait sizes. The failure
was not caused by the metric-column breakpoint. The browser requested reduced motion, but the CSS
sidebar transition still used the normal `500ms` duration while JavaScript used a short state timer.
The test could therefore measure an intermediate sidebar rectangle.

The durable fix disabled the sidebar shell, overlay, and toggle transitions under the reduced-motion
media query and added a responsive contract test. The test continues to assert the original
viewport contract rather than relaxing it.

### Cache page `DOMContentLoaded` timeouts

The next CI run reported 19 browser failures, mostly `Page.goto: Timeout 30000ms exceeded` for Cache
pages. The Flask route and every requested static resource returned `200`, so the server response was
not the root cause.

The shared segmented-control script used a `MutationObserver`. Its callback wrote `aria-checked` on
Cache anchor options on every pass, and the observer watched that same attribute. The browser main
thread never reached `DOMContentLoaded`.

The durable fix removed the self-written data attribute from the observer filter and made all
attribute and style writes idempotent. The regression test pins the observed attribute set. The
complete local gate and the pushed GitHub Quality gate must both pass after this class of change.

## Completion checklist

Before handoff, confirm all items below:

- [ ] The latest workflow run, not an older run, was inspected.
- [ ] The exact failing node was reproduced locally.
- [ ] The root cause was classified with source and log evidence.
- [ ] The focused regression test passes.
- [ ] The complete related suite passes.
- [ ] The platform quality gate passes: `TZ=UTC ./scripts/check.sh`
      on macOS/Linux or `$env:TZ='UTC'; .\scripts\check.ps1` on Windows.
- [ ] JavaScript syntax and static checks pass.
- [ ] Cache-busters and durable documentation were updated when browser assets changed.
- [ ] The change was pushed and the new GitHub Quality gate was read back as successful.
- [ ] No unrelated dirty files were staged or reverted.

## Maintenance rule

Keep this file focused on reusable triage rules and verified incident patterns. Put current
unresolved defects in `docs/KNOWN_ISSUES.md`, stable test contracts in `docs/TESTING.md`, and
per-agent collaboration rules in the repository `AGENTS.md`.
