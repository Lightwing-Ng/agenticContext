"""Store derived JSON cells losslessly without duplicating their expanded bytes.

Code version: v1.0.1-codex.0

This transport never changes canonical JSON text or proof hashes. Existing TEXT
cells remain valid; only the private review compactor opts into encoded cells.
"""

from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import struct
import sys
from threading import RLock
import zlib


VERSION = "v1.0.1-codex.0"
SQL_FUNCTION = "_wechat_completion_json_v1"
MAGIC = b"ACJ1"
HEADER = struct.Struct(">4sQ32s")
MAX_DECODED_BYTES = 16 * 1024**2
MAX_CACHE_BYTES = 8 * 1024**2
MAX_CACHE_ENTRIES = 128
CACHE_ENTRY_OVERHEAD = 256
_CACHE = OrderedDict()
_CACHE_BYTES = 0
_LOCK = RLock()


def encode_json_cell(value):
    """Encode only a bounded cell whose complete framed representation is smaller."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("A derived JSON encoder requires original text.")
    raw = value.encode("utf-8")
    if len(raw) > MAX_DECODED_BYTES:
        return value
    compressed = zlib.compress(raw, level=1)
    framed = HEADER.pack(MAGIC, len(raw), hashlib.sha256(raw).digest()) + compressed
    return framed if len(framed) < len(raw) else value


def decode_json_cell(value):
    """Recover exact text from one complete, length-bound and hash-bound frame."""
    global _CACHE_BYTES

    if value is None or isinstance(value, str):
        return value
    if not isinstance(value, bytes) or not HEADER.size < len(value) <= MAX_DECODED_BYTES:
        raise ValueError("A derived JSON cell has an unsupported storage frame.")
    with _LOCK:
        if value in _CACHE:
            text, _size = _CACHE[value]
            _CACHE.move_to_end(value)
            return text
    magic, expected_length, expected_hash = HEADER.unpack(value[: HEADER.size])
    if magic != MAGIC or expected_length > MAX_DECODED_BYTES:
        raise ValueError("A derived JSON frame exceeds its declared format or bounds.")
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(value[HEADER.size :], expected_length + 1)
        if (
            len(raw) != expected_length
            or not decoder.eof
            or decoder.unused_data
            or decoder.unconsumed_tail
            or hashlib.sha256(raw).digest() != expected_hash
        ):
            raise ValueError("A derived JSON frame changed its length, bytes or checksum.")
        text = raw.decode("utf-8")
    except (zlib.error, UnicodeError) as error:
        raise ValueError("A derived JSON frame is incomplete or invalid UTF-8.") from error
    # Unicode storage width can exceed UTF-8 size for mixed-width text.
    size = sys.getsizeof(value) + sys.getsizeof(text) + CACHE_ENTRY_OVERHEAD
    if size <= MAX_CACHE_BYTES:
        with _LOCK:
            previous = _CACHE.pop(value, None)
            if previous is not None:
                _CACHE_BYTES -= previous[1]
            _CACHE[value] = text, size
            _CACHE_BYTES += size
            while len(_CACHE) > MAX_CACHE_ENTRIES or _CACHE_BYTES > MAX_CACHE_BYTES:
                _CACHE_BYTES -= _CACHE.popitem(last=False)[1][1]
    return text


def load_json_cell(value):
    """Parse JSON only after the storage transport has been completely verified."""
    text = decode_json_cell(value)
    return json.loads(text) if text is not None else None


def register_json_codec(connection):
    """Expose the same decoder to verified SQL views and their activity guards."""
    connection.create_function(SQL_FUNCTION, 1, decode_json_cell, deterministic=True)


def clear_json_codec_cache():
    """Release only derived in-memory transport results."""
    global _CACHE_BYTES

    with _LOCK:
        _CACHE.clear()
        _CACHE_BYTES = 0
