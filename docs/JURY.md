# Browser Jury

Documentation version: `v1.6.1-codex.1`

## Workflow

Agentic retains the existing project controller. Jurors opens `/jury/edge` by default and uses only
authenticated provider websites. There is no project picker, context upload, or Terminal
readiness check. Edge and Chrome admit all four providers, including Claude when it is explicitly
checked. On macOS, Safari is also a Jury runtime for ChatGPT, Grok, and Gemini only, using the
same Browser control and persisted selection. Safari shows Claude so the four-provider catalog
remains legible, but marks it unavailable and leaves it unchecked; switching to Safari clears any
Claude selection, restored Safari preferences drop it, and `/api/jury/check` plus
`/api/jury/start` reject the combination. Safari does not fall back to Edge.

ChatGPT Latest / Extra High, Grok Auto, and Gemini 3.1 Pro are selected by default.
On Edge and Chrome, Claude is available only when explicitly checked. Requested model labels are
verified on the live provider page before any question is submitted; an unavailable label fails
visibly.
Grok readiness uses the same authenticated chat endpoint, composer, and exact model as Jury;
it does not depend on Cache's Files synchronization page or download capability.
Opening a blank Jury page and changing its browser, provider, or model selection do not launch a
browser account probe. Choose `Check accounts` explicitly when the selection is ready; changing
that selection invalidates the prior result and returns the card to `Not checked`. All selected
accounts must pass this browser-only login check before the readiness checkmark appears. A selected
provider that cannot sign in blocks the run before any prompt is sent; the user can deselect it and
proceed with at least two jurors. Start rechecks the same selection before sending any prompt.

On macOS, Edge Jury never clones or opens the user's daily Edge profile. Its first explicit
`Check accounts` creates an owner-only project profile under the active Agent runtime, starts one
verified Microsoft Edge CDP process, and opens one setup tab per selected provider. Sign in to the
provider websites in that window, without signing the browser itself into a Microsoft account,
then choose `Check accounts` again. A missing project profile fails closed with those instructions;
it never falls back to the daily profile. Later account checks and Jury workers attach to the same
project Edge process and use a distinct owned Page in its persistent default context. A juror closes
only its Page and CDP connection, so another juror is not terminated by local cleanup. After the
last Page lease exits, the project Edge process remains available under the existing project-browser
ownership policy for future Jury runs. Windows Edge and Chrome keep their established launch and
debug-profile behavior, and macOS Chrome keeps its clone-backed behavior.

The last browser, selected jurors, and every provider's model tier are remembered locally under
`cachelikes:jury-runtime-preferences:v1` and restored on the next blank Jury page. Restored values
must still exist in the rendered browser and model catalogs; stale or malformed values fall back to
the current safe defaults. Readiness, account diagnostics, prompts, and session content are never
stored in this preference record, and restoration never starts an account check.

Recent sessions keeps its own bounded vertical scrollport, with internal physical-effect bleed so
the standard pill shadows are not cut off by the list or disclosure body. A fully cleaned failed
session exposes a keyboard-accessible `Delete failed Jury session` action. Deletion removes only
that owner-only local JSON archive; running, nonfailed, cleanup-pending, and still-finalizing
sessions remain nondeletable.

Every question creates one Jury session and one provider conversation for each selected
juror. A provider context stays on one owning thread for its entire lifetime. The first pass asks
for independent research. Subsequent passes give every juror the same frozen set of preceding
opinions, citations, and objections. A candidate conclusion is nominated only when verdicts
match; every juror must explicitly accept that exact candidate identifier, supply evidence
(unless the verdict is unverified), and report no unresolved objection before consensus is
shown. Agreement is a recorded outcome, not a guarantee of truth or independent source quality.
Every preserved round uses the shared Collapse structure and trailing chevron. Its ordinal reuses
the Agent active-session count circle, while the accessible summary still names the full round and
review phase. Each opinion heading reuses the same nearly square provider icon shell and image as
the corresponding provider selector; the icon remains decorative and the provider name remains the
accessible text label.

Safari holds one nonblocking global automation lease for the Jury and creates one task-owned Safari
window with one tab per selected juror inside that context. Account checks use that same shape: one
window, one tab per selected juror, login/composer/model verification only, and no prompt or remote
conversation. Native Safari input is serialized, so the coordinator asks ChatGPT, Grok, and Gemini
sequentially on one owning thread. Every juror still receives the same frozen evidence packet for a
given round: round 1 contains no peer result, and later rounds contain only the complete prior-round
barrier. ChatGPT model selection holds one short focus transaction, because its Radix menu closes when
Safari loses focus. Other model-menu and Send actions capture and restore focus independently;
response polling runs outside that native-input interval. Window creation, tab
creation, and cleanup restore the prior app only while the task still owns the foreground, so a
later user switch is not overwritten. If the task-owned window cannot be closed, readiness fails
closed, its page and Safari lease remain tracked for retry, and another Safari Jury is rejected
until cleanup succeeds. The lease durably records a random ownership token, the pre-creation window
and tab-count inventory, and the exact owned window ID. After a service restart, Safari admission
checks that record without opening a window: an existing or ambiguous orphan remains blocked, while
a window proven absent clears the record automatically. An uncertain creation remains quarantined
through a bounded settle interval and two stable read-only window inventories, so a delayed Safari
Apple Event cannot silently create a second task window. Cleanup closes the exact owned Safari
window directly without activating or clicking the current front window. SIGINT, SIGTERM, and
process exit share one idempotent browser-service shutdown callback. Shutdown closes admission,
cancels and waits for any synchronous account check, and waits for the Safari coordinator to close
its window. Restoring the previous app is best-effort; macOS Stage
Manager grouping is not programmatically controlled. Closing, stopping, or failing a check or Jury
closes only that task-owned window and never falls back to Edge or enters its credential-storage
path.

The current Web workflow does not send or infer a fixed turn count. It continues automatically
while parsed verdicts, source URLs and their stated support, exact candidate acceptance, or
concrete unresolved objections are materially changing. It stops with a visible inconclusive
result after cross-review is complete without unanimous acceptance, or when a complete evidence
state recurs without a new candidate requiring one review opportunity. A one-hour monotonic
wall-clock boundary and a 6,500,000-byte durable-record soft boundary prevent an unbounded provider
run without turning a pass count into the business rule. Each juror response is limited to 50,000
UTF-8 bytes so a final complete barrier remains below the 8,000,000-byte reload boundary. Legacy
clients that explicitly submit a valid two-to-six-round budget retain their bounded behavior; the
current page submits none. Every termination reason is stored with the record. A differing or
malformed vote never counts as assent. Provider failure stops the selected jury; the application
never silently removes a juror or sends an uncertain message again. Stop cancels generation and
waits for all owned browser contexts to close before admitting another question. If browser cleanup
outlives the coordinator grace period, a finalizer keeps admission closed until the real owners exit.
User browser windows remain owned by the user.

The strict vote parser accepts a complete JSON object, one JSON code fence, or a browser-rendered
`JSON` language label separated from the complete object by whitespace, including a same-line
space, tab, or line break. It never extracts an object from surrounding prose. On reload, a legacy
archive whose invalid vote still contains one such strict envelope is reparsed in memory without
rewriting the archive, sending a provider message, or reopening a conversation. Recovered terminal
runs remain inconclusive because archive recovery does not rerun deliberation or retroactively
claim consensus. Their recovered conclusions, evidence, and genuine objections are shown instead
of the old technical parse-failure placeholder. A genuinely invalid response is labeled
`invalid vote` and `Structured vote unavailable`; `Remaining objections` is reserved for concrete
objections inside a valid structured vote. Repeated genuinely malformed votes terminate as
`structured_vote_stalled`; they are not described as a stable evidence disagreement and cannot
nominate or accept a candidate.

## Architecture and storage

`app/core/jury.py` owns validation, evidence barriers, automatic convergence, explicit votes,
task admission, and archival state. `app/core/jury_browser.py` adapts the existing model selector, conversation binding,
submission receipt, and response reader without a workspace controller. The Agent domain
facade exports `JuryService`; Flask routes preserve the application network, Origin, LAN
unlock, disabled-external-operation, and no-store boundaries.

`/api/jury/check` checks selected logins. `/api/jury/start` admits a background task and
rechecks sign-in before any provider submission. `/api/jury/status` and `/api/jury/sessions`
read archived state; `/api/jury/stop` requests cancellation. `DELETE /api/jury/session` removes
one failed archive only after the service proves that no worker or browser cleanup remains.
Provider links are persisted as soon as the transport verifies conversation binding, including
before the final answer.

Owner-only, atomically replaced JSON records reside in `local_store/agent/jury/` unless a
test or caller supplies an isolated Agent runtime root. Records contain the explicit question,
provider opinions, citations, and conversation links. Reloading a page resumes observation,
never submission. After a service restart, unfinished records become interrupted archives;
no browser sessions or prompts are recreated. Admission is process-local, so run one app
process for this local feature. Safari's separate file lease and durable ownership record also
prevent a restarted app process from opening a second task window over an unresolved orphan.
The macOS Edge Jury Profile resides beside that Jury runtime under
`agent_browser_profile/edge`; its directories are mode `0700` and runtime markers are mode `0600`.
It contains only data created in that project Edge instance. The application does not copy the
daily Edge `Local State`, Cookies, password database, OneAuth identity, or Microsoft account data,
and it does not read, modify, reset, or grant access to macOS Keychain entries.

Gemini can acknowledge its first prompt before exposing a conversation URL. Jury waits on
that same page for at most 60 seconds only while the exact current submission receipt remains
visible. It still requires the canonical URL before reading or accepting a vote. Missing
receipts, cancellation, navigation, and the deadline fail closed; no prompt is resent.

## Verification

The focused offline layer is:

```bash
./scripts/test.sh tests/test_jury.py tests/test_jury_browser.py tests/test_jury_routes.py tests/test_jury_e2e.py
```

It uses fake provider boundaries and disposable local browser contexts. Live acceptance requires
explicit authorization, an already signed-in profile for the selected browser, and the requested
model choices. A Safari acceptance run should select ChatGPT, Grok, and Gemini and must verify that
one Safari window owns those provider tabs for both account check and the formal Jury, without
opening Edge, a second Safari window, or Claude. Use one explicit question, record each provider
conversation URL across rounds, and confirm cleanup. An unavailable account or exact model is
reported before any prompt. Never treat an offline test as provider acceptance, and never treat
restoring the previous frontmost app as a Stage Manager grouping guarantee.
For macOS Edge, separately verify the lifecycle on an isolated runtime and port: two consecutive
2–4 juror setup/start/stop cycles must retain one project Edge main process, allocate a different
Page per juror, create no `cachelikes-edge-*` Profile, leave the daily Edge process unchanged, and
show no new OneAuth Keychain prompt. A provider login or human-verification block is a separate
provider result and must not be bypassed. This native observation cannot be replaced by mocks.
