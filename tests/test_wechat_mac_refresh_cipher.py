"""Synthetic-only tests for mac_sqlcipher_read_v1; no secret or database I/O.

Code version: v1.0.0-codex.0. All fixtures use synthetic keys and pages.
"""

import hashlib
import hmac
import sqlite3
import struct
import sys
from unittest.mock import patch
import unittest

from app.core import wechat_mac_refresh_cipher as reader

KEY = bytes(range(32))
SALT = bytes(range(32, 48))
WAL_SALTS = bytes(range(91, 99))


def plain_page(number, marker=0, count=3):
    page = bytearray(reader.PAGE_SIZE)
    if number == 1:
        page[:16] = b"SQLite format 3\x00"
        page[16:24] = b"\x10\x00\x02\x02\x50\x40\x20\x20"
        for offset, value in (
            (24, 1),
            (28, count),
            (44, 4),
            (56, 1),
            (60, marker),
            (92, 1),
            (96, 3043000),
        ):
            page[offset : offset + 4] = struct.pack(">I", value)
        page[100] = 13
        page[105:107] = struct.pack(">H", 4016)
    else:
        page[:4016] = bytes([marker]) * 4016
    return bytes(page)


def encrypt_page(plaintext, number, key=KEY, salt=SALT):
    start = 16 if number == 1 else 0
    iv = bytes([number % 256]) * 16
    encrypted = reader._aes_cbc(key, iv, plaintext[start:4016], 0)
    prefix = salt if number == 1 else b""
    page = prefix + encrypted + iv
    auth_key = hashlib.pbkdf2_hmac(
        "sha512", key, bytes(value ^ 0x3A for value in salt), 2, 32
    )
    tag = hmac.new(
        auth_key, page[start:] + struct.pack("<I", number), hashlib.sha512
    ).digest()
    return page + tag


def checksum_reference(data, byte_order, seed=(0, 0)):
    first, second = seed
    for offset in range(0, len(data), 8):
        left = int.from_bytes(data[offset : offset + 4], byte_order)
        right = int.from_bytes(data[offset + 4 : offset + 8], byte_order)
        first = (first + left + second) % (2**32)
        second = (second + right + first) % (2**32)
    return first, second


def make_wal(frames, byte_order="little", salts=WAL_SALTS):
    magic = 0x377F0682 if byte_order == "little" else 0x377F0683
    header = struct.pack(">IIII", magic, 3007000, reader.PAGE_SIZE, 7) + salts
    checksum = checksum_reference(header, byte_order)
    result = bytearray(header + struct.pack(">II", *checksum))
    for number, commit_size, page in frames:
        first_eight = struct.pack(">II", number, commit_size)
        checksum = checksum_reference(first_eight + page, byte_order, checksum)
        result.extend(first_eight + salts + struct.pack(">II", *checksum) + page)
    return bytes(result)


class ReadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.native_aes = reader._aes_cbc
        cls.portable_boundary = None
        if sys.platform != "darwin":
            # Unsupported hosts still exercise HMAC/WAL behavior through a reversible
            # cipher boundary; the native backend must separately reject that host.
            def reversible_boundary(key, iv, data, operation):
                reader._valid_key(key)
                return bytes(
                    value ^ key[index % 32] ^ iv[index % 16]
                    for index, value in enumerate(data)
                )

            cls.portable_boundary = patch.object(
                reader, "_aes_cbc", reversible_boundary
            )
            cls.portable_boundary.start()
        cls.pages = [plain_page(1), plain_page(2, 22), plain_page(3, 33)]
        cls.encrypted = [
            encrypt_page(page, index + 1) for index, page in enumerate(cls.pages)
        ]
        cls.main = b"".join(cls.encrypted)

    @classmethod
    def tearDownClass(cls):
        if cls.portable_boundary is not None:
            cls.portable_boundary.stop()

    def test_commoncrypto_known_aes256_cbc_vector(self):
        if sys.platform != "darwin":
            with self.assertRaisesRegex(reader.RecoveryError, "macOS CommonCrypto"):
                type(self).native_aes(KEY, bytes(16), bytes(16), 1)
            return
        key = bytes.fromhex(
            "603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4"
        )
        iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plaintext = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
        ciphertext = bytes.fromhex("f58c4c04d6e5f1ba779eabfb5f7bfbd6")
        self.assertEqual(reader._aes_cbc(key, iv, plaintext, 0), ciphertext)
        self.assertEqual(reader._aes_cbc(key, iv, ciphertext, 1), plaintext)

    def test_page_one_and_later_page_roundtrip(self):
        for number in (1, 2, 3):
            self.assertEqual(
                reader.decrypt_page(KEY, self.encrypted[number - 1], number, SALT),
                self.pages[number - 1],
            )
        self.assertEqual(
            reader.decrypt_page(KEY, self.encrypted[0], 1, SALT)[-80:], bytes(80)
        )

    def test_wrong_key_page_number_and_tampering(self):
        with self.assertRaises(reader.RecoveryError):
            reader.decrypt_page(bytes(reversed(KEY)), self.encrypted[0], 1, SALT)
        with self.assertRaises(reader.RecoveryError):
            reader.decrypt_page(KEY, self.encrypted[1], 3, SALT)
        for offset in (0, 16, 100, 4016, 4032, 4095):
            tampered = bytearray(self.encrypted[0])
            tampered[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(reader.RecoveryError):
                reader.decrypt_page(KEY, bytes(tampered), 1, SALT)

    def test_no_wal_main_snapshot(self):
        plaintext, metadata = reader.decrypt_database(self.main, b"", KEY)
        self.assertEqual(plaintext, b"".join(self.pages))
        self.assertEqual(metadata["output_page_count"], 3)
        self.assertTrue(metadata["complete"])
        self.assertTrue(metadata["encrypted_pages_authenticated"])

    def test_decrypted_empty_sqlite_database_is_readable(self):
        main = encrypt_page(plain_page(1, 123, 1), 1)
        plaintext, _metadata = reader.decrypt_database(main, b"", KEY)
        # A rollback-mode header lets the memory-only SQLite connection open it.
        plaintext = plaintext[:18] + b"\x01\x01" + plaintext[20:]
        connection = sqlite3.connect(":memory:")
        try:
            connection.deserialize(plaintext)
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 123
            )
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
        finally:
            connection.close()

    def test_multi_transaction_duplicate_page_and_uncommitted_tail(self):
        early = encrypt_page(plain_page(2, 42), 2)
        later = encrypt_page(plain_page(2, 43), 2)
        third = encrypt_page(plain_page(3, 44), 3)
        uncommitted = bytes(reader.PAGE_SIZE)
        frames = [(2, 0, early), (2, 3, later), (3, 3, third), (2, 0, uncommitted)]
        for byte_order in ("little", "big"):
            with self.subTest(byte_order=byte_order):
                wal = make_wal(frames, byte_order)
                overlay, parsed = reader.parse_wal(wal)
                self.assertEqual(overlay, {2: later, 3: third})
                self.assertEqual(parsed["commit_count"], 2)
                self.assertEqual(parsed["committed_frame_count"], 3)
                self.assertEqual(parsed["uncommitted_valid_frame_count"], 1)
                self.assertEqual(parsed["tail_status"], "uncommitted_frames")
                self.assertTrue(parsed["complete"])
                plaintext, metadata = reader.decrypt_database(self.main, wal, KEY)
                self.assertEqual(
                    plaintext, self.pages[0] + plain_page(2, 43) + plain_page(3, 44)
                )
                self.assertEqual(metadata["authenticated_committed_frame_count"], 3)

    def test_no_commit_does_not_apply_frame_or_authenticate_its_hmac(self):
        wal = make_wal([(2, 0, bytes(reader.PAGE_SIZE))])
        overlay, metadata = reader.parse_wal(wal)
        self.assertEqual(overlay, {})
        self.assertEqual(metadata["committed_frame_count"], 0)
        plaintext, _metadata = reader.decrypt_database(self.main, wal, KEY)
        self.assertEqual(plaintext, b"".join(self.pages))

    def test_commit_resize_truncates_and_does_not_resurrect_old_main_pages(self):
        second = encrypt_page(plain_page(2, 77), 2)
        third = encrypt_page(plain_page(3, 88), 3)
        shrunk_header = plain_page(1, count=2)
        shrunk_page_one = encrypt_page(shrunk_header, 1)
        grown_page_one = self.encrypted[0]
        wal = make_wal([(3, 3, third), (1, 0, shrunk_page_one), (2, 2, second)])
        overlay, _metadata = reader.parse_wal(wal)
        self.assertEqual(overlay, {1: shrunk_page_one, 2: second})
        plaintext, _metadata = reader.decrypt_database(self.main, wal, KEY)
        self.assertEqual(plaintext, shrunk_header + plain_page(2, 77))
        regrowth_without_page = make_wal(
            [
                (1, 0, shrunk_page_one),
                (2, 2, second),
                (1, 0, grown_page_one),
                (2, 3, second),
            ]
        )
        with self.assertRaisesRegex(reader.RecoveryError, "missing page"):
            reader.decrypt_database(self.main, regrowth_without_page, KEY)
        regrowth = make_wal(
            [
                (1, 0, shrunk_page_one),
                (2, 2, second),
                (1, 0, grown_page_one),
                (3, 3, third),
            ]
        )
        plaintext, _metadata = reader.decrypt_database(self.main, regrowth, KEY)
        self.assertEqual(
            plaintext, self.pages[0] + plain_page(2, 77) + plain_page(3, 88)
        )

    def test_effective_page_one_size_must_match_the_final_commit(self):
        cases = [
            make_wal([(2, 2, self.encrypted[1])]),
            make_wal([(1, 3, encrypt_page(plain_page(1, count=4), 1))]),
        ]
        for wal in cases:
            with self.assertRaisesRegex(
                reader.RecoveryError, "header size.*WAL commit"
            ):
                reader.decrypt_database(self.main, wal, KEY)

    def test_zero_or_invalid_header_page_count_uses_wal_commit_boundary(self):
        zero_count = plain_page(1, count=0)
        invalid_count = bytearray(plain_page(1, count=4))
        invalid_count[92:96] = struct.pack(">I", 2)
        for page_one in (zero_count, bytes(invalid_count)):
            with self.subTest(header_count=page_one[28:32]):
                wal = make_wal([(1, 3, encrypt_page(page_one, 1))])
                plaintext, metadata = reader.decrypt_database(self.main, wal, KEY)
                self.assertEqual(plaintext, page_one + self.pages[1] + self.pages[2])
                self.assertEqual(metadata["output_page_count"], 3)
                self.assertEqual(metadata["completeness_scope"], "wal_tail")

    def test_wal_page_one_is_restored_and_header_checked(self):
        updated = plain_page(1, 456)
        wal = make_wal([(1, 3, encrypt_page(updated, 1))])
        plaintext, _metadata = reader.decrypt_database(self.main, wal, KEY)
        self.assertEqual(plaintext[: reader.PAGE_SIZE], updated)
        invalid = bytearray(updated)
        invalid[20] = 0
        wal = make_wal([(1, 3, encrypt_page(bytes(invalid), 1))])
        with self.assertRaisesRegex(reader.RecoveryError, "SQLite header"):
            reader.decrypt_database(self.main, wal, KEY)

    def test_authenticates_overwritten_committed_frames(self):
        bad = bytearray(self.encrypted[1])
        bad[-1] ^= 1
        wal = make_wal([(2, 0, bytes(bad)), (2, 3, self.encrypted[1])])
        with self.assertRaisesRegex(reader.RecoveryError, "authentication"):
            reader.decrypt_database(self.main, wal, KEY)

    def test_overwritten_main_page_does_not_need_authentication(self):
        bad_main = (
            self.main[: reader.PAGE_SIZE]
            + bytes(reader.PAGE_SIZE)
            + self.main[2 * reader.PAGE_SIZE :]
        )
        wal = make_wal([(2, 3, self.encrypted[1])])
        plaintext, _metadata = reader.decrypt_database(bad_main, wal, KEY)
        self.assertEqual(plaintext, b"".join(self.pages))

    def test_zero_and_old_salt_preallocated_tails_are_explicitly_incomplete(self):
        prefix = make_wal([(2, 3, self.encrypted[1])])
        stale = make_wal([(3, 3, self.encrypted[2])], salts=b"oldsalts")[32:]
        for suffix, status in (
            (bytes(100), "zero_preallocation"),
            (stale, "stale_salt_tail"),
        ):
            with self.subTest(status=status):
                overlay, metadata = reader.parse_wal(prefix + suffix)
                self.assertEqual(overlay, {2: self.encrypted[1]})
                self.assertFalse(metadata["complete"])
                self.assertEqual(metadata["tail_status"], status)
                self.assertEqual(metadata["tail_offset"], len(prefix))
                self.assertEqual(metadata["tail_bytes"], len(suffix))
                plaintext, metadata = reader.decrypt_database(
                    self.main, prefix + suffix, KEY
                )
                self.assertEqual(plaintext, b"".join(self.pages))
                self.assertFalse(metadata["complete"])

    def test_incomplete_tails_return_only_a_bounded_committed_prefix(self):
        prefix = make_wal([(2, 3, self.encrypted[1])])
        second_frame = make_wal([(2, 3, self.encrypted[1]), (3, 0, self.encrypted[2])])[
            len(prefix) :
        ]
        for suffix, status in (
            (second_frame[:12], "incomplete_frame_header"),
            (second_frame[:100], "incomplete_frame"),
        ):
            with self.subTest(status=status):
                overlay, metadata = reader.parse_wal(prefix + suffix)
                self.assertEqual(overlay, {2: self.encrypted[1]})
                self.assertFalse(metadata["complete"])
                self.assertEqual(metadata["tail_status"], status)
                plaintext, metadata = reader.decrypt_database(
                    self.main, prefix + suffix, KEY
                )
                self.assertEqual(plaintext, b"".join(self.pages))
                self.assertFalse(metadata["complete"])

    def test_current_generation_checksum_corruption_fails_closed(self):
        prefix = make_wal([(2, 3, self.encrypted[1])])
        second_frame = make_wal([(2, 3, self.encrypted[1]), (3, 0, self.encrypted[2])])[
            len(prefix) :
        ]
        corrupted = bytearray(second_frame)
        corrupted[24] ^= 1
        with self.assertRaises(reader.RecoveryError):
            reader.parse_wal(prefix + bytes(corrupted))
        corrupt_header = bytearray(prefix)
        corrupt_header[24] ^= 1
        with self.assertRaisesRegex(reader.RecoveryError, "header checksum"):
            reader.parse_wal(bytes(corrupt_header))

    def test_authenticated_wal_hmac_corruption_and_wrong_key_fail(self):
        bad = bytearray(self.encrypted[1])
        bad[4032] ^= 1
        wal = make_wal([(2, 3, bytes(bad))])
        with self.assertRaisesRegex(reader.RecoveryError, "authentication"):
            reader.decrypt_database(self.main, wal, KEY)
        with self.assertRaises(reader.RecoveryError):
            reader.decrypt_database(
                self.main, make_wal([(2, 3, self.encrypted[1])]), bytes(reversed(KEY))
            )

    def test_input_header_and_size_limits(self):
        cases = [b"x", self.main[:-1], b"", b"SQLite format 3\x00" + self.main[16:]]
        for main in cases:
            with (
                self.subTest(length=len(main)),
                self.assertRaises(reader.RecoveryError),
            ):
                reader.decrypt_database(main, b"", KEY)
        for wal in (
            b"x",
            bytes(32),
            struct.pack(">III", 0x377F0682, 3007000, 8192) + bytes(20),
        ):
            with self.assertRaises(reader.RecoveryError):
                reader.parse_wal(wal)
        with self.assertRaises(reader.RecoveryError):
            reader.decrypt_database(self.main, b"", KEY, max_database_bytes=4096)
        with self.assertRaises(reader.RecoveryError):
            reader.parse_wal(make_wal([(2, 3, self.encrypted[1])]), max_wal_bytes=32)
        with self.assertRaises(reader.RecoveryError):
            reader.parse_wal(b"", max_wal_bytes=reader.MAX_WAL_BYTES + 1)
        with self.assertRaises(reader.RecoveryError):
            reader.parse_wal(
                make_wal([(4, 4, self.encrypted[1])]),
                max_database_bytes=3 * reader.PAGE_SIZE,
            )

    def test_authenticated_header_detects_whole_page_main_truncation(self):
        with self.assertRaisesRegex(reader.RecoveryError, "Incomplete main database"):
            reader.decrypt_database(self.main[: -reader.PAGE_SIZE], b"", KEY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
