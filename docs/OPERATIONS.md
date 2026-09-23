# Operations guide

Documentation version: `v1.35.1-codex.0`

## Launch

Prepare the supported local runtime:

```bash
./scripts/setup_python.sh
```

On Windows:

```powershell
.\scripts\setup_python.ps1
```

If the host interpreter is shared with an application that requires incompatible package versions,
use a project-local environment and retain the override for setup and launch:

```bash
python3 -m venv .venv
export AGENTIC_CONTEXT_PYTHON="$PWD/.venv/bin/python"
./scripts/setup_python.sh
./scripts/run_app.sh
```

On Windows:

```powershell
py -3 -m venv .venv
$env:AGENTIC_CONTEXT_PYTHON = (Resolve-Path .venv\Scripts\python.exe)
.\scripts\setup_python.ps1
.\scripts\run_app.ps1
```

Start the Flask console:

```bash
./scripts/run_app.sh
```

On Windows:

```powershell
.\scripts\run_app.ps1
```

On macOS, the launcher prefers `python3` from `PATH`, then tries unversioned platform Python
installations. It skips an otherwise supported interpreter when required application modules are
missing, so an already prepared platform installation can still start the app without a manual
interpreter override.

Directory controls use an in-page local folder browser on macOS and Windows. The browser starts
from the current valid value or the nearest readable existing parent, supports breadcrumbs, Up,
absolute-path navigation, empty folders, and cancellation, and writes the canonical absolute path
only after `Select current folder`. It does not invoke Terminal, Finder, AppleScript, an OS-native
panel, `showDirectoryPicker()`, uploads, probe files, or same-name path guessing. Manual path entry
continues to use the existing validation route. Browsing is loopback-only and remains subject to
the application's Host, same-origin write, and LAN-session boundaries; it does not change Agent
workspace authority or persisted settings until the existing field save flow runs. A running Agent
task keeps its project control locked.

The normal server address is `http://127.0.0.1:8666`, and the application binds only to loopback by
default. To opt in to trusted-LAN access, set `AGENTIC_CONTEXT_HOST=0.0.0.0` and set
`AGENTIC_CONTEXT_AGENT_PASSWORD` to exactly six ASCII digits before launch. There is no built-in
password. A successful unlock is stored in the signed Flask session for that browser.

When LAN access is enabled, every application route requires that signed session; static assets
and the unlock flow are the only pre-authentication exceptions. Unsafe private-network requests
also require a matching same-origin `Origin` header, and repeated failed unlocks are throttled per
client. Public, cross-site, host-rebinding, and malformed-host requests are rejected. Keep the
console on a trusted local network and do not expose it through router port forwarding or a
general-purpose public proxy. The only supported exception is the dedicated Gemini hostname in
[GEMINI_TUNNEL_SETUP.md](GEMINI_TUNNEL_SETUP.md): the application recognizes that exact Host and
admits only its OAuth metadata/endpoints and `/mcp/gemini`; every console path remains closed.

## Browser-session preconditions

- X caching begins from the currently signed-in Likes page in a supported host browser.
- Grok and ChatGPT syncing use their existing authenticated browser sessions.
- Opening or restoring a blank Jury page, choosing another browser, changing a juror or model,
  and choosing `New session` do not launch a browser clone. `Check accounts` is the explicit
  readiness probe; Start performs the required server-side recheck before it sends a prompt.
  Safari account checks use one task-owned window and one tab per selected juror, send no prompt,
  and reject Claude. Edge and Chrome continue to admit Claude when it is explicitly checked.
- Jury remembers only the last browser, juror keys, and model-tier keys in same-origin local
  browser storage. It validates them against the current rendered options before reuse and never
  persists readiness, account diagnostics, prompts, or session content in that record.
- The current Jury page sends no turn count. It continues only while parsed material evidence is
  changing, records consensus or stable disagreement explicitly, and retains one-hour wall-clock
  and 6,500,000-byte durable-record safety boundaries. Cached legacy clients that explicitly send
  a valid two-to-six-round budget remain bounded. Do not replace these boundaries with an
  unbounded loop or admit another Jury before every owned browser context has closed.
- A standalone `JSON` renderer label immediately before a complete object is a supported provider
  response shape. Other malformed output remains invalid. Repeated invalid votes terminate as
  `structured_vote_stalled`, never as a factual `evidence_stalled` disagreement; inspect the
  preserved raw responses before retrying.
- Recent sessions allows deletion only for a `failed` Jury archive whose coordinator, provider
  workers, Safari window, and durable cleanup ownership have all finished. The local API rechecks
  those conditions before unlinking the exact owner-only JSON record; never remove Jury records
  directly while cleanup is pending.
- Both Zhihu workflows support only Chrome and Edge. They use an isolated temporary clone of the
  selected signed-in profile and perform credentialed same-origin API reads; Safari and an
  unauthenticated standalone HTTP client are not supported.
- A Safari-backed Cache task is macOS-only, opt-in, and owns one standard, visible background window
  with native window controls. It restores the user's previous frontmost application after
  every window-affecting operation, then closes and verifies that exact window at task end;
  it must not hide, minimize, move offscreen, or accumulate Safari windows.
  If a previous process left a recorded task window behind, Recheck closes that leftover window
  when its owner pid is gone or belongs to this process. If Safari refuses to close it, Recheck
  reuses that same task window instead of blocking Account status. A lease that points at a
  pre-existing window (the daily Safari login window) is cleared without closing or navigating it.
  Cache pages that support text/media use `/cache/<source>/<text|media>/<browser>`, for example
  `/cache/grok/text/safari`. The short `/cache/grok` path still renders.
- Chrome, Edge, and Safari support differs by source and automation engine; use the session probe
  in the console before a long sync.
- Passive Agent checks use a quiet, isolated Chromium context. ChatGPT source checks use a
  non-headless context because the provider's Cloudflare challenge rejects headless clones with
  HTTP 403. Windows probes retain offscreen/minimized launch arguments. Existing macOS silent
  probes retain their task-stage window policy and foreground-app restoration.
- Executing Chrome tasks on macOS use an isolated profile clone without offscreen or
  start-minimized launch arguments. macOS Edge Agent probes, Recheck, login, and tasks use
  the persistent project debug Edge over CDP, like Windows, even before it is initialized.
  A daily Edge clone is challenged by Cloudflare on page load, and Playwright-launched clones
  fail Project `Send`. The failure lesson is recorded in [`CHATGPT_AGENT_CLOUDFLARE.md`](CHATGPT_AGENT_CLOUDFLARE.md).
  On Windows, a clone may be used before the project debug profile is initialized. After a
  successful debug-browser launch creates its marker, every later Windows Agent readiness,
  source, Project, and history probe or Agent task restarts or reuses that same persistent
  project profile over CDP, including after the user closes its window. Ordinary Cache
  probes and sync workers retain their existing clone-first behavior. The task selects or creates its provider page before
  normalizing that page's window. Windows uses CDP to request normal state and position
  `(80, 80)` at `1,280 × 900`, without
  requesting activation. A CDP window-control failure aborts before provider prompt submission.
  These fixed bounds are not a display-work-area guarantee; scaling and multiple monitors still
  require native Windows verification. One context does not guarantee one restored native window.
  Only macOS restores
  the previous foreground app; macOS decides Stage Manager grouping. Human verification reuses
  the same clone and retains the existing Resume gate. Automated execution never opens the user's
  original profile for writing. First-run, crash, notification, and repost prompts remain disabled.
- macOS Edge Jury shares that project debug profile. Its first
  `Check accounts` creates `<agent-runtime>/agent_browser_profile/edge` with owner-only directory
  permissions, starts one project Edge process, and opens one provider setup tab per selected
  juror. Complete the web-service sign-ins in those tabs and choose `Check accounts` again. Do not
  sign the Edge browser itself into a Microsoft account. The project Profile is never initialized
  from the daily Edge Profile and never copies `Local State`, Cookies, passwords, OneAuth data, or
  other browser identity material. Missing initialization and provider authentication both fail
  closed with actionable diagnostics; neither condition falls back to the daily Profile.
- Every macOS Edge Jury worker independently attaches over verified CDP to that one project process
  and creates its own Page in the persistent default context. Page leases are reference-counted in
  process; worker cleanup closes only its Page and connection. The last lease releases all task
  ownership but leaves the project Edge process and Profile available for reuse. A profile launch
  is serialized across threads and local service processes with an owner-only lock. The code does
  not use mock Keychain, basic password storage, disabled encryption, or Keychain mutation.
- Explicit login handoff opens the selected browser visibly. macOS Chrome and Safari login,
  and Windows conversation handoff, use the normal resolved browser. Windows Edge or Chrome login
  and macOS Edge login use the project-owned persistent debug profile so Recheck and later tasks
  read the same authentication state.
  Opening the page does not establish sign-in; the user must choose Recheck. Native Windows 11
  browser execution remains unverified on this macOS host.
- Windows Edge or Chrome login targets the project-owned debug browser instead of the user's
  daily profile. Because a running Edge or Chrome can keep its sign-in cookies under an exclusive
  OS lock, the Agent reuses a separate Chromium instance launched with
  `--remote-debugging-port` against a dedicated user-data directory under
  `local_store/agent_browser_profile/<browser>`. The login handoff navigates that debug browser to
  the provider home so the authenticated session lands in the debug profile the Agent reads over
  CDP. Once initialized, subsequent Windows Agent checks and tasks reattach to that same window
  while the user leaves it open. Playwright is not attached while `auth.openai.com` or a
  Cloudflare interstitial is visible, because enabling Runtime restarts Turnstile. Complete
  verification in the already-open window, then Recheck. The first sign-in on a fresh Windows
  debug profile is the only manual login the user performs there. One process-local lock owns each
  browser for the full CDP caller lifetime. Waiting is bounded to five seconds, and a busy
  operation fails clearly instead of blocking indefinitely. Windows Edge or Chrome and macOS Edge
  therefore admit one active task. macOS Chrome keeps isolated-clone concurrency.
  Multiple application processes are not coordinated by this lock.
- A Cloudflare or CAPTCHA page on ChatGPT, Gemini, Grok, or Claude is not clicked, filled, or
  reloaded. ChatGPT's `auth.openai.com` authorize popup is the same fail-closed case: leave that
  window alone until it returns to chatgpt.com. The Agent aside Account row tells the user to
  complete verification now in the open browser, then Recheck. Repeating the probe, opening
  login again, or clicking Turnstile repeatedly restarts the challenge.
- Copying profile files and launching the clone do not prove provider authentication. The copy
  is not an atomic snapshot of a running browser. Check readiness in the clone; report copy or
  access errors without closing the user's browser or retrying against its writable profile.
  On Windows, when the host browser holds its `Network/Cookies` lock, the launcher falls back to
  the project-owned debug browser over CDP instead of failing the clone; no file is read from the
  locked profile.
- Normal clone-backed task exit closes the isolated context and removes its temporary profile. A
  CDP-backed Windows or macOS Edge exit closes only the Playwright connection and leaves the project
  browser and persistent login profile available. Each subsequent Chromium launch also removes only abandoned
  `cachelikes-edge-*` or `cachelikes-chrome-*` directories older than 24 hours; unrelated temporary
  paths are not touched.
- A failed temporary-profile removal reports its retained directory and remains protected from
  stale-profile cleanup in the current process. Cleanup errors become exception notes and log
  warnings when an earlier task, launch, copy, or close error already exists; otherwise they fail
  the operation. Inspect task-owned browser processes before manual cleanup. This protection is
  process-local, not a durable orphan-process tracker across service restarts.
- Do not add or repeatedly troubleshoot login flows as part of normal runtime operation. The
  application assumes an existing signed-in session.

## Computer Use Agent

- Host-loopback requests continue directly. When trusted-LAN binding is explicitly enabled,
  RFC1918 private IPv4 and IPv6 ULA clients must unlock before any application page or API is
  served; public, cross-site, and host-rebinding requests remain rejected.
- Each task defaults to a new root-level ChatGPT, Gemini, Grok, or Claude Web conversation in the
  selected authenticated browser session. On macOS, Safari supports ChatGPT and Grok Agent
  execution; Safari Gemini and Claude remain valid source-only routes for account, Recent sessions,
  and Project browsing, while their full execution uses Edge or Chrome. macOS Safari Jury runs
  ChatGPT, Grok, and Gemini in one owned window with one tab per juror, including the account
  check, and does not fall back to Edge. Safari Jury rejects Claude; Edge and Chrome still admit
  it when explicitly checked. ChatGPT model selection holds one short focus transaction, because its
  menu closes when Safari loses focus. Other model-menu and Send actions restore focus
  independently; response polling does not hold a focus transaction. Window operations
  preserve a later user app switch. A failed task-window close retains the Safari lease and blocks
  another Safari Jury until tracked cleanup succeeds. A durable ownership token, pre-creation
  window/tab inventory, and owned window ID survive an app restart: admission remains blocked while
  that window exists or creation is ambiguous, then clears automatically once absence is proven.
  An uncertain create waits through a 40-second persisted settle interval and two stable read-only
  inventories 0.5 seconds apart before the lease can clear. Cleanup closes only the exact owned
  window through Safari itself; it never clicks whichever Safari window happens to be frontmost.
  SIGINT, SIGTERM, and process exit share one idempotent cleanup callback. Shutdown first closes
  Jury admission, cancels and waits for active account checks, then waits for the Safari coordinator
  to close its owned window. Restoring the previous frontmost app remains best-effort and does not
  control Stage Manager grouping.
  Browser/provider/model/workspace preferences are queued as one revisioned snapshot and restored
  before the first status or source request after reload. A retired or wrong-provider model invalidates
  the pending snapshot without a POST or retry loop. A selected Safari route is retained rather than
  silently replaced with Edge. On execution-capable routes, the Agent sidebar can
  also join one of the 20 most recent root
  sessions, start a session in one of the 20 most recent projects, or join one of a project's
  20 most recent sessions.
- Gemini `New session in project` proves only a fresh transfer receipt on the selected Notebook
  route; it does not prove that Gemini created an independent provider-side subconversation.
- Settings → Agent controls the operating system, terminal permissions, context limit, turn limit,
  command timeout, and per-operating-system prompts. The operating-system setting detects the host and
  selects macOS or Windows automatically. The selected value must match the host; Windows uses
  Edge or Chrome and does not expose Safari.
- Chromium can attach the generated Markdown context directly. Safari falls back to compact
  on-demand reads because web content cannot programmatically assign a local file to a protected
  file input.
- Sending a task transmits the generated context and requested source excerpts to the selected Web
  account. Review that provider's data controls before using private or regulated source code.
  Safari Gemini and Claude source-only browsing never attaches local context or submits a provider
  prompt.
- Stop requests end current web generation and terminate the active local command process group.
  Each request binds both the selected session and its current run ID; a delayed Stop from an older
  run of the same session returns HTTP 409. Safari resolves one unique semantic Stop control on the
  exact page and sends one trusted native Return. An ambiguous control causes zero input, and an
  uncertain post-input result is confirmed only through generation-state reads, never a retry.
  On Windows, approved `.ps1` scripts run through the PowerShell controller, and process-tree
  cleanup uses `taskkill /T /F` where applicable. That mechanism is not an OS-level sandbox.
  Stop requests do not stop a detached durable compute job; use its dedicated `Stop job` control
  or `job_stop` with the exact `job_id`.
- Local Web UI acceptance uses the `browser_acceptance` controller action rather than a provider
  browser profile or an arbitrary `run` command. The controller serves only the selected safe
  workspace directory from a task-owned process bound to `127.0.0.1`, refuses port 8666 and any
  occupied requested port, launches clean unauthenticated Chromium with no user-data directory,
  blocks non-loopback/file/credentialed requests, and checks fixed desktop and narrow viewports.
  Screenshot and trace evidence is retained only below the Agent runtime root with size caps;
  incomplete task artifacts are removed. Projects may additionally reserve up to 64 ports in a
  workspace-root `.agenticContext-browser-acceptance.json` file using schema version 1 and a
  `protected_ports` integer array; malformed, linked, oversized, or invalid configuration fails
  closed. Stop, timeout, launch failure, assertion setup failure, and controller exceptions close
  only that task's context, browser, preview process tree, and temporary artifacts. The Action and
  bounded observation are recorded in the normal Agent event chain, and Activity labels the
  workspace root/target plus desktop+narrow Chromium coverage.
- Agent source discovery is cached in `local_store/agent/agent_source_catalog.parquet` for 15
  minutes per provider/browser/Project key. Fresh reads use process memory; the first read after a
  restart hydrates memory from Parquet. Expired passive reads retain the previous catalog and never
  launch a background browser collector. One initial Agent bootstrap cache miss performs a bounded
  check. ChatGPT model and effort discovery happens in its capability bootstrap; later task
  submissions verify their requested settings without a separate refresh button. Concurrent
  non-forced requests share one flight.
  A forced Recheck waiting on an older same-key flight runs a new serialized collector after the old
  flight exits and does not accept the older result as its refresh. The
  response's `cache.status` is `hit`, `miss`, `refreshed`, or `stale`; a stale response means the
  previous verified catalog was retained after an explicit check failed.
- Safari admission is bidirectional: a Cache start is rejected while a Safari Agent owns the
  browser, and an Agent start is rejected while any Safari Cache worker owns it. While a Windows
  Agent task owns Edge or Chrome, or a macOS Agent task owns Safari, source,
  Project-session, history, and bootstrap routes do not start another live collector for that
  browser. They return a cached entry without background refresh, return `unprobed` on a
  catalog/bootstrap miss, or return HTTP 409 on a history miss. The Windows rule is browser-wide
  because every provider shares that browser's CDP context; the Safari rule protects its serialized
  Apple Events context. macOS Agent Edge and Chrome retain their isolated-context behavior; macOS
  Edge Jury alone uses the project process and Page leases described above.
- ChatGPT, Gemini, Grok, and Claude on `/agent` use one agent-scoped browser bootstrap through Recent sessions:
  the same bounded initial check verifies readiness, collects the root session/project catalog,
  returns it to the selector, and seeds the memory/Parquet cache. Cache reuse and task completion
  do not add a browser launch. A later browser launch is reserved for an explicit refresh, task
  submission, or later Project-session selection; an expired keyed Project-session read may serve
  stale rows while one coalesced quiet refresh runs. Safari Grok uses the same visible-composer and
  authenticated-conversations checks through page-local requests without exporting cookies or
  cloning an Edge profile. Restricted Claude accounts remain unavailable and are not sent through
  a login-bypass flow. Safari Gemini and Claude stop after catalog discovery, report execution as
  unsupported, and never enable Ask.

### ChatGPT Tunnel connection

Agent → **Tunnel**, with **ChatGPT** selected as the Web service, lets ChatGPT work on explicitly
registered local projects directly, through
the OpenAI Secure MCP Tunnel. No separate desktop app is involved: this service serves the MCP
endpoint at `/mcp` and supervises OpenAI's official `tunnel-client`.

One-time setup:

1. In Agent → Tunnel, Step 1 contains four expandable visual guides for the OpenAI Platform flow:
   create a Tunnel with a name, short description, organization, and ChatGPT workspace; copy the
   resulting `tunnel_` ID; open API keys and create a key with Expiration set to Never and
   Permissions set to All; then copy the complete `sk-proj-` value immediately because the
   Platform displays it only once. The guides are lightweight inline SVG reconstructions with no
   macOS window chrome, a shared 10px card radius, and intentionally no real identifiers or
   secrets. On narrow screens, focus or swipe the guide body to inspect the full-width card without
   shrinking its labels below their readable size. A Never-expiring key is long-lived: store it
   securely and rotate or revoke it when needed.
2. Enter the Tunnel ID and API key in Agent → Tunnel, Step 2; a qualified pair saves
   automatically. The key is written to `tunnel-credentials.json` beside `settings.json` with
   owner-only permissions and is never rendered back into the page; a blank key field keeps the
   saved key. Save and format errors appear in the sidebar Tunnel status hint. Saving the same
   qualified pair is also an explicit reconnect request, so recovery never requires rotating or
   re-entering a working secret.
3. In ChatGPT, enable Developer mode (Settings → Security and login) and create an app with
   Connection **Tunnel** (the same Tunnel), Authentication **No auth**. After switching the server
   behind an existing Tunnel app, refresh that app under ChatGPT Settings → Apps so it lists the
   current tools.
4. Register the projects ChatGPT may use in `tunnel-projects.json` beside `settings.json`
   (`~/Library/Application Support/agenticContext/` on macOS). The file is user-owned; the
   service only reads it, and edits apply on the next tool call without a restart:

   ```json
   {
     "schema_version": 1,
     "projects": [
       {"id": "agenticContext", "root": "/Users/<you>/Desktop/agenticContext", "writable": true},
       {"id": "worthward", "root": "/Users/<you>/Desktop/worthward", "writable": false},
       {"id": "neoMe", "root": "/Users/<you>/Desktop/neoMe", "writable": false},
       {"id": "shared-docs", "root": "/Users/<you>/Desktop/shared_docs", "writable": false}
     ]
   }
   ```

   `writable` defaults to `false`; grant it only to projects ChatGPT may change. The registry is the
   maximum authorization boundary. Roots must be absolute canonical paths, must not overlap, and
   must not be the filesystem root or the home folder. A structurally invalid registry fails
   closed, but a valid entry whose directory is absent makes only that project unavailable; the
   other valid projects remain usable. Without this file, the Agent's selected workspace is the
   only project, and only when it is a Git repository root, so a parent folder such as the Desktop
   is never exposed as one project.
5. Return to Agent → Tunnel. Check the registered projects that should form the preferred discovery
   set, then choose one checked and available project as `Current`. Each row shows the exact id,
   host path, read/write authority, identity fingerprint, and availability. The preferred set and
   current id are saved together only after the service accepts the current selection revision.
   This checklist does not change registry authority: an explicitly named registered reference
   remains available, and a checked read-only project remains read-only. If the current project is
   later removed or becomes unavailable, the page can fall back to another available preferred
   project instead of leaving a deleted entry as a global blocker.
6. The circular Project folder action opens the local directory browser at the current macOS
   account's `Path.home() / "Desktop"` when available, otherwise at that account's home directory.
   The start folder is not an authorized project. Selecting a folder switches only when its
   canonical path exactly equals a registered root; a nested, parent, same-named, or unregistered
   directory leaves permissions and selection unchanged.

Runtime behavior:

- `python3 main.py` starts the Tunnel when credentials exist. Saving credentials requests a
  reconnect after a short debounce, and the ChatGPT status card also exposes Retry/Reconnect using
  the saved pair. Test and isolated app instances never start `tunnel-client`.
- State lives in `tunnel/` beside `settings.json`: the pinned client under `tools/`, the client log
  (`tunnel-client.log`, previous run in `.log.1`), its pid file, health URL, a per-start bearer
  token file (`0600`), durable check jobs, and mutation request records. A client left behind by a
  killed service is found by its state path and stopped before a new one starts.
- The API key reaches the child only as `CONTROL_PLANE_API_KEY`. The child receives a strict
  cross-platform allowlist of process, locale, temporary-directory, and CA-certificate variables;
  unrelated host credentials are not inherited. The control-plane proxy comes from
  `HTTPS_PROXY`/`HTTP_PROXY` or, on macOS, the system proxy; loopback is always exempt so MCP
  traffic stays local.
- `/mcp` accepts only loopback callers that present the current bearer token. `GET /mcp` returns
  405 (no SSE stream). An `OAuth discovery failed` warning in the client log is expected: the app
  uses No auth, so no OAuth metadata is published and readiness does not depend on it.
- Both authenticated MCP ingress routes cap JSON request bodies at 2 MiB. JSON-RPC batches are
  rejected before dispatch when they contain more than eight items, so an oversized batch cannot
  partially execute. Each accepted request therefore has a bounded parse cost, dispatch count, and
  aggregate response fan-out.
- Credentials configured, transport ready, a tool request observed, and a successful operation in
  the selected project are separate status facts. A configured, currently ready transport may
  start its first read-only check or normal task without any historical call. Historical success
  does not keep the card green after a status timeout, HTTP failure, stale response, or readiness
  loss. The page bounds and cancels status fetches, shows the last successful status time and the
  current redacted problem, and offers Retry/Reconnect with the saved credentials.
- Preflight, download, and transient network failures use bounded exponential retry; a stable
  ready interval resets the backoff. A client process that remains alive but unhealthy past its
  readiness deadline is recycled under its current generation. Superseded process or status
  results cannot overwrite a newer connection, and uncertain in-flight work is surfaced for
  reconciliation instead of being assumed successful. Closing the management page has no effect
  on the service-owned Tunnel process.
- `current_project` is the only project-less tool. It lets a direct natural-language request such
  as `@AgenticContext check the current project` discover the locally selected registered id,
  identity, permission, availability, selection revision, and preferred project ids before calling
  `project_overview`. It also returns bounded records and preferred-state flags for all other
  registry-authorized projects, so one task may explicitly consult a read-only reference even when
  that project is not in the local preferred checklist. Host paths stay local to the management
  page and are not returned to the model.
- Every other tool names one resolved `project`; the normal page workflow requires a registered
  project. The no-registry compatibility fallback is explicitly unregistered and read-only, and
  the page will not present it as authorized or enable kickoff. Each id matches exactly and is never
  interpreted as a path. `project_overview` establishes the id and identity; every later
  project-scoped call
  must carry the returned `project_identity`, so changing the page selection cannot redirect an
  older request. The identity also binds the directory's native filesystem identity: replacing a
  root at the same path invalidates the old identity, and the failed request is never replayed
  against the replacement. Paths are relative to
  that project's root (absolute and `~` paths are refused), and every path goes through the Browser
  Agent's confinement, symlink, ignored-folder, and credential-file rules for that root. Read-only
  projects refuse every mutation or check before touching anything. Each project keeps its own read
  receipts, SHA-256 guards, edit generation, and verification evidence.
- The registry cache key includes the canonical registry path, device and file identity, modified
  and change timestamps, and byte size. Replacing a same-sized registry while preserving its
  modification time therefore invalidates cached authority. Queued project operations resolve and
  compare the project identity and permission again after acquiring the per-project lock; a
  revocation or remapping while queued is refused before the operation runs.
- If the registry file is absent, the read-only compatibility fallback is available only when
  `git rev-parse --show-toplevel` confirms that the selected directory is itself the work-tree
  root. A `.git` placeholder or a nested directory is not sufficient.
- Availability is evaluated per registered project. A root that is temporarily absent remains
  visible as registered but unavailable and cannot be selected or opened; it does not suppress
  other usable projects. If that root later appears, discovery binds its native filesystem
  identity before any tool call can proceed.
- The public catalog is exactly: `current_project`; `project_overview` (writability, root and nested
  instruction files of that project only, bounded Git status); `list_files`; `search_files`;
  `read_files` (1-8 files or ranges with SHA-256, truncation, and the next line and optional
  character offset to request);
  `apply_edits` (1-16 exact replacements with write-stage recovery); `write_file` (new UTF-8 files
  with optional project-local parent creation, or whole-file replacement only with the SHA-256
  from `read_files`); `delete_file` (the current SHA-256 from `read_files`); `run_check`;
  `start_check`; `observe_check`; `stop_check`; `show_changes` (status, staged/unstaged stats, and an
  optional bounded patch); and `review_changes` (bodycheck, the final gate). Arguments are
  validated against published closed schemas, so unknown fields fail. Removed compatibility names
  such as `read_file`, `replace_in_file`, `create_file`, `call_runtime_tool`, `list_projects`,
  `git_log`, and `git_diff_hunks` are not dispatchable.
- Batch reads distinguish `all_succeeded`, `partial`, and `all_failed`; a top-level success never
  hides an item failure. Their combined model-facing file content is bounded, and a single long
  line can be continued with the returned `next_start_line` and `next_start_character`. Every
  mutation requires a stable `request_id`. A completed retry returns the durable recorded result
  instead of applying it again; a record whose final outcome is unknown refuses blind replay and
  tells the caller to read and reconcile first. When a completed response ages out of the replay
  journal, a compact request-id tombstone remains so that the same id returns `outcome_unknown`
  rather than applying the mutation again. Tombstones are capped at 100,000 records; once that
  safety limit is exhausted, new mutations fail closed instead of discarding older ids. Preserve
  the `expired-ids.log` file beside the request records. Multi-file
  edits preflight the entire batch, verify every published file, and compare current content before
  rollback. A concurrent edit is preserved and reported as a recovery conflict instead of being
  overwritten. The result names committed, uncommitted, rolled-back, and conflicted files so it
  cannot claim all-or-nothing after a partial write.
- `show_changes` always reports path-scoped status and staged/unstaged stats. Set
  `include_patch=true` for a bounded unified patch; `staged=true` selects the staged patch, and
  `path` confines status, stats, and patch together. Credential and controller-internal names
  and patch segments are withheld from every model-visible Git summary. An ordinary untracked
  file appears in status but has no Git patch yet.
- `run_check` invokes only the controller's approved, shell-free command forms with a confined
  project working directory and bounded time, output, and process cleanup. For longer work,
  `start_check` launches the same allowlisted command in a durable bounded runner;
  `observe_check` retrieves status and a bounded output tail across MCP requests or a service
  restart, and `stop_check` cancels only the runner whose recorded process identity still matches.
  An identical active command or repeated idempotency key is deduplicated. A runner that vanished
  without a terminal result is reported as `unknown`, never left permanently `running`. Terminal
  reconciliation is durable and ordered, so querying an older failed job cannot revoke a newer
  pass; every reported successful verification is still rechecked against the current workspace
  fingerprint. The
  invoked test or build remains trusted project code: it runs as the AgenticContext service
  account and is not placed in a hermetic filesystem or network sandbox. Register only trusted
  projects and checks.
- Mutations, synchronous checks, and final review for one project are serialized; different
  projects do not block each other. Read-only file/Git/check-status observation does not wait
  behind a long synchronous check. Only a successful check whose captured workspace fingerprint
  still matches can satisfy `review_changes`; later edits invalidate it.
- Runtime selection is internal to AgenticContext. No public tool accepts or requires
  `runtime`, `runtime_name`, `execution_mode`, `runtime_gateway`, `adaptive_runtime`, or
  `full_operator_runtime`. Transport-added routing metadata is ignored before closed-schema
  validation and cannot select a project or action; every other undeclared argument is rejected.
- The Python tool catalog is process-static, so `initialize` and `server/discover` advertise
  `tools.listChanged=false` and no list-changed notification is ever sent. Each layer refreshes
  only its own state:
  - Restarting the AgenticContext service (Python process) is the only step that loads a changed
    `TUNNEL_TOOLS` or dispatcher; `tools/list` and `tools/call` both read that one table.
  - A `tunnel-client` reconnect (a Step 2 credential save, or the automatic restart) renews forwarding and the
    bearer token only; it reloads no Python code and changes no tool.
  - An MCP client reconnect (`initialize` again) receives the running process's catalog, but a
    client may keep its cached tool snapshot.
  - ChatGPT Settings → Apps → AgenticContext → Refresh/Scan Tools replaces ChatGPT's stored
    snapshot. After a catalog change, restart the service first, then refresh the app.
- Every tool call is logged as
  `Tunnel tool <name> provider=<provider> ok=<bool> duration=<s> project=<id> target=<path or command>` in the
  application log, so a ChatGPT session can be audited after a restart.
- The status card exposes one `Tokens:` row. Its value is a bounded estimate for retained MCP
  request/response tool text, not ChatGPT billing, account balance, remaining quota, or a model
  task total. The accessible label and tooltip identify that estimate explicitly, and incomplete
  data is shown as unavailable instead of becoming a fabricated zero.
- Arbitrary shell commands, arbitrary executables, and general background processes are
  intentionally not exposed; only the approved verification commands run through `run_check` or
  the bounded durable-check lifecycle.
- WebCodex Desktop, its DMG, GUI helper processes, and desktop IPC are not part of this path and
  are not required at runtime.

Daily two-page workflow:

1. Open `http://localhost:8666/agent/tunnel/chatgpt`, check the registered projects wanted in the
   preferred set, choose one available checked project as `Current`, and wait for the saved
   selection revision. Confirm its path, permission, and a currently Ready transport; use
   Retry/Reconnect if the transport is stale or failed. The folder action is an exact registry-root
   matcher, not a registration or write-permission action.
2. Open ChatGPT, choose `@AgenticContext`, and describe the task normally. Copying the local prompt
   is optional; when used, it includes the exact project id and preserves the user's task text as
   the selection changes.
3. A task with no supplied id calls `current_project`, pins the returned id/identity, then reads
   `project_overview` and the applicable instructions before any file operation. Every later call
   still sends that explicit id.
4. After changes, read back hashes or inspect changes, run an approved synchronous or durable
   check, and call `review_changes`. A successful check from older workspace content is rejected.
5. Ordinary project switching or transport recovery needs no plugin recreation. Adoption after a
   source or tool-catalog change has three distinct stages: files updated on disk; the
   AgenticContext Python service restarted and serving that code; and ChatGPT's cached app catalog
   refreshed through Settings → Apps → AgenticContext → Refresh/Scan Tools. A Tunnel reconnect
   updates forwarding only. Confirm each stage separately, then start a new ChatGPT conversation.

Maintaining the client when OpenAI changes it:

1. The version and six archive/binary SHA-256 pairs are pinned in `app/core/tunnel_runtime.py`.
   Take new values only from the official `openai/tunnel-client` release. For a one-off trial,
   `AGENTIC_CONTEXT_TUNNEL_CLIENT_BIN` points the service at another binary.
2. Run `tests/test_tunnel_runtime.py`, `tests/test_tunnel_mcp.py`, and
   `tests/test_tunnel_credentials.py`.
3. Exercise the real forwarding path without ChatGPT: with the service running, start
   `tunnel-client dev proxy --mcp-server-url url=http://127.0.0.1:8666/mcp,channel=main` with
   `MCP_EXTRA_HEADERS="Authorization: file:<tunnel/mcp-authorization>"`, then send `initialize`,
   `tools/list`, and a read-only `tools/call` to the printed MCP URL.
4. Ablate before adopting a new flag, header, or protocol version: remove one change at a time and
   keep it only if its removal breaks readiness (`/readyz`), the `dev proxy` round trip, or a live
   ChatGPT call. Update `MCP_LEGACY_PROTOCOL_VERSIONS` and `MCP_STATELESS_PROTOCOL_VERSION` in
   `app/core/tunnel_mcp.py` the same way.

### Gemini Custom App connection

Select **Gemini** in Agent → Tunnel to configure the independent Gemini transport. It reuses the
same exact fourteen-tool catalog and project authority but does not reuse the OpenAI Tunnel ID, API key,
control plane, local bearer, or supervised process. Gemini requires a stable public HTTPS MCP URL,
so the operator supplies a dedicated reverse tunnel and AgenticContext supplies a static OAuth 2.1
client with PKCE S256.

Follow [GEMINI_TUNNEL_SETUP.md](GEMINI_TUNNEL_SETUP.md) for the complete setup, Cloudflare named-
tunnel example, current consumer-account prerequisites, Gemini Connected Apps steps, verification,
rotation, and troubleshooting. Operational invariants are:

- Keep AgenticContext bound to `127.0.0.1:8666`; the reverse tunnel forwards to loopback and
  sets the origin Host to the dedicated public hostname. The proxy itself must allowlist only the
  six documented OAuth/MCP paths and return its own 404 for every other path.
- Only the documented OAuth discovery, authorization, token, and `/mcp/gemini` paths are reachable
  on that Host. A public 404 for the console, local APIs, static files, ChatGPT `/mcp`, and every
  other path is expected.
- The private `gemini-tunnel-credentials.json` is independent of `tunnel-credentials.json`. POSIX
  hosts enforce mode `0600`; on Windows verify that the current user's settings-directory ACL is
  restricted to the intended account. Never put the Gemini client secret, signing key, access
  token, or refresh token in a URL, repository, reverse-proxy configuration, or support log.
- Configured means only that the local origin and OAuth material exist. Active requires an
  authenticated Gemini tool call observed by this process. Local tests do not prove a real Gemini
  connection, consumer eligibility, enterprise compatibility, or write-confirmation behavior.
- Clearing the public origin revokes the current local OAuth material. Stop or remove the external
  reverse tunnel and disconnect or remove the Custom App in Gemini separately.
- A process restart preserves the saved static client credentials but invalidates every in-memory
  authorization code, access token, and refresh token. Reconnect the Gemini Custom App after the
  service restarts.
- Copying the client secret never approves a callback. A complete valid authorization request is
  held in process memory for 10 minutes while the authenticated local Agent UI displays its exact
  callback URI. Approve or deny that exact URI there; a different redirect, state, PKCE challenge,
  client, resource, or scope cannot consume the approval. An opaque review identity also prevents
  a stale local page from approving a request that replaced the one it displayed.
- The OAuth adapter does not guess an undocumented consumer callback hostname. It pins the first
  approved complete callback URI for the current process. Deny an unexpected URI instead of
  broadening or bypassing the local review.

### Durable optimization jobs

Before `job_start`, create a reviewed workspace-root `.agenticContext-compute.json` file. It must pin
the exact approved entrypoint bytes, for example:

```json
{
  "schema_version": 1,
  "entrypoints": [
    {
      "id": "optimizer",
      "path": "scripts/optimizer.py",
      "sha256": "64-lowercase-hex-characters",
      "max_runtime_seconds": 43200
    }
  ]
}
```

The runtime must be between 43,200 and 86,400 seconds. Recalculate and deliberately review the
digest after every entrypoint change; a stale digest fails closed. The optimizer receives only
`--config <runtime-copy> --job-runtime <task-directory>` and, for an explicit resume,
`--resume <prior-checkpoint>`. Its config is a workspace-relative regular JSON file no larger than
1 MiB. Shell operators, redirects, executable names, environment enumeration, download commands,
and arbitrary argument vectors are not part of the action protocol.
On macOS, the worker also runs through `/usr/bin/sandbox-exec` with `network*` denied. The Windows
path does not currently apply an equivalent OS-level network-denying sandbox profile; the worker
runs with the current user's permissions. Optimizers must therefore use only local datasets and
must not depend on license servers, remote telemetry, distributed network workers, or localhost
sockets during the job.

Publish heartbeat data atomically to `progress.json`. Supported fields are generation or iteration,
completed and total evaluations, best objective, elapsed time, optional reliable ETA, and a bounded
summary. Publish the final export to `result.json`. Logs roll in place at 5 MiB; `job_status` returns
only the latest 4,000 characters.

Use `write_optimizer_checkpoint_atomic()` for `checkpoint.json`. Schema version 1 requires the
optimizer version, iteration, population or optimizer state, RNG state, seed, best objective, best
parameters, and evaluation count. A restart marks a missing worker interrupted. Resume is always an
explicit new `job_start` with a new idempotency key and the prior `resume_job_id`; the service never
duplicates an expensive run automatically.

Closing or refreshing the Agent page, finishing one provider turn, or reaching a Web Agent final
response does not stop the worker. Normal idle sleep is inhibited while it runs on macOS. The
project does not currently inhibit Windows idle sleep for the equivalent task. Closing the lid,
explicit Sleep, logout, reboot, power loss, service-host failure, or hardware failure can still
interrupt it, so the optimizer must checkpoint frequently enough for the workload.

## Local data

| Location | Contents |
| --- | --- |
| `local_store/x/` | X media and cache-catalog state |
| `local_store/media/grok/` | Grok media, catalog, manifest, and work queue |
| `local_store/media/chatgpt/<project-name>/` | ChatGPT images and catalog state |
| `local_store/llm/chatgpt/history.parquet` | ChatGPT typed text history |
| `local_store/llm/gemini/history.parquet` | Gemini typed text history |
| `local_store/llm/grok/history.parquet` | Grok typed text history |
| `local_store/llm/claude/history.parquet` | Claude typed text history |
| `local_store/llm/zhihu/history.parquet` | Formal Zhihu answer text, rich-text source, and source links |
| `local_store/prompt/prompts.parquet` | Saved prompt content snapshots and source pointers; prompts remain available if source history disappears |
| `local_store/agent/agent_source_catalog.parquet` | Provider-neutral Agent session and Project discovery cache |
| `local_store/.cache_task.lock` | Cross-source advisory task lock |
| `local_store/.browser-trash/` | Recoverable previews moved by the local-media browser |
| `local_store/.browser_deleted.json` | Browser deletion tombstones and exclusion identities |
| `logs/cachelikes.log.jsonl` | Structured local application log |
| Platform-native agenticContext settings path (`~/Library/Application Support/agenticContext/...` on macOS; `%APPDATA%\agenticContext\...` on Windows) | Device-local saved settings |

All cache and log paths are ignored by Git. Back up local media before using any destructive reset
operation.

### Zhihu text cache

Open `GET /cache/zhihu`. Edge is selected by default; Chrome is the only alternative. The account
card calls the ordinary browser-session probe and enables Start only after
`https://www.zhihu.com/api/v4/me` identifies a signed-in account.

Leave `Answerer URL` blank to cache every unique answer found while paging that account's
`MEMBER_VOTEUP_ANSWER` activity. Enter a validated Zhihu people or Answers URL to cache every
answer exposed by that answerer's paginated API instead. Both modes recheck the newest page before
an atomic cumulative merge into `local_store/llm/zhihu/history.parquet`; a later run never removes
answers collected by an earlier mode. An activity target whose body is unavailable is retained as
an excerpt-plus-source-link record instead of aborting the complete liked-answer run. Review the
answers under Local resources with source `Zhihu`; the source-specific sidebar removes Clear
filters and adds a standard Answerer select populated from cached names. Its selection scopes the
metrics, sessions, search, ordering, and pagination. The list shows the literal answerer, omits its
redundant Source column and answer ID, and removes the inapplicable Projects metric. Detail tables
use the literal answerer name instead of Role and render the complete stored text without the
generic message-collapse limit. Original answer, question, profile, embedded anchor, and remote
image URLs remain explicit source links. The collector selects the canonical answer-body node,
preserves paragraphs, lists, headings, emphasis, links, rules, figures, and captions, and writes the
same structure as portable Markdown. Page controls are excluded. Rich text is normalized again and
sanitized at the final render boundary; duplicate fallback and lazy-loader copies become one local
`photo.badge.arrow.down.svg` placeholder per figure. The default `Export Markdown` action downloads
every cached answer for the selected answerer across all Local resources pages. No provider image URL
is mounted and no remote image binary is downloaded.

## Concurrency and local compute

The shared `Download workers` setting accepts values from `1` through `8`. Older settings files
remain readable; values below or above that range are normalized on load, and direct
`CrawlConfig` construction applies the same limit. Grok remains capped at four download workers,
ChatGPT remains capped at three isolated Chromium workers, and Safari remains serialized.

ChatGPT visual-signature hydration is a separate local CPU stage. Small batches stay in the parent
process, while larger batches may use the automatically discovered conservative process budget.
The payload budget is enforced before reads; oversized files are decoded directly by the parent
instead of being queued or sent to a process worker.
The status snapshot exposes only numeric stage counts, durations, worker counts, queue depth, backend
name, and fallback counts. It never records image bytes, prompts, browser data, cookies, URLs, or
file paths. The base installation reports GPU unavailable because no optional GPU framework or
adapter is installed.

For a repeatable isolated measurement, run:

```bash
python3 scripts/benchmark_compute.py
```

On Windows:

```powershell
py -3 scripts/benchmark_compute.py
```

The benchmark creates a temporary synthetic fixture, performs one warmup and five measured runs,
and reports distributions for the legacy sequential path and the bounded CPU backend. It does not
read the local cache or launch a browser.

## Reset, deletion, and restoration

The local-media browser's Delete action moves a supported media file into recoverable browser trash
and records a tombstone so the same source resource is not immediately re-downloaded. Restore moves
the retained preview back to its original cache path.

The Grok and ChatGPT reset actions are more destructive: they remove their cached media and source
state for a complete future resync. Run them only while the corresponding task is idle and only when
you intend to discard that cache. Do not use reset operations as a routine troubleshooting step.

## Troubleshooting

- Missing Playwright Chromium: on macOS run `./scripts/setup_python.sh`, or run
  `python3 -m playwright install chromium` with the same Python 3.13 or newer interpreter
  used by the application. On Windows run `.\scripts\setup_python.ps1` or `py -3 -m playwright install chromium`.
- Missing downloader: install the project requirements so `yt-dlp` is available to the selected
  supported interpreter.
- Browser profile lock: on Windows a running Edge or Chrome locks `Network/Cookies`, so the
  Agent falls back to the project-owned debug browser over CDP and the first sign-in on that
  debug profile is the only manual step. On macOS, close duplicate normal browser windows, then
  retry the session probe.
- macOS Edge Jury setup: choose `Check accounts` once to open the owner-only project Profile, sign
  in to each selected provider tab, then choose `Check accounts` again. Do not close or alter the
  daily Edge Profile, do not approve or reset Keychain entries as a troubleshooting step, and do
  not add `--use-mock-keychain`, `--password-store=basic`, or encryption-disabling flags. If a fresh
  project Profile still produces a OneAuth prompt, stop and investigate Edge/OneAuth or Keychain
  ACL behavior; do not hide or automate the prompt.
- ChatGPT parallel sync: Cache Download workers retain their clone-first path and can use up to
  three isolated Edge contexts. If Windows profile copying reaches the existing Cookie-lock CDP
  fallback, that fallback is serialized through the single-owner project debug browser. Lower the
  shared Download workers setting only when the machine cannot sustain the browser load.
- ChatGPT Text history schema 3 keeps visible user prompts and completed final assistant replies.
  Tool recipients, non-final channels, hidden context, reasoning payloads, and incomplete replies
  are excluded using provider metadata, not wording or JSON detection. Ordinary legacy replies
  without channel/completion metadata remain supported. Older cached sessions are fetched again
  on the next Text sync even when their provider revision matches; each successful fetch replaces
  that session's rows while preserving retained messages' first-seen timestamps. Failed fetches
  leave existing rows intact. Let an active cache task finish before restarting into the new code
  and running Text sync; do not edit its live Parquet file or reset cached media to apply this change.
- Legacy ChatGPT tool traces can be reviewed offline with
  `python3 -m app.core.chatgpt_history_cleanup` (use `py -3 -m` on Windows).
  Add `--apply` to remove recognized traces preceding a later reply in the same turn.
  This includes multiline `fast|`, `slow|`, `open|`, and `find|` command batches with
  an optional trailing `length|short`, `length|medium`, or `length|long` option.
  Every line must match the protocol; mixed prose and fenced examples are retained.
  Ordinary prose is retained unless explicitly reviewed with `--remove-message chat-<id>`.
  The command acquires the cache task lock, backs up the original Parquet and a removal manifest
  under `local_store/recovery/`, and atomically writes the cleaned history. It preserves user
  messages, retained content, timestamps, and session IDs. Do not restore the backup over an
  active cache job. Channel-less future messages also reject standalone tool syntax; explicit
  final-channel JSON or code answers remain supported.
- Sync failure: inspect `logs/cachelikes.log.jsonl` for full structured diagnostics. The UI shows a
  bounded status message while retaining the detailed local log.
- Grok Text cache: the legacy text runtime follows all Grok conversation pages and response
  trees, but `/cache/grok` no longer renders a redundant sidebar action. Review the resulting
  history through Local resources, and see [CACHE_HANDOFF.md](CACHE_HANDOFF.md) for status
  routes, verified counts, and recovery commands.
- Gemini Text cache defaults to Edge on macOS and Windows. Safari is macOS-only; if it is explicitly
  selected there, preserve the saved Safari navigation interval. If Safari reaches `Failed to open
  page`, stop that run, close its single task window, and restart after confirming the window count
  returned to baseline. Never accelerate the run by reducing the saved interval and never open
  parallel Safari task windows.
- Gemini Text on Edge: use the headless Chromium history-RPC path. It follows the authenticated
  `MaZiqc` cursor, stores a 24-hour discovery checkpoint, and resumes by skipping cached session
  IDs. The current verified run exposed `740` sessions and cached `736` text-bearing sessions
  with `4,045` messages. A no-text session is an expected skip; a Google human-verification
  challenge must stop the task and be reported to the operator.
- Zhihu verification: HTTP 401/403, error code `40352`, `need_login`, or a visible human-check page
  is a terminal result for that run. Keep the existing archive, open the profile in the selected
  host browser, complete the provider's check manually, and explicitly retry. Do not copy cookies,
  add a CAPTCHA solver, switch to a raw HTTP scraper, or loop retries around the challenge.
- Zhihu count mismatch: do not edit the Parquet file or invent missing IDs. A short terminal result
  whose raw count is also below the reported total fails closed. A duplicate-backed provider gap is
  accepted only after two full enumerations reproduce the same ordered unique IDs and pagination
  counts; the gap is then persisted and shown as Not enumerable. Any drift preserves the earlier
  archive until a later explicit run succeeds.
- Cache inconsistency: use the local-media browser to inspect the affected source before choosing a
  source-specific reset. Avoid deleting catalog or manifest files by hand.

## Safe development checks

Run `./scripts/test.sh` or `./scripts/check.sh` (macOS/Linux) or `.\scripts\test.ps1` or
`.\scripts\check.ps1` (Windows) for offline validation. Pytest redirects all
default runtime paths into temporary directories; tests must never be pointed at the production
cache, Beta archive, log, settings, or browser-profile locations.
