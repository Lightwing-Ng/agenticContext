# Known operating constraints and behavior-change history

Documentation version: `v1.26.3-codex.0`

## Tunnel host parity for Windows and macOS on 24 Sep 2026

- Orphaned `tunnel-client` recovery previously ran only on POSIX. It now runs on Windows too, and
  on both hosts it stops only a `run` process whose pid-file and health-file arguments belong to
  this state folder, after re-verifying the process start time. Windows confirms ownership while
  holding a process handle, which prevents PID reuse from redirecting the termination.
- Without proxy environment variables, Windows now passes the manual Internet Options proxy to the
  child, matching the macOS System Settings behavior. PAC, WPAD, SOCKS-only, and WinHTTP proxies
  remain unsupported on both hosts.
- `tunnel-credentials.json` and the per-start bearer file now carry a verified protected
  owner-only DACL on Windows, equivalent to mode `0600` on macOS. Legacy files are narrowed on the
  next read. Other local stores, including the Gemini Tunnel record, still rely on the inherited
  Windows ACL described under Windows host operating constraints.

## Windows controller delete fallback on 23 Sep 2026

- The controller `delete` action failed closed on Windows because anchored POSIX directory
  descriptors are unavailable there. Windows delete now uses the guarded path-based fallback held by
  `_windows_workspace_mutation_guard`, with the same read-receipt and quarantine-restoration
  contract as `replace`: the supplied SHA-256, read receipt, and workspace generation are checked
  before any mutation, and the file is renamed to a `.agent-delete-` tombstone and re-verified
  before its unlink commits.
- A transient Windows access-denied error on the quarantine rename or tombstone unlink is retried
  briefly. A failure after quarantine restores the original name; when that name is occupied or
  restoration fails, the tombstone is preserved and its path is reported.
- The existing capability registry advertisement of `delete` on every host is now backed by a
  working Windows implementation. Hosts that are neither anchored POSIX nor Windows still fail
  closed.

## macOS ChatGPT Agent Cloudflare loop on 17 Sep 2026

- Cache ChatGPT at `/cache/chatgpt` succeeded by cloning the daily signed-in Edge profile.
  Agent ChatGPT at `/agent/edge/chatgpt` looped on Cloudflare while it used the empty project
  debug Edge under `local_store/agent_browser_profile/edge`. The durable lesson is
  [`CHATGPT_AGENT_CLOUDFLARE.md`](CHATGPT_AGENT_CLOUDFLARE.md).
- 18 Sep 2026: the daily-clone detour is reverted. A daily clone launched as native Edge hit
  Cloudflare with nothing attached, and Playwright-launched clones fail Project Send. macOS Edge
  Agent probes, login, and tasks again use the project debug Edge, like Windows. Login now opens
  it over HTTP only, so sign in there once. Do not `connect_over_cdp` onto an `auth.openai.com`
  challenge, and do not `open -n` a second Edge against that occupied profile.
- 18 Sep 2026, Safari ChatGPT Agent: model selection failed with `model-state-transition-unverified`
  and `view: closed`. ChatGPT's Radix model menu closes when Safari loses focus, and each trusted
  Return restored the previous app. ChatGPT model selection now holds one focus transaction.
- The project debug Edge profile also failed Cloudflare on `auth.openai.com` with nothing attached,
  while fresh profiles passed. That profile's stored Cloudflare state was suspect; it was not reset.
- Windows Agent ChatGPT remains on the project debug Chrome or Edge. A live debug Chrome session
  was verified by reading `/api/auth/session` from the ChatGPT tab with
  [`scripts/tmp_probe_chrome_tab.py`](../scripts/tmp_probe_chrome_tab.py).
- 25 Sep 2026: macOS ChatGPT Edge Agent now reuses Cache's daily signed-in Edge profile for
  account checks, source and history reads, and tasks, without falling back to the project debug
  browser or requiring a second login. A read-only Project navigation on 24 Sep 2026
  remained on a verification page without a composer. A second isolated check on 25 Sep 2026
  also found no usable composer after 30 seconds, although its URL and title did not identify
  a verification challenge; that check did not establish the cause. Project Send remains
  blocked on this host. Other macOS Edge Agent providers and Edge Jury retain their project
  debug profiles; Windows Agent continues to use its project debug browser.
- The current recovery copy still assumes a persistent verification window. A macOS ChatGPT
  Edge Agent check closes its temporary clone when it returns, so opening daily Edge cannot
  complete that clone's challenge. A cached `human_verification` status also remains until a
  manual Recheck, even after its normal 30-minute cache limit. These UI cases need a scoped
  recovery-state change without automatically reopening a challenge.

## Jury structured-vote normalization and round presentation on 16 Sep 2026

- Safari can expose an otherwise valid provider code block as a standalone `JSON` language-label
  line followed by the complete object. Jury now accepts only that exact renderer shape in addition
  to raw or fenced JSON; arbitrary prose remains invalid and can never imply agreement.
- Repeated invalid structured votes no longer become `evidence_stalled`. They stop with the explicit
  `structured_vote_stalled` reason and preserve every raw response without inferring a factual
  disagreement. Existing archives are not rewritten into retrospective consensus.
- Safari displays Claude as unavailable and unchecked rather than removing it from the provider
  catalog. Safari still rejects Claude execution; use Edge or Chrome when Claude should serve as a
  juror.
- Round summaries now use the Worthward Collapse geometry and trailing chevron, with the Agent
  active-session count circle providing the round ordinal.

## Jury failed-session cleanup and Recent sessions effects on 16 Sep 2026

- Failed Jury sessions expose a trailing delete action only after all provider and Safari cleanup
  owners have exited. Inconclusive, stopped, running, cleanup-pending, and still-finalizing records
  remain preserved and cannot be deleted through that action.
- The bounded Recent sessions scrollport now reserves internal space for the standard pill shadows,
  while its disclosure body remains an overflow-visible effect host. Long histories still scroll
  locally instead of expanding through the fixed sidebar Dock.

## Safari Jury single-window tabs on 16 Sep 2026

- Safari Jury now runs ChatGPT, Grok, and Gemini in one task-owned Safari window with one tab per
  selected juror. Additional SOTA selections add tabs, not windows. After each window-affecting
  step the controller restores the previous frontmost application so Stage Manager can keep that
  window off the user's current stage.
- Gemini model proof uses the same trusted native-input lease as ChatGPT and Grok. A Stage Manager
  animation can delay document focus; native activation now waits for that focus instead of failing
  closed on the first unfocused attempt. Safari Jury does not admit Claude; Claude remains optional
  only when Jury uses Edge or Chrome.
- A timed-out Safari window creation remains fail-closed for a persisted settle interval and two
  stable inventories. During that interval a new Safari task reports cleanup pending instead of
  risking a delayed second window. Exact-window cleanup uses Safari's own close command and never
  clicks the user's current front Safari window.

## Safari Grok Agent model-tier selection on 15 Sep 2026

- The Agent model catalog exposes both current Grok tiers, `Build` and `Auto`, on Safari as well as
  Edge and Chrome. `Build` remains the strongest default, while an explicit `Auto` choice persists
  through the versioned Agent preference contract and page reload.
- Grok execution continues to fail closed unless the remote selector reads back the exact configured
  tier. Restoring the selectable `Auto` tier does not weaken the trusted-trigger, controlled-menu,
  checked-state, closed-trigger, Stop, or pre-transfer gates.

## Windows host operating constraints

- The project does not currently inhibit Windows idle sleep while compute or Agent tasks are
  running. Configure Windows power settings when a long-running task must not be interrupted by
  system sleep.
- The Windows path does not currently apply an OS-level network-denying sandbox equivalent to the
  macOS `sandbox-exec` profile. Compute workers run with the current user's permissions.
  Process-tree cleanup uses `taskkill /T /F` where applicable, while the verified allowlist remains
  an application-level control rather than an OS sandbox boundary.
- Safari is macOS-only and is normalized away on Windows (`normalize_host_browser` in
  `app/core/config.py`); Agent sessions on Windows require Edge or Chrome.
- Windows file permissions use inherited ACLs rather than the explicit POSIX `0700`/`0600` mode
  bits applied on macOS. Local stores inherit the containing directory's ACL, except the OpenAI
  Tunnel credential and bearer files, which carry a verified owner-only DACL.
- Prefer the `py -3` launcher on Windows; the resolver also accepts a `python` command that
  resolves to Python 3.13 or newer. Do not assume `python3` exists. `AGENTIC_CONTEXT_PYTHON`
  overrides the resolver on both platforms.

## Safari Agent execution and source-only support on 14 Sep 2026

- macOS Safari now supports Grok Agent routes, persisted browser/provider/model preferences,
  strict composer and authenticated-conversations readiness, exact Build model readback, and
  receipt-bearing turns. It uses page-local authenticated requests without exporting cookies or
  cloning an Edge profile, so choosing Safari does not enter Edge's credential-storage path.
- Safari Gemini and Claude remain valid source-only Agent routes: they may check the account and
  browse Recent sessions and Projects, and the selection persists without an Edge fallback. Full
  Agent execution stays disabled; choose Edge or Chrome to start a Gemini or Claude task. Safari
  Jury is a separate runtime: ChatGPT, Grok, and Gemini jurors can submit prompts from one owned
  Safari window, while Claude is available only to an Edge- or Chrome-backed Jury.
- Safari automation still serializes owned windows across local processes and closes only the
  task-owned window at completion. A leftover task window from a dead or same-process owner is
  closed when possible, otherwise reused for the next Recheck; a live foreign owner still blocks.
  Daily Safari is never closed to recover that lease. While a Safari Agent owns that serialized
  context, account, source, Project-session, and history probes serve cached state or fail busy
  instead of waiting on the same lock. Admission is bidirectional: a Safari Cache task also blocks
  a new Safari Agent, and an active Safari Agent blocks a new Safari Cache task.
- Safari activates one exact marked control with one trusted native Return only after
  official-origin, window, tab, exact-URL, document/element focus, center hit-test, and
  no-sheet/no-dialog checks. A post-input transport or receipt failure is uncertain: callers use
  readback only and never retry the native key. Grok selection still requires exact checked-state
  and closed-trigger proof; Stop requires one unique semantic control in the main answer scope.
- Safari same-origin requests reject redirects and revalidate every nonempty response URL. WebKit
  may expose an empty `Response.url` for a same-origin service-worker synthetic response; that empty
  value is accepted only after the request target and redirect state have already passed their
  independent guards.
- macOS lock state or unavailable Accessibility control prevents current live native acceptance.
  Read-only authenticated requests and catalog discovery do not establish that trusted Return was
  accepted by the current provider DOM.
- The preference outbox uses a sessionStorage-scoped client identifier and process-local revision
  gate. A browser Duplicate Tab can copy both values; concurrent duplicate tabs are therefore not a
  durable cross-tab transaction and may need an explicit selection retry if their writes collide.
  Unit, Flask, and isolated Chromium coverage do not by themselves establish current provider-side
  DOM or native authenticated acceptance.

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
- At the time of this rollout, Gemini failed closed on an exact `Gemini 3.1 Pro` selection, and Grok
  failed closed on an exact `Build` selection with a `Build Beta` trigger readback. The legacy
  persisted `grok-auto` and `grok-heavy` keys migrated to `grok-build`; neither `Auto` nor the
  unavailable paid `Heavy` mode was accepted as proof that Grok was using the agentic Build mode
  available to that account.
  The current two-tier Agent contract is documented in the 15 Sep 2026 entry above.
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
  A provider remount may temporarily expose an older user node after the current receipt was already
  observed. That same-count regression is treated as incomplete hydration rather than a new user turn;
  supersession still requires a later user-row count, and no response can be accepted without the
  current receipt returning to the latest visible turn.
  Before Send, exactly one semantic chat composer must preserve the full prompt; a challenge reload
  may safely refill it, while feedback, search, dialog, menu, navigation, and header textareas are
  excluded. Grok Build's `Ask Grok anything` ProseMirror composer is read from its direct paragraph
  structure so blank lines remain canonical. Non-empty text beside those paragraphs, unsupported
  atomic nodes, or any other extra content makes the proof ambiguous and prevents Send.
- ChatGPT, Gemini, Grok, and Claude CAPTCHA or Cloudflare pages pause the same in-flight browser
  turn. The controller does not click or reload the challenge. The same Edge or Chrome window is
  surfaced for the user, restored afterward, and the run continues only after the challenge clears
  and Resume is selected. The Agent aside Account status uses the same fail-closed reminder so a
  sidebar probe does not keep launching Edge against Cloudflare, including ChatGPT's
  `auth.openai.com` authorize popup. macOS does not start a second project Edge while that
  profile is still occupied. Conversation text that merely mentions verification does not
  pause while the normal composer remains usable.
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
  catalog in the same Edge, Chrome, or supported macOS Safari context. It additionally requires an authenticated Grok
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
- Explicit LAN binding now places every application route behind the same private-network, Host,
  Origin, signed-session, and throttled password gate. Agent-scoped `/api/browser-session`
  responses additionally carry `no-store`, `Pragma`, and expired `Expires` headers.
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
- The application defaults to loopback-only binding. LAN access requires an explicit host override
  and an explicitly configured six-ASCII-digit password; all application routes then require the
  signed unlock session. The application remains suitable only for trusted local networks and must
  not be publicly exposed.
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
