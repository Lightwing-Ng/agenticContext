"""Read-receipt deletion through pinned Windows file and directory handles.

Code version: v1.0.0-codex.2

Native contracts:
https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntopenfile
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle
https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/open-osfhandle
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import logging
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import stat


LOGGER = logging.getLogger(__name__)
_GENERIC_READ = 0x80000000
_DELETE = 0x00010000
_SYNCHRONIZE = 0x00100000
_FILE_SHARE_READ = 1
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_OPEN_REPARSE_POINT = 0x00200000
_FILE_DIRECTORY_FILE = 1
_FILE_NON_DIRECTORY_FILE = 0x40
_FILE_SYNCHRONOUS_IO_NONALERT = 0x20
_OBJ_CASE_INSENSITIVE = 0x40
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_DISPOSITION_INFO_CLASS = 4
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _UnicodeString(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_uint16), ("MaximumLength", ctypes.c_uint16), ("Buffer", ctypes.c_void_p)]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_uint32), ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(_UnicodeString)), ("Attributes", ctypes.c_uint32),
        ("SecurityDescriptor", ctypes.c_void_p), ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IoStatusValue(ctypes.Union):
    _fields_ = [("Status", ctypes.c_int32), ("Pointer", ctypes.c_void_p)]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("Value", _IoStatusValue), ("Information", ctypes.c_size_t)]


class _FileDispositionInfo(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_ubyte)]


@dataclass(frozen=True, slots=True)
class _PinnedHandle:
    handle: int
    fd: int


class _WindowsFileApi:
    """Keep handle ownership in a CRT descriptor while using native operations."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Native Windows safe deletion is unavailable on this host.")
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        self._create_file = kernel32.CreateFileW
        self._create_file.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        self._create_file.restype = ctypes.c_void_p
        self._nt_open_file = ntdll.NtOpenFile
        self._nt_open_file.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32,
            ctypes.POINTER(_ObjectAttributes), ctypes.POINTER(_IoStatusBlock),
            ctypes.c_uint32, ctypes.c_uint32,
        ]
        self._nt_open_file.restype = ctypes.c_int32
        self._ntstatus_to_error = ntdll.RtlNtStatusToDosError
        self._ntstatus_to_error.argtypes = [ctypes.c_int32]
        self._ntstatus_to_error.restype = ctypes.c_uint32
        self._set_information = kernel32.SetFileInformationByHandle
        self._set_information.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32]
        self._set_information.restype = ctypes.c_int32
        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = [ctypes.c_void_p]
        self._close_handle.restype = ctypes.c_int32
        self._open_osfhandle = msvcrt.open_osfhandle

    def _adopt(self, handle: int) -> _PinnedHandle:
        try:
            fd = self._open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
            if fd < 0:
                raise OSError("The native deletion handle could not be adopted.")
        except BaseException as exc:
            if not self._close_handle(handle):
                exc.add_note("The unadopted Windows handle could not be closed.")
            raise
        # open_osfhandle transfers ownership: only os.close(fd) may close it now.
        return _PinnedHandle(handle, fd)

    def open_root(self, workspace: Path) -> _PinnedHandle:
        name = str(workspace)
        if not name.startswith("\\\\?\\"):
            name = "\\\\?\\UNC\\" + name[2:] if name.startswith("\\\\") else "\\\\?\\" + name
        handle = self._create_file(
            name, _GENERIC_READ, _FILE_SHARE_READ, None, _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_OPEN_REPARSE_POINT, None,
        )
        if handle in (None, _INVALID_HANDLE):
            raise ctypes.WinError(ctypes.get_last_error())
        return self._adopt(handle)

    def open_child(self, parent: _PinnedHandle, component: str, *, directory: bool) -> _PinnedHandle:
        buffer = ctypes.create_unicode_buffer(component)
        encoded_length = len(component.encode("utf-16-le"))
        if encoded_length > 65_532:
            raise ValueError("The deletion path component is too long.")
        name = _UnicodeString(encoded_length, encoded_length + 2, ctypes.cast(buffer, ctypes.c_void_p))
        attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes), parent.handle, ctypes.pointer(name),
            _OBJ_CASE_INSENSITIVE, None, None,
        )
        status = _IoStatusBlock()
        handle = ctypes.c_void_p()
        result = self._nt_open_file(
            ctypes.byref(handle), _GENERIC_READ | _SYNCHRONIZE | (0 if directory else _DELETE),
            ctypes.byref(attributes), ctypes.byref(status), _FILE_SHARE_READ,
            _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT
            | (_FILE_DIRECTORY_FILE if directory else _FILE_NON_DIRECTORY_FILE),
        )
        if result < 0:
            raise ctypes.WinError(self._ntstatus_to_error(result))
        if handle.value in (None, _INVALID_HANDLE):
            raise OSError("Windows did not return a valid anchored deletion handle.")
        return self._adopt(handle.value)

    @staticmethod
    def metadata(pinned: _PinnedHandle):
        # Python's fstat keeps its native volume/file-ID and nanosecond semantics,
        # including 128-bit Windows file IDs, consistent with the read receipt.
        return os.fstat(pinned.fd)

    @staticmethod
    def read(pinned: _PinnedHandle, count: int) -> bytes:
        return os.read(pinned.fd, count)

    def mark_delete(self, pinned: _PinnedHandle) -> None:
        disposition = _FileDispositionInfo(1)
        if not self._set_information(
            pinned.handle, _FILE_DISPOSITION_INFO_CLASS,
            ctypes.byref(disposition), ctypes.sizeof(disposition),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def close(pinned: _PinnedHandle) -> None:
        os.close(pinned.fd)


def _relative_components(relative: Path) -> tuple[str, ...]:
    parsed = PureWindowsPath(str(relative))
    if parsed.drive or parsed.root or not parsed.parts or any(
        component in {"", ".", ".."} or ntpath.isreserved(component)
        for component in parsed.parts
    ):
        raise ValueError("Safe delete requires one workspace-relative regular-file path.")
    return parsed.parts


def _require_plain_directory(metadata) -> tuple[int, int]:
    attributes = getattr(metadata, "st_file_attributes", None)
    if attributes is None:
        raise RuntimeError("Windows did not provide the directory's reparse attributes.")
    if not stat.S_ISDIR(metadata.st_mode) or int(attributes) & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("Safe delete cannot traverse a linked or reparse-point directory.")
    if int(metadata.st_ino) <= 0:
        raise RuntimeError("Windows did not provide a stable directory identity.")
    return int(metadata.st_dev), int(metadata.st_ino)


def _file_identity(metadata, max_bytes: int) -> tuple[int, int, int, int, int]:
    attributes = getattr(metadata, "st_file_attributes", None)
    if attributes is None:
        raise RuntimeError("Windows did not provide the file's reparse attributes.")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or int(attributes) & _FILE_ATTRIBUTE_REPARSE_POINT
        or int(metadata.st_nlink) != 1
    ):
        raise ValueError("Safe delete requires one unlinked regular file without reparse points.")
    if int(metadata.st_ino) <= 0:
        raise RuntimeError("Windows did not provide a stable file identity.")
    if not 0 <= int(metadata.st_size) <= max_bytes:
        raise ValueError(f"Safe delete refuses files larger than {max_bytes:,} bytes.")
    return (
        int(metadata.st_dev), int(metadata.st_ino), int(metadata.st_size),
        int(metadata.st_mtime_ns),
        # Windows path stat synthesizes executable bits from the extension,
        # whereas fstat does not. Match the controller's normalized receipt.
        int(metadata.st_mode) & ~0o111 if os.name == "nt" else int(metadata.st_mode),
    )


def delete_read_verified_file(
    workspace: Path,
    relative: Path,
    workspace_identity: tuple[int, int],
    expected_identity: tuple[int, int, int, int, int],
    expected_sha256: str,
    max_bytes: int,
) -> int:
    """Delete exactly the read file through its verified handle, or raise.

    The root and every parent remain pinned until the file handle is closed.
    Child names are opened relative to those handles, never by re-resolving an
    absolute child path. Denying write/delete sharing blocks writers, writable
    mappings and renames while the receipt is checked. Existing readers can
    delay physical reclamation under Windows' normal delete-pending semantics.
    No path-based deletion fallback is permitted.
    """
    components = _relative_components(relative)
    if not workspace.is_absolute() or ".." in workspace.parts:
        raise ValueError("Safe delete requires an absolute workspace root.")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or max_bytes < 0:
        raise ValueError("Safe delete requires a current SHA-256 receipt and a nonnegative size limit.")
    api = _WindowsFileApi()
    handles = []
    primary_error: BaseException | None = None
    deletion_marked = False
    try:
        root = api.open_root(workspace)
        handles.append(root)
        if _require_plain_directory(api.metadata(root)) != workspace_identity:
            raise RuntimeError("The Agent workspace changed before deletion; start a new task.")
        parent = root
        for component in components[:-1]:
            parent = api.open_child(parent, component, directory=True)
            handles.append(parent)
            _require_plain_directory(api.metadata(parent))
        file = api.open_child(parent, components[-1], directory=False)
        handles.append(file)
        before = _file_identity(api.metadata(file), max_bytes)
        if before != expected_identity:
            raise ValueError("The file no longer matches the current read receipt; read it again before deleting.")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = api.read(file, min(64 * 1_024, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("The file exceeded the deletion size limit while being checked.")
            digest.update(chunk)
        after = _file_identity(api.metadata(file), max_bytes)
        if before != after or total != before[2] or digest.hexdigest() != expected_sha256:
            raise ValueError("The file changed while checking the read receipt; read it again before deleting.")
        api.mark_delete(file)
        deletion_marked = True
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        close_errors = []
        for handle in reversed(handles):
            try:
                api.close(handle)
            except Exception as exc:
                close_errors.append(exc)
        if close_errors:
            message = "Could not close all pinned Windows deletion handles; completion is unverified."
            LOGGER.warning("%s %s", message, close_errors[0])
            if primary_error is not None:
                primary_error.add_note(message)
            else:
                error = RuntimeError(message)
                setattr(error, "delete_disposition_set", deletion_marked)
                raise error from close_errors[0]
    return total
