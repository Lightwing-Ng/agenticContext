# Mac WeChat conversation refresh

Documentation version: `v1.0.0-codex.0`

Local resources shows Refresh chat history immediately before Share on a private
or group conversation when local, owner-private database-key metadata matches its
verified native account. It does not expose the action for official accounts,
unclassified conversations, unconfigured accounts or nonlocal requests.

The action starts one bounded background job for the canonical conversation ID.
Duplicate activation reuses that job; another conversation waits until it finishes.
The existing centered waiting card reports progress, followed by a result notice.
Success reloads the exact current URL and retains unrelated history state. Partial
source coverage is explicit and does not establish complete account recovery.

## Existing local configuration

The feature consumes the existing key registry under
`~/Library/Application Support/agenticContext/WeChat Recovery/Keys/`, using
`macos-*/verified-db-keys*.jsonl`. Key files must be resident, bounded, owner-only
regular files without symlink indirection. Native sources remain under the standard
Mac WeChat container's `xwechat_files` directory. Eligibility reads metadata only;
clicking Refresh is the explicit boundary for opening the configured source.

There is no key-capture, process-injection or key-entry flow. Keys, source paths and
plaintext are never returned to the browser or included in public status. The HTTP
API accepts only the canonical session ID, not account, peer, database or key inputs.
POST and status responses are private and noncacheable, behind the application's
existing access and origin checks and an additional loopback requirement.

## Source and publication boundaries

The reader takes a stable in-memory group of main database, WAL, SHM and journal
identities. It authenticates SQLCipher 4 pages and committed WAL transactions, then
runs SQLite integrity checks. Account ownership, exact peer and known conversation
anchors must agree before the target conversation can be published. Witness reads
are limited to bounded, previously verified coordinates; they do not scan all bodies.

Verified packets publish through the Mac recovery bank. The canonical archive is
unchanged. Existing native and imported source occurrences retain their identities
and provenance. Exact replay creates no additional bank candidate. Publication uses
an owner-private cross-process lock, all-input identity checks and a final
cancellation check immediately before replacement. Active source writes, identity
drift and unsupported data fail closed. Temporary runtime candidates are bounded;
the feature does not accumulate persistent plaintext source snapshots.

Older external publishers already loaded before the publication-lock change cannot
participate in that lock. Let those jobs finish before overlapping a UI refresh.

## Interpreter and shutdown

The application still supports Python 3.13 or newer. A refresh requires the standard
Zstandard capability provided by Python 3.14 or newer; a 3.13 application host probes
a local compatible worker without replacing or restarting the host interpreter.
`AGENTIC_CONTEXT_WECHAT_PYTHON` can select that worker explicitly. Only server-owned
scope reaches its arguments, and public output is bounded and allowlisted.

Application shutdown, parent EOF and cancellation stop the owned worker before
publication. Status errors remain generic and do not include private stderr or keys.
Verification uses the synthetic suites listed in [Testing](TESTING.md); it neither
refreshes production history nor demonstrates real-account completeness.
