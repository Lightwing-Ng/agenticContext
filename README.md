# agenticContext

Documentation version: `v1.16.3-codex.1`

agenticContext is a local Flask web console for preserving and using AI context
across conversations, media, prompts, projects, and browser agents. It caches
media from the currently signed-in X account's Likes timeline, Grok's Files
library, and configured ChatGPT projects or sessions. It stores media and text
history locally and provides a browser for reviewing, deleting, and restoring
resources.

Its Agent workspace uses the selected authenticated Web session for ChatGPT, Gemini, Grok, or Claude.
The sidebar exposes one provider-neutral Project concept: ChatGPT Projects, Gemini Notebooks, and
Grok Projects, and Claude Projects are adapted behind the same Project selector and execution contract. It defaults to
a new root-level session, while the sidebar can join one of the 20 most recent sessions, start a
session in one of the 20 most recent Projects, or join one of a Project's 20 most recent sessions.
Gemini's Project-new mode is a receipt-isolated controller task on the selected Notebook route; the
provider route does not prove that Gemini created a distinct subconversation.
The selected Web provider supplies reasoning while a bounded local Computer Use controller
reads, changes, runs, and verifies only the selected project. This fallback uses no API,
command-line coding-agent runtime, MCP connection, or third-party agent bridge.

Top-level application pages also publish a conservative OpenAI Site tools (WebMCP) adapter for
Agent Optimization. It exposes bounded capability discovery, current-page metadata, and allowlisted
same-origin navigation while keeping cached records, Cache lifecycle actions, Agent execution,
terminal authorization, and settings writes outside the v1 tool boundary. Browsers without Site
tools retain the complete human interface.

## Visual Style Reference

This is a sibling project of `../worthward/app`. Its visual
language is the source of truth for shared application-shell, typography, surface,
control, and motion decisions. Read [STYLE_REFERENCE.md](docs/STYLE_REFERENCE.md) before
making any UI change.

This project starts a web console on `http://localhost:8666` and listens on all network
interfaces so devices on the same LAN can use `http://<computer-ip>:8666`. Cache and Local
resources pages remain trusted-LAN surfaces; the Agent control plane adds a six-digit password
gate for private-network requests. Do not expose the LAN endpoint through port forwarding or a
public reverse proxy.

## Requirements

- macOS or Windows with a supported Python 3.13 or newer interpreter; the resolver prefers the
  host `python3` or the Windows `py -3` launcher when it is supported
- A signed-in Chrome or Edge session on Windows, or Chrome, Edge, or Safari on macOS, for the
  source you want to cache
- Playwright Chromium for Chromium-backed X, Grok, and ChatGPT automation
- `yt-dlp` for X media downloads
- An authenticated ChatGPT, Gemini, Grok, or Claude Web account for the optional Computer Use Agent workspace

ChatGPT project caching uses up to three isolated Edge workers in parallel. The worker count is
bounded deliberately because each worker owns a separate authenticated browser context.

## Quick Start

```bash
./scripts/setup_python.sh
```

Then start the app from the built-in Terminal on macOS:

```bash
./scripts/run_app.sh
```

On Windows, run the PowerShell entrypoint instead:

```powershell
.\scripts\setup_python.ps1
.\scripts\run_app.ps1
```

Set `AGENTIC_CONTEXT_SKIP_PLAYWRIGHT_INSTALL=1` only for an offline test-only dependency setup.
`AGENTIC_CONTEXT_PYTHON` is an explicit Python 3.13 or newer override intended primarily for CI or
local runtime compatibility.

## Quality Checks

Run the offline suite, including disposable Chromium tests, with:

```bash
./scripts/test.sh
```

On Windows:

```powershell
.\scripts\test.ps1
```

Run the complete local quality gate with:

```bash
./scripts/check.sh
```

On Windows:

```powershell
.\scripts\check.ps1
```

The quality gate runs Ruff, local documentation link checks, JavaScript syntax checks, the shared Agent Optimization Node contract,
the Python suite with branch coverage, and disposable Chromium browser flows. It is the same command
executed by GitHub Actions.
The browser flow uses a clean context against an isolated local server; the suite never opens
an authenticated profile, downloads media, or writes to user-owned caches, logs, or settings.
The CI portability rules and failure-triage contract are documented in
[docs/TESTING.md](docs/TESTING.md#ci-portability-contract).

## Documentation

Start with the [documentation index](docs/README.md) to distinguish current contracts from dated evidence.

- [Cache handoff and operating runbook](docs/CACHE_HANDOFF.md)
- [Architecture guide](docs/ARCHITECTURE.md)
- [Testing guide](docs/TESTING.md)
- [Operations guide](docs/OPERATIONS.md)
- [Known operating constraints](docs/KNOWN_ISSUES.md)
- [ChatGPT Web Computer Use Agent](docs/COMPUTER_USE_AGENT.md)
- [OpenAI Site tools and Agent Optimization](docs/AGENT_OPTIMIZATION.md)
- [Static-file numbered-copy housekeeping](docs/STATIC_FILE_HOUSEKEEPING.md)
- [Third-party notices](docs/THIRD_PARTY_NOTICES.md)
- [Engineering and test contract](docs/AGENTS.md)
- [Test coverage map](docs/TEST_COVERAGE.md)

## Project Layout

- `main.py`: supported Python 3.13 or newer entrypoint
- `app/core/`: cache services, browser automation, downloaders, state, storage, and logging
- `app/web/`: Flask routes, templates, static assets, and the local-media browser
- `tests/`: deterministic unit and Flask integration coverage
- `scripts/`: supported setup, launch, test, and quality-gate commands
- `local_store/`: ignored user-owned media cache
- `logs/`: ignored structured local logs

## Notes

- The first run works best when normal Chrome windows are closed.
- The default Chrome profile path follows the host platform: macOS uses
  `~/Library/Application Support/Google/Chrome`, while Windows uses
  `%LOCALAPPDATA%\Google\Chrome\User Data`.
- Media download relies on `yt-dlp --cookies-from-browser chrome`.
