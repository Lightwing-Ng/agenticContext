# Shared UI workflow

Documentation version: `v1.1.0-codex.0`

## Shared select keyboard adapter

`app/web/static/select-controller.js` v1.0.1 is vendored byte-for-byte from
Worthward's `app/web/static/assets/js/select-controller.js`. The keyboard
contract is owned by the sibling's `docs/SHARED_UI_WORKFLOW.md`.
`browser-filter-select.js` and `browser-source-filter.js` delegate navigation
and DOM focus to this controller; submission, metadata, pointer dismissal, and
header-menu positioning remain local. Escape is consumed by the nested select so
it cannot close an enclosing overlay. Opening with ArrowUp/ArrowDown now focuses
the selected option consistently; it no longer advances the native-select adapter
on its first key press. Other pickers remain unmigrated.
Both entrypoints also import the controller when a cached template lacks its
script tag, before upgrading the original form controls.

Run `node --test tests/test_select_controller.mjs` and
`./scripts/test.sh tests/test_select_keyboard_e2e.py` for focused validation.

This is the short entrypoint for shared visual and interaction work. The only
long-form synchronization state lives in:

`/Users/lightwing/Desktop/shared_docs/SHARED_UI_SYNC.md`

The mandatory three-project typography policy lives in:

`/Users/lightwing/Desktop/shared_docs/SHARED_UI_TYPOGRAPHY_CONTRACT.md`

## Read order

1. Read this file.
2. Read the shared typography contract.
3. Read the central ledger's `Fast path for agents` section and the matching row.
4. Read `docs/STYLE_REFERENCE.md` and inspect the named implementation in
   `/Users/lightwing/Desktop/worthward` before editing agenticContext. Inspect
   `/Users/lightwing/Desktop/neoMe` when the shared component also exists there.

## Contract

- `worthward` is the canonical complete baseline and the final convergence target.
- agenticContext is an adapter: reuse the sibling's tokens, structure,
  states, responsive behavior, and accessibility contract while preserving local
  routes and product-specific markup.
- `neoMe` is the third shared-style consumer. Reuse applicable tokens and components
  while preserving its health-record routes and privacy boundary.
- Typography is not a local adaptation. All three projects follow the central
  Univers Next for HSBC contract, including technical text; do not introduce a
  platform Western stack or a separate monospace family during convergence.
- A Cache-first improvement is a `Candidate review`, not a finished synchronization.
  Inspect and promote it into `worthward`, then verify every applicable consumer,
  before the ledger can say `Synchronized`.
- If only this repository is authorized, do not edit either sibling. Set the ledger
  row to `Pending` and include the exact sync reminder in the handoff.
- Never declare parity from source text, one green test, or visual similarity alone.

## Verification minimum

For a shared UI change, preserve unrelated dirty files and record the component row,
paths, versions or commit, invariant, focused checks, and live route evidence. Run
the smallest relevant AgenticContext tests first, then focused checks in every
applicable sibling. For a visual or interaction change, verify the production DOM at
desktop and narrow widths in every project that exposes the component.
Use the row's named test files rather than rerunning unrelated suites.

Typical AgenticContext-focused command:

```bash
./scripts/test.sh tests/test_style_tokens.py tests/test_web_app.py
```

On Windows:

```powershell
.\scripts\test.ps1 tests/test_style_tokens.py tests/test_web_app.py
```

Before handoff, report the exact commands, pass/fail counts, live verification, and
any unrelated failures. Update the central ledger only after the evidence exists.
