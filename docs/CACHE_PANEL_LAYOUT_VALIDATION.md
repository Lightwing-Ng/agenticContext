# Cache panel layout validation

Documentation version: v1.0.0
Date: 6 Sep 2026

## Result

Cache Text and Media share three layers: resource totals, current-run progress, and
recent activity. Resource counters appear once, while distinct queued/processed
counters remain in the run section. Phase is a compact label beside Current progress.
The progress bar spans the section width. Event timestamps have a separate muted
line and use D MMM YYYY. Log scrolling and floating pagination retain their owners.

Metric columns adapt to the available width; subgrid aligns values when neighboring
labels wrap. Existing Univers Next typography, palette, transparent Foundation
metrics, panel radii, glass controls, and shared title rails remain authoritative.

## Changed paths and versions

- `app/web/static/style.css`: v2.96.0-codex.1, Cache-scoped layout rules.
- `app/web/static/cache-page.js`: v1.12.0-codex.1, phase state and event presentation.
- `app/web/templates/_cache_page.html`: v1.27.0-codex.1, shared information hierarchy.
- `app/web/templates/chatgpt.html`, `grok.html`, `claude.html`, `gemini.html`, and
  `index.html`: remove duplicate resource totals from progress blocks.
- `agent.html`, `agent_access_unlock.html`, `browser.html`, `settings.html`, and
  `settings_style_tokens.html`: stylesheet cache-key propagation only for this task.
  Concurrent Local resources edits in `browser.html` were preserved.
- `tests/test_sidebar_e2e.py`, `tests/test_web_app.py`, and `tests/test_style_tokens.py`:
  responsive behavioral coverage and updated asset/counter expectations.

## Verification

- `/usr/local/bin/python3.13 -m pytest tests/test_style_tokens.py tests/test_web_app.py -q`:
  230 passed, 541 subtests passed.
- `TZ=UTC /usr/local/bin/python3.13 -m pytest tests/test_sidebar_e2e.py -k
  'cache_overview_groups or cache_text_metrics or cache_metric_cards_blend or
  cache_events_card or cache_title_rail or cache_annotations or cache_action_row' -q`:
  13 passed. This covers desktop/narrow geometry, dark surfaces, title rails, event
  scrollports, action state, source-specific Text/Media totals, and live polling.
- Final metric-alignment rerun: four light/dark, 982px/390px cases traverse ChatGPT
  Text/Media, Grok Text/Media, Claude Text, Gemini Text, and X Media. They check unique
  visible status fields, value alignment, section order, full-width progress,
  horizontal containment, stopped-to-failed polling, and event pages 1 and 3.
- Rendered screenshots inspected for Text desktop and Media narrow layouts.
  These use synthetic status fixtures and disabled external operations.
- Ruff for the three changed test files, `node --check app/web/static/cache-page.js`,
  and `git diff --check` passed.

An intermediate asset-version expectation was updated. A separate Local resources
assertion changed during another task's work and passed in the latest complete
Style/Web rerun; it was not repaired as part of this Cache task.
A full quality gate was already running in another task (observed PID 44008), so
this task did not start a concurrent coverage run or claim a full-gate result.

## Live runtime and synchronization boundary

The selected 8666 tab was inspected through browser DOM. Listener PID 94294 had
cwd `/Users/lightwing/Desktop/agenticContext` and served cached old HTML with
style-v2.95.1 and cache-page-v1.10.0. It was not restarted. A service restart is
needed before production-template acceptance; isolated test results do not prove
production deployment. Native Terminal access for a separate preview launch was
unavailable through Computer Use, so visual acceptance used disposable test servers.

The historical `antigravity` path is absent. The current Worthward workspace CSS
was inspected read-only, as identified by the shared synchronization ledger.
Sibling UI sync pending: review the equivalent summary/progress/activity layout in
Worthward (the canonical sibling) before declaring shared-component convergence.
No sibling source was changed.

## Housekeeping

The numbered-copy inventory is `/tmp/cache-panel-housekeeping.json`. Fifteen
candidates were retained: twelve protected cache/log files and three coverage
files with different bytes. No user data or unrelated dirty work was removed.
