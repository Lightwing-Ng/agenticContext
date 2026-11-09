# WeChat dataset exchange

Documentation version: `v1.0.1-codex.0`

The Local resources WeChat surface offers two exports. CSV remains the default
readable timeline export. Dataset exchange creates a `.wechatx` package containing
literal text, structured message fields, source observations, and their recovery
dependencies. It does not include image, audio, video, file, or sticker binaries.

Hover or focus the conversation's export button to select a format. Touch users
can tap the button to open the same menu. When a WeChat account is selected, the
conversation list places the standard import button immediately to the left of
the top search field. It is absent from the Source picker, conversation details,
and the account-unscoped list. The button opens a file selection and validation dialog. Validation
shows account, source, conversation, message, resource, duplicate, and conflict
counts. Only the subsequent Import action publishes records. Cancel releases the
uploaded temporary package; abandoned previews expire after 30 minutes.
The selected toolbar account does not override the package's source-account identity.
Dataset preparation runs in the background with a visible preparing state. The
finished package downloads through a private, expiring URL. Large conversations
with complete provenance can take several minutes to prepare. A local service
restart invalidates an unfinished preparation token; the interface then allows a
fresh attempt. CSV retains its native download behavior.

## Scope and identity

An exchange export covers the complete selected conversation, independently of
pagination, text search, and display sorting. Independently verified native group
accounts sharing an exact room identifier can join the same package. Verified
reciprocal private-chat owners can join the same package. Matching titles, display
names, text, local message identifiers, or account labels do not establish a
conversation family. The Account sources page metric is not a family proof.

Raw source occurrences remain distinct when the readable timeline has a proved
duplicate or preferred representative. Source clock discrepancies, unknown time
zones, unresolved identities, extraction flags, and unsupported compressed-message
gaps remain explicit. A package cannot restore text that its source has not
decoded. Referenced source snapshots, contacts, quoted and forwarded components,
aliases, resource occurrences, and clock evidence travel with the scoped records.

Each installation has a private `export_origin.json` namespace beside its runtime
WeChat database. That UUID and the package hashes identify declarations and bytes;
they do not authenticate a person or prove a native account identity. Imported
sources receive separate local namespaces and retain their original identifiers
in immutable typed row envelopes. Their identities are labeled Imported origin
claim. Foreign contact names do not replace local contact preferences. Foreign
source paths remain evidence strings and cannot authorize opening files on the
recipient's computer.

Exact replays add no records. A changed row with the same declared origin and
original key is retained as a separate conflict variant. A subsequent export
retains the foreign origin envelopes needed for lossless recovery while limiting
their closure to the selected conversation.

## Container and tables

The media type is `application/vnd.agenticcontext.wechat-exchange`. The container
is ZIP with stored members and has a single `manifest.json`. Data members are
allowlisted tables under `tables/<domain>/<table>.jsonl.zst` or `.parquet`.

The manifest declares `agenticcontext.wechat-exchange`, format version `1`, a
content-derived package identifier, the origin namespace, scoped conversation
identifiers, row counts, typed columns, original primary keys, member sizes,
codecs, and SHA-256 hashes. Its JSON is canonical UTF-8. Each original JSON field
inside a SQLite row remains a literal string, including its original whitespace.
SQLite integers, finite reals, nulls, text, and binary values retain explicit types;
binary JSONL cells use Base64. Packages never contain executable SQL, scripts,
arbitrary archive paths, or native media payloads.

The exporter measures each table using JSONL with Zstandard compression and
Parquet with Zstandard compression, then stores the smaller member. Both use
compression level 19. There is no additional ZIP compression. The initial bounded
12,000-message comparison, including common schema metadata and ZIP framing,
measured 1,535,985 bytes for object JSONL, 1,757,709 bytes for Parquet, and
1,682,998 bytes for a compact SQLite stream with Zstandard.
These measurements describe that sample rather than a universal compression
ranking. SQLite remains the application's runtime storage format.

`exchange.timeline` is a convenient message dataset for retrieval-augmented
generation (RAG). The original domain tables retain the source graph required by
the project. A consumer can use the manifest's codec and column descriptors to
read JSONL through Arrow's Zstandard stream or read Parquet directly. Hugging Face
Datasets supports [JSON and Parquet inputs](https://huggingface.co/docs/datasets/en/loading);
keeping the source and clock fields
alongside text lets downstream pipelines filter uncertain observations without
discarding the original evidence. Nothing is uploaded to a dataset host.

## Validation and publication

Version 1 applies bounded package, expansion, cell, table, and row limits. The
package and upload limit is 256 MiB, the typed-cell budget is 256 MiB, the JSON
serialization expansion budget is 512 MiB, and the total row budget is 250,000.
These are distinct budgets: JSON field names and escaping are not additional
source cells. Packages beyond the supported budget fail before
publication. Member names must exactly match the manifest; encrypted, duplicate,
undeclared, symbolic-link, and unexpected compression members are rejected. Every
member hash, column type, and row count is checked before import, and the package
is checked again when a reviewed import is committed.

Import stages a source-separated append under the existing cache maintenance
lock. Existing records remain protected against update and deletion. Publication
uses verified local clones, sufficient peak disk space, coordinated resource
metadata pins, rollback copies, and exact readback. A changed target invalidates a
pending preview. An import failure restores the previous publication rather than
leaving a partially applied exchange.

An unchanged, purely imported conversation can be exported from its preserved
original table graph. This avoids accumulating nested wrapper copies on each
exchange. Local edits, conflicts, or mixed source scopes require a new scoped
package and remain subject to the same explicit budgets.

The complete source graph has a runtime indexing cost distinct from its download
size. The bounded real-group verification produced a 21,338,569-byte exchange
package for 40,822 canonical parent messages, 14,138 resource references, and
192,260 source-closure rows. Restoring its full evidence and presentation indexes
to an isolated blank database used approximately 1.0 GiB. Disk checks use the
expanded data and index estimate rather than the compressed package size. The
original 40,825 source events remain preserved; three proved duplicate events are
folded only in the readable timeline.

The implementation lives in `app/core/wechat_exchange.py`,
`app/core/wechat_exchange_import.py`, and
`app/web/wechat_exchange_routes.py`. Focused codec, import, route, and disposable
browser tests live in the corresponding `tests/test_wechat_exchange*.py` files.
