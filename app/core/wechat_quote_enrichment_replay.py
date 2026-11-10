"""Reprove one reviewed quote omission without selecting a replacement witness.

Code version: v1.0.0-codex.0
The caller pins the complete decision manifest and immutable input files. This
strategy returns an exact prior projection for a new private output. Shared
account verification may maintain connection-local TEMP validation context;
persistent input rows are never restored, changed, deleted, or published.
"""

from __future__ import annotations

import re


CODE_VERSION = "v1.0.0-codex.0"
_REASON = "New authenticated native quote body conflicts with a completed resource."
_DECISION_FIELDS = {
    "ledger_id",
    "message_id",
    "prior_row_sha256",
    "prior_source_key",
    "prior_witness_key",
    "source_key",
    "witness_key",
    "prior_native_sha256",
    "native_sha256",
    "integrity",
    "event_scope",
    "same_raw_content_sha256",
    "same_physical_native_row",
    "same_source_account",
    "sender_conflict",
    "hard_fact_conflict_fields",
    "reply_relation",
    "quoted_body_relation",
    "native_clock_delta",
    "decision",
}
_VALIDATED = object()


def _one(connection, statement, parameters):
    cursor = connection.execute(statement, parameters)
    row = cursor.fetchone()
    if row is None:
        raise ValueError("Reviewed quote replay lost an exact retained proof row.")
    return dict(zip((column[0] for column in cursor.description), row, strict=True))


def _components(native, kinds):
    if not isinstance(native, dict) or not isinstance(
        native.get("text_components"), list
    ):
        raise ValueError("Reviewed quote replay needs explicit typed components.")
    result = []
    for item in native["text_components"]:
        if not isinstance(item, dict):
            raise ValueError("Reviewed quote replay has malformed components.")
        if item.get("kind") in kinds:
            if not isinstance(item.get("text"), str):
                raise ValueError("Reviewed quote replay needs literal component text.")
            result.append((item["kind"], item["text"]))
    return result


def _source(derived, history, source_key, source_cache, common):
    from . import wechat_macos_recovery_bank as bank

    retained = _one(derived, "SELECT * FROM sources WHERE id=?", (source_key,))
    header = common.load_json_cell(retained["header"])
    if common._sha(header) != source_key or retained["sha256"] != source_key:
        raise ValueError("Reviewed quote replay has a changed source header.")
    cache_key = (CODE_VERSION, history, source_key)
    cached = source_cache.get(cache_key)
    if not (
        isinstance(cached, tuple)
        and len(cached) == 2
        and cached[0] is _VALIDATED
        and cached[1] == header
    ):
        bank._validate_account_binding(history, {**header, "messages": []})
        source_cache[cache_key] = (_VALIDATED, header)
    return header


def _event(derived, header, source_key, witness_key, expected_sha, common, *, prior):
    from .wechat_resource_sender_evidence import validate_secondary_native_event

    witness = _one(derived, "SELECT * FROM witnesses WHERE id=?", (witness_key,))
    message = common.load_json_cell(witness["message"])
    native_sha = common._sha(message)
    keys = {common._sha([source_key, message["local_id"], message["content_sha256"]])}
    if not prior:
        keys.add(
            common._sha(
                [
                    "incremental_conflicting_record",
                    source_key,
                    message["local_id"],
                    message["content_sha256"],
                    native_sha,
                ]
            )
        )
    if (
        witness["source_key"] != source_key
        or witness["sha256"] != native_sha
        or expected_sha != native_sha
        or witness_key not in keys
    ):
        raise ValueError("Reviewed quote replay has a changed native witness.")
    event = {**header, **message}
    validate_secondary_native_event(event)
    return event


def _replay_quote_enrichment(
    derived, history, catalog, decision, *, snapshots, source_cache
):
    """Return only the independently regenerated, byte-identical prior quote row."""
    from . import wechat_secondary_completions as common
    from .wechat_resource_sender_evidence import match_secondary_native_event

    if not isinstance(decision, dict) or set(decision) != _DECISION_FIELDS:
        raise ValueError(
            "Reviewed quote replay needs the closed decision manifest row."
        )
    if type(decision.get("ledger_id")) is not int or decision["ledger_id"] <= 0:
        raise ValueError("Reviewed quote replay needs an exact positive ledger ID.")
    for field in (
        "message_id",
        "prior_row_sha256",
        "prior_source_key",
        "prior_witness_key",
        "source_key",
        "witness_key",
        "prior_native_sha256",
        "native_sha256",
    ):
        if (
            not isinstance(decision.get(field), str)
            or re.fullmatch(r"[0-9a-f]{64}", decision[field]) is None
        ):
            raise ValueError(
                "Reviewed quote replay needs exact lowercase proof digests."
            )
    ledger = _one(
        derived,
        "SELECT * FROM incremental_resource_conflicts WHERE id=?",
        (decision["ledger_id"],),
    )
    if ledger["reason"] != _REASON or any(
        ledger[field] != decision[field]
        for field in (
            "message_id",
            "prior_row_sha256",
            "prior_source_key",
            "prior_witness_key",
            "source_key",
            "witness_key",
        )
    ):
        raise ValueError(
            "Reviewed quote replay differs from its exact conflict ledger."
        )
    ambiguity = _one(
        derived, "SELECT * FROM ambiguities WHERE id=?", (ledger["message_id"],)
    )
    if ambiguity["reason"] != _REASON:
        raise ValueError(
            "Reviewed quote replay cannot replace another ambiguity policy."
        )
    if (
        derived.execute(
            "SELECT 1 FROM entries WHERE id=?", (ledger["message_id"],)
        ).fetchone()
        is not None
    ):
        raise ValueError("Reviewed quote replay cannot replace an active completion.")
    original = _one(
        catalog, "SELECT * FROM messages WHERE id=?", (ledger["message_id"],)
    )
    conversation = _one(
        catalog,
        "SELECT * FROM conversations WHERE id=?",
        (original["conversation_id"],),
    )
    if ambiguity["conversation_id"] != conversation["id"]:
        raise ValueError("Reviewed quote replay crossed its quarantined conversation.")
    events = []
    for prior, prefix in ((True, "prior_"), (False, "")):
        source_key, witness_key = (
            ledger[prefix + "source_key"],
            ledger[prefix + "witness_key"],
        )
        header = _source(derived, history, source_key, source_cache, common)
        events.append(
            _event(
                derived,
                header,
                source_key,
                witness_key,
                decision["prior_native_sha256" if prior else "native_sha256"],
                common,
                prior=prior,
            )
        )
    old, new = events
    natives = [event.get("native_text") for event in events]
    if any(
        not isinstance(native, dict)
        or native.get("text_extraction_kind") != "quoted_reply"
        or type(native.get("appmsg_subtype")) is not int
        or native["appmsg_subtype"] != 57
        for native in natives
    ):
        raise ValueError(
            "Reviewed quote replay requires two exact quoted-reply envelopes."
        )
    replies = [_components(native, {"reply_text"}) for native in natives]
    quotes = [
        _components(native, {"quoted_text", "quoted_forwarded_text"})
        for native in natives
    ]
    if (
        not replies[0]
        or replies[0] != replies[1]
        or not any(text.strip() for _kind, text in replies[0])
        or not quotes[0]
        or not any(text.strip() for _kind, text in quotes[0])
        or quotes[1]
        or common._quoted_reference_conflicts(*natives)
        or common._quoted_literal_conflicts(*natives)
    ):
        raise ValueError("Reviewed quote replay is not the proven literal omission.")
    for event in events:
        if (
            match_secondary_native_event(
                original, conversation, [event], max_clock_skew=1
            )
            is None
        ):
            raise ValueError(
                "Reviewed quote replay differs from the exact primary resource event."
            )
    if (
        old["sender_id"] != new["sender_id"]
        or abs(old["create_time"] - new["create_time"]) > 1
    ):
        raise ValueError(
            "Reviewed quote replay has conflicting actors or native clocks."
        )
    same_physical = (
        old["account_id"] == new["account_id"]
        and all(
            old["source"].get(key) == new["source"].get(key)
            for key in ("main_sha256", "wal_sha256", "database_relative_path", "table")
        )
        and old["local_id"] == new["local_id"]
    )
    recomputed = {
        "integrity": True,
        "event_scope": True,
        "same_raw_content_sha256": bool(old["content_sha256"] == new["content_sha256"]),
        "same_physical_native_row": same_physical,
        "same_source_account": old["account_id"] == new["account_id"],
        "sender_conflict": False,
        "hard_fact_conflict_fields": [],
        "reply_relation": "exact",
        "quoted_body_relation": "old_present_new_absent",
        "native_clock_delta": new["create_time"] - old["create_time"],
        "decision": "same_reply_less_complete_observation",
    }
    if any(
        type(decision[key]) is not type(value) or decision[key] != value
        for key, value in recomputed.items()
    ):
        raise ValueError(
            "Reviewed quote replay contradicts its measured decision claims."
        )
    completed = common._complete_resource(original, conversation, [old], snapshots)
    if completed is None or completed[2] != "native_quote":
        raise ValueError(
            "Reviewed quote replay cannot regenerate its prior quote strategy."
        )
    message = {**completed[0], "time_sort": float(completed[0]["time_sort"])}
    if common._sha(message) != ledger["prior_row_sha256"]:
        raise ValueError("Reviewed quote replay changed the original completed row.")
    return {
        "message": message,
        "original": original,
        "conversation": conversation,
        "event": old,
        "strategy": "native_quote",
        "ledger_id": ledger["id"],
        "prior_row_sha256": ledger["prior_row_sha256"],
    }


def replay_quote_enrichment(
    derived, history, catalog, decision, *, snapshots, source_cache
):
    """Fail closed with one public error type for malformed retained evidence."""
    if not isinstance(source_cache, dict):
        raise ValueError("Reviewed quote replay needs a private validation cache.")
    try:
        return _replay_quote_enrichment(
            derived,
            history,
            catalog,
            decision,
            snapshots=snapshots,
            source_cache=source_cache,
        )
    except (KeyError, TypeError, IndexError, OverflowError) as error:
        raise ValueError(
            "Reviewed quote replay has malformed retained evidence."
        ) from error
