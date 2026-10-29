# OpenClaw preservation and source attribution

Documentation version: `v1.0.0-codex.0`

## Storage contract

`app/core/openclaw_archive.py` creates a private, self-contained snapshot under
`local_store/openclaw_archive/<snapshot>/`. Its `state/` tree mirrors the source
OpenClaw state directory. `manifest.json` records every copied file's SHA-256,
size, original path, copy method, exclusions, symlinks, and validation result.
`latest.json` changes only after a complete snapshot succeeds. SQLite databases,
including backups with nonstandard suffixes, use the online backup API to include
committed WAL records without copying a potentially inconsistent live database.
Snapshots are consistent per database; they do not establish a transaction across
every file and database. A concurrently changing ordinary file aborts preservation.

The snapshot retains legacy reset/deleted/imported transcripts, compressed
archives, current and backup databases, embedded Codex history, media, workspace
artifacts, memory, and operational state. Browser profiles, top-level package
installations and caches, and dependency trees are explicitly excluded. Symlinks
are recorded without following external targets. Check these exclusions before
claiming that every independent user asset is preserved. Archive directories use
mode `0700`; files use `0600`. Configuration and databases can contain credentials;
the entire archive remains in ignored local storage and must not be published.

## Parsing and review

`app/core/openclaw_history.py` reads the preserved snapshot without an OpenClaw
installation, gateway, browser, or network request. It handles SQLite JSON and
Zstandard events, database transcript archives, older JSONL layouts with reset,
deleted, backup, or imported suffixes, and embedded Codex rollouts. The existing
Arrow Zstandard codec avoids an additional dependency or a higher Python minimum.
Trajectory context snapshots, tools, and internal reasoning remain in the raw
archive rather than appearing as invented user-facing messages. Attachment
payloads remain in the archive; the text reader shows attachment markers rather
than promising a media preview.

Each provider has a separate `local_store/llm/<source>/history.parquet` and Cache
page: `chatgpt_openclaw`, `gemini_openclaw`, `claude_openclaw`, `grok_openclaw`,
`zhipu_openclaw`, `doubao_openclaw`, `other_openclaw`, and `unknown_openclaw`.
These never merge into browser ChatGPT/Gemini or the native Codex cache. Every row
retains original session/event identity, provider, model, harness, attribution
method, archive path, and all matching record locations. Embedded Codex and
OpenClaw wrapper transcripts retain distinct conversation identities. Their raw
thread-to-session metadata remains available for review; they are not asserted to
be independent human conversations.

Assistant attribution uses its explicit provider/model, with temporal model-change
evidence as a fallback. A user's prompt follows the responding provider in that
turn; if different providers respond, the prompt can appear in each relevant
provider partition to preserve context. Current session settings never relabel
older messages. OpenClaw delivery mirrors and gateway injections remain under
`Unknown OpenClaw`. Unknown does not imply that a particular provider produced the
message. Exact duplicate records merge their provenance; conflicting text retains
both versions. Repeated identical messages without IDs retain occurrence identity.
Import merges preserve existing history after partial failure or interruption.
Earlier synthetic IDs are replaced only when corrected records from the same
archive prove matching content, timestamps, and every original provenance
location. Unproven cached records remain available after a partial import.

Source-specific search, session review, prompt saving, and Markdown export are
available through `Local resources`. Provider session counts can overlap when a
conversation changes provider; their sum is not a unique conversation count.
`Read archived sessions` rebuilds the selected cache from the current preserved
snapshot and never reads live OpenClaw state. A new snapshot must be created
explicitly to capture later OpenClaw activity.

## Reproducible local operations

Use the project's supported Python interpreter from the repository root:

```python
from app.core.config import LOCAL_STORE_ROOT
from app.core.openclaw_archive import create_openclaw_archive
from app.core.openclaw_history import import_openclaw_history

archive = create_openclaw_archive(LOCAL_STORE_ROOT)
result = import_openclaw_history(LOCAL_STORE_ROOT)
assert result["failed"] == 0 and not result["stopped"]
```

Review the private manifest and repeat import without changes to confirm stable
identities. Before deleting OpenClaw state, compare current chat-bearing files and
database transcript records with the snapshot. Stop ongoing writers as part of
the actual uninstall, capture any final difference, and verify again. Review
enabled cron jobs, channel integrations, external workspaces, and symlink targets;
preserving chat does not migrate their behavior.

The installed CLI's noninteractive preview is:

```bash
openclaw uninstall --dry-run --all --yes --non-interactive
```

The `--dry-run` flag is mandatory for review. Actual removal is a separate action.
The official [uninstall guide](https://docs.openclaw.ai/install/uninstall) describes
service/state/workspace/app removal and the separate removal of the global CLI.
The snapshot's location outside `.openclaw` survives that removal. Do not remove
the shared Node or Python runtime to remove OpenClaw.

## Verification

Run the OpenClaw archive, parser, Cache page, reader, and responsive browser tests:

```bash
./scripts/test.sh tests/test_openclaw_archive.py tests/test_openclaw_history.py \
  tests/test_openclaw_cache_web.py tests/test_openclaw_browser.py \
  tests/test_openclaw_browser_e2e.py
```

Tests use synthetic temporary state, never production transcripts or credentials.
The parser regressions cover compressed and historical storage, provider switches,
unknown infrastructure, context attribution, content conflicts, backup-only
history, malformed input, idempotence, repeated id-less messages, source isolation,
and source immutability.
