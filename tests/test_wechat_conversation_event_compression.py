"""Large event proofs remain lossless and bounded through compressed transport.

Code version: v1.0.1-codex.0
"""

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import zlib

import pytest

from app.core import wechat_conversation_events as events
from tests import test_wechat_conversation_event_cache as cache_fixtures
from tests import test_wechat_conversation_events as fixtures


event_store = fixtures.event_store


def _envelope(payload):
    return {
        "cache_transport": events._COMPRESSED_TRANSPORT,
        "decoded_bytes": len(payload),
        "decoded_sha256": hashlib.sha256(payload).hexdigest(),
        "data": base64.b64encode(zlib.compress(payload)).decode("ascii"),
    }


def _payload(proof):
    return (events._json(events._pack_cache(proof)) + "\n").encode()


def test_compressed_roundtrip_preserves_source_roles_and_future_fields(
    tmp_path, monkeypatch
):
    proof = cache_fixtures._proof()
    member = next(iter(proof["members_by_preferred"].values()))[0]
    member.update(
        event_kind="quoted_reply",
        quoted_original_id="a" * 64,
        quoted_sender_id="legacy_native_author",
        quoted_server_id="8904644184295715244",
        future={"literal": "中文  two spaces\n\n", "values": [True, None, 3]},
    )
    before = deepcopy(proof)
    monkeypatch.setattr(events, "MAX_CACHE_BYTES", 64)
    monkeypatch.setattr(events, "_COMPRESSION_THRESHOLD_BYTES", 0)
    encoded = events._encode_cache(proof)
    assert json.loads(encoded)["cache_transport"] == events._COMPRESSED_TRANSPORT
    path = tmp_path / "proof.json"
    path.write_text(encoded)
    assert events._read_cache(path, proof["fingerprint"], proof["session"]) == proof
    assert proof == before


def test_small_proof_stays_in_unchanged_json_transport():
    proof = cache_fixtures._proof()
    assert events._encode_cache(proof) == _payload(proof).decode()


def test_compressed_publication_is_reused_without_source_rescan(
    event_store, monkeypatch
):
    before = event_store.repository.path.read_bytes(), event_store.registry.read_bytes()
    monkeypatch.setattr(events, "MAX_CACHE_BYTES", 64)
    monkeypatch.setattr(events, "_COMPRESSION_THRESHOLD_BYTES", 0)
    connection, proof = fixtures._run(event_store)
    connection.close()
    assert proof["status"] == "ready"
    path = events._cache_path(event_store.repository, fixtures.SESSION)
    assert (
        json.loads(path.read_text())["cache_transport"] == events._COMPRESSED_TRANSPORT
    )
    monkeypatch.setattr(
        events, "_scan", lambda *_: pytest.fail("Do not rescan accepted sources.")
    )
    connection, cached = fixtures._run(event_store)
    connection.close()
    assert cached == proof
    assert events.cache_only_summaries(event_store.repository) == {fixtures.SESSION: 3}
    assert before == (
        event_store.repository.path.read_bytes(),
        event_store.registry.read_bytes(),
    )


@pytest.mark.parametrize(
    "damage",
    [
        "encoding",
        "extra_field",
        "missing_digest",
        "boolean_size",
        "float_size",
        "negative_size",
        "zero_size",
        "larger_size",
        "smaller_size",
        "wrong_digest",
        "invalid_digest",
        "invalid_base64",
        "nonstring_data",
        "invalid_stream",
        "truncated",
        "trailing",
        "concatenated",
        "invalid_json",
        "invalid_utf8",
        "nested",
        "deep_json",
        "array_proof",
    ],
)
def test_malformed_compressed_proofs_are_rejected(tmp_path, damage):
    proof = cache_fixtures._proof()
    value = _envelope(_payload(proof))
    if damage == "encoding":
        value["cache_transport"] = "zlib-json-v999"
    elif damage == "extra_field":
        value["extra"] = True
    elif damage == "missing_digest":
        value.pop("decoded_sha256")
    elif damage == "boolean_size":
        value["decoded_bytes"] = True
    elif damage == "float_size":
        value["decoded_bytes"] = float(value["decoded_bytes"])
    elif damage == "negative_size":
        value["decoded_bytes"] = -1
    elif damage == "zero_size":
        value["decoded_bytes"] = 0
    elif damage == "larger_size":
        value["decoded_bytes"] += 1
    elif damage == "smaller_size":
        value["decoded_bytes"] -= 1
    elif damage == "wrong_digest":
        value["decoded_sha256"] = "0" * 64
    elif damage == "invalid_digest":
        value["decoded_sha256"] = "not-a-digest"
    elif damage == "invalid_base64":
        value["data"] = "!invalid!"
    elif damage == "nonstring_data":
        value["data"] = []
    elif damage == "invalid_stream":
        value["data"] = base64.b64encode(b"not-zlib").decode()
    elif damage in {"truncated", "trailing", "concatenated"}:
        raw = base64.b64decode(value["data"])
        raw = (
            raw[:-1]
            if damage == "truncated"
            else raw + (b"extra" if damage == "trailing" else raw)
        )
        value["data"] = base64.b64encode(raw).decode()
    elif damage == "invalid_json":
        value = _envelope(b"{")
    elif damage == "invalid_utf8":
        value = _envelope(b"\xff")
    elif damage == "nested":
        value = _envelope(events._json(value).encode())
    elif damage == "deep_json":
        value = _envelope(("[" * 2000 + "0" + "]" * 2000).encode())
    elif damage == "array_proof":
        value = _envelope(b"[]")
    path = tmp_path / "proof.json"
    path.write_text(events._json(value))
    assert events._read_cache(path, proof["fingerprint"], proof["session"]) is None


def test_declared_decode_limit_rejects_before_decompression(tmp_path, monkeypatch):
    proof = cache_fixtures._proof()
    value = _envelope(_payload(proof))
    monkeypatch.setattr(events, "MAX_DECODED_CACHE_BYTES", value["decoded_bytes"] - 1)
    monkeypatch.setattr(
        events.zlib,
        "decompressobj",
        lambda: pytest.fail("Reject the claim before decoding."),
    )
    path = tmp_path / "proof.json"
    path.write_text(events._json(value))
    assert events._read_cache(path, proof["fingerprint"]) is None


def test_expansion_beyond_claim_is_bounded_and_rejected(tmp_path):
    proof = cache_fixtures._proof()
    value = _envelope(b"x" * 1_000_000)
    value["decoded_bytes"] = 8
    path = tmp_path / "proof.json"
    path.write_text(events._json(value))
    assert events._read_cache(path, proof["fingerprint"]) is None


def test_publication_respects_both_compressed_and_decoded_limits(monkeypatch):
    proof = cache_fixtures._proof()
    monkeypatch.setattr(events, "MAX_CACHE_BYTES", 64)
    monkeypatch.setattr(events, "_COMPRESSION_THRESHOLD_BYTES", 0)
    encoded = events._encode_cache(proof)
    monkeypatch.setattr(events, "MAX_COMPRESSED_CACHE_BYTES", len(encoded.encode()) - 1)
    assert events._encode_cache(proof) is None
    monkeypatch.setattr(events, "MAX_COMPRESSED_CACHE_BYTES", 64 * 1024**2)
    monkeypatch.setattr(events, "MAX_DECODED_CACHE_BYTES", len(_payload(proof)) - 1)
    assert events._encode_cache(proof) is None


def test_deep_ordinary_json_is_rejected(tmp_path):
    path = tmp_path / "deep.json"
    path.write_text("[" * 2000 + "0" + "]" * 2000)
    assert events._read_cache(path, "0" * 64) is None


def test_summary_skips_oversized_entries_without_losing_later_proof(
    event_store, monkeypatch
):
    connection, proof = fixtures._run(event_store)
    connection.close()
    directory = events._directory(event_store.repository)
    oversized = directory / "oversized.json"
    with oversized.open("wb") as stream:
        stream.truncate(events.MAX_COMPRESSED_CACHE_BYTES + 1)
    original = type(directory).glob

    def ordered_glob(path, pattern):
        return iter(
            sorted(
                original(path, pattern),
                key=lambda entry: entry.name != "oversized.json",
            )
        )

    monkeypatch.setattr(type(directory), "glob", ordered_glob)
    assert events.cache_only_summaries(event_store.repository) == {proof["session"]: 3}


def test_read_rejects_file_identity_change_since_budget_charge(tmp_path):
    proof = cache_fixtures._proof()
    path = tmp_path / "proof.json"
    path.write_bytes(_payload(proof))
    before = events._identity(path)
    path.write_bytes(_payload(proof) + b" ")
    assert (
        events._read_cache(path, proof["fingerprint"], expected_identity=before) is None
    )


def test_summary_budget_skips_a_large_entry_but_accepts_a_later_small_one(
    event_store, monkeypatch
):
    connection, proof = fixtures._run(event_store)
    connection.close()
    directory = events._directory(event_store.repository)
    paths, sessions, sizes = [], [], []
    for index in range(3):
        value = {key: item for key, item in proof.items() if key != "status"}
        value["session"] = "wechat:" + hashlib.sha256(str(index).encode()).hexdigest()
        if index == 1:
            value["future_metadata"] = "x" * 1_000
        path = directory / f"proof-{index}.json"
        path.write_text(events._json(events._pack_cache(value)))
        paths.append(path)
        sessions.append(value["session"])
        sizes.append(path.stat().st_size)
    budget = sizes[0] + sizes[2] + 2
    assert sizes[1] < budget
    monkeypatch.setattr(events, "MAX_SUMMARY_SCAN_BYTES", budget)
    monkeypatch.setattr(Path, "glob", lambda *_: iter(paths))
    assert events.cache_only_summaries(event_store.repository) == {
        sessions[0]: 3,
        sessions[2]: 3,
    }


def test_file_growth_during_open_cannot_expand_the_pinned_read_budget(
    tmp_path, monkeypatch
):
    proof = cache_fixtures._proof()
    path = tmp_path / "proof.json"
    path.write_bytes(_payload(proof))
    pinned_size = path.stat().st_size
    original_open = Path.open
    requests = []

    class GrowingReader:
        def __enter__(self):
            with original_open(path, "ab") as stream:
                stream.write(b" " * 1_000_000)
            self.stream = original_open(path, "rb")
            return self

        def read(self, size):
            requests.append(size)
            return self.stream.read(size)

        def __exit__(self, *_args):
            self.stream.close()

    def racing_open(target, mode="r", *args, **kwargs):
        if target == path and mode == "rb":
            return GrowingReader()
        return original_open(target, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)
    assert events._read_cache(path, proof["fingerprint"]) is None
    assert requests == [pinned_size + 1]
