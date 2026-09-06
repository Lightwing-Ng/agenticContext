# Documentation index

Documentation version: `v1.0.0-codex.1`

Use the current contracts below for implementation and operation. Dated evidence records describe
the checkout and environment observed at that time; they do not establish current behavior.

## Current contracts

| Need | Authoritative local entrypoint |
| --- | --- |
| Install, launch, and understand the project | [Repository README](../README.md) |
| Repository collaboration rules | [Root AGENTS.md](../AGENTS.md) and [engineering contract](AGENTS.md) |
| Runtime, services, and persistence boundaries | [Architecture](ARCHITECTURE.md) |
| Tests, marker selection, coverage, and CI | [Testing guide](TESTING.md) |
| Find tests for a behavior | [Test coverage map](TEST_COVERAGE.md) |
| Diagnose a failed quality gate | [CI failure playbook](CI_FAILURE_PLAYBOOK.md) |
| Operate the local application | [Operations](OPERATIONS.md) |
| Cache routes, source behavior, and recovery | [Cache runbook](CACHE_HANDOFF.md) |
| Agent execution and browser boundaries | [Computer Use Agent](COMPUTER_USE_AGENT.md) |
| Site tools and automated verification | [Agent Optimization](AGENT_OPTIMIZATION.md) |
| UI authority and sibling synchronization | [Style reference](STYLE_REFERENCE.md) and [UI workflow](SHARED_UI_WORKFLOW.md) |
| Numbered-copy review and protected data | [Static-file housekeeping](STATIC_FILE_HOUSEKEEPING.md) |
| Operating limitations and prior repairs | [Known issues](KNOWN_ISSUES.md) |
| Bundled third-party material | [Third-party notices](THIRD_PARTY_NOTICES.md) |

## Dated evidence

- [Local documentation and test-system audit](DOCUMENTATION_TEST_AUDIT.md)
- [Test and CI history](TEST_HISTORY.md)
- [Cache Text audit](CACHE_TEXT_AUDIT_REVIEW.md)
- [Cache panel validation](CACHE_PANEL_LAYOUT_VALIDATION.md)
- [Account button validation](ACCOUNT_BUTTON_VALIDATION.md)

Some operational guides retain dated source-specific counts and incident examples. Preserve their
dates when citing them. Record new validation with its command, interpreter, operating system,
checkout boundary, and result; distinguish focused tests, complete gates, disposable browser
checks, and authenticated live checks.

## Maintenance

Update the document that owns the changed behavior, then link to it instead of repeating the full
contract. Keep runnable examples independent of a workstation-specific Python minor-version path.
Python 3.13 is the minimum; the CI baseline and historical execution records may name that version.
Both quality gates check repository-local Markdown navigation through `scripts/check_docs.py`.
Shared cross-repository references are reported separately and must be read locally when the
applicable collaboration rule requires them.
