# Test Suite

Test-suite version: `v1.3.4-codex.1`

The authoritative test workflow, coverage baseline, isolation contract, and CI behavior are
documented in [TESTING.md](TESTING.md). Use `./scripts/test.sh` and `./scripts/check.sh` on
macOS/Linux, or `.\scripts\test.ps1` and `.\scripts\check.ps1` on Windows.

## Coverage Map

This is a behavior map, not a claim of complete coverage or a current test-count baseline.

- `test_test_entrypoints.py` and `test_windows_entrypoints.py`: minimum Python version,
  interpreter arguments, safe marker selection, installer failures, pytest exit propagation,
  and rejection of absent or stale coverage reports. Native cases are platform-scoped.
- `test_documentation.py`: local Markdown navigation, reference links, image paths, repeated
  heading anchors, missing destinations, and portable treatment of shared references.
- `test_main.py` and `test_core_architecture.py`: application startup and core module boundaries.
- `test_config_and_state.py`, `test_state.py`, and `test_notice_banner.py`: configuration,
  state transitions, and notice behavior.
- `test_compute_jobs.py`, `test_agent_capability_registry.py`, `test_agent_event_chain.py`, and
  `test_agent_doctor.py`: durable jobs, capability contracts, event persistence, and recovery.
- `test_agent_controller_hardening.py` and `test_agent_live_capabilities.py`: deterministic
  controller safety and capability behavior; a filename containing "live" is not a pytest marker.
- `test_agent_session_sources.py`, `test_chatgpt_agent_sources.py`, and `test_agent_activity_e2e.py`:
  session discovery, source selection, and disposable-browser activity interactions.
- `test_chatgpt_downloader.py`, `test_gemini_downloader.py`, `test_grok_history.py`, and
  `test_claude_history.py`: provider parsing and persistence using isolated fixtures and fakes.
- `test_chat_history_browser.py`, `test_local_media_browser.py`, `test_prompt_store.py`, and
  `test_shadow_backup.py`: local resource browsing, prompts, recovery, and filesystem boundaries.
- `test_job_lock.py`: cache job ownership and contention.
- `test_safari_automation.py`: Safari automation protocol behavior through mocked boundaries.
- `test_web_app.py`, `test_style_tokens.py`, `test_style_token_registry.py`,
  `test_layout_anchor_contract.py`, and `test_style_alignment_e2e.py`: routes, design contracts,
  and rendered alignment.
- `test_agent_optimization.py`, `test_agent_optimization_browser.py`, and
  `test_agent_optimization.mjs`: Site tools contracts and disposable-browser registration.
- `test_demo_flight_agentic_crud.py`: controller CRUD in an ephemeral copy of an allowlisted
  local demo fixture; source snapshots are read-only and the original is verified afterward.

- `test_config*.py` and `test_state.py`: persisted settings, account-name safety, startup
  hydration, task state transitions, and event retention.
- `test_cache_catalog.py`: canonical X URLs, local-media classification, Parquet catalog
  recovery, cache claims, and per-account summaries.
- `test_downloader.py`: yt-dlp output classification, browser cookie arguments, and retry
  behavior.
- `test_scraper*.py` and `test_scraper_and_browser_sessions.py`: X handle discovery,
  timeline payload parsing, GraphQL request templates, and browser-session parsers.
- `test_grok_downloader*.py` and `test_grok_storage.py`: media signature validation,
  catalog deduplication, timestamp recovery, manifest recovery, and durable Grok queue
  transitions.
- `test_compute_backend.py`: bounded local image-analysis batches, resource limits, GPU/CPU
  recovery, deterministic result publication, metrics privacy, and parent streaming for oversized
  payloads.
- `test_service.py` and `test_services_and_web.py`: concurrent download orchestration,
  emergency-stop semantics, status summaries, Flask pages, APIs, settings, and reset routes.
- `test_logging_setup.py`: structured logging setup, JSON line output, file permissions,
  and credential redaction across messages, fields, exceptions, and stack traces.
- `test_runtime_isolation.py`: process-wide pytest runtime redirection and the Grok
  snapshot default-path regression.
- `test_responsive_contract.py`: shared CSS and JavaScript breakpoints, independent compact-content
  and sidebar-overlay boundaries, global hidden behavior, and bootstrap load order.
- `test_sidebar_e2e.py`: disposable Chromium coverage for target iPhone, iPad, and desktop
  viewports, touch dismissal, viewport transitions, horizontal overflow, toggle hit testing,
  and the runtime Chinese language-boundary contract that preserves source text.
- `test_computer_use_agent.py`: Web Computer Use Agent routing, provider/model contracts,
  context attachment, fenced JSON controller actions, project confinement, stop recovery,
  bodycheck gating, and managed browser-session lifecycle regressions.
- `test_agent_access_security.py` and `test_agent_source_cache.py`: Agent access authorization,
  source discovery isolation, cache freshness behavior, and protected Web session metadata flows.
- `test_scraper_and_browser_sessions.py`: managed browser-session creation, cleanup, isolation,
  and browser automation boundary regressions.

## Isolation Rules

`conftest.py` changes `HOME`, `AGENTIC_CONTEXT_RUNTIME_ROOT`, and `AGENTIC_CONTEXT_SETTINGS_PATH` before
application modules load. Filesystem tests use pytest temporary paths. The sidebar E2E suite uses
a clean disposable browser context against a local isolated Flask server. Authenticated browser
profiles, yt-dlp, X, Grok, remote network transport, and user-owned local media remain outside the
test boundary.

Run the full suite with `./scripts/test.sh` on macOS/Linux or `.\scripts\test.ps1` on Windows.
