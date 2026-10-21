# Gemini Takeout import and browser refresh protection

Documentation version: `v1.0.0-codex.0`

The Gemini browser collector can expose only a rendered portion of a long conversation.
An official Google Takeout export provides a separate source for recovering older text. The
importer preserves existing cached rows and the original export so that source uncertainty
does not become silent data loss.

## Prepare the source

In Google Takeout, select My Activity, open its content selection, and select Gemini
Apps. Extract the downloaded archive locally. Supply the dedicated `Gemini Apps` directory
containing `MyActivity.html`, or that HTML file. Do not supply an entire Downloads directory.
The separate Gemini product export is not a substitute for the My Activity conversation export.

Keep the export private. It can contain prompts, answers, uploaded files, and executable files
that the user previously attached. The importer reads those files as bytes and never executes
them. No browser, credentials, network requests, or signed-in session is required.

## Review, then apply

Run the module from the repository with the supported Python 3.13 or newer interpreter:

```bash
python3 -m app.core.gemini_export_import "/path/to/Gemini Apps" \
  --local-store-root "/path/to/local_store" --timezone "Asia/Taipei"
```

On Windows, use `py -3` instead of `python3`. The command defaults to a dry run: it reports the
proposed change without publishing history, backups, or import artifacts. Use the timezone in
which the export's displayed timestamps were written. The explicit IANA timezone prevents an
ambiguous abbreviation such as `CST` from silently selecting the wrong UTC offset.

After reviewing the report, run the same command with `--apply`:

```bash
python3 -m app.core.gemini_export_import "/path/to/Gemini Apps" \
  --local-store-root "/path/to/local_store" --timezone "Asia/Taipei" --apply
```

Wait for all cache tasks to finish before applying. The importer uses the full cache
maintenance lock for the selected store, refuses active resource locks, and never stops a job
or ignores another provider's lock. It verifies that both the source and history still match
the reviewed inputs before publication.

The applied import stores the original export and file hashes under
`local_store/llm/gemini/imports/`, records source provenance, saves existing history as
`history-before.parquet` inside that import bundle, and publishes a verified typed Parquet file
atomically. Reapplying the same source against unchanged history is idempotent. If another
operation changes or rewrites history after import, the receipt refuses automatic replay;
reconcile that change before reusing the source. Preserve the import artifacts together with
history when backing up the store; they retain the evidence needed to reconcile interrupted
imports.

## Interpret the result

Only dedicated activity metadata supplies conversation IDs. URLs quoted inside prompts or
answers do not establish ownership. A record with one genuine conversation ID can supplement
that conversation. Records that cannot be aligned safely retain an explicit uncertain-alignment
label rather than silently replacing an old message or claiming a unique original turn.

Grouped activity can contain several conversation URLs and flattened assistant responses.
Its shared prompt can be retained as source context, while an individual timestamp or answer
assignment remains unknown. Non-prompt activity and raw responses remain in provenance; the
importer does not invent user or assistant turns from an action label.

Empty source bodies remain empty facts. Message-record counts can include labeled source
alternatives and uncertain supplements, so a larger count does not prove an exact reconstruction
of every original conversation. Read the coverage and uncertainty report alongside the counts.

Uploaded files are preserved in the raw bundle. This text-import command does not automatically
register them in the native image catalog or make unsupported audio and document types viewable
in Local resources. Generated-media counts do not prove that generated output bytes were exported.

## Browser refresh protection

Browser refreshes use a conservative write path separate from authoritative session replacement.
An equal snapshot preserves every existing row. An extension can append only after the old
message positions, roles, text, and source timestamps establish a complete matching prefix.
A shorter, conflicting, or unprovable snapshot preserves the cached conversation and reports
the session as incomplete. Existing message keys and media bindings remain stable.

This protects recovered history; it does not make the rendered browser collector a complete
long-conversation exporter. The existing authoritative replacement API remains available to
callers that possess a complete, deliberate replacement.

## Verification

The focused synthetic tests are `tests/test_gemini_export_import.py` and
`tests/test_gemini_downloader.py`. They use temporary stores and fabricated activity HTML;
real exports, caches, attachment bytes, and account identifiers must never enter Git fixtures.
Run these tests and the repository quality gate before publishing importer changes. See the
[testing guide](TESTING.md) and [cache runbook](CACHE_HANDOFF.md).
