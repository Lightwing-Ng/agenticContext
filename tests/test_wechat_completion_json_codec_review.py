"""Independent frame-boundary and concurrent cache regressions.

Code version: v1.0.1-codex.0
"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import sys
from threading import Barrier
import zlib

import pytest


from app.core import wechat_completion_json_codec as codec


@pytest.fixture(autouse=True)
def empty_cache():
    codec.clear_json_codec_cache()
    yield
    codec.clear_json_codec_cache()


def frame(raw):
    return codec.HEADER.pack(
        codec.MAGIC, len(raw), hashlib.sha256(raw).digest()
    ) + zlib.compress(raw)


def test_zlib_checksum_damage_is_rejected_even_with_the_correct_raw_hash():
    valid = frame(b'{"proof":"retained literal source"}')
    corrupt = valid[:-1] + bytes([valid[-1] ^ 1])
    with pytest.raises(ValueError):
        codec.decode_json_cell(corrupt)
    assert not codec._CACHE


def test_exact_decoded_boundary_is_accepted_and_one_more_byte_is_rejected(monkeypatch):
    monkeypatch.setattr(codec, "MAX_DECODED_BYTES", 512)
    assert codec.decode_json_cell(frame(b"x" * 512)) == "x" * 512
    codec.clear_json_codec_cache()
    with pytest.raises(ValueError):
        codec.decode_json_cell(frame(b"x" * 513))
    assert not codec._CACHE


def test_concurrent_misses_for_the_same_frame_do_not_double_count_cache_bytes(
    monkeypatch,
):
    text = "Same retained proof " * 100
    encoded = frame(text.encode())
    barrier = Barrier(8)
    original = zlib.decompressobj

    class CoordinatedDecoder:
        def __init__(self):
            self.decoder = original()

        def decompress(self, *args):
            barrier.wait(timeout=5)
            return self.decoder.decompress(*args)

        def __getattr__(self, name):
            return getattr(self.decoder, name)

    monkeypatch.setattr(codec.zlib, "decompressobj", CoordinatedDecoder)
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert list(executor.map(codec.decode_json_cell, [encoded] * 8)) == [text] * 8
    with codec._LOCK:
        assert len(codec._CACHE) == 1
        assert codec._CACHE_BYTES == sum(item[1] for item in codec._CACHE.values())
        assert codec._CACHE_BYTES <= codec.MAX_CACHE_BYTES


def test_concurrent_eviction_and_clear_keep_the_exact_text_and_accounting(monkeypatch):
    monkeypatch.setattr(codec, "MAX_CACHE_BYTES", 1600)
    monkeypatch.setattr(codec, "MAX_CACHE_ENTRIES", 3)
    texts = [str(index) + " Source evidence 中文 😀 " * 10 for index in range(17)]
    encoded = [frame(text.encode()) for text in texts]

    def replay(worker):
        for offset in range(60):
            index = (worker + offset) % len(texts)
            assert codec.decode_json_cell(encoded[index]) == texts[index]
            if offset % 11 == 0:
                codec.clear_json_codec_cache()

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(replay, range(16)))
    with codec._LOCK:
        assert codec._CACHE_BYTES == sum(item[1] for item in codec._CACHE.values())
        assert 0 <= codec._CACHE_BYTES <= codec.MAX_CACHE_BYTES
        assert len(codec._CACHE) <= codec.MAX_CACHE_ENTRIES


def test_unicode_cache_budget_counts_the_resident_string_representation(monkeypatch):
    text = "x" * 2000 + "😀"
    encoded = frame(text.encode())
    resident_size = sys.getsizeof(text) + sys.getsizeof(encoded)
    utf8_size = len(text.encode()) + len(encoded)
    assert resident_size > utf8_size * 3
    monkeypatch.setattr(codec, "MAX_CACHE_BYTES", resident_size - 1)
    assert codec.decode_json_cell(encoded) == text
    assert not codec._CACHE
    assert codec._CACHE_BYTES == 0
