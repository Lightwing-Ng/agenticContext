"""Read supplied SQLCipher 4 bytes using authenticated pages and committed WAL.

Code version: v1.0.0-codex.0. Python 3.13+; AES uses macOS CommonCrypto only.
Derived from the locally verified mac_sqlcipher_read_v1 v1.0.1 reader.
This module performs no file, database, process, network, or key discovery I/O.
Callers must provide a stable main/WAL snapshot. Raw 32-byte keys are accepted;
passphrase derivation, SQLCipher compatibility modes, and SHM recovery are absent.
The 80 reserved bytes of every decrypted page are zeroed deliberately.
"""

import ctypes
import hashlib
import hmac
import struct
import sys

__version__ = "1.0.0-codex.0"
PAGE_SIZE = 4096
MAX_DATABASE_BYTES = 2 * 1024 * 1024 * 1024
MAX_WAL_BYTES = 64 * 1024 * 1024
_SQLITE_HEADER = b"SQLite format 3\x00"
_WAL_HEADER_SIZE = 32
_FRAME_SIZE = 24 + PAGE_SIZE
_UINT32_MASK = 0xFFFFFFFF
_COMMON_CRYPTO = None


class RecoveryError(ValueError):
    """Reject invalid, unauthenticated, unsupported, or ambiguous input bytes."""


def _require_bytes(value, description):
    if not isinstance(value, bytes):
        raise RecoveryError("Expected immutable bytes for " + description)


def _check_limits(max_database_bytes, max_wal_bytes):
    for value, minimum, description in (
        (max_database_bytes, PAGE_SIZE, "database limit"),
        (max_wal_bytes, _WAL_HEADER_SIZE, "WAL limit"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise RecoveryError("Invalid " + description)
    if max_database_bytes > MAX_DATABASE_BYTES or max_wal_bytes > MAX_WAL_BYTES:
        raise RecoveryError("Limits may be lowered but not exceed the safety caps")


def _valid_key(key32):
    if not isinstance(key32, bytes) or len(key32) != 32:
        raise RecoveryError("Expected a 32-byte raw key")


def _aes_cbc(key32, iv16, data, operation):
    """Use unpadded AES-256 CBC; operation 0 encrypts and 1 decrypts."""
    global _COMMON_CRYPTO
    _valid_key(key32)
    if len(iv16) != 16 or not data or len(data) % 16 or operation not in (0, 1):
        raise RecoveryError("Invalid AES-CBC parameters")
    if sys.platform != "darwin":
        raise RecoveryError("AES-CBC requires macOS CommonCrypto")
    if _COMMON_CRYPTO is None:
        try:
            library = ctypes.CDLL("/usr/lib/system/libcommonCrypto.dylib")
            function = library.CCCrypt
            function.argtypes = (
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_size_t),
            )
            function.restype = ctypes.c_int32
            _COMMON_CRYPTO = (library, function)
        except (OSError, AttributeError):
            raise RecoveryError("macOS CommonCrypto is unavailable") from None
    key_buffer = ctypes.create_string_buffer(key32, len(key32))
    iv_buffer = ctypes.create_string_buffer(iv16, len(iv16))
    input_buffer = ctypes.create_string_buffer(data, len(data))
    output_buffer = ctypes.create_string_buffer(len(data))
    moved = ctypes.c_size_t()
    try:
        status = _COMMON_CRYPTO[1](
            operation,
            0,
            0,
            key_buffer,
            len(key32),
            iv_buffer,
            input_buffer,
            len(data),
            output_buffer,
            len(data),
            ctypes.byref(moved),
        )
        if status != 0 or moved.value != len(data):
            raise RecoveryError("CommonCrypto AES-CBC operation failed")
        return output_buffer.raw
    finally:
        for buffer in (key_buffer, iv_buffer, input_buffer, output_buffer):
            ctypes.memset(ctypes.addressof(buffer), 0, ctypes.sizeof(buffer))


def _authentication_key(key32, db_salt16):
    salt = bytes(value ^ 0x3A for value in db_salt16)
    return hashlib.pbkdf2_hmac("sha512", key32, salt, 2, 32)


def _authenticate_page(authentication_key, encrypted4096, page_number):
    offset = 16 if page_number == 1 else 0
    authenticated = encrypted4096[offset:4032] + struct.pack("<I", page_number)
    expected = hmac.new(authentication_key, authenticated, hashlib.sha512).digest()
    if not hmac.compare_digest(expected, encrypted4096[4032:4096]):
        raise RecoveryError("Encrypted page authentication failed")


def decrypt_page(key32, encrypted4096, page_number, db_salt16):
    """Authenticate and decrypt one page; return exactly 4,096 plaintext bytes.

    Page one restores the SQLite signature. The 80 codec-reserved bytes are
    zeroed. The salt must come from the stable main database's first 16 bytes.
    """
    _valid_key(key32)
    _require_bytes(encrypted4096, "encrypted page")
    _require_bytes(db_salt16, "database salt")
    if len(encrypted4096) != PAGE_SIZE or len(db_salt16) != 16:
        raise RecoveryError("Invalid encrypted page or database salt length")
    if isinstance(page_number, bool) or not isinstance(page_number, int):
        raise RecoveryError("Invalid database page number")
    if not 1 <= page_number <= _UINT32_MASK:
        raise RecoveryError("Invalid database page number")
    if page_number == 1 and encrypted4096[:16] != db_salt16:
        raise RecoveryError("Page-one salt does not match the database salt")
    _authenticate_page(
        _authentication_key(key32, db_salt16), encrypted4096, page_number
    )
    start = 16 if page_number == 1 else 0
    plaintext = _aes_cbc(key32, encrypted4096[4016:4032], encrypted4096[start:4016], 1)
    prefix = _SQLITE_HEADER if page_number == 1 else b""
    return prefix + plaintext + bytes(80)


def _checksum(data, byte_order, seed=(0, 0)):
    """Compute SQLite's paired-word rolling WAL checksum."""
    if len(data) % 8:
        raise RecoveryError("Invalid WAL checksum input length")
    first, second = seed
    for left, right in struct.iter_unpack(byte_order + "II", data):
        first = (first + left + second) & _UINT32_MASK
        second = (second + right + first) & _UINT32_MASK
    return first, second


def _scan_wal(walbytes, max_database_bytes, max_wal_bytes):
    _require_bytes(walbytes, "WAL")
    _check_limits(max_database_bytes, max_wal_bytes)
    if len(walbytes) > max_wal_bytes:
        raise RecoveryError("WAL exceeds the configured size limit")
    metadata = {
        "code_version": __version__,
        "wal_present": bool(walbytes),
        "checksum_byte_order": None,
        "valid_frame_count": 0,
        "committed_frame_count": 0,
        "commit_count": 0,
        "last_commit_database_pages": None,
        "minimum_commit_database_pages": None,
        "overlay_page_count": 0,
        "uncommitted_valid_frame_count": 0,
        "tail_status": "none",
        "tail_offset": len(walbytes),
        "tail_bytes": 0,
        "complete": True,
        "completeness_scope": "wal_tail",
        "encrypted_pages_authenticated": False,
    }
    if not walbytes:
        return {}, metadata, []
    if len(walbytes) < _WAL_HEADER_SIZE:
        raise RecoveryError("Incomplete WAL header")
    magic, version, page_size = struct.unpack(">III", walbytes[:12])
    if magic not in (0x377F0682, 0x377F0683):
        raise RecoveryError("Invalid WAL magic")
    if version != 3007000 or page_size != PAGE_SIZE:
        raise RecoveryError("Unsupported WAL format or page size")
    byte_order = "<" if magic == 0x377F0682 else ">"
    metadata["checksum_byte_order"] = "little" if byte_order == "<" else "big"
    checksum = _checksum(walbytes[:24], byte_order)
    if checksum != struct.unpack(">II", walbytes[24:32]):
        raise RecoveryError("WAL header checksum mismatch")
    salts = walbytes[16:24]
    page_limit = max_database_bytes // PAGE_SIZE
    overlay = {}
    pending = {}
    valid_frames = []
    offset = _WAL_HEADER_SIZE
    while offset < len(walbytes):
        remaining = len(walbytes) - offset
        frame_header = walbytes[offset : offset + 24]
        if not any(frame_header) and not any(walbytes[offset:]):
            metadata.update(tail_status="zero_preallocation", complete=False)
            break
        if remaining < 24:
            metadata.update(tail_status="incomplete_frame_header", complete=False)
            break
        if frame_header[8:16] != salts:
            metadata.update(tail_status="stale_salt_tail", complete=False)
            break
        if remaining < _FRAME_SIZE:
            metadata.update(tail_status="incomplete_frame", complete=False)
            break
        page_number, database_pages = struct.unpack(">II", frame_header[:8])
        if not 1 <= page_number <= page_limit or database_pages > page_limit:
            raise RecoveryError(
                "WAL frame page number or commit size exceeds the limit"
            )
        page = walbytes[offset + 24 : offset + _FRAME_SIZE]
        next_checksum = _checksum(frame_header[:8], byte_order, checksum)
        next_checksum = _checksum(page, byte_order, next_checksum)
        if next_checksum != struct.unpack(">II", frame_header[16:24]):
            raise RecoveryError("Current-generation WAL frame checksum mismatch")
        checksum = next_checksum
        valid_frames.append((page_number, page))
        pending[page_number] = page
        if database_pages:
            overlay.update(pending)
            overlay = {
                number: content
                for number, content in overlay.items()
                if number <= database_pages
            }
            pending.clear()
            metadata["commit_count"] += 1
            metadata["committed_frame_count"] = len(valid_frames)
            metadata["last_commit_database_pages"] = database_pages
            minimum = metadata["minimum_commit_database_pages"]
            metadata["minimum_commit_database_pages"] = (
                database_pages if minimum is None else min(minimum, database_pages)
            )
        offset += _FRAME_SIZE
    metadata["valid_frame_count"] = len(valid_frames)
    metadata["uncommitted_valid_frame_count"] = (
        len(valid_frames) - metadata["committed_frame_count"]
    )
    metadata["overlay_page_count"] = len(overlay)
    metadata["tail_offset"] = offset
    metadata["tail_bytes"] = len(walbytes) - offset
    if metadata["tail_status"] == "none" and metadata["uncommitted_valid_frame_count"]:
        metadata["tail_status"] = "uncommitted_frames"
    return overlay, metadata, valid_frames[: metadata["committed_frame_count"]]


def parse_wal(
    walbytes, *, max_database_bytes=MAX_DATABASE_BYTES, max_wal_bytes=MAX_WAL_BYTES
):
    """Return the last committed encrypted overlay and nonsecret WAL metadata.

    Both checksum byte orders are supported. Valid uncommitted frames are
    ignored. All-zero preallocation and a suffix with older-generation salts
    terminate the usable prefix and set complete=False. An incomplete frame
    also returns only the bounded committed prefix with complete=False; the
    missing bytes may include a later commit. A same-salt checksum failure fails.
    This function validates WAL checksums, not SQLCipher HMACs.
    The complete flag describes WAL-tail parsing, not SQLite integrity.
    """
    overlay, metadata, _frames = _scan_wal(walbytes, max_database_bytes, max_wal_bytes)
    return overlay, metadata


def _validate_plaintext_header(page):
    if (
        page[:16] != _SQLITE_HEADER
        or page[16:18] != b"\x10\x00"
        or page[18] not in (1, 2)
        or page[19] not in (1, 2)
        or page[20] != 80
        or page[21:24] != b"\x40\x20\x20"
    ):
        raise RecoveryError("Invalid or unsupported decrypted SQLite header")


def decrypt_database(
    mainbytes,
    walbytes,
    key32,
    *,
    max_database_bytes=MAX_DATABASE_BYTES,
    max_wal_bytes=MAX_WAL_BYTES,
    check_cancel=None,
):
    """Return a plaintext committed snapshot and metadata without performing I/O.

    Every valid WAL frame through the last commit is HMAC-authenticated, even
    if a later committed frame overwrites it. Uncommitted frame HMACs are not
    needed. Main pages selected into the result are authenticated. Earlier
    committed truncation prevents stale main-file pages from reappearing on
    later growth. Missing pages fail rather than being filled with invented data.
    A nonzero valid page-one size must agree with the final WAL commit size.
    The complete flag describes the WAL tail only; callers must independently
    validate SQLite integrity before accepting the recovered database.
    Python immutable secret buffers cannot be guaranteed to be securely erased.
    """
    _valid_key(key32)
    _require_bytes(mainbytes, "main database")
    _check_limits(max_database_bytes, max_wal_bytes)
    if len(mainbytes) > max_database_bytes:
        raise RecoveryError("Main database exceeds the configured size limit")
    if len(mainbytes) < PAGE_SIZE or len(mainbytes) % PAGE_SIZE:
        raise RecoveryError("Incomplete main database")
    overlay, metadata, committed_frames = _scan_wal(
        walbytes, max_database_bytes, max_wal_bytes
    )
    salt = mainbytes[:16]
    if salt == _SQLITE_HEADER:
        raise RecoveryError("Expected an encrypted SQLCipher database")
    authentication_key = _authentication_key(key32, salt)
    for frame_index, (page_number, encrypted_page) in enumerate(committed_frames):
        if check_cancel is not None and frame_index % 64 == 0:
            check_cancel()
        if page_number == 1 and encrypted_page[:16] != salt:
            raise RecoveryError("WAL page-one salt does not match the main database")
        _authenticate_page(authentication_key, encrypted_page, page_number)
    main_pages = len(mainbytes) // PAGE_SIZE
    final_pages = metadata["last_commit_database_pages"] or main_pages
    minimum = metadata["minimum_commit_database_pages"]
    main_reusable_pages = (
        min(main_pages, minimum) if minimum is not None else main_pages
    )
    result = bytearray()
    used_main_pages = 0
    for page_number in range(1, final_pages + 1):
        if check_cancel is not None and (page_number - 1) % 64 == 0:
            check_cancel()
        if page_number in overlay:
            encrypted_page = overlay[page_number]
        elif page_number <= main_reusable_pages:
            start = (page_number - 1) * PAGE_SIZE
            encrypted_page = mainbytes[start : start + PAGE_SIZE]
            used_main_pages += 1
        else:
            raise RecoveryError("Committed database growth has a missing page")
        plaintext = decrypt_page(key32, encrypted_page, page_number, salt)
        if page_number == 1:
            _validate_plaintext_header(plaintext)
            header_pages = struct.unpack(">I", plaintext[28:32])[0]
            header_size_valid = (
                bool(header_pages) and plaintext[24:28] == plaintext[92:96]
            )
            if (
                metadata["last_commit_database_pages"] is None
                and header_size_valid
                and header_pages > main_pages
            ):
                raise RecoveryError(
                    "Incomplete main database according to the SQLite header"
                )
            if (
                metadata["last_commit_database_pages"] is not None
                and header_size_valid
                and header_pages != final_pages
            ):
                raise RecoveryError(
                    "SQLite header size does not match the final WAL commit size"
                )
        result.extend(plaintext)
    metadata.update(
        encrypted_pages_authenticated=True,
        authenticated_committed_frame_count=len(committed_frames),
        main_page_count=main_pages,
        selected_main_page_count=used_main_pages,
        output_page_count=final_pages,
        output_bytes=len(result),
        reserved_bytes_policy="zeroed_80_bytes_per_page",
    )
    return bytes(result), metadata
