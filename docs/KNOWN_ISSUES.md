# Known operating constraints and behavior-change history

Documentation version: `v1.19.4-codex.1`

## Windows host operating constraints

- The project does not currently inhibit Windows idle sleep while compute or Agent tasks are
  running. Configure Windows power settings when a long-running task must not be interrupted by
  system sleep.
- The Windows path does not currently apply an OS-level network-denying sandbox equivalent to the
  macOS `sandbox-exec` profile. Compute workers run with the current user's permissions.
  Detached compute jobs use native Job Object lifetime containment; ordinary Agent inspection
  processes retain the best-effort `taskkill /T /F` fallback. The verified allowlist remains
  an application-level control rather than an OS sandbox boundary.
- Safari is macOS-only and is normalized away on Windows (`normalize_host_browser` in
  `app/core/config.py`); Agent sessions on Windows require Edge or Chrome.
- Windows file permissions use inherited ACLs rather than the explicit POSIX `0700`/`0600` mode
  bits applied on macOS. Local stores inherit the containing directory's ACL.
- Prefer the `py -3` launcher on Windows; the resolver also accepts a `python` command that
  resolves to Python 3.13 or newer. Do not assume `python3` exists. `AGENTIC_CONTEXT_PYTHON`
  overrides the resolver on both platforms.

## Bounded local compute rollout on 1 Sep 2026

- ChatGPT catalog visual hydration now has a pure CPU backend with a 64-image parallel threshold,
  conservative process and memory budgets, deterministic result validation, and privacy-safe stage
  metrics. Browser contexts, authentication headers, catalogs, task state, and final files remain
  outside process workers.
- The base requirements intentionally contain no CUDA, MPS/Metal, ROCm, PyTorch, CuPy, or other
  GPU framework. `detect_gpu_capability()` therefore reports GPU unavailable. The adapter contract
  and failure tests are extensibility points only; this project has no live GPU hardware evidence
  and must not report mock or static validation as GPU acceleration.
- A failed or partial optional GPU batch is never committed. The complete batch is recomputed on a
  clean CPU backend. A CPU process-worker failure follows the same safe parent-process recompute.
- The shared `download_workers` setting is now bounded to `1` through `8` while preserving legacy
  JSON readability. ChatGPT's maximum of three isolated Chromium contexts, Grok's maximum of four
  media workers, Safari serialization, and the cross-workflow task lock are unchanged.
- The ChatGPT conversation and project-index result queues now have finite capacity. Normal producer
  blocking is intentional Stop-aware backpressure; queue capacity is independent from browser worker
  count. Image-analysis payload bytes are checked before reads, and oversized files stay in the
  parent path rather than exceeding the batch budget.
- The measured synthetic benchmark is not a universal speed guarantee. Small batches remain on the
  parent CPU path because process startup cost can exceed the decode/signature work. Network,
  browser, Safari, Parquet serialization/commit, backup, and Agent provider stages were not moved
  to GPU because this rollout did not produce evidence that GPU execution would help them.

## Edge Gemini and Grok Agent parity hardening on 28 Aug 2026

- Controller action parsing now rejects duplicate JSON keys, non-finite constants, malformed
  structured blocks, and distinct same-name actions instead of applying last-value or last-candidate
  semantics. Exact duplicate actions remain safe to de-duplicate. Stop is rechecked inside terminal
  invalid-response handling so cancellation cannot be replaced by a retry-exhaustion error. Final
  publication atomically claims the same Stop signal: an accepted Stop wins, and a later Stop is
  rejected once finalization has linearized.
- The verification allowlist now supports focused top-level project `unittest` files and non-linked
  Python scripts named for `check`, `lint`, `test`, or `verify`. This covers standard-library
  playground gates without allowing arbitrary Python entrypoints or installed unittest modules.
  Approved modules start through isolated Python and load before the workspace is added to the
  import path, blocking same-name modules and `sitecustomize` from replacing the approved runner;
  `unittest` targets are existing top-level files with import-safe names.
- Repeated malformed provider output no longer aborts on the first duplicate. The controller uses
  all three strict-format retries with escalating, non-identical instructions, never executes the
  malformed response, and offers only a read-only `list` action on the final correction when the
  model cannot choose its next step. Strict parsing and the bounded terminal failure remain intact.
- Gemini now fails closed on an exact `Gemini 3.1 Pro` selection, and Grok fails closed on an
  exact `Build` selection with a `Build Beta` trigger readback. The legacy persisted `grok-auto`
  and `grok-heavy` keys migrate to `grok-build`; neither `Auto` nor the unavailable paid `Heavy`
  mode is accepted as proof that Grok is using the agentic Build mode available to this account.
- Grok model verification uses trusted clicks on the exact current Radix trigger and its controlled
  menu. The exact `Build` radio must prove selection through both `aria-checked="true"` and
  `data-state="checked"` after a reopen, and the closed trigger must read `Build Beta`. Nested menus,
  duplicate candidates, upgrade dialogs, disabled controls, and unknown overlays fail closed. The
  controller may dismiss only the exact `Meet Grok Bot` and `Introducing Build Mode` onboarding
  dialogs through one enabled exact `Dismiss` control; it never force-clicks through an overlay.
  The control may hydrate for about 30 seconds, may expose `aria-controls` only while open, and may
  require a trusted `Escape` keypress when Radix intercepts the closing trigger click. Every closure
  path still requires the controlled menu to be absent or hidden.
- Gemini's anonymous shell now exposes a usable-looking composer and conversation-shaped links.
  A visible exact sign-in action without a visible Google Account control is still treated as
  signed out, and an exact `Sign in for all models` menu barrier prevents the controller from
  clicking `3.1 Pro`. Both checks run before context attachment or prompt submission.
- All Chromium providers now capture URL, visible user and assistant order, raw fenced controller
  text, generation state, and composer state in one DOM snapshot. Gemini and Grok therefore reject
  assistant content that still appears before the latest user turn and never treat a missing
  composer as proof that a prompt was accepted.
- Every Gemini and Grok controller turn now carries a unique receipt. Submission and response reads
  require that receipt in the current latest user turn, fail closed if another user turn supersedes
  it, and treat a navigation-time Send exception as an uncertain commit that must never be retried.
  Before Send, exactly one semantic chat composer must preserve the full prompt; a challenge reload
  may safely refill it, while feedback, search, dialog, menu, navigation, and header textareas are
  excluded. Grok Build's `Ask Grok anything` ProseMirror composer is read from its direct paragraph
  structure so blank lines remain canonical. Non-empty text beside those paragraphs, unsupported
  atomic nodes, or any other extra content makes the proof ambiguous and prevents Send.
- Gemini CAPTCHA and Grok Cloudflare or human-verification pages pause the same in-flight browser
  turn. The same isolated Edge or Chrome clone is surfaced for the user, restored afterward, and
  the run continues only after the challenge clears and Resume is selected. Conversation text that
  merely mentions verification does not pause while the normal composer remains usable.
- Grok's authenticated browser fetches use a 30-second `AbortController` timeout before entering
  the existing bounded retry path, so an unresponsive provider request cannot block Stop forever.
- Gemini `New session in project` remains a receipt-isolated task on one selected Notebook route.
  The route does not prove that Gemini created a distinct provider-side subconversation, so the UI
  must not present that mode as equivalent to a ChatGPT Project subconversation.

## Four-provider Agent pre-transfer hardening on 27 Aug 2026

- ChatGPT, Gemini, Grok, and Claude now all fail closed when the configured remote model cannot be
  read back. The failure occurs before context attachment and prompt submission, and the diagnostic
  explicitly states that no project data was sent. Regression coverage proves zero attachment and
  zero submission for ChatGPT `GPT-5.6 Sol`, Gemini `Gemini 3.1 Pro`, Grok `Auto`, and Claude
  `Auto` failures. Claude accepts only the literal `Auto` label; `Default` and the brand name
  `Claude` are not model readbacks.
- An `Auto` model readback now requires both popup behavior and explicit model, mode, or
  provider-model metadata. An unrelated popup button whose visible text is merely `Auto` cannot
  satisfy Grok or Claude model verification; English metadata uses token boundaries so strings such
  as `modern`, `breakfast`, and `octopus` cannot impersonate `mode`, `fast`, and `opus`. The real
  Grok `Model select` trigger remains accepted.
- Gemini readiness distinguishes account authentication from provider availability. If the signed-in
  page reports that Gemini is unavailable in the selected browser's current region, both the direct
  readiness helper and the shared browser-status API fail closed, Ask remains disabled, and no
  project context is transferred. English, Simplified Chinese, and Traditional Chinese landing-page
  copy are recognized. The Agent rechecks this terminal state while waiting for the composer and after
  a missing-model-control wait, closing the skeleton-composer race without treating conversation text
  as a region failure.
- Gemini's current menu renders `3.1 Pro` with an `Advanced reasoning` subtitle and shortens the
  closed trigger to `Pro`. Selection therefore resolves only the exact controlled menu, matches the
  visible primary `.label` to the sole UI value `3.1 Pro` rather than a brand-prefixed alias, observes
  the menu close, and then supports the provider's normal
  unmount/remount cycle by resolving the same controlled ID again. The trigger itself may also be
  replaced; before each click or readback, exactly one live trigger must retain the original
  `aria-controls` value. Readback requires both the `selected` class and a visible `Selected` marker
  before the menu is closed and confirmed hidden. A bare `Pro` trigger, duplicate trigger, subtitle
  or wrapper match, hidden proof, nested popup, unselected option, or unconfirmed close remains
  fail-closed.
- Gemini may render a usable composer several seconds before its model control finishes hydrating.
  The Chromium controller now selects the matching provider tab first and retries only a missing
  model control for about 15 seconds in Stop-aware 250 ms slices. A fully loaded page with unrelated
  visible buttons no longer causes a premature failure. All ambiguous or invalid control states,
  readback mismatches, and closure failures still fail immediately before data transfer. Persisted
  missing-control diagnostics contain only bounded state and element counts plus fixed menu-role
  values; remote page titles and arbitrary visible button text are excluded.
- Gemini Notebook discovery rejects the provider's `create` and `new` route aliases while reading
  the live DOM and at the source-catalog API boundary. Even a fresh in-memory or Parquet cache hit is
  revalidated before response, so those actions cannot be relabeled as Projects or normalized to
  invalid `/app/create` and `/app/new` targets.
- Non-ChatGPT model selection runs through the same linearized Stop gate as other browser side
  effects. A Stop already accepted prevents DOM inspection or clicks; a concurrent Stop is ordered
  against the bounded selector operation before it is published. Failed readback diagnostics retain
  the observed trigger text and no longer imply that an unverified remote model will be used.
- The browser-status cache is terminal only for a fresh positive authenticated result. A fresh
  negative result is displayed while a new probe runs, and an explicit refresh always bypasses the
  cache. Chromium coverage starts with a fresh signed-out Gemini cache row, proves an immediate
  authenticated retry enables Ask, and proves a subsequent forced refresh starts another request.
- The 27 Aug 2026 host Edge validation first reproduced `ERR_CONNECTION_TIMED_OUT`, then reached
  Gemini's Simplified Chinese current-region-unavailable landing page after refresh. The exact
  connection-timeout marker is now included in the shared bounded navigation retry contract. The
  provider-region state remains an external operating constraint: the controller must keep Ask
  disabled and must not attach project context or submit a prompt until that same Edge session can
  access Gemini normally.

## Grok Agent session binding and readiness hardening on 26 Aug 2026

- Fresh Grok runs treat only the normal home-to-`/c/<id>` transition as a candidate new session. The
  latest visible user message must also echo that run's high-entropy transfer ID, and the receipt URL
  must survive an immediate canonical recheck before the controller binds it. Project sessions
  preserve both Project ID and `chat` identity; root chats, cross-Project chats, same-Project old-chat
  switches, composer-only markers, and receipt URL drift fail closed before any local action.
- Before a fresh root or Project submit, Grok's complete same-scope conversation catalog is captured
  as a denylist. Invalid rows, incomplete schemas, repeated cursors, and pagination overflow fail
  closed; a pre-existing conversation cannot become the run's new session even if it later echoes
  the current transfer ID. Fresh runs do not upload context before this binding completes.
- Grok Agent readiness uses the signed-in home-page message composer and collects the initial source
  catalog in the same Edge or Chrome context. It additionally requires an authenticated Grok
  conversations request, and visible login or account-creation actions fail even when an anonymous
  composer is present. The cache-oriented `/files` probe remains separate.
- Grok `Auto` matching rejects compound controls such as `Auto-play`, requires a semantic model
  trigger, and limits choices to its opened menu, listbox, or dialog. Finding or clicking a menu item
  is not a successful readback; the selector trigger must expose the chosen label after the click.
- A fresh signed bootstrap supersedes stale cached source data or errors before loaded/loading guards
  execute and invalidates the older request. Grok Project fallback rows must use the selected
  Project's own `?chat=<id>` URL. Gemini Notebook aliases converge on one typed `/app/<id>` identity
  and remain receipt-gated for a Project-new run. Existing Gemini Notebook session ownership cannot
  be proven, so the Project-session catalog is empty and execution accepts only New session in project.
- The browser-status controller ignores both success and failure from a request whose browser or
  provider is no longer selected. A delayed ChatGPT failure therefore cannot overwrite a newer Grok
  ready state in the same Edge selector.
- Completed Agent snapshots are displayed only when both their provider and browser match the
  canonical route. A mismatch is rendered as an idle, empty task on the server's first frame and in
  later polling updates; provider or browser changes also clear the composer. This prevents an old
  ChatGPT prompt, activity log, or response from being relabeled or resubmitted as a Grok task.
- Send checks the official host and exact selected target in the same page evaluation that clicks
  the button. Fresh unbound Grok runs disable the non-atomic Enter fallback; reused or bound Grok
  sessions check Stop immediately after the DOM send-button scan. A Stop
  accepted during that scan therefore returns without sending the next observation.
- After the final 8666 restart, both the isolated Edge production probe and a fresh host Edge tab
  remained at Grok's Cloudflare `Verify you are human` security page. The controller correctly kept
  Ask disabled and transferred no project context. The operator must complete the provider-required
  browser verification; this project does not automate or bypass that external security boundary.

## Agent Web execution safety contract established on 24 Aug 2026

- Every provider now fails closed before project-data transfer: the visible compatible model
  control must read back the configured model before the controller attaches context or sends a
  prompt. A missing or ambiguous selector stops the run instead of retaining an unverified remote
  model.
- A Chromium attachment is accepted only when the composer visibly exposes the exact context
  filename. A missing filename or reported upload failure falls back to bounded on-demand reads
  instead of claiming that the package was attached.
- Controller discovery now excludes recognized credential, environment, cookie, and private-key
  paths. `search` is literal-only in both the bounded fixed-string `rg` path and its project-confined
  Python fallback. `run` rejects direct `rg`, network or out-of-project targets,
  and mutating or unbounded flags; a bounded project fingerprint also detects command-side writes,
  fails that verification, and makes the prior bodycheck stale.
- Agent-scoped `/api/browser-session` requests now pass through the same network, Host, Origin, and
  password gate as the rest of the control plane. Admitted responses carry `no-store`, `Pragma`,
  and expired `Expires` headers.
- The service atomically persists only bounded run metadata through a same-directory unique temporary
  file. POSIX runtime directories and snapshots use `0700` and `0600`; Windows additionally depends
  on the configured application-data directory's inherited ACL. Prompt bodies, responses,
  conversation history, source text, and error stacks are not stored in that snapshot. Task context
  removal, including new-session contexts, is attempted on every exit path. Startup also removes
  unreferenced app-owned timestamp contexts, rejects linked or junction-backed runtime ancestors,
  metadata, run directories, hard-linked context files, and FIFO/device/socket persistence inputs,
  and blocks the next task on any cleanup-boundary failure rather than following or reading them.
- A synchronous worker-thread launch failure is committed as `failed` with `running=false` before
  the start error returns. Stop therefore remains unavailable for a worker that never existed, and
  the next valid task is not blocked by a stale `starting` or `stopping` snapshot.
- The macOS idle-sleep assertion is bound to the service PID through `caffeinate -w`. Worker
  completion and service shutdown atomically claim one shared assertion, the shutdown hook releases
  an unclaimed assertion after its bounded worker wait, and a registration arriving after shutdown
  immediately attempts to release it. Assertion startup and registration exceptions now enter the
  same completion path instead of leaving a stale `running=true` snapshot. Closing the lid, choosing
  Sleep, or ending the service can still interrupt the Web task, but the assertion cannot outlive the
  service PID.
- macOS and Windows prompts share the same complete 11-action JSON schema. Prompt migrations preserve
  custom guidance and use an owner-only, `fsync`-backed atomic settings replacement so a failed write
  cannot truncate the previous configuration.
- Stop is checked during Safari composer/send polling and Chromium navigation retries. The exception
  completion barrier reads Stop under the lifecycle lock, so an accepted Stop remains `stopped` and
  clears stale error or handoff state even when a synchronous browser error returns afterward.
- Structured console and JSON-line logging redact recognized browser credentials across messages,
  fields, exceptions, and stack traces. Active and rotated log files use mode `0600`. Chromium
  context cleanup ignores only known already-closed or driver-disconnected second-close errors,
  still removes the cloned profile, and propagates unexpected close failures.

## Agentic traditional Edge handoff is explicit on 31 Aug 2026

- A normal ChatGPT browser window cannot perform project-confined local file actions, so it is not
  treated as a successful Agent replacement. When an Edge and ChatGPT controller run fails after the
  exact conversation URL is known, the service keeps the Agent phase failed and marks local edits and
  bodycheck unfinished; it does not open a second traditional Edge window automatically.
- The response toolbar exposes `Continue in Edge`. Opening targets the browser selected for the task
  instead of the system default and occurs only after the user chooses that action.

## ChatGPT Agent rendered-action and history retry recovery on 19 Aug 2026

- The latest failed ChatGPT Agent run returned action strings containing quotes, backslashes, and CSS comment
  delimiters. Reading the bare JSON through rendered Markdown removed significant characters, after which four
  format attempts failed. Agent actions now use fenced `json` transport and prefer the literal code-block text.
- The selected-session history route also observed a transient TLS disconnect while reading `/api/auth/session`.
  Session authorization now reuses the bounded ChatGPT API retry path instead of surfacing that first disconnect
  as an HTTP 500 response.

## Agent action recovery and response typography on 19 Aug 2026

- A prompt that asks the Agent response node to use the sibling `--font-size-5` token now applies
  `font-size: var(--font-size-5)` to `.agent-response-answer-content`, updates the CSS cache-buster,
  and has a focused style contract. If a provider repeats multiple complete candidates with the same
  controller action, the local loop keeps the final candidate instead of exhausting format retries.
- Candidates with different action names remain rejected so an ambiguous provider response cannot perform
  an unintended operation.

## Agentic provider audit and Grok Auto follow-up limitation on 19 Aug 2026

- The canonical Agent URL now preserves the selected browser and Web provider, such as
  `/agent/edge/chatgpt`; Edge tasks use an isolated offscreen, minimized clone and restore the
  persisted default to Edge + ChatGPT after provider-specific audits.
- Switching the Agent provider now resets a stale previous-provider target URL before official-host
  validation. Grok's live textarea and Submit control also have a bounded Enter fallback for a
  briefly absent follow-up button.
- Real named `08.19 Agentic` Edge tasks completed ChatGPT and Gemini read-only audits with
  `bodycheck`. Grok opened the signed-in conversation and completed its first read action, but
  Grok Auto stayed in a long second-turn thinking state in two bounded audits; the task was
  stopped safely and must not be presented as a complete Grok audit.

## Touch-safe iPad sidebar contract established on 12 Aug 2026

- Sidebar overlay behavior now extends through `900 px`, while compact content remains limited to
  `600 px`. iPad portrait layouts therefore use the overlay interaction without inheriting the
  phone content layout.
- CSS and JavaScript consume one semantic breakpoint registry. A new overlay session starts with
  the sidebar closed, while an explicit `sessionStorage` choice remains stable across viewport
  transitions.
- The overlay sidebar and 44 × 44 CSS px toggle use safe-area-aware fixed geometry. The toggle is
  above the transparent backdrop, fixed notices, sidebar title, and dock; closed sidebar and
  backdrop states cannot receive pointer input.
- `[hidden] { display: none !important; }` is now a global contract. A hidden backdrop cannot be
  reactivated by a later responsive `display` declaration and therefore leaves layout, paint, and
  hit testing together.
- The toggle is rendered as a direct child of the page rather than inside the app shell. This
  keeps its touch layer independent from Safari's fixed-position stacking behavior while the
  backdrop remains inside the shell beside the sidebar.
- The quality gate now runs a seeded local Chromium E2E suite in a disposable browser context. It
  validates target phone, iPad, and desktop viewports without reading an authenticated profile.

## Current operating constraints

- X, Grok, and ChatGPT acquisition depends on already authenticated host browser sessions. Their
  remote pages, APIs, and anti-automation behavior can change independently of this project.
- The application deliberately binds to the LAN. Cache and Local resources routes do not have a
  login layer, while the Agent control plane requires its six-digit password for private-network
  requests. The application remains suitable only for trusted local networks and must not be
  publicly exposed.
- X media acquisition relies on yt-dlp's browser-cookie integration. A browser, cookie-store, or
  yt-dlp compatibility change can block downloads even when the local web console remains healthy.
- Grok and ChatGPT caches retain local catalog and recovery state. A source-specific reset removes
  that state and cached media, so it is intentionally an explicit operator action.
- Browser deletion is recoverable only while its retained preview exists in `.browser-trash/`.
  Removing that preview outside the application prevents restoration.
- The default quality gate remains local-only and does not use authenticated browser state. Its
  sidebar E2E layer requires Playwright-managed Chromium, Chrome, or Edge to be installed.

## Quality and isolation foundation established on 9 Aug 2026

- Added a project documentation set covering architecture, testing, operations, and active
  constraints.
- Added one supported setup, launch, test, and quality-gate workflow under `scripts/`.
- Added GitHub Actions that calls the same quality gate used locally and preserves coverage output.
- Redirected pytest's default cache, log, settings, and browser-home paths into a temporary runtime
  before application modules load.
- Replaced the Grok snapshot helper's import-time path default with call-time resolution so test
  redirection cannot be bypassed by a frozen default argument.

Future behavior changes should add a concise dated entry here when they alter an operating
constraint, data-safety guarantee, or externally visible recovery contract.
