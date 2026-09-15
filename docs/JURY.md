# Browser Jury

Documentation version: `v1.3.0-codex.1`

## Workflow

Agentic retains the existing project controller. Jurors opens `/jury/edge` by default and uses only
authenticated provider websites. There is no project picker, context upload, or Terminal
readiness check. Edge and Chrome admit all four providers. On macOS, Safari is also a Jury runtime
for ChatGPT and Grok, using the same Browser control and persisted selection. Gemini and Claude
remain source-only in Safari and fail readiness with an explicit Edge-or-Chrome diagnostic before
any Jury prompt is sent.

ChatGPT Latest / Extra High, Grok Auto, and Gemini 3.1 Pro are selected by default.
Claude is available only when explicitly checked. Requested model labels are verified on the
live provider page before any question is submitted; an unavailable label fails visibly.
Grok readiness uses the same authenticated chat endpoint, composer, and exact model as Jury;
it does not depend on Cache's Files synchronization page or download capability.
Opening a blank Jury page and changing its browser, provider, or model selection do not launch a
browser account probe. Choose `Check accounts` explicitly when the selection is ready; changing
that selection invalidates the prior result and returns the card to `Not checked`. All selected
accounts must pass this browser-only login check before the readiness checkmark appears. A selected
provider that cannot sign in blocks the run before any prompt is sent; the user can deselect it and
proceed with at least two jurors. Start rechecks the same selection before sending any prompt.

The last browser, selected jurors, and every provider's model tier are remembered locally under
`cachelikes:jury-runtime-preferences:v1` and restored on the next blank Jury page. Restored values
must still exist in the rendered browser and model catalogs; stale or malformed values fall back to
the current safe defaults. Readiness, account diagnostics, prompts, and session content are never
stored in this preference record, and restoration never starts an account check.

Every question creates one Jury session and one provider conversation for each selected
juror. A provider context stays on one owning thread for its entire lifetime. The first pass asks
for independent research. Subsequent passes give every juror the same frozen set of preceding
opinions, citations, and objections. A candidate conclusion is nominated only when verdicts
match; every juror must explicitly accept that exact candidate identifier, supply evidence
(unless the verdict is unverified), and report no unresolved objection before consensus is
shown. Agreement is a recorded outcome, not a guarantee of truth or independent source quality.

Safari holds one nonblocking global automation lease for the Jury and creates one task-owned Safari
window per selected juror inside that context. Because native Safari input is serialized, the
coordinator asks ChatGPT and Grok sequentially on one owning thread. Both still receive the same
frozen evidence packet for a given round: round 1 contains no peer result, and later rounds contain
only the complete prior-round barrier. Closing, stopping, or failing the Jury closes only those
task-owned windows and never falls back to Edge or enters its credential-storage path.

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

## Architecture and storage

`app/core/jury.py` owns validation, evidence barriers, automatic convergence, explicit votes,
task admission, and archival state. `app/core/jury_browser.py` adapts the existing model selector, conversation binding,
submission receipt, and response reader without a workspace controller. The Agent domain
facade exports `JuryService`; Flask routes preserve the application network, Origin, LAN
unlock, disabled-external-operation, and no-store boundaries.

`/api/jury/check` checks selected logins. `/api/jury/start` admits a background task and
rechecks sign-in before any provider submission. `/api/jury/status` and `/api/jury/sessions`
read archived state; `/api/jury/stop` requests cancellation. Provider links are persisted as
soon as the transport verifies conversation binding, including before the final answer.

Owner-only, atomically replaced JSON records reside in `local_store/agent/jury/` unless a
test or caller supplies an isolated Agent runtime root. Records contain the explicit question,
provider opinions, citations, and conversation links. Reloading a page resumes observation,
never submission. After a service restart, unfinished records become interrupted archives;
no browser sessions or prompts are recreated. Admission is process-local, so run one app
process for this local feature.

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
model choices. A Safari acceptance run must select only ChatGPT and Grok and must verify that one
Safari context owns both provider windows without opening Edge.
Use one explicit question, record each provider conversation URL across rounds, and confirm
cleanup without starting Claude. An unavailable Gemini account or exact model is reported
separately from ChatGPT/Grok acceptance. Never treat an offline test as provider acceptance.
