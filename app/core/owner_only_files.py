"""Owner-only secret files with one access boundary on POSIX and Windows.

Code version: v1.0.0-claude.0

POSIX hosts express the boundary as a regular file with mode ``0600`` owned by
the effective user. Windows honors only the read-only bit from a POSIX mode, so
the same boundary is a protected DACL with exactly one ACE that grants the
current user full control and inherits nothing from the parent directory.

Secret bytes are written only after the boundary is applied and verified on the
new file. Any failure removes the partial file and raises ``OwnerOnlyFileError``
instead of leaving a secret under a wider boundary.
"""

from __future__ import annotations

import os
import re
import stat
from functools import lru_cache
from pathlib import Path
from typing import Any

IS_WINDOWS = os.name == "nt"
_SDDL_DACL = re.compile(r"^D:(?P<flags>[A-Z_]*)(?P<aces>(?:\([^)]*\))*)$")


class OwnerOnlyFileError(OSError):
    """A secret file could not be placed under the owner-only boundary."""


class _WindowsSecurity:
    """Native Win32 access-control calls, loaded only on Windows."""

    SE_FILE_OBJECT = 1
    DACL_SECURITY_INFORMATION = 0x00000004
    PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    SDDL_REVISION_1 = 1
    TOKEN_QUERY = 0x0008
    TOKEN_USER_CLASS = 1

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = wintypes.HANDLE
        pointer = ctypes.c_void_p

        self._get_current_process = kernel32.GetCurrentProcess
        self._get_current_process.restype = handle
        self._get_current_process.argtypes = []
        self._close_handle = kernel32.CloseHandle
        self._close_handle.restype = wintypes.BOOL
        self._close_handle.argtypes = [handle]
        self._local_free = kernel32.LocalFree
        self._local_free.restype = pointer
        self._local_free.argtypes = [pointer]

        self._open_process_token = advapi32.OpenProcessToken
        self._open_process_token.restype = wintypes.BOOL
        self._open_process_token.argtypes = [handle, wintypes.DWORD, ctypes.POINTER(handle)]
        self._get_token_information = advapi32.GetTokenInformation
        self._get_token_information.restype = wintypes.BOOL
        self._get_token_information.argtypes = [
            handle,
            ctypes.c_int,
            pointer,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._convert_sid_to_string = advapi32.ConvertSidToStringSidW
        self._convert_sid_to_string.restype = wintypes.BOOL
        self._convert_sid_to_string.argtypes = [pointer, ctypes.POINTER(wintypes.LPWSTR)]
        self._string_to_descriptor = (
            advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        )
        self._string_to_descriptor.restype = wintypes.BOOL
        self._string_to_descriptor.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(pointer),
            ctypes.POINTER(wintypes.ULONG),
        ]
        self._descriptor_to_string = (
            advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
        )
        self._descriptor_to_string.restype = wintypes.BOOL
        self._descriptor_to_string.argtypes = [
            pointer,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(wintypes.ULONG),
        ]
        self._get_descriptor_dacl = advapi32.GetSecurityDescriptorDacl
        self._get_descriptor_dacl.restype = wintypes.BOOL
        self._get_descriptor_dacl.argtypes = [
            pointer,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(pointer),
            ctypes.POINTER(wintypes.BOOL),
        ]
        self._set_named_security = advapi32.SetNamedSecurityInfoW
        self._set_named_security.restype = wintypes.DWORD
        self._set_named_security.argtypes = [
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            pointer,
            pointer,
            pointer,
            pointer,
        ]
        self._get_named_security = advapi32.GetNamedSecurityInfoW
        self._get_named_security.restype = wintypes.DWORD
        self._get_named_security.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.POINTER(pointer),
            ctypes.POINTER(pointer),
            ctypes.POINTER(pointer),
            ctypes.POINTER(pointer),
            ctypes.POINTER(pointer),
        ]

    def _last_error(self, operation: str) -> OwnerOnlyFileError:
        code = self._ctypes.get_last_error()
        return OwnerOnlyFileError(f"{operation} failed with Windows error {code}.")

    def current_user_sid(self) -> str:
        ctypes = self._ctypes
        wintypes = self._wintypes
        token = wintypes.HANDLE()
        if not self._open_process_token(
            self._get_current_process(), self.TOKEN_QUERY, ctypes.byref(token)
        ):
            raise self._last_error("OpenProcessToken")
        try:
            needed = wintypes.DWORD()
            self._get_token_information(
                token, self.TOKEN_USER_CLASS, None, 0, ctypes.byref(needed)
            )
            buffer = ctypes.create_string_buffer(max(int(needed.value), 1))
            if not self._get_token_information(
                token,
                self.TOKEN_USER_CLASS,
                buffer,
                needed,
                ctypes.byref(needed),
            ):
                raise self._last_error("GetTokenInformation")
            # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first field is the PSID.
            sid = ctypes.c_void_p.from_buffer(buffer).value
            text = wintypes.LPWSTR()
            if not self._convert_sid_to_string(sid, ctypes.byref(text)):
                raise self._last_error("ConvertSidToStringSidW")
            try:
                return str(text.value or "")
            finally:
                self._local_free(ctypes.cast(text, ctypes.c_void_p))
        finally:
            self._close_handle(token)

    def canonical_dacl(self, sddl: str) -> str:
        """Round-trip SDDL so aliases and ordering match what Windows reports."""
        ctypes = self._ctypes
        descriptor = ctypes.c_void_p()
        if not self._string_to_descriptor(
            sddl, self.SDDL_REVISION_1, ctypes.byref(descriptor), None
        ):
            raise self._last_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
        try:
            return self._descriptor_dacl_text(descriptor)
        finally:
            self._local_free(descriptor)

    def set_protected_dacl(self, path: str, sddl: str) -> None:
        ctypes = self._ctypes
        wintypes = self._wintypes
        descriptor = ctypes.c_void_p()
        if not self._string_to_descriptor(
            sddl, self.SDDL_REVISION_1, ctypes.byref(descriptor), None
        ):
            raise self._last_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
        try:
            present = wintypes.BOOL()
            defaulted = wintypes.BOOL()
            dacl = ctypes.c_void_p()
            if not self._get_descriptor_dacl(
                descriptor,
                ctypes.byref(present),
                ctypes.byref(dacl),
                ctypes.byref(defaulted),
            ) or not present.value:
                raise self._last_error("GetSecurityDescriptorDacl")
            result = self._set_named_security(
                ctypes.create_unicode_buffer(path),
                self.SE_FILE_OBJECT,
                self.DACL_SECURITY_INFORMATION | self.PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                dacl,
                None,
            )
            if result:
                raise OwnerOnlyFileError(
                    f"SetNamedSecurityInfoW failed with Windows error {result}."
                )
        finally:
            self._local_free(descriptor)

    def dacl_text(self, path: str) -> str:
        ctypes = self._ctypes
        descriptor = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        result = self._get_named_security(
            path,
            self.SE_FILE_OBJECT,
            self.DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if result:
            raise OwnerOnlyFileError(
                f"GetNamedSecurityInfoW failed with Windows error {result}."
            )
        try:
            return self._descriptor_dacl_text(descriptor)
        finally:
            self._local_free(descriptor)

    def _descriptor_dacl_text(self, descriptor: Any) -> str:
        ctypes = self._ctypes
        wintypes = self._wintypes
        text = wintypes.LPWSTR()
        if not self._descriptor_to_string(
            descriptor,
            self.SDDL_REVISION_1,
            self.DACL_SECURITY_INFORMATION,
            ctypes.byref(text),
            None,
        ):
            raise self._last_error("ConvertSecurityDescriptorToStringSecurityDescriptorW")
        try:
            return str(text.value or "")
        finally:
            self._local_free(ctypes.cast(text, ctypes.c_void_p))


@lru_cache(maxsize=1)
def _windows_security() -> Any:
    return _WindowsSecurity()


@lru_cache(maxsize=1)
def _windows_owner_sddl() -> str:
    sid = _windows_security().current_user_sid()
    if not sid.startswith("S-1-"):
        raise OwnerOnlyFileError("The current Windows user SID could not be read.")
    return f"D:P(A;;FA;;;{sid})"


def _parse_dacl(text: str) -> tuple[str, tuple[str, ...]] | None:
    match = _SDDL_DACL.match(str(text or "").strip())
    if match is None:
        return None
    return match.group("flags"), tuple(re.findall(r"\([^)]*\)", match.group("aces")))


def _windows_problem(path: Path) -> str:
    security = _windows_security()
    expected = _parse_dacl(security.canonical_dacl(_windows_owner_sddl()))
    actual = _parse_dacl(security.dacl_text(str(path)))
    if expected is None or actual is None:
        return "its Windows DACL could not be parsed"
    # "P" marks a protected DACL; the auto-inherit flags "AI" and "AR" never contain it.
    if "P" not in actual[0]:
        return "its Windows DACL still inherits parent permissions"
    if actual[1] != expected[1]:
        return "its Windows DACL grants access beyond the current user"
    return ""


def owner_only_problem(path: Path) -> str:
    """Return why ``path`` is outside the owner-only boundary, or an empty string."""
    try:
        metadata = path.lstat()
    except OSError as exc:
        return f"it cannot be inspected ({exc.__class__.__name__})"
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return "it is not a regular file"
    if IS_WINDOWS:
        try:
            return _windows_problem(path)
        except OSError as exc:
            return f"its Windows DACL could not be verified ({exc})"
    if metadata.st_uid != os.geteuid():
        return "it is owned by another user"
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        return "its mode grants group or other access"
    return ""


def _apply_boundary(path: Path, descriptor: int | None = None) -> None:
    if IS_WINDOWS:
        _windows_security().set_protected_dacl(str(path), _windows_owner_sddl())
    elif descriptor is not None:
        os.fchmod(descriptor, 0o600)
    else:
        # Callers reject symlinks with lstat() before reaching this path.
        os.chmod(path, 0o600)


def ensure_owner_only(path: Path) -> None:
    """Narrow an existing regular file to the owner-only boundary and verify it."""
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OwnerOnlyFileError(f"{path.name} is not a regular file.")
    if not owner_only_problem(path):
        return
    _apply_boundary(path)
    problem = owner_only_problem(path)
    if problem:
        raise OwnerOnlyFileError(f"{path.name} is not owner-only: {problem}.")


def write_owner_only_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace ``path`` with ``content`` under the owner-only boundary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    # A leftover from an interrupted write may carry a wider boundary; never reuse it.
    temporary.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        try:
            _apply_boundary(temporary, descriptor)
            problem = owner_only_problem(temporary)
            if problem:
                raise OwnerOnlyFileError(f"{path.name} is not owner-only: {problem}.")
            with os.fdopen(descriptor, "w", encoding=encoding) as handle:
                descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    problem = owner_only_problem(path)
    if problem:
        # Rename keeps the verified boundary; a destination that lost it keeps no secret.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise OwnerOnlyFileError(f"{path.name} is not owner-only: {problem}.")


__all__ = [
    "IS_WINDOWS",
    "OwnerOnlyFileError",
    "ensure_owner_only",
    "owner_only_problem",
    "write_owner_only_text",
]
