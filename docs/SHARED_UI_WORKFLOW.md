# Shared UI workflow

Documentation version: `v1.0.4`

This is the short entrypoint for shared visual and interaction work. The only
long-form synchronization state lives in:

`/Users/lightwing/Desktop/shared_docs/SHARED_UI_SYNC.md`

## Read order

1. Read this file.
2. Read the central ledger's `Fast path for agents` section and the matching row.
3. Read `docs/STYLE_REFERENCE.md` and inspect the named implementation in
   `/Users/lightwing/Desktop/worthward` before editing agenticContext.

## Contract

- `worthward` is the canonical complete baseline and the final convergence target.
- agenticContext is an adapter: reuse the sibling's tokens, structure,
  states, responsive behavior, and accessibility contract while preserving local
  routes and product-specific markup.
- A Cache-first improvement is a `Candidate review`, not a finished synchronization.
  Inspect and promote it into `worthward` before the ledger can say `Synchronized`.
- If only this repository is authorized, do not edit the sibling. Set the ledger row
  to `Pending` and include the exact sibling-sync reminder in the handoff.
- Never declare parity from source text, one green test, or visual similarity alone.

## Verification minimum

For a shared UI change, preserve unrelated dirty files and record the component row,
paths, versions or commit, invariant, focused checks, and live route evidence. Run
the smallest relevant Cache tests first, then the sibling's focused checks. For a
visual or interaction change, verify the production DOM at desktop and narrow widths.
Use the row's named test files rather than rerunning unrelated suites.

Typical Cache-focused command:

```bash
./scripts/test.sh tests/test_style_tokens.py tests/test_web_app.py
```

On Windows:

```powershell
.\scripts\test.ps1 tests/test_style_tokens.py tests/test_web_app.py
```

Before handoff, report the exact commands, pass/fail counts, live verification, and
any unrelated failures. Update the central ledger only after the evidence exists.
