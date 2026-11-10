"""Lossless proof transport keeps legacy cells and rejects partial or forged data.

Code version: v1.0.2-codex.0
"""

import hashlib
import json
import sqlite3
import sys
import zlib

import pytest


from app.core import wechat_completion_json_codec as codec


@pytest.fixture(autouse=True)
def clear_cache():
    codec.clear_json_codec_cache()
    yield
    codec.clear_json_codec_cache()


def _frame(raw, *, magic=codec.MAGIC, length=None, digest=None):
    return codec.HEADER.pack(
        magic,
        len(raw) if length is None else length,
        hashlib.sha256(raw).digest() if digest is None else digest,
    ) + zlib.compress(raw)


def test_canonical_text_and_proof_hash_survive_compression_exactly():
    text = json.dumps(
        {"provenance": ["Synthetic 中文 source with repeated evidence"] * 200},
        ensure_ascii=False,
        separators=(", ", ": "),
    )
    encoded = codec.encode_json_cell(text)
    assert isinstance(encoded, bytes)
    assert len(encoded) < len(text.encode()) / 10
    decoded = codec.decode_json_cell(encoded)
    assert decoded == text
    assert (
        hashlib.sha256(decoded.encode()).digest()
        == hashlib.sha256(text.encode()).digest()
    )
    assert codec.load_json_cell(encoded) == json.loads(text)


@pytest.mark.parametrize("value", [None, "{}", '{"literal": "no compression needed"}'])
def test_legacy_text_null_and_small_cells_remain_unchanged(value):
    assert codec.encode_json_cell(value) == value
    assert codec.decode_json_cell(value) == value


@pytest.mark.parametrize("value", [23, b'{"not":"a framed cell"}', bytearray(b"x")])
def test_undeclared_binary_and_nontext_cells_are_not_guessed(value):
    with pytest.raises(ValueError):
        codec.decode_json_cell(value)


@pytest.mark.parametrize(
    "frame",
    [
        _frame(b"{}", magic=b"ACJ2"),
        _frame(b"{}", length=1),
        _frame(b"{}", length=3),
        _frame(b"{}", digest=b"x" * 32),
        _frame(b"{}")[:-1],
        _frame(b"{}") + b"trailing bytes",
        _frame(b"{}") + zlib.compress(b"{}"),
        _frame(b"\xff"),
        _frame(b"{}", length=codec.MAX_DECODED_BYTES + 1),
    ],
)
def test_partial_trailing_forged_and_oversized_frames_fail_closed(frame):
    with pytest.raises(ValueError):
        codec.decode_json_cell(frame)
    assert not codec._CACHE


def test_decoded_length_bounds_prevent_a_compressed_expansion_bomb():
    frame = _frame(b"x" * (2 * 1024**2), length=4)
    with pytest.raises(ValueError):
        codec.decode_json_cell(frame)
    assert not codec._CACHE


def test_legacy_large_text_is_preserved_instead_of_newly_rejected(monkeypatch):
    monkeypatch.setattr(codec, "MAX_DECODED_BYTES", 12)
    text = '{"source":"' + "x" * 100 + '"}'
    assert codec.encode_json_cell(text) is text
    assert codec.decode_json_cell(text) is text


def test_cache_uses_bounded_resident_bytes_and_entries(monkeypatch):
    monkeypatch.setattr(codec, "MAX_CACHE_BYTES", 500)
    monkeypatch.setattr(codec, "MAX_CACHE_ENTRIES", 2)
    for index in range(12):
        text = json.dumps({"source": str(index) * 100})
        assert codec.decode_json_cell(_frame(text.encode())) == text
        assert codec._CACHE_BYTES <= 500
        assert len(codec._CACHE) <= 2


def test_mixed_width_unicode_cache_budget_counts_actual_python_storage(monkeypatch):
    monkeypatch.setattr(codec, "MAX_CACHE_BYTES", 20_000)
    text = "x" * 10_000 + "\U0001f600"
    frame = _frame(text.encode())
    assert len(text.encode()) + len(frame) < codec.MAX_CACHE_BYTES
    assert sys.getsizeof(text) > codec.MAX_CACHE_BYTES
    assert codec.decode_json_cell(frame) == text
    assert not codec._CACHE
    assert codec._CACHE_BYTES == 0


def test_cached_cells_charge_their_python_value_allocations():
    text = "Synthetic source " * 40
    frame = _frame(text.encode())
    assert codec.decode_json_cell(frame) == text
    assert codec._CACHE_BYTES == (
        sys.getsizeof(frame) + sys.getsizeof(text) + codec.CACHE_ENTRY_OVERHEAD
    )


def test_sql_json_guards_and_provenance_return_the_original_strings():
    original = json.dumps({"sender_id": "verified_source", "details": "x" * 1000})
    encoded = codec.encode_json_cell(original)
    with sqlite3.connect(":memory:") as connection:
        codec.register_json_codec(connection)
        connection.execute("CREATE TABLE examples(value)")
        connection.executemany(
            "INSERT INTO examples VALUES(?)", [(original,), (encoded,), (None,)]
        )
        assert connection.execute(
            "SELECT _wechat_completion_json_v1(value) FROM examples"
        ).fetchall() == [(original,), (original,), (None,)]
        assert (
            connection.execute(
                "SELECT count(*) FROM examples WHERE json_extract("
                "_wechat_completion_json_v1(value),'$.sender_id')='verified_source'"
            ).fetchone()[0]
            == 2
        )
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "SELECT _wechat_completion_json_v1(?)", (b"forged",)
            ).fetchone()
