"""Verify faithful WCDB decoding and Python 3.13's explicit backend boundary.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from app.core import wechat_wcdb
from app.core.wechat_wcdb import (
    MissingDictionaryError,
    WCDBBackendUnavailableError,
    WCDBDecodeError,
    decode_wcdb_text,
    get_wcdb_dictionary_id,
)


@pytest.fixture
def no_backend(monkeypatch):
    def unavailable(name):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(wechat_wcdb.importlib, "import_module", unavailable)


@pytest.mark.parametrize("marker", range(6))
def test_plain_sqlite_text_is_exact_without_a_backend(marker, no_backend):
    assert decode_wcdb_text("  你好😀\r\nliteral <xml>  ", marker) == (
        "  你好😀\r\nliteral <xml>  "
    )


@pytest.mark.parametrize("marker", [0, 1])
@pytest.mark.parametrize("wrapper", [bytes, bytearray, memoryview])
def test_plain_utf8_blob_is_portable(marker, wrapper, no_backend):
    assert decode_wcdb_text(wrapper("你好\n".encode()), marker) == "你好\n"


@pytest.mark.parametrize("marker", [None, True, "2", -1, 6, 100])
def test_unknown_markers_never_silently_pass_plaintext(marker):
    with pytest.raises(WCDBDecodeError, match="marker"):
        decode_wcdb_text("message", marker)


@pytest.mark.parametrize("value", [None, 12, object()])
def test_non_sqlite_values_are_rejected(value):
    with pytest.raises(TypeError):
        decode_wcdb_text(value, 0)


@pytest.mark.parametrize("limit", [True, -1, 1.5, "10"])
def test_invalid_limits_are_rejected(limit):
    with pytest.raises(ValueError, match="limit"):
        decode_wcdb_text("", 0, limit=limit)


@pytest.mark.parametrize("value", ["你好", "你好".encode()])
def test_plaintext_limit_counts_utf8_bytes(value, no_backend):
    assert decode_wcdb_text(value, 0, limit=6) == "你好"
    with pytest.raises(WCDBDecodeError, match="limit"):
        decode_wcdb_text(value, 0, limit=5)


@pytest.mark.parametrize("value", [b"\xff", "\ud800"])
def test_invalid_unicode_is_not_replaced(value, no_backend):
    with pytest.raises(WCDBDecodeError, match="UTF-8"):
        decode_wcdb_text(value, 0)


def test_missing_backend_only_blocks_compressed_cells(no_backend):
    for marker in (2, 3, 4, 5):
        with pytest.raises(WCDBBackendUnavailableError, match="Python 3.14"):
            decode_wcdb_text(b"compressed", marker)
    with pytest.raises(WCDBBackendUnavailableError):
        get_wcdb_dictionary_id(b"compressed")


@pytest.fixture
def fake_backend(monkeypatch):
    class FakeZstdError(Exception):
        pass

    class FakeDictionary:
        def __init__(self, raw):
            if raw != b"dictionary-3":
                raise FakeZstdError("Invalid synthetic dictionary")
            self.dict_id = 3

    backend = SimpleNamespace(
        ZstdError=FakeZstdError,
        ZstdDict=FakeDictionary,
        DecompressionParameter=SimpleNamespace(window_log_max="window"),
        frame=SimpleNamespace(decompressed_size=None, dictionary_id=3),
        output="合成".encode(),
        eof=True,
        unused_data=b"",
        calls=[],
    )

    def frame_info(raw):
        if raw == b"bad-header":
            raise FakeZstdError("Invalid synthetic frame")
        return backend.frame

    class FakeDecompressor:
        def __init__(self, **kwargs):
            backend.calls.append(kwargs)
            self.eof = backend.eof
            self.unused_data = backend.unused_data

        def decompress(self, raw, *, max_length):
            backend.calls.append(max_length)
            if raw == b"bad-payload":
                raise FakeZstdError("Invalid synthetic payload")
            return backend.output[:max_length]

    backend.get_frame_info = frame_info
    backend.ZstdDecompressor = FakeDecompressor
    monkeypatch.setattr(wechat_wcdb, "_zstd_backend", lambda: backend)
    return backend


@pytest.mark.parametrize("marker", [2, 3])
@pytest.mark.parametrize("dictionary_bytes", [True, False])
def test_injected_backend_checks_dictionary_and_output(
    marker, dictionary_bytes, fake_backend
):
    dictionary = b"dictionary-3"
    if not dictionary_bytes:
        dictionary = fake_backend.ZstdDict(dictionary)
    assert decode_wcdb_text(b"frame", marker, {3: dictionary}, limit=6) == "合成"
    assert fake_backend.calls[0]["zstd_dict"].dict_id == 3
    assert fake_backend.calls[1] == 7
    assert get_wcdb_dictionary_id(b"frame") == 3


def test_injected_backend_preserves_missing_dictionary_id(fake_backend):
    with pytest.raises(MissingDictionaryError) as error:
        decode_wcdb_text(b"frame", 2)
    assert error.value.dictionary_id == 3
    assert not fake_backend.calls


def test_injected_backend_rejects_mismatched_or_invalid_dictionary(fake_backend):
    dictionary = fake_backend.ZstdDict(b"dictionary-3")
    dictionary.dict_id = 5
    for value in (dictionary, b"not-a-dictionary"):
        with pytest.raises(WCDBDecodeError, match="dictionary"):
            decode_wcdb_text(b"frame", 2, {3: value})
    with pytest.raises(TypeError, match="ZstdDict"):
        decode_wcdb_text(b"frame", 2, {3: object()})
    with pytest.raises(TypeError, match="map"):
        decode_wcdb_text(b"frame", 2, [])


def test_injected_backend_rejects_marker_frame_mismatch(fake_backend):
    with pytest.raises(WCDBDecodeError, match="Normal Zstd"):
        decode_wcdb_text(b"frame", 4)
    fake_backend.frame.dictionary_id = 0
    with pytest.raises(WCDBDecodeError, match="no dictionary ID"):
        decode_wcdb_text(b"frame", 2)
    assert decode_wcdb_text(b"frame", 4) == "合成"


@pytest.mark.parametrize(
    "change", ["declared-size", "actual-size", "incomplete", "trailing", "utf8"]
)
def test_injected_backend_rejects_unfaithful_output(change, fake_backend):
    fake_backend.frame.dictionary_id = 0
    if change == "declared-size":
        fake_backend.frame.decompressed_size = 7
    elif change == "actual-size":
        fake_backend.output = b"1234567"
    elif change == "incomplete":
        fake_backend.eof = False
    elif change == "trailing":
        fake_backend.unused_data = b"tail"
    else:
        fake_backend.output = b"\xff"
    with pytest.raises(WCDBDecodeError):
        decode_wcdb_text(b"frame", 4, limit=6)


@pytest.mark.parametrize("value", [b"bad-header", b"bad-payload"])
def test_injected_backend_wraps_malformed_frames(value, fake_backend):
    fake_backend.frame.dictionary_id = 0
    with pytest.raises(WCDBDecodeError):
        decode_wcdb_text(value, 4)


def test_native_backend_round_trips_and_rejects_malformed_cells():
    try:
        backend = importlib.import_module("compression.zstd")
    except ImportError:
        # Python 3.13 still verifies the explicit unsupported-runtime contract.
        with pytest.raises(WCDBBackendUnavailableError):
            decode_wcdb_text(bytes.fromhex("28b52ffd2000010000"), 4)
        return

    samples = [
        f"Synthetic archive message {index}: repeated phrase hello 中文 {index % 7}".encode()
        for index in range(200)
    ]
    trained = backend.train_dict(samples, 1024)
    dictionary_bytes = bytearray(trained.dict_content)
    dictionary_bytes[4:8] = (3).to_bytes(4, "little")
    dictionary = backend.ZstdDict(bytes(dictionary_bytes))
    text = "Synthetic archive message 600: repeated phrase hello 中文 5\n😀"
    normal = backend.compress(text.encode())
    compressed = backend.compress(text.encode(), zstd_dict=dictionary)
    assert get_wcdb_dictionary_id(normal) == 0
    assert get_wcdb_dictionary_id(compressed) == 3
    for marker in (2, 3):
        for value in (dictionary, bytes(dictionary_bytes)):
            assert decode_wcdb_text(compressed, marker, {3: value}) == text
    for marker in (4, 5):
        assert decode_wcdb_text(normal, marker) == text
    for invalid in (normal[:-1], normal + b"tail", normal + normal, b"garbage"):
        with pytest.raises(WCDBDecodeError):
            decode_wcdb_text(invalid, 4)
    with pytest.raises(WCDBDecodeError, match="limit"):
        decode_wcdb_text(normal, 4, limit=1)
    with pytest.raises(WCDBDecodeError, match="UTF-8"):
        decode_wcdb_text(backend.compress(b"\xff"), 4)
    with pytest.raises(MissingDictionaryError) as error:
        decode_wcdb_text(compressed, 2)
    assert error.value.dictionary_id == 3
