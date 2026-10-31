"""Decode bounded WeChat WCDB text without opening media or attachment files.

Code version: v1.0.0-codex.0

Plaintext works on Python 3.13. Compressed cells require Python 3.14's optional
``compression.zstd`` standard-library backend and, when applicable, the exact
dictionary identified by the frame. No backend is imported for plaintext.
"""

from __future__ import annotations

from collections.abc import Mapping
import importlib
from typing import Any


MAX_TEXT_BYTES = 2 * 1024 * 1024


class WCDBDecodeError(ValueError):
    """The cell cannot be decoded faithfully within the configured bounds."""


class WCDBBackendUnavailableError(RuntimeError):
    """Compressed text requires a runtime with the standard Zstd backend."""


class MissingDictionaryError(WCDBDecodeError):
    """A valid dictionary-compressed frame needs an unavailable dictionary."""

    def __init__(self, dictionary_id: int):
        self.dictionary_id = dictionary_id
        super().__init__(f"Required Zstd dictionary {dictionary_id} is unavailable")


def _zstd_backend() -> Any:
    try:
        return importlib.import_module("compression.zstd")
    except ImportError as error:
        raise WCDBBackendUnavailableError(
            "Compressed WCDB text requires Python 3.14 with compression.zstd"
        ) from error


def _blob(value: Any) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError("Expected SQLite TEXT or BLOB")
    return bytes(value)


def _frame_info(backend: Any, raw: bytes) -> Any:
    try:
        return backend.get_frame_info(raw)
    except (backend.ZstdError, ValueError) as error:
        raise WCDBDecodeError("Invalid or incomplete Zstd frame header") from error


def _utf8(raw: bytes, limit: int) -> str:
    if len(raw) > limit:
        raise WCDBDecodeError("Text exceeds the output byte limit")
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise WCDBDecodeError("Text is not valid UTF-8") from error


def get_wcdb_dictionary_id(value: bytes | bytearray | memoryview) -> int:
    """Inspect a Zstd frame; return zero for a frame without a dictionary.

    This helper requires the compressed-text backend. It does not infer a frame
    from ordinary plaintext or a WCDB marker.
    """
    raw = _blob(value)
    backend = _zstd_backend()
    return _frame_info(backend, raw).dictionary_id


def decode_wcdb_text(
    value: Any,
    marker: int,
    dictionaries: Mapping[int, Any] | None = None,
    limit: int = MAX_TEXT_BYTES,
) -> str:
    """Decode a cell already identified by its caller as a textual message.

    WCDB stores ``(compression_type << 1) | original_blob_bit``: markers 0/1
    are plain, 2/3 use a Zstd dictionary, and 4/5 use normal Zstd. SQLite TEXT
    is already decompressed, matching WCDB's own decompression function.
    Dictionary values may be backend ZstdDict objects or dictionary bytes.
    Unknown markers, missing dictionaries, malformed frames, extra frames,
    invalid UTF-8, and oversized output raise explicitly; no replacement text
    or partial output is returned.
    """
    if (
        isinstance(marker, bool)
        or not isinstance(marker, int)
        or marker not in range(6)
    ):
        raise WCDBDecodeError("Unknown WCDB compression marker")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("The output byte limit must be a nonnegative integer")
    if isinstance(value, str):
        if len(value) > limit:
            raise WCDBDecodeError("Text exceeds the output byte limit")
        try:
            raw_text = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise WCDBDecodeError("Text is not valid UTF-8") from error
        if len(raw_text) > limit:
            raise WCDBDecodeError("Text exceeds the output byte limit")
        return value

    raw = _blob(value)
    if marker < 2:
        return _utf8(raw, limit)

    backend = _zstd_backend()
    frame = _frame_info(backend, raw)
    if frame.decompressed_size is not None and frame.decompressed_size > limit:
        raise WCDBDecodeError("Text exceeds the output byte limit")

    dictionary = None
    if marker in (2, 3):
        if not frame.dictionary_id:
            raise WCDBDecodeError("Dictionary-compressed frame has no dictionary ID")
        if dictionaries is not None and not isinstance(dictionaries, Mapping):
            raise TypeError("Dictionaries must map frame IDs to dictionary values")
        if dictionaries is None or frame.dictionary_id not in dictionaries:
            raise MissingDictionaryError(frame.dictionary_id)
        dictionary = dictionaries[frame.dictionary_id]
        try:
            if isinstance(dictionary, (bytes, bytearray, memoryview)):
                dictionary = backend.ZstdDict(bytes(dictionary))
            elif not isinstance(dictionary, backend.ZstdDict):
                raise TypeError("Expected ZstdDict or dictionary bytes")
            if dictionary.dict_id != frame.dictionary_id:
                raise WCDBDecodeError("Zstd dictionary ID does not match the frame")
        except (backend.ZstdError, ValueError) as error:
            raise WCDBDecodeError("Invalid or mismatched Zstd dictionary") from error
    elif frame.dictionary_id:
        raise WCDBDecodeError("Normal Zstd marker unexpectedly requires a dictionary")

    try:
        decoder = backend.ZstdDecompressor(
            zstd_dict=dictionary,
            options={backend.DecompressionParameter.window_log_max: 27},
        )
        result = decoder.decompress(raw, max_length=limit + 1)
    except (backend.ZstdError, ValueError) as error:
        raise WCDBDecodeError("Zstd decompression failed") from error
    if len(result) > limit or not decoder.eof or decoder.unused_data:
        raise WCDBDecodeError("Oversized, incomplete, or trailing Zstd frame data")
    return _utf8(result, limit)
