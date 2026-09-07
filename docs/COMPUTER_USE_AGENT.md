# Web Computer Use Agent

Documentation version: `v3.55.8-codex.1`

## Purpose

The Agent workspace is a browser-mediated fallback for times when the local coding-agent token
pool is constrained. It uses an already signed-in Web session for ChatGPT, Gemini, Grok, or Claude, with
Edge or Chrome as the supported background Chromium browsers. ChatGPT also remains available in
Safari for the existing session flows. Edge is the default because its Chromium controller does
not depend on desktop clicks; Chrome uses the same isolated controller.

The default is a new root-level session. Every provider can also join one of the 20 most recent
sessions or start a session in one of the 20 most recent Projects. ChatGPT, Grok, and Claude can
also join one of that Project's 20 most recent sessions. Gemini Notebook session ownership cannot
be proved from its current Web routes, so Gemini Projects fail closed to `New session in project`.
For Gemini, that mode starts a receipt-isolated controller task on the selected Notebook surface;
it does not prove that Gemini created a distinct provider-side subconversation.
The adapter maps ChatGPT Projects, Gemini Notebooks, Grok Projects, and Claude
Projects to the same Project contract, so the Agent UI and execution loop do not expose
provider-specific container names. Claude source discovery reads rendered links only and does not
call a Claude API or extract credentials. A run-scoped `session_title` is preserved as the local session label and included
in the first provider message; the provider may still choose its own remote conversation title. The
selected Web provider supplies reasoning; a bounded local controller
performs project actions and returns compact observations to the same conversation.

The recent-session, Project, and Project-session catalogs use one shared read-through Parquet cache
under `local_store/agent/agent_source_catalog.parquet`. The cache key isolates provider, browser,
catalog kind, and Project URL. Fresh entries are reused from process memory for 15 minutes; the
first process read hydrates that memory from Parquet. After expiry, passive page loads, polling,
and task-completion rendering return the last verified catalog without starting another browser
collector. A cache miss performs one bounded bootstrap check that collects account readiness,
models, efforts, and sources together. There is no separate effort-refresh button. Subsequent task
submission verifies its requested model and effort in the task's own browser context. Concurrent
requests share one browser flight, and response revisions prevent an older response from replacing
the accepted result. The API retains a prior verified catalog with `cache.status: "stale"` when a
new check fails.


On `/agent`, ChatGPT, Grok, and Claude use an agent-scoped bootstrap request: the selected browser
context verifies the actual Web composer and collects Recent sessions and Projects in one launch.
For Edge or Chrome with ChatGPT, that same launch also discovers the rendered model catalog and the chosen model's complete live effort
slider before the user submits a task, so the first-run selector is not limited to a hard-coded
default.
The selector always starts with the local `Highest available` policy. Every exact provider label is
read dynamically from the live ChatGPT slider, never from a plan-specific list. A complete,
matching catalog remains selectable while it retains verifiable browser-session provenance,
including its server, stale, or session cache record; the UI identifies it as the latest verified
result rather than implying that a passive render is a new probe. Initial authorization discovers
these controls automatically without asking the user to launch a separate refresh.
The server-rendered first frame also writes `highest_available` into the hidden effort field until a
matching complete catalog is verified. A persisted provider label is retained as data-only
preference state and restored only when that verified catalog still exposes the label.
When `Recent sessions` is selected, the catalog is rendered directly as a bounded, vertically
scrollable list in the sidebar rather than a second dropdown. A ready Agent status cached without
its bootstrap catalog is treated as incomplete and refreshed once, so the status probe and source
catalog do not launch separate browser processes.
Grok readiness is verified on `https://grok.com/`; it does not depend on the separate `/files`
download surface or account-label scraping. A visible composer is necessary but not sufficient:
the same browser context must also complete one authenticated Grok conversations request, and any
visible login, signup, or account-creation action fails closed. The status payload carries the
catalog directly to the selector and seeds the shared L1 and Parquet L2 cache. A fresh bootstrap
supersedes older in-memory or session-storage catalog state before loaded/loading guards run,
aborting and invalidating any older request. The page therefore does not open a second browser for
the Recent sessions step. Loading sessions inside a selected Project remains a later, separately
keyed operation; an expired Project-session entry may return stale rows while one coalesced quiet
refresh runs because this request is explicitly user-initiated, unlike passive status polling.
Grok's DOM fallback exposes only same-Project `?chat=<id>` sessions.

This route uses no provider developer API, command-line coding-agent runtime, MCP connection, or
third-party agent bridge. Readiness and catalog discovery may call the provider's own authenticated
Web endpoints inside the cloned browser context.
ChatGPT plan limits, file-upload limits, data controls, storage, and retention still apply.

## Canonical navigation

The Agent entrypoint is scoped by the selected browser and Web provider. The canonical form is
`/agent/<browser>/<platform>`, such as `/agent/edge/chatgpt` or `/agent/edge/claude`; `/agent/<browser>/`
is a browser-scoped compatibility alias, and the legacy `/agent` path redirects to
the persisted selection. Changing either selector updates the canonical path without reloading the
page, so a copied URL preserves the intended Edge/ChatGPT selection.

Completed UI state is bound to both the provider and browser recorded by the run. Opening or
switching to another canonical route renders an idle phase with an empty activity list, response,
and composer instead of relabeling an older provider's result. The API keeps the global persisted
snapshot for recovery, but the server-rendered first frame and subsequent client polling both apply
the same provenance check. Changing the provider or browser also clears the current composer so an
older task cannot be submitted accidentally through a different Web session.

## Activity history

Activity is a keyboard-operable disclosure. While a run is active, collapsing it
keeps only the latest event visible and shows the shared green breathing live
marker beside the heading. Polling updates that current event without reopening
history; a new run can open history again. Completed runs hide the current-only
preview, while their history remains available on demand.

Completed events use the local `checkmark.circle.svg` mask with
`--theme-success-strong`; running events reuse `cache-phase-live-marker` in the
same green token. The marker slot centers on the first action-label line, and the
Activity heading shares the Working text rail. The response card owns the shared
`--agent-status-icon-size` and `--agent-status-copy-gap` variables; the Working
indicator center is the horizontal anchor for every activity signal, including the
collapsed heading and current-event preview. Wrapped detail lines start at the
same text rail. Activity has no inline border offset. Opening retains the shared gel
animation; closing uses its motion duration and bouncy easing. Reduced-motion
users bypass closing animation.

Validation on 5 Sep 2026: focused Style/Web checks passed 225 tests and 539
subtests; `tests/test_agent_activity_e2e.py` passed four desktop/narrow motion
cases; five existing running/completion/hydration/stale-run browser cases passed.
Live 8666 verification used a separate tab to preserve the original unsent draft;
the heading aligned within 0.56px of Working and the completed glyph centered
within 0.01px of its label. The user-owned service was not restarted.

## Execution loop

1. Select one local project, a Web provider/model, and an authenticated browser on the canonical
   Agent route.
   Configure the operating system in Settings → Agent; the setting detects the host and selects
   macOS or Windows automatically. If local permissions are needed, explicitly use `Open terminal
   permissions` in Settings → Agent. macOS opens Full Disk Access for the Terminal that starts
   the service; Windows requests PowerShell administrator authorization through UAC. Automatic
   detection never opens an authorization surface.
   The browser status card also reports whether the host Terminal or PowerShell executable is
   available and the selected project currently grants read, write, and directory-entry access.
2. Keep `New session` or choose a recent session/Project and, for a Project, either
   `New session in project` or one of its recent sessions. A completed run refreshes the
   session catalog and keeps the Open conversation link, but it does not switch the page
   off `New session` or write a conversation URL into the next submit. Reuse requires an
   explicit user selection that is present in the latest catalog.
3. Enter a task. The service validates the selected provider's official URL and opens it in the
   selected browser profile. When a request switches away from the persisted provider, the service
   resets a stale previous-provider target URL to the new provider's official home before
   validation.
4. Before attaching project data or submitting a prompt, every provider must expose a compatible
   model control and visibly read back the configured model. ChatGPT resolves `Best available`
   from the rendered catalog, then proves that exact model and a trusted live thinking-effort slider; the controller reads its live ARIA
   range and rendered labels instead of assuming a fixed effort vocabulary. Gemini must prove
   `Gemini 3.1 Pro`, Grok must prove `Build`, and Claude must prove
   `Auto`. A missing,
   changed, localized, or ambiguous selector fails closed without attaching context or sending a
   prompt. Only exact model labels and explicit model or mode selector wrappers are accepted;
   compound controls such as `Auto-play`, subscription labels such as `SuperGrok Build plan`, and
   unrelated popup buttons whose text happens to match a model label are rejected. An `Auto`
   trigger must expose popup semantics plus model, mode, or
   provider-model metadata. English metadata is matched as complete tokens, so unrelated identifiers
   such as `modern-theme`, `breakfast-options`, or `octopus-picker` cannot satisfy `mode`, `fast`,
   or `opus`. The current Grok `Model select` trigger is an accepted explicit model
   wrapper. After a click, the selector itself must read back the chosen model before the run can
   publish `model_verified=true`. Gemini is the narrow exception because its current trigger shortens
   `3.1 Pro` to `Pro`: the controller resolves only the exact `aria-controls` menu, requires a
   visible primary `.label` equal only to the UI label `3.1 Pro` rather than a brand-prefixed alias,
   observes that selection closed the menu, reopens a
   freshly resolved node with the same ID, and requires both the provider's `selected` class and its
   visible `Selected` marker. Both the controlled menu and its trigger may be replaced during normal
   open and close transitions; the controller therefore resolves exactly one live trigger with the
   original `aria-controls` value before every click and readback. Nested popups, duplicate triggers,
   subtitles, wrappers, hidden labels or markers, and the shortened trigger alone never prove the
   configured model. Every exit either confirms that the controlled menu is closed or fails the run.
   ChatGPT may expose the thinking-effort trigger under any subscription-provided label, an
   unlabeled composer pill, or a `model-switcher` control. The controller therefore resolves the
   live menu-semantic trigger by its rendered DOM structure and composer relationship; it does not
   use a plan-specific effort vocabulary. The live ARIA slider may appear inside the open model
   menu or beside the composer. The controller reads `aria-valuemin`, `aria-valuemax`,
   `aria-valuetext` or the rendered label, then visits every exposed position. `Highest available`
   selects the live maximum position rather than a hard-coded name such as `Pro` or `Extra High`.
   After discovery, the Agent selector exposes every
   subscription label and can request any exact label. Both the final integer `aria-valuenow` and
   rendered label must match. Missing controls, fractional or out-of-range values, incomplete
   catalogs, and unavailable requested labels fail closed before context attachment or submission.
   Until a matching, complete catalog with browser-session provenance exists, the selector keeps
   only the `Highest available` policy option. An older complete catalog remains visible as the
   latest verified set for that same browser and provider, but execution always repeats the live
   slider discovery and fails closed if the requested label is no longer present.
   A checked item for the exact resolved model remains the required model proof, even when a
   thinking-effort radio such as `Medium` is also selected in the same menu. `Highest available`
   is a local policy rather than a provider label, but a verified live integer reasoning-effort
   slider is a prerequisite for execution. Missing, ambiguous, unrelated, or incomplete slider
   evidence fails closed before context attachment or prompt submission. The bounded
   `available_efforts`, selected `thinking_effort`, and `effort_catalog_complete` evidence are
   persisted with the run, so a subscription rename or additional tier needs no hard-coded update.
   When a Chromium model trigger exposes an active `aria-controls` surface, a menu slider is
   accepted only from that exact controlled surface; a redraw that changes its identity fails
   closed. A model-looking option in another menu cannot preempt a uniquely trusted composer
   slider. If the trigger cannot prove its controlled surface, only the uniquely bound composer
   slider remains eligible. ChatGPT can report the selected model before React mounts or replaces
   that trusted slider. The controller therefore shares one roughly two-second retry budget across
   only the same bounded semantic slider binding; a still missing, ambiguous, or unreadable
   control remains a pre-transfer failure.
   ChatGPT's combined composer pill can expose the checked model in a separate `Select model`
   view while keeping the live effort slider in its simple view. The controller enters that
   exact controlled view for model readback, then closes and reopens the same trigger to return
   to the effort view before binding the slider. Inert alternate views are excluded from both
   model and effort evidence. If that trusted visible trigger is clicked but ChatGPT's Radix menu
   does not render, the controller closes any partial state and retries the control up to three
   times. A readable menu
   that proves a different model still fails immediately; the retry applies only to an unreadable
   menu or a trigger replaced during the open transition.
   Chromium first reuses the matching official provider tab, without focusing it, before navigation.
   Some provider shells expose a composer before their model picker has hydrated. A missing
   ChatGPT, Gemini, or Claude control is therefore rechecked up to 61 times in 250 ms Stop-aware
   slices, for a maximum wait of about 15 seconds. Grok's trusted selector independently waits up
   to 121 slices,
   or about 30 seconds, for its current Radix control to hydrate. Only `model-control-not-found` is
   eligible for either wait;
   an ambiguous control, invalid surface, unavailable option, failed readback, or unproved menu close
   still fails immediately. The wait changes only when proof is attempted, not what counts as proof.
   Gemini's account and region state is rechecked throughout composer readiness and after a missing
   model-control wait, so a late English, Simplified Chinese, or Traditional Chinese region-unavailable
   landing page is reported as a provider availability failure rather than a model mismatch. Gemini's
   anonymous shell can expose both a composer and conversation-shaped links; a visible exact sign-in
   action without a visible Google Account control therefore remains signed out. Its model menu's
   exact `Sign in for all models` barrier is a second pre-transfer check and is never clicked as if it
   were a model option. Grok uses only the exact visible `#model-select-trigger` whose accessible
   name is `Model select` and whose popup type is `menu`. The closed trigger may omit
   `aria-controls`; after a trusted Playwright click, the controller requires that attribute to
   identify exactly one visible controlled surface. The selected candidate must be one exact `Build` or `Build Beta`
   `menuitemradio` owned directly by that menu. The controller then reopens the menu and requires
   both `aria-checked="true"` and `data-state="checked"`, closes it, and requires the trigger to
   read back `Build Beta`. Nested menus, upgrade dialogs, duplicate controls or options, disabled
   choices, and unknown overlays fail closed. When Radix intercepts the closing trigger click after
   unmounting the menu, the controller may use one trusted `Escape` keypress and still requires a
   closed-surface readback. Only the exact `Meet Grok Bot` and `Introducing Build
   Mode` onboarding dialogs may be dismissed, through one visible enabled exact `Dismiss` button;
   no forced click is used.
   Missing-control hydration diagnostics retain enumerated readiness and element counts, but never
   persist the remote page title or arbitrary visible DOM text.
   Chromium composer readiness is polled in 250 ms slices so Stop can terminate the initial page
   verification before model selection, context attachment, or prompt submission. Its single
   recovery reload waits only for navigation commit and is capped at five seconds. Stop is checked
   again before and throughout eligible reused-session context attachment, after attachment state publication, and before
   prompt submission; Chromium submitters also return before reading or filling a composer when a
   stop is already pending. Grok's bounded Enter fallback is unavailable for an unbound fresh run;
   a reused or already bound session rechecks Stop after its last DOM send-button scan and before
   pressing Enter, so a Stop accepted during that scan cannot submit another controller observation.
   Gemini CAPTCHA and Grok Cloudflare or human-verification interstitials are detected separately
   from conversation text. A visible challenge control always pauses; marker-only detection also
   requires the normal composer to be unavailable, so a prompt or response that mentions a CAPTCHA
   cannot self-trigger the pause. The controller surfaces the same isolated Chromium clone once,
   preserves the outstanding submit, and waits for both the challenge to clear and an explicit
   Resume. Stop remains effective, provider deadlines exclude the paused interval, and the clone's
   prior off-screen or minimized bounds are restored after the pause ends.
   A detected macOS lock screen is also a recoverable interruption. It remains paused without the
   ordinary five-minute browser-interruption deadline, so unlocking the Mac can resume the same
   outstanding turn without resubmitting it. Stop remains effective while the screen is locked.
5. The service builds one owner-readable Markdown context package containing the request,
   repository instruction files, a bounded file index, dirty-worktree status, and project entry
   files. Credential locations, environment files, cookie stores, and private-key formats are
   excluded from the file index and controller access.
6. Fresh root and Project runs do not attach the package before the new provider conversation is
   bound; they stream bounded context through controller reads on demand. Reused sessions may attach
   the package directly when the selected provider exposes a file input.
   The controller treats the upload as accepted only after the composer visibly reads back the
   exact context filename; a populated hidden file input alone is insufficient. If the filename
   never becomes visible or the page reports an upload failure, the run continues without claiming
   an attachment and requests only the bounded files needed for subsequent actions. After a
   confirmed attachment or that on-demand fallback, the controller identifies exactly one visible,
   enabled provider composer outside dialogs, menus, navigation, headers, and feedback surfaces.
   It fills that exact element, reads back the complete prompt, and then clicks only a semantic Send
   control in the same bounded chat scope. Every Gemini, Grok, or Claude turn carries a high-entropy
   receipt that must remain in the latest visible user turn through response attribution. A challenge
   or remount before Send can safely refill the still-uncommitted prompt; once a click or Enter may
   have committed, recovery verifies the receipt and never sends that turn again. In the same browser
   evaluation that clicks Send, the controller first checks the official host and exact selected
   landing or bound conversation identity; a tab switch to an old conversation therefore cannot race
   the final click. Grok Build may replace its textarea with the exact visible `Ask Grok anything`
   contenteditable ProseMirror composer. Direct paragraph children are serialized with blank lines
   and inline breaks preserved; non-empty sibling text and unsupported atomic nodes make the
   readback ambiguous and block Send. Grok accepts its known `chat-submit` control or a semantic
   chat composer and bounded Send pair; if that control is briefly absent after a follow-up observation, the controller
   falls back to pressing Enter and still verifies the per-turn receipt.
   For every fresh root or Project run, the first prompt starts with a high-entropy transfer ID so a
   truncated ChatGPT bubble still exposes it. The controller binds the new conversation only when a
   user turn outside the composer echoes that ID, including collapsed `textContent`, and the URL
   atomically observed with the receipt still matches a second canonical URL read. This proves where
   the transfer landed. ChatGPT's fresh-URL proof wait is 30 seconds because the subscription's
   highest thinking-effort position can delay the `/c/<id>` assignment after Send. Before a fresh
   Grok submit, the
   controller additionally enumerates the complete root or selected-Project
   conversation catalog. Pagination loops, malformed rows, repeated cursors, and incomplete schemas
   fail closed, and a conversation present in that pre-submit baseline cannot be bound even if it
   later displays the current transfer ID. Gemini does not expose an equivalent complete pre-submit
   conversation catalog, so its transfer receipt does not independently prove that the destination
   was absent from the account beforehand. The binding wait is bounded and Stop-aware; no local
   controller action executes before it succeeds. After a fresh ChatGPT conversation is bound, a
   same-tab navigation is given one bounded settle window; the controller may recover only when the
   bound conversation or its transfer receipt is observed, and still rejects an unproved session.
   ChatGPT may first expose a client `WEB:` conversation id and then replace it with a
   server-assigned `/c/<id>` in the same root or Project container. The controller may rebind that
   one conversation when the current transfer receipt is observed on the server URL; a different
   server conversation still fails closed.
   Gemini Notebook routes converge on their typed `/app/<id>` identity and use the same receipt gate even
   when the provider remains on that URL.
7. The selected Web provider returns exactly one JSON action at a time inside a fenced `json` code block so
   rendered Markdown cannot consume action quotes, backslashes, asterisks, or source-code delimiters. The
   controller prefers that code block's literal text and supports `list`, `read`, `search`, `replace`,
   `replace_base64`, `write`, `write_base64`, `delete`, `run`, `bodycheck`, and `final`. Multiple exact
   copies of one action are harmlessly de-duplicated;
   any distinct second candidate is rejected as ambiguous, even when both use the same action name.
   `search` uses project-confined `rg` when available, with structured UTF-8 JSON output, a 2 MiB
   file-size cap, an 8,000-character single-line literal-query cap, and case-insensitive root and
   recursive command-layer exclusions for ignored,
   runtime-internal, and sensitive paths. Ripgrep's external config and symlink following are
   explicitly disabled, an end-of-options marker keeps option-shaped queries literal, and raw
   diagnostics are never returned to the Web provider. A second output filter applies the same
   ignored, sensitive, path, and glob rules as the Python fallback. Structured path fields avoid
   delimiter ambiguity in legal filenames containing colons. Ripgrep searches hidden and normally
   ignored safe project files while symlinks remain unfollowed.
   Native paths, including ripgrep's leading `./`, are normalized to the fallback's workspace-relative
   output form without rewriting legal POSIX backslashes. Explicit file globs match basename,
   workspace-relative paths, and search-root-relative paths, so `path=app` plus `glob=core/*.py`
   stays consistent across engines. The controller accepts the common literal, separator, `*`, `?`,
   and `**` glob subset and rejects negated, brace-expansion, and character-class patterns whose
   ripgrep and Python meanings can diverge. Windows glob matching normalizes separators and case;
   POSIX keeps a literal backslash literal. Ripgrep output is consumed through a bounded queue and the
   process is stopped on Stop, after the global result or raw-event limit, or after 30 seconds;
   individual returned lines are also bounded. If the service cannot launch `rg`, a bounded Python
   literal-text fallback skips ignored, symlinked, sensitive, and oversized files and still
   enforces the requested path, glob, result, file-count, and time limits.
   The configured budget counts local controller actions. When the last action's observation is
   submitted at the boundary, exactly one additional non-mutating `final` response is accepted;
   any non-final response in that bounded closing window interrupts the run without executing it.
8. A malformed non-JSON reply receives up to three strict-format corrections without spending the
   configured controller-action budget. Corrections identify repeated invalid output, require only
   the single next unfinished action, and on the last retry offer one exact read-only `list` action
   as a safe way to resume. Strict JSON parsing rejects duplicate keys, non-finite constants,
   malformed JSON-like containers, fenced or preformatted blocks, and any two distinct action objects, including actions
   with the same name but different arguments. Exact duplicate objects may be de-duplicated. No
   malformed or ambiguous action is executed, and the run still fails after the bounded retry budget.
   Stop is rechecked inside the parser-error path, including immediately before terminal retry
   exhaustion, so cancellation remains authoritative at that boundary. A valid `final` must then
   atomically claim completion against the same linearized Stop signal before rendering or
   publishing; a Stop accepted first wins, while later Stop requests are rejected as already finalizing.
9. After an edit, the controller rejects a final answer until at least one approved verification
   command and `bodycheck` both succeed for the current edit generation. If an
   Edge and ChatGPT run still fails after an exact conversation URL exists, the service preserves the
   failed state without opening traditional Edge. The local page exposes an explicit `Continue in
   Edge` handoff instead of claiming completion. A traditional ChatGPT window can continue the
   conversation, but it cannot perform or verify local file actions through this controller; local
   edits and bodycheck therefore remain unfinished. A legacy persisted turn-limit failure is
   reclassified as an interrupted task after restart so the same bound ChatGPT conversation can be
   continued explicitly when its safety metadata is still valid; the historical failed event remains
   unchanged and no continuation starts automatically.
10. The local page renders the final Markdown and links to the selected Web conversation in the
   browser encoded by the task, rather than the system default browser. When a
   ChatGPT recent session or project session is selected, the page fetches that conversation's
   read-only mapping through the selected signed-in browser and loads its user/assistant history
   into the same response article. The response card keeps one question-and-answer pair per page,
   opens on the newest page, and uses the shared paginator to revisit earlier exchanges. Its
   ellipsis controls open the shared grouped page-range menu, including keyboard navigation and
   Escape-to-close behavior. The question header and Markdown answer each have an independent
   vertical scroll region and reuse the standard expand/collapse control. The composer remains a
   non-shrinking bottom flex item so long responses cannot push it out of view. The sidebar session
   trigger stays on `New session` after a completed run unless the user explicitly
   chooses a catalog session; the Open conversation link still targets the finished
   conversation.

## Durable compute jobs

The controller exposes three actions that are deliberately separate from verification `run`:

- `job_start` accepts an approved entrypoint id, one workspace-relative JSON config path, a stable
  idempotency key, and an optional prior `resume_job_id`.
- `job_status` accepts an optional exact job id and returns bounded metadata, progress, and at most
  4,000 log-tail characters.
- `job_stop` accepts an exact job id and terminates only its identity-verified process group/tree.

These actions are for genetic or mutation search, Bayesian or black-box optimization, and similar
long CPU/GPU workloads. The provider plans the work and inspects progress; it must not participate
in each generation or evaluation. The local optimizer loop remains ordered and fully local.

`job_start` requires `.agenticContext-compute.json` in the selected workspace. One entry must uniquely
match the requested id, name a regular non-linked `.py` file below that workspace, pin its exact
SHA-256, and approve 43,200 through 86,400 seconds. The action accepts neither shell text nor an
argument list. The runtime invokes only the current Python interpreter and the fixed optimizer
protocol. A changed source digest, absolute path, traversal, linked path, non-JSON config, or a
second active job fails closed. An idempotency key reused for the same request returns the existing
job; reuse for different bytes or parameters is rejected.
On macOS, the fixed optimizer command runs through `/usr/bin/sandbox-exec` with all network
operations denied. This converts the action-level ban on download commands into an operating-system
network boundary for the approved worker as well. The Windows path does not currently apply an
equivalent OS-level network-denying sandbox profile.

Each job has an unpredictable 32-hex-character `job_id` and an external task-owned directory. Its
atomic metadata contains state, PID and birth identity, timestamps, approved entrypoint identity,
config identity, exit status, relative checkpoint/result paths, progress summary, and the minimum
resume lineage. It never stores environment variables, browser data, provider transcripts, prompts,
or secrets. The source workspace remains the only source boundary; runtime output is excluded from
verification fingerprints by location rather than by weakening fingerprint rules.

The detached wrapper owns a fixed 12-hour default and 24-hour hard maximum instead of inheriting
`command_timeout_seconds`. A provider turn, browser refresh, Web stream interruption, normal Agent
final, or Web Agent Stop does not terminate it. On macOS, its idle-sleep assertion watches the
worker PID and remains independent of the Web task assertion. Worker completion, dedicated Stop,
and stale terminal reconciliation release the assertion; `caffeinate -w` also self-releases when
the worker exits.

On service construction and every status read, active metadata is reconciled against the stored
birth identity. A live match is rebound. A missing or mismatched identity becomes interrupted and
is never signaled, preventing PID reuse from killing an unrelated process. Stop first rechecks that
identity, then terminates the owned process group. The default active-job limit is one per workspace.

Optimizers should import `write_compute_progress_atomic()` and
`write_optimizer_checkpoint_atomic()` from `app.core.agent.compute_jobs`. Checkpoint schema 1
requires `optimizer_version`, `iteration`, `population` or `optimizer_state`, `rng_state`, `seed`,
`best_objective`, `best_parameters`, and `evaluation_count`. Resume accepts only a validated complete
checkpoint from a terminal job and always creates a new job after an explicit request; it never
automatically spends compute after a restart. Final export belongs in `result.json`. The status UI
shows the latest job state, id, heartbeat, Stop control, and whether explicit resume is available.

An Agent final response remains valid while the job is still running, after the ordinary source-edit
verification and bodycheck gates are current. It should report the job id and current bounded state
instead of waiting hours for completion.

## Capability registry, event chain, and Doctor

The Agent Action protocol, including closed input schemas, controller handler identifiers, prompt
examples, and read-only task rules; page-observation names; WebMCP metadata; and human-page
destinations are registered in `app/core/agent/capability_registry.py`. `WorkspaceController`
rejects any action that is not registered before dispatch, and the WebMCP manifest is generated from
the same registry. The public Site surface still exposes only the bounded discovery, page-context,
and allowlisted-navigation tools; it does not expose Agent execution or recovery routes.

The registry-owned closed schema is an execution boundary, not only prompt or manifest metadata.
Before `WorkspaceController` dispatches a non-final Action, it validates the action constant,
required fields, field types and bounds, and rejects undeclared fields. `final` has no controller
handler, so the Web loop validates it against that same registry before verification and bodycheck
gates, rendering, or publication. A provider message therefore cannot create a more permissive
execution path.

Every run receives a `run-<hex>` identifier and a persisted monotonic `run_revision`. The revision
increments once for each new run and lets a client reject out-of-order snapshots even when two
starts share the same second; it contains no task or provider content. The service persists an
owner-only JSONL event chain at the runtime root under `events/<run_id>.jsonl`. A valid chain starts at `run.started`,
then links each parsed Action to `action.requested`, its bounded controller observation, and the
relevant `verification` or `bodycheck` event before one terminal event. The real loop also emits
registered `agent_status`, `browser_session`, `provider_turn`, `browser_interruption`, and
`agent_response` page observations. Provider/browser state is recorded as bounded page observations,
while Resume, Continue, and context cleanup are recorded as recovery events. Prompt bodies, provider
responses, source text, command text, and page content are removed from event payloads; the
persisted `last-run.json` keeps bounded run metadata documented above alongside chain health and
last-event metadata, while still omitting prompt, response, source, command, and page-content text.
The root event stores workspace identity only as `{device,inode}`. Successful read observations and
delete audit data can add a read receipt's SHA-256 digest, generation, and file identity; delete
also records that digest as `delete_digest`. A browser-session event stores only a SHA-256 conversation identity
derived from the platform and canonical conversation URL. The chain records no absolute workspace
path, raw provider URL, or file/page/provider content; ordinary action summaries may retain a
bounded workspace-relative path.

When a run is paused, interrupted, failed, or leaves temporary context cleanup pending, the Agent
page loads `GET /api/agent/doctor` and opens a Doctor panel with the failed checks, bounded event
timeline, and safe actions. Completed runs with public event metadata retain that timeline for
inspection. Resume continues a paused turn without a duplicate submit. Context cleanup reconciles
only the app-owned temporary bundle. `Continue interrupted task` is narrower: it is enabled only for
an interrupted Edge and ChatGPT run whose persisted metadata proves the original conversation was
bound after its first submission. It revalidates the workspace, operating system, local-permission
state, conversation URL, and effort policy, then sends one fixed generic continuation request in
that conversation without reconstructing or uploading project context. It never runs automatically
and never reuses an unbound pre-submission URL. Provider handoff and New task remain explicit UI
actions.

## Safety boundary

- Every file action and returned observation resolves below the selected project. `.git` and Agent runtime internals are
  inaccessible. Environment files, credential stores, cookies, and private keys are excluded from
  context indexes and from `list`, `read`, and `search` observations.
- Existing file contents change only through an exact, single-match replacement. New files use an
  explicit write action. `delete` accepts one regular, single-link file only after the same
  controller has returned its current SHA-256 through `read`; any intervening edit invalidates the
  receipt. On supported POSIX hosts, deletion opens every parent below the recorded workspace with
  `O_NOFOLLOW`, takes an advisory directory lock, rechecks the leaf identity, and unlinks the leaf
  through the anchored directory descriptor. The POSIX unlink primitive is name-based rather than
  inode-addressed; uncooperative external renames remain a non-atomic limitation and are detected
  where possible, while hosts without the anchored primitives fail closed. Directories, symlinks,
  hard links, recursive targets, and files larger than 20 MiB are rejected.
- Shell commands are restricted to bounded inspection, build, lint, and test work. Approved PATH
  tools are resolved once to an absolute executable outside the workspace before launch; Python
  verification is pinned to the service's own Python runtime. Approved Python modules are imported
  under isolated interpreter startup before the workspace enters `sys.path`, preventing project
  modules, `sitecustomize`, and environment paths from shadowing the approved tool. Focused
  `unittest` runs may target only existing top-level project test Python files with import-safe
  names. Python may also launch an existing non-linked
  project verification script whose filename begins with `check`, `lint`, `test`, or `verify`;
  other directly named Python scripts remain blocked. A directly executable workspace script must
  be a real, platform-compatible executable below `scripts/`, with no symbolic link, junction, or
  hard-linked regular file in its path.
  `git status` is filtered inspection only and never satisfies the post-edit verification gate. The
  command layer rejects direct `rg` execution in favor of `search`, paths or network targets outside
  the selected project, linked or sensitive path arguments, pytest configuration/package/plugin
  overrides, non-check Ruff modes, TypeScript invocations other than the exact `tsc --noEmit`
  inspection command, mutating or unbounded flags, file-writing redirection, deletion, moving,
  installation, downloads, publishing, environment enumeration, and Git-history mutation.
  A bounded before-and-after content fingerprint covers up to 12,000 files, 12,000 directories,
  512 MiB, and 15 seconds per scan while excluding the documented ignored/runtime directories.
  An incomplete initial fingerprint prevents launch; a changed or incomplete final fingerprint
  fails the run, advances the edit generation, and invalidates both verification and bodycheck.
  Verification output is decoded with replacement for invalid UTF-8, retained to 48,000 characters,
  and drained through a bounded queue. Stop, timeout, stream failure, and normal completion all
  perform a process-group cleanup before the final fingerprint on POSIX.
- This controller is not an operating-system sandbox. Pytest, package scripts, Make targets, and
  approved workspace scripts execute code from the selected repository and can have side effects
  that a path parser or after-the-fact fingerprint cannot prevent outside that repository. Production
  use therefore assumes a trusted local repository and a cooperative Web model. Use an OS sandbox
  when the repository or generated test code is untrusted.
- Stop is honored during the initial Chromium composer gate, ends Web-provider generation, and
  terminates the current local process group. Completion first clears the active process and removes
  the temporary context, then atomically claims the macOS idle-sleep assertion and attempts to
  release it. Only after those cleanup steps does it clear context metadata and persist
  `running=false` as the completion barrier. Worker completion and process shutdown cannot both
  claim the same assertion.
  During service exit, the shutdown hook requests Stop, waits up to eight seconds, and then claims
  any assertion that the worker has not already claimed. A late worker registration after shutdown
  immediately attempts to release its assertion instead of repopulating the shared slot. The
  On macOS, the assertion also runs as `caffeinate -i -w <service PID>`, so termination of the service PID releases
  it even when Python cleanup or the daemon worker cannot finish. A context deletion failure instead
  publishes a failed phase, persists the context path and size as bounded
  recovery metadata, and logs the cleanup error. The next production run retries that exact
  runtime-local cleanup, sweeps every other unreferenced app-owned timestamp context, and remains
  blocked if any cleanup fails. Sleep-assertion release failures cannot prevent the final barrier.
  Unexpected assertion startup or registration failures also pass through the failed completion
  path, persist `running=false`, and do not prevent a later task from starting.
  Local context-package construction, Chromium profile cloning, and browser-context launch are
  synchronous and not fully preemptible; initial navigation can also remain in flight until its
  configured timeout. Stop can wait for those phases. Gates before browser startup, immediately
  after context launch, after navigation, during Chromium navigation retries, and throughout
  Safari composer/send polling avoid later work where possible and still prevent context attachment
  or prompt submission. A synchronous browser error observed after an accepted Stop is published as
  `stopped`, not as a task failure.
- The Flask control routes accept host-loopback traffic directly. Private-network requests must
  first unlock `/agent` with the six-digit password gate; the successful signed session also
  authorizes same-origin `/api/agent/*` requests. Public and host-rebinding requests are rejected.
  The default password is `195135`, and `AGENTIC_CONTEXT_AGENT_PASSWORD` overrides it before launch.
- `/api/browser-session?...&scope=agent` uses that same network, Host, Origin, and password gate.
  Responses produced after admission carry `Cache-Control: no-store`, `Pragma: no-cache`, and an
  expired `Expires` value so Agent account-readiness data is not retained by browser caches.
- Project context and requested source files are transmitted to the selected Web account only
  when a task is sent. Selecting a ChatGPT session reads its existing conversation history for
  display and does not write those remote messages to the local cache.
- The service atomically persists only bounded run metadata, including phase, provider, model
  verification, attachment confirmation, timestamps, conversation target, bodycheck state, and a
  temporary context cleanup path and byte count while a run is active or cleanup recovery is pending. It does not persist prompt
  bodies, responses, conversation history, source text, or error stacks in that snapshot. The
  runtime directory and each task directory are owner-only, and context and snapshot files use mode
  `0600`. Its run identifier, monotonic run revision, event-chain state, event count, last action, last event kind, and
  verification state and confirmed-conversation-binding flag are also bounded metadata. If a persisted run was
  still marked active when the service exited, the next process restores it as `interrupted`
  instead of claiming that it is still running or completed.
- The generated context package is task-scoped and is normally deleted after success, stop, or
  failure, including a task that opened a new Web session. Service startup and the next task also
  delete unreferenced `context.md` files only from app-owned timestamp directories while preserving
  the exact recovery pointer. A deletion failure is visible or logged and blocks the next task until
  the runtime-local file can be removed. Structured log
  formatters redact recognized
  browser credentials from messages, structured fields, exceptions, and stack traces. Active and
  rotated JSON-line logs use owner-only mode `0600`.

The question-and-answer pages are bounded to the latest 100 completed Agent exchanges per Web
conversation and live only for the current local service process. Selected ChatGPT history is
also bounded to the latest 100 paired exchanges and persisted in the shared Agent Parquet
catalog. Revisiting a saved history, including after restart or TTL expiry, does not launch
a background browser refresh. A cache miss collects once; `refresh=1` explicitly refreshes it.
The cache retains its age metadata so a saved view is never presented as a fresh provider read.

## Settings

Settings → Agent stores:

- the default target operating system;
- the context Markdown byte limit;
- the maximum controller-turn count;
- the local command timeout;
- the ChatGPT `Highest available` policy or an exact runtime-discovered effort label;
- separate macOS and Windows system prompts.

Both operating-system defaults use one shared, complete 11-action JSON schema. The prompt also
defines the Web provider as a transport-only reasoning surface, requires one action per turn,
states the hard semantics for paths, receipts, writes, deletes, verification, and read-only tasks,
and repeats a compact turn contract plus the registry action catalog after each controller
observation. User-configured guidance is advisory: it cannot widen controller authority, the
action schema, path boundaries, or verification gates, and model/session claims require controller
observations rather than prompt text. A read-only run may end with `final` because it publishes
only a local summary and does not mutate the workspace. At load time and every Settings write,
prompts missing the
fenced-JSON or base64 transport contract are replaced with the current safe defaults.
Marker-complete prompts are normalized to one current action catalog and one current protocol
section; repeated catalogs, incomplete sections, and placeholder delete digests are not retained.
Former `text or regex` query fields are normalized even when their JSON whitespace differs, and the
authoritative literal-only instruction is added when absent. User-authored guidance outside
generated protocol sections and unrelated settings remain intact. Settings are
written through an owner-only, same-directory temporary file, flushed with `fsync`, and atomically
replaced. A failed write preserves the complete previous file. Successful migrations are immediate
and idempotent across later service starts.

The selected provider's file-upload limit remains authoritative. ChatGPT documents a 512 MB hard
file limit and a 2 million-token limit for text and document files; the application uses the lower
applicable boundary and keeps the local byte ceiling configurable. Gemini, Grok, and Claude may impose
different limits or attachment behavior, so the controller requires a visible exact-filename
readback before claiming an attachment and otherwise falls back to bounded controller observations.

Windows verification commands accept `py -3 -m pytest` and approved project scripts such as
`py -3 check.py`. The controller normalizes the launcher selector before validating module
arguments and runs its own Python runtime; it does not launch a second Python installation.
Module restrictions, plugin/configuration checks, and project path confinement still apply.

Windows uses the same complete action schema and file-action boundary with native Windows paths, a new process group, an
absolute System32 `taskkill /T /F` fallback, and Edge or Chrome Chromium sessions. Explicit Edge and Chrome handoffs
resolve an installed browser executable rather than relying on `PATH`; the default-browser helpers continue to use
Windows URL association. Chromium session probes serialize the complete synchronous Playwright lifecycle, including
context cleanup, so independent Flask worker threads do not enter that lifecycle concurrently. This round's
Windows process-tree contract is covered statically and by mocks, not by a real Windows end-to-end
run. If a Windows group leader exits before its descendants, `taskkill` is best effort rather than
the strict Job Object completion barrier required for hostile child processes. Safari remains
macOS-only. The selected operating system must match the host running the local service.

Edge and Chrome run through an isolated clone of the selected signed-in profile and operate the
selected provider's DOM directly. Passive source checks use a quiet, task-independent context.
ChatGPT source checks use a non-headless context because ChatGPT's Cloudflare challenge rejects
headless clones with HTTP 403. Windows retains the backgrounded/offscreen probe policy; existing
macOS silent probes retain their task-stage window policy and foreground-app restoration.
On macOS and Windows, an executing Edge or Chrome task selects or creates its provider page before
normalizing that page's window in the isolated, non-offscreen context. Windows requests normal
state and fixed bounds `(80, 80, 1280, 900)` through CDP without requesting activation; it does not
run macOS foreground-app capture or restore. Missing CDP window control or a failed window command
stops the task before provider prompt submission. Fixed geometry does not verify the available
display work area, and a cloned context may restore more than one native window. On macOS, the previous
foreground app is restored if the browser took focus, leaving the task window available for the user to
inspect through macOS window management. macOS ultimately determines Stage Manager grouping.
Automated execution never opens the user's original profile for writing. Chromium still suppresses first-run,
crash, notification, and repost prompts; a normal task exit closes the isolated context and removes
its temporary profile. The next Chromium launch removes only abandoned `cachelikes-edge-*` or
`cachelikes-chrome-*` directories older than 24 hours. Safari uses one shared Apple Events context,
restores the previous frontmost application after window operations, and closes every task-owned
window on success, stop, failure, or exception. Safari remains available only for ChatGPT's existing
session flows. Claude requires Edge or Chrome. If Claude renders an
account suspension, ban, deactivation, or other restricted-state message, the readiness card reports
that state and does not attempt a login bypass.

When an Agent browser status is not signed in, the status card exposes an `Open <Browser> to sign in`
action. It opens the selected browser visibly at the provider home page; the user must then choose the
existing recheck action to verify the new session. Windows login and conversation handoff explicitly
select the data root and profile from the same saved configuration used by probes and tasks: Edge
uses `Default`, while Chrome uses its configured profile. The explicit handoff lets the real browser
persist the user's login; automated execution still uses a temporary clone. Copying the profile is
not an atomic snapshot of a running browser, and neither copying nor launching proves that the
provider session is authenticated. Readiness must be checked inside the clone. Opening the browser
does not imply sign-in and does not add automatic login polling. Windows runtime behavior for this
workflow remains not locally verified.

Chromium cleanup treats only the known Playwright already-closed and driver-disconnected close
errors as an idempotent second close, while still removing the temporary profile. Unexpected
context-close failures continue to propagate instead of being hidden. If a task error already
exists, subsequent close and profile-removal failures are logged and attached as exception notes
without replacing it. A profile-removal failure with no earlier error fails the operation and
identifies the retained temporary directory. Failed removals remain excluded from this process's
stale-profile cleanup; this is not durable process-ownership tracking across service restarts.

The traditional Edge handoff is intentionally separate from the isolated Agent context. On a failed
Edge and ChatGPT run with a verified conversation URL, the service records an available handoff but
does not create a normal `Microsoft Edge` window automatically. Clicking the handoff pill is the
user's explicit choice to open the same URL in Edge normally.

The model selector is provider-specific: ChatGPT exposes `Best available` and exact discovered model labels, Gemini exposes `3.1 Pro`, Grok
exposes `Build Beta`, and Claude exposes `Auto`. Each provider is fail-closed: the controller must select or observe
and then visibly read back the exact configured model before any attachment or send. ChatGPT proves that local option
through a checked item matching the resolved model after opening the live menu-semantic trigger. For subscriptions that
expose a thinking-effort slider in the model menu or beside the composer, the controller reads the complete ARIA range at runtime, records every rendered
position, and selects either `Highest available` or one exact observed label without assuming fixed names. The final
integer position and visible label must both verify. A localized or changed menu that cannot prove the model and effort
stops the run without transferring project data.
For an `Auto` readback, a generic popup wrapper is insufficient: the trigger must also identify
itself as a model or mode control, or expose provider-specific model metadata.

Non-Agent browser readiness uses a short session/storage cache only for a positive authenticated
result. A fresh negative result is rendered immediately but also re-probed, so completing login or
a provider security check does not leave Ask blocked for five minutes. Agent-scoped bootstrap uses
the shared AgentSourceCache described above, including its bounded negative-cache and coalesced
refresh behavior. An explicit controller refresh always requests a fresh result. A signed-in Gemini page that states the service is unavailable in the browser's
current region is not an authenticated-ready result: the probe reports the provider condition,
keeps Ask disabled, and transfers no project data. Gemini Notebook discovery rejects provider-owned creation aliases such as
`/notebook/create` and `/notebooks/new`. Every source-catalog API response revalidates cached Project
URLs through the current provider contract, so those aliases cannot appear as selectable Projects
even when an older in-memory or Parquet catalog contains them.

The Agent page keeps its heading on the shared title rail: the title uses the standard top anchor,
while the readiness sentence below carries the current phase as a readable prefix such as
`Idle ·` or `Running ·`. There is no separate phase badge, so the title, global theme action, and
sidebar heading retain one vertical anchor without reserving an extra status-card row.

While an Agent task is running on macOS, the service holds an idle-sleep assertion bound to the
service PID until it has attempted task-scoped context removal during success, stop, or failure
cleanup. Worker completion and service shutdown use an atomic ownership transfer before attempting
to terminate that assertion, while `caffeinate -w` is the final safeguard if the service exits
before cleanup. A failed context removal is published as a recoverable failed state rather than
keeping an orphan worker marked active. On Windows, the controller isolates the active process in a
new process group and uses a cooperative stop before terminating it. The project does not currently
inhibit Windows idle sleep for Agent tasks. On macOS, closing a MacBook lid, choosing Sleep,
restarting, losing network access, or ending the local service can still suspend or interrupt a
task; Windows system sleep can likewise interrupt the task.

## Verification

Default tests use temporary projects, fake browser runners, and isolated settings paths. Browser
E2E fixtures create their own App with explicit settings and runtime roots, plus an opt-in
external-operations boundary. That boundary permits fixture-private preference persistence but
returns a deterministic unavailable browser status and rejects every operation that could probe a
signed-in browser, collect remote sessions or history, open a browser or Terminal authorization
surface, start or resume an Agent worker, or start a ChatGPT download refresh. It is not inferred
from `TESTING`, so production behavior and route-level tests remain unchanged unless the fixture
explicitly requests the boundary. Live
read-only capability probes verified signed-in sessions, composers, send controls, and model-menu
behavior for ChatGPT, Gemini, and Grok in both Edge and Chrome on 14 Aug 2026. Claude's provider
contract is covered by mocked readiness, URL, source, and route checks in this change; a live Claude
probe was not possible because the selected account is currently restricted. The probes did not
send project content. Any live signed-in browser run must be treated as an external data transfer;
confirm the target and data scope before sending a real project task.

The local `demo_flight` controller acceptance treats the named Demo as immutable test input. It
snapshots the SHA-256 digest of every `DEMO_FLIGHT_FILES` member, copies only that allowlist into a
temporary workspace, exercises controller CRUD and cold verification in the copy, and recomputes
every original allowlisted digest before returning. The automated test never modifies the original
Demo project. A live `/agent/edge/chatgpt` task may edit that folder when it is selected as the
current project; `demo_flight/README.md` documents that token-pool fallback, including
`Highest available` live effort discovery rather than a hard-coded provider label.

On 27 Aug 2026, delayed Gemini hydration, Stop interruption, strict model proof, bounded diagnostic
privacy, localized region gating, and transient navigation retry coverage passed 307 controller
tests, 89 complete Chromium E2E tests, 20 Gemini tests, and 70 hardening tests. The complete project
gate passed 994 tests and 370 subtests with 68.56% branch coverage. A live Edge check reproduced
`ERR_CONNECTION_TIMED_OUT`; the shared transient marker now retries that exact error. The refreshed
host tab then returned Gemini's Simplified Chinese current-region-unavailable landing page. The
controller treated that as a terminal provider condition before context attachment or prompt
submission, so the interrupted external task was not represented as completed.

On 26 Aug 2026, 315 focused controller/hardening tests passed with the bundled ripgrep available.
The same suite passed 314 tests with only its real-ripgrep integration test skipped under an explicit
no-`rg` PATH; all mocked ripgrep JSON, Stop, timeout, post-filter, and diagnostic-isolation cases still
executed through a workspace-external trusted fixture. The production-hardening suite covered
recursive search parity, Stop propagation, completion cleanup, explicit session reuse,
verification-gate ordering, canonical executable and argument confinement, hard-link rejection,
strict direct `tsc --noEmit` parsing, shared cross-platform action-schema migration, atomic settings
replacement, unique atomic run-snapshot replacement, Safari submission Stop gates, Chromium retry
Stop gates, linked-path-confined orphaned-context housekeeping, bounded content fingerprints, and
real POSIX leader-exited descendant processes. macOS sleep lifecycle regressions cover service-PID-bound
`caffeinate`, shutdown-first and worker-first ownership races, join timeout takeover, late
registration after shutdown, and assertion startup or registration exceptions followed by a clean
second run. Runtime regressions reject linked or junction-backed
runtime ancestors, timestamp directories, snapshot metadata, hard-linked context files, and
non-regular persisted inputs without changing external content or permissions or blocking startup.
A worker-thread launch failure is also published and persisted as `failed` with `running=false`, so
Stop is not falsely accepted and a later task can start. The complete project gate passed 936 tests
and 366 subtests with 68.40%
overall coverage and branch measurement enabled. A protected runtime inventory also found and then
removed 45 historical orphaned context bundles totaling 1,481,451 bytes; no timestamp context remained.
This verification did not restart the user-owned service or send a new Web-provider task; Windows
received static and mock validation only.

On 19 Aug 2026, the named `08.19 Agentic` Edge tasks completed a 38-turn ChatGPT audit and a
9-turn Gemini audit with `bodycheck`. Grok completed its first read action and the live Submit/Enter
fallback was verified, but Grok Auto remained in a long second-turn thinking state during two
bounded audits; this provider-specific runtime limitation remains observable and is not reported
as a completed full audit.

On 26 Aug 2026, production preparation for a new Grok task added receipt-correlated session binding.
A fresh root run may bind only the `/c/<id>` conversation whose latest user message contains that
run's transfer ID. Project-new runs add the same proof and may bind only inside the selected Project,
while Project-session runs compare the Project ID and `chat` query exactly. Grok freshness now also
requires a complete pre-submit conversation baseline, and Send performs its target check atomically
with the click. Fresh runs transfer no attachment before binding. The pass also added positive
authenticated-API readiness, semantic-menu-scoped and re-read model selection, same-Project catalog
fallback filtering, Gemini Notebook Project-session fail-closed behavior, fresh-bootstrap
reconciliation over stale catalog state, provider-aware stale-request suppression, and strict
provider/browser provenance isolation for completed UI state and composer content. Focused
controller/source/Web regressions passed 399 tests and 342 subtests; all 55 Chromium UI/E2E tests
then passed. The full gate passed as recorded above. After the final 8666 restart, the live Edge
Grok route loaded `computer-use-agent-v3.22.0-codex.1` and rendered `idle` with an empty response
and composer even though the persisted global snapshot belonged to a completed ChatGPT run. Both the
controller's isolated Edge probe and a fresh host Edge tab still showed Grok's Cloudflare
`Verify you are human` security page. The local Agent kept Ask disabled. No CAPTCHA or security
barrier was bypassed, and no project context or prompt was sent; the operator must complete that
verification in Edge before the real project run can begin.

## Capability bootstrap and restored answers

`Best available` prefers the provider-owned `Latest` alias; otherwise it resolves the newest
full-capability GPT version exposed by the selected browser. `Latest` is displayed verbatim and
is not relabeled as a concrete model version without provider proof.
Lightweight variants do not participate in automatic selection. This is an explicit version-based
policy, not a benchmark ranking; there is no hard-coded next-generation model name. Legacy Sol
keys remain readable for saved runs. Concrete choices use bounded `live:` labels and must be
verified again against the provider before a task can attach context or send a prompt.

One bootstrap discovers models, the selected model's complete effort range, and session sources.
An initially unreadable model catalog receives one bounded retry in the same browser context;
other verification failures remain fail-closed and no retry opens another browser.
The highest-effort policy displays the actual final slider label while retaining its policy value.
Effort options belong to that model and browser; choosing another model clears that proof until
task-time verification. Source-list failure does not erase capability evidence. Upgrading from
the old bootstrap cache causes one new probe; reloads reuse the resulting catalog.

Live and restored final envelopes share a safe Markdown renderer, including case-insensitive JSON
fences with optional export metadata such as `id`, CRLF line endings, and fully JSON-encoded export wrappers. Raw response bytes remain
available for copying. Ambiguous, malformed, ordinary JSON, and embedded examples stay unchanged.

Axis verification on 5 Sep 2026: Style/Web checks passed 225 tests and 539
subtests; four responsive motion cases verify icon centers and hanging text
within 0.1px. Live desktop and narrow DOM readbacks measured zero horizontal
difference between Working and the last three activity icons and detail lines,
with no narrow-page horizontal overflow. Stylesheet version: v2.93.5-codex.1.

## Sidebar icon rail and Investment live marker

On 5 Sep 2026, stylesheet v2.93.6-codex.1 scopes the sidebar icon rail to
`#agent_runtime_form`. Provider, browser, source-folder, project, plus, account,
and Terminal icons share the provider's 28px slot and control inset. The status
card reuses the control border token; its 18px glyph stays centered in that slot.
The Dock is outside this form and has no changed selector or markup. Live
before/after DOM checks confirmed identical Dock position and size.

Activity live indicators now match Worthward Investment's 6px glowing core,
20px outer ring, 14px inner ring, 1.8-second period, and 0.9-second inner delay.
The inner ring starts at scale 0.48 and the outer at 0.36; both expand and fade.
The Activity panel allows the halo to escape. Reduced-motion behavior is retained.
The shared Cache marker outside the Agent response card remains unchanged.

Validation: 225 Style/Web tests and 539 subtests passed; four Activity motion
cases and two sidebar/project-icon cases passed at desktop and narrow widths.
Motion tests sample transforms over time, and sidebar tests wait for sidebar
animation to settle before comparing Dock geometry. No live prompts were sent;
original tabs and their drafts were preserved.
