# Media SQLite storage

Documentation version: `v1.0.0`

## Scope and format

Five media metadata resources use SQLite: `.chatgpt_catalog.sqlite3`,
`.grok_catalog.sqlite3`, `.grok_download_manifest.sqlite3`,
`.grok_work_queue.sqlite3`, and `.cache_catalog.sqlite3` outside the LLM root.
The corresponding Parquet stems remain read-only compatibility sources when a
canonical database is absent. Original images, videos, transcripts, numbered
copies, and recovery evidence are not converted or deleted by runtime adoption.

`media_info` records the storage version, exact serialized Arrow schema, primary
keys, and indexes. `media_rows` contains typed scalar SQL columns and JSON list
columns, with an independent original ordinal. SQLite format 1 is distinct from
the compressed LLM format. Embedded Parquet Arrow schemas retain original list
child names and provenance instead of losing them to physical Parquet normalization.

The runtime uses `FULL` synchronous transactions with a rollback journal. Dirty
upserts and deletions commit together; unchanged rows are not updated. Conditional
task claims compare the persisted status and attempt count in the same transaction.
Filesystem moves and completed media files remain outside SQL transactions and
continue to use the existing task lock and crash-recovery checks.

## Migration and rollback

Follow [Operations](OPERATIONS.md#media-metadata-migration). Preview records file
metadata without reading private contents. Apply preserves a gzip rollback verified
against the original file hash, validates SQLite integrity and exact typed readback,
checks source drift and active ownership, and publishes without replacing an
independently created target. Existing matching targets can be verified; conflicting
targets require reconciliation. Original Parquet is retained unless retirement is
explicitly requested and all checks pass.

To roll back, quiesce writers, preserve the current SQLite file and receipt, restore
the verified gzip source to a separate candidate, and verify its decoded hash.
Restore the old source and runtime only after checking that SQLite contains no newer
independent changes. Never overwrite newer records merely to restore an older format.

## Reproducible performance and storage report

```bash
python3 scripts/benchmark_media_sqlite.py --rows 1000 10000 --repeats 9 \
  --report test-results/media-synthetic.json
```

The deterministic public fixture contains no account data. It measures an untimed
warmup and repeated warm-cache samples, retaining median, P95, and every sample.
Point queries, one-row mutation, and full reads are distinct operations. The old
write comparison starts with an in-memory snapshot, as the real catalogs did;
SQLite receives one changed row. Disk-query measurements are not a comparison
against process-local memory hits or a complete download loop. Grok additionally
checks persisted records for freshness; ChatGPT and X retain their fast local maps.

SQLite prioritizes indexed access, small transactional mutations, and consistency.
Parquet can remain substantially smaller for repetitive columnar metadata, and its
full-table reads can remain faster. Measure both outcomes rather than asserting
that SQLite always improves them. Synthetic ratios are not production forecasts.

Private migration reports record original placeholder allocation, hydrated source
allocation, SQLite logical/allocation bytes, verified rollback bytes, and source
retention separately. Retained originals and rollback backups must be included in
net storage accounting. File allocation is not proof of APFS container free-space
recovery, and cloud placeholders must not be counted as resident bytes reclaimed.
