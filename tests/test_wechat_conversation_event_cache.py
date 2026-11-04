"""Compact event proofs retain exact source observations within the cache budget.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import hashlib

import pytest

from app.core import wechat_conversation_events as events
from tests import test_wechat_conversation_events as fixtures


event_store = fixtures.event_store


def _id(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _proof():
    members, mapping = {}, {}
    for ordinal in range(3):
        preferred, alternate = _id(f"native-{ordinal}"), _id(f"html-{ordinal}")
        contexts = []
        for source, identifier in (("native", preferred), ("html", alternate)):
            context = {
                "id": identifier,
                "source_id": source,
                "source_snapshot_id": source + "-snapshot",
                "source_label": "iPad archive" if source == "native" else "HTML export",
                "source_clock": "2019-04-07 14:39:25",
                "timestamp_value": "2019-04-07T14:39:25"
                + ("+08:00" if source == "native" else ""),
                "contact_id": "wechat:contact:" + _id("author"),
                "record_id": f"{source}-record-{ordinal}",
                "sender_id": "wxid_source_author",
                "sender_label": "Literal ⻩ label; preserve  two spaces",
            }
            contexts.append(context)
        members[preferred] = contexts
        mapping[alternate] = preferred
    return {
        "version": events.VERSION,
        "fingerprint": _id("pinned-source-fingerprint"),
        "session": "wechat:" + _id("session"),
        "original_count": 6,
        "display_count": 3,
        "preferred_by_id": mapping,
        "members_by_preferred": members,
        "count_delta": 3,
    }


def _write(path, value):
    path.write_text(events._json(value) + "\n", encoding="utf-8")


def test_compact_roundtrip_preserves_every_source_field_and_json_type(tmp_path):
    proof = _proof()
    context = next(iter(proof["members_by_preferred"].values()))[0]
    context.update(
        event_kind="quoted_reply",
        quoted_original_id=_id("original"),
        quoted_contact_id="wechat:contact:" + _id("quoted-author"),
        future_metadata={"literal": "中文 ⻩;  spaces\n", "active": False, "count": 3},
    )
    context["future_metadata"].update(optional=None, values=[1, "1", True])
    original = deepcopy(proof)
    packed = events._pack_cache(proof)
    assert len(packed["context_sources"]) == 2
    assert packed["cache_encoding"] == events._CACHE_ENCODING
    assert "中文 ⻩" in events._json(packed)
    assert proof == original
    path = tmp_path / "proof.json"
    _write(path, packed)
    restored = events._read_cache(path, proof["fingerprint"], proof["session"])
    assert restored == proof
    assert "cache_encoding" not in restored
    assert "context_sources" not in restored


def test_verbose_legacy_cache_is_read_without_conversion_or_data_loss(tmp_path):
    proof = _proof()
    path = tmp_path / "legacy.json"
    _write(path, proof)
    before = path.read_bytes()
    assert events._read_cache(path, proof["fingerprint"], proof["session"]) == proof
    assert path.read_bytes() == before


def test_incomplete_legacy_context_stays_verbose_and_readable(tmp_path):
    proof = _proof()
    for group in proof["members_by_preferred"].values():
        for context in group:
            identifier = context["id"]
            context.clear()
            context.update(id=identifier, future_metadata={"value": False})
    assert events._pack_cache(proof) is proof
    path = tmp_path / "legacy-minimum.json"
    _write(path, proof)
    assert events._read_cache(path, proof["fingerprint"], proof["session"]) == proof


@pytest.mark.parametrize(
    "damage",
    [
        "unknown_encoding",
        "missing_encoding",
        "missing_sources",
        "nonarray_sources",
        "short_source",
        "nonarray_group",
        "nonarray_member",
        "short_member",
        "long_member",
        "boolean_index",
        "float_index",
        "negative_index",
        "out_of_range_index",
        "nonobject_extra",
        "extra_id_collision",
        "extra_source_collision",
        "invalid_id",
        "stale_fingerprint",
    ],
)
def test_malformed_compact_cache_is_not_accepted(tmp_path, damage):
    proof = _proof()
    packed = events._pack_cache(proof)
    preferred, group = next(iter(packed["members_by_preferred"].items()))
    row = group[0]
    if damage == "unknown_encoding":
        packed["cache_encoding"] = "source-context-array-v999"
    elif damage == "missing_encoding":
        packed.pop("cache_encoding")
    elif damage == "missing_sources":
        packed.pop("context_sources")
    elif damage == "nonarray_sources":
        packed["context_sources"] = {}
    elif damage == "short_source":
        packed["context_sources"][0].pop()
    elif damage == "nonarray_group":
        packed["members_by_preferred"][preferred] = {}
    elif damage == "nonarray_member":
        group[0] = {"id": row[0]}
    elif damage == "short_member":
        row.pop()
    elif damage == "long_member":
        row.extend([{}, "unexpected"])
    elif damage in {
        "boolean_index",
        "float_index",
        "negative_index",
        "out_of_range_index",
    }:
        row[1] = {
            "boolean_index": True,
            "float_index": 0.0,
            "negative_index": -1,
            "out_of_range_index": len(packed["context_sources"]),
        }[damage]
    elif damage == "nonobject_extra":
        row.append([])
    elif damage == "extra_id_collision":
        row.append({"id": _id("wrong-original")})
    elif damage == "extra_source_collision":
        row.append({"source_id": "different-owner"})
    elif damage == "invalid_id":
        row[0] = "not-a-message-id"
    elif damage == "stale_fingerprint":
        packed["fingerprint"] = _id("different-source-fingerprint")
    path = tmp_path / "invalid.json"
    _write(path, packed)
    assert events._read_cache(path, proof["fingerprint"], proof["session"]) is None


def _bounded_fixture(fixture):
    connection, result = fixtures._run(fixture)
    connection.close()
    payload = {key: value for key, value in result.items() if key != "status"}
    verbose_bytes = len((events._json(payload) + "\n").encode())
    packed_bytes = len((events._json(events._pack_cache(payload)) + "\n").encode())
    assert packed_bytes < verbose_bytes
    events._cache_path(fixture.repository, fixtures.SESSION).unlink()
    return result, verbose_bytes, packed_bytes


def test_packed_proof_publishes_when_verbose_proof_exceeds_same_budget(
    event_store, monkeypatch
):
    fixture = event_store
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    expected, verbose_bytes, packed_bytes = _bounded_fixture(fixture)
    budget = (verbose_bytes + packed_bytes) // 2
    monkeypatch.setattr(events, "MAX_CACHE_BYTES", budget)
    connection, actual = fixtures._run(fixture)
    connection.close()
    assert actual == expected
    cache = events._cache_path(fixture.repository, fixtures.SESSION)
    assert cache.stat().st_size == packed_bytes <= budget < verbose_bytes
    assert '"cache_encoding":"source-context-array-v1"' in cache.read_text()
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )
    monkeypatch.setattr(
        events, "_scan", lambda *_args: pytest.fail("Published proof must be reused.")
    )
    connection, cached = fixtures._run(fixture)
    connection.close()
    assert cached == expected
    assert events.cache_only_summaries(fixture.repository) == {fixtures.SESSION: 3}


def test_packed_proof_still_respects_the_actual_file_budget(event_store, monkeypatch):
    fixture = event_store
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    _, _, packed_bytes = _bounded_fixture(fixture)
    monkeypatch.setattr(events, "MAX_CACHE_BYTES", packed_bytes - 1)
    connection, result = fixtures._run(fixture)
    connection.close()
    assert result["status"] == "bounded"
    assert result["original_count"] == result["display_count"] == 6
    assert result["preferred_by_id"] == {}
    assert not events._cache_path(fixture.repository, fixtures.SESSION).exists()
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )
