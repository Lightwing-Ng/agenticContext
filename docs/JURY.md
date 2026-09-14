# Browser Jury

Documentation version: `v1.0.1-codex.1`

## Workflow

Agentic retains the existing project controller. Jurors opens `/jury/edge` and uses only
authenticated provider websites. There is no project picker, context upload, or Terminal
readiness check. Edge and Chrome use the existing Browser control design.

ChatGPT Latest / Extra High, Grok Auto, and Gemini 3.1 Pro are selected by default.
Claude is available only when explicitly checked. Requested model labels are verified on the
live provider page before any question is submitted; an unavailable label fails visibly.
Grok readiness uses the same authenticated chat endpoint, composer, and exact model as Jury;
it does not depend on Cache's Files synchronization page or download capability.
All selected accounts must pass the browser-only login check before the readiness checkmark
appears. A selected provider that cannot sign in blocks the run before any prompt is sent;
the user can deselect it and proceed with at least two jurors.

Every question creates one Jury session and one provider conversation for each selected
juror. A provider context stays on one owning thread for its entire lifetime. Round 1 asks
for independent research. Subsequent rounds give every juror the same frozen set of preceding
opinions, citations, and objections. A candidate conclusion is nominated only when verdicts
match; every juror must explicitly accept that exact candidate identifier, supply evidence
(unless the verdict is unverified), and report no unresolved objection before consensus is
shown. Agreement is a recorded outcome, not a guarantee of truth or independent source quality.

The default limit is three rounds, adjustable from two to six. A differing or malformed vote
does not count as assent. At the limit, the result remains inconclusive and retains the
opinions. Provider failure stops the selected jury; the application never silently removes a
juror or sends an uncertain message again. Stop cancels generation and waits for all owned
browser contexts to close before admitting another question. User browser windows remain owned
by the user.

## Architecture and storage

`app/core/jury.py` owns validation, round barriers, explicit votes, task admission, and archival
state. `app/core/jury_browser.py` adapts the existing model selector, conversation binding,
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
explicit authorization, an already signed-in Edge profile, and the requested model choices.
Use one explicit question, record each provider conversation URL across rounds, and confirm
cleanup without starting Claude. An unavailable Gemini account or exact model is reported
separately from ChatGPT/Grok acceptance. Never treat an offline test as provider acceptance.
