# AGENTS.md

Code policy version: v1.3.3-agent-contract.0

This file defines mandatory collaboration rules for all coding agents in this repository (Claude, Codex, Antigravity, Trae, and future agents).

## 1) Scope and Priority
- This document applies to all AI-authored changes in this repository.
- Follow direct user instructions first.
- Then follow repository rules in this file.
- Keep behavior deterministic and auditable.

## 2) Change Discipline
- Make the smallest correct change that solves the requested problem.
- Do not refactor unrelated code unless explicitly requested.
- Do not modify files outside task scope.
- Preserve existing behavior unless behavior change is required by the request.
- For any operation that can create, copy, rename, export, compile, or restore a static file, read
  [`docs/STATIC_FILE_HOUSEKEEPING.md`](docs/STATIC_FILE_HOUSEKEEPING.md) and its canonical shared
  contract, then complete the numbered-copy scan before continuing, committing, handing off, or
  responding. A ` 2`, ` 3`, or other numbered name is only a review candidate; exact-byte,
  untracked or reproducible duplicates require active-process and protected-path checks before
  recoverable cleanup.

## 3) Read Before Edit
- Read target files and closely related call sites before editing.
- Verify assumptions from current source, not memory.
- If requirements are ambiguous, ask for clarification before high-impact changes.

## 4) Style and Structure
- Keep code comments and technical docs in American English.
- Reuse existing project patterns for naming, layout, and error handling.
- Prefer clear, maintainable code over clever one-liners.
- Avoid introducing new dependencies unless strictly necessary.
- For UI work, read [`docs/SHARED_UI_WORKFLOW.md`](docs/SHARED_UI_WORKFLOW.md) first.
  Treat `../worthward/app` as this project's sibling and visual source of truth;
  read `docs/STYLE_REFERENCE.md` before changing markup, CSS, assets, or UI behavior.
- Before changing a UI pattern shared with the sibling project, read the single local
  synchronization ledger at `../SHARED_UI_SYNC.md` and follow its mandatory workflow.
  `worthward` is the canonical complete baseline and final convergence target.
  A Cache-first improvement is only a `Candidate review` until it is promoted into
  `worthward` and both projects have current evidence.
- A task scoped only to this repository does not authorize editing the sibling. When a
  shared component changes here, update the ledger, mark the sibling `Pending`, and report
  the required sibling-sync reminder at handoff. When the user authorizes both projects,
  synchronize and verify both in the same task.

## 5) Testing and Verification
- Run focused checks for touched areas.
- Add or update tests when behavior changes or regression risk is meaningful.
- Do not add low-value tests that only mirror implementation details.
- If tests are not added, explain why validation is still sufficient.
- When a CI or GitHub quality gate fails, read [`docs/CI_FAILURE_PLAYBOOK.md`](docs/CI_FAILURE_PLAYBOOK.md)
  before changing production code, tests, responsive breakpoints, browser support, skips, or retries.
  Treat automated repair suggestions as hypotheses until the latest failure is reproduced locally.

## 6) UI and Frontend Changes
- Use local assets first (icons, images, styles) before introducing new resources.
- Maintain accessibility basics: labels, keyboard reachability, and semantic structure.
- Keep responsive behavior consistent with existing breakpoints and patterns.
- Use `Local resources` as the canonical user-facing label for the third Dock item and its related navigation copy. Do not reintroduce `Browser` or `浏览器` for that UI surface. In explanatory sentences, use sentence case; preserve `Local resources` exactly when referring to the product label.

## 7) Safety and Git Hygiene
- Never run destructive commands (for example, reset/clean forcefully) without explicit approval.
- Never revert unrelated local changes made by the user.
- Do not amend commits unless explicitly requested.
- Keep commits logically grouped and easy to review.
- Do not use a broad ignore rule or recursive deletion as a substitute for the numbered-copy
  housekeeping workflow.

## 8) Host Runtime and Local Tooling
- Treat the authenticated likes page for the currently signed-in X account as the canonical entry page for this project.
- Assume Chrome on the host machine is already authenticated for that page.
- Do not rework, replace, or repeatedly troubleshoot login unless the user explicitly asks for login-related changes.
- Assume future user sessions will also start from that already logged-in page.
- Use the host platform's canonical terminal for local development launches, restarts, process control,
  and log observation: the built-in Terminal on macOS, or PowerShell on Windows.
- Do not use PyCharm as a runtime entrypoint or process-control interface.
- Do not close or disrupt the user's existing Terminal or PowerShell windows unless explicitly requested.
- Use Python 3.13 or newer for project execution, with no fixed minor version or upper-version cap.
  The resolver prefers the host `python3` on macOS and the `py -3` launcher on Windows;
  set `AGENTIC_CONTEXT_PYTHON` to override with any interpreter meeting the minimum version.
- On macOS, treat `python3` as the valid Python alias. On Windows, prefer the `py` launcher and do
  not assume `python3` is available.

## 9) Handoff Requirements
- Summarize what changed and why.
- Include touched file paths.
- Report verification steps performed and their outcomes.
- Call out known limitations or follow-up recommendations.

## 10) Definition of Done
- Requested behavior is implemented.
- Relevant checks pass or known failures are clearly reported.
- No unrelated files are changed.
- Handoff summary is complete and actionable.
