"""Publish compute records while existing Windows readers retain their old file.

Code version: v1.0.0-codex.1
"""

from __future__ import annotations

import ctypes
import ntpath
import os
from pathlib import Path
import secrets
import stat


class _RenameInfo(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("root", ctypes.c_void_p),
        ("name_length", ctypes.c_uint32),
        ("name", ctypes.c_uint16 * 1),
    ]


class _AttributeTagInfo(ctypes.Structure):
    _fields_ = [("attributes", ctypes.c_uint32), ("reparse_tag", ctypes.c_uint32)]


def _extended_name(path: Path) -> str:
    name = str(path.absolute())
    if name.startswith("\\\\?\\"):
        return name
    return "\\\\?\\UNC\\" + name[2:] if name.startswith("\\\\") else "\\\\?\\" + name


def _note_cleanup(
    primary: BaseException, label: str, failure: BaseException, *, candidate: Path | None = None,
    published: bool = False,
) -> None:
    secondary_notes = tuple(getattr(failure, "__notes__", ())) if failure is not primary else ()
    detail = " ".join(str(failure).split())[:240]
    primary.add_note(f"Compute JSON {label} also failed: {type(failure).__name__}: {detail}")
    if candidate is not None:
        name = str(candidate)
        bounded = name if len(name) <= 480 else "..." + name[-477:]
        state = "published target candidate" if published else "source candidate may remain"
        primary.add_note(f"Compute JSON cleanup context ({state}): {bounded}")
    for note in secondary_notes:
        primary.add_note(note)


class _WindowsComputeIO:
    """Own native publication handles without changing file or directory ACLs."""

    def __init__(self):
        import msvcrt

        self.crt = msvcrt
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        declarations = {
            "CreateFileW": (
                [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                 ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p], ctypes.c_void_p,
            ),
            "CloseHandle": ([ctypes.c_void_p], ctypes.c_int32),
            "GetFileInformationByHandleEx": (
                [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32],
                ctypes.c_int32,
            ),
            "SetFileInformationByHandle": (
                [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32],
                ctypes.c_int32,
            ),
            "GetFinalPathNameByHandleW": (
                [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32],
                ctypes.c_uint32,
            ),
        }
        for name, (arguments, result) in declarations.items():
            function = getattr(self.kernel, name)
            function.argtypes = arguments
            function.restype = result

    def _create(self, path: Path, access: int, sharing: int, disposition: int, flags: int):
        handle = self.kernel.CreateFileW(
            _extended_name(path), access, sharing, None, disposition, flags, None
        )
        if handle in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def close_handle(self, handle) -> None:
        if not self.kernel.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def open_parent(self, path: Path):
        # Data read access makes the no-delete-sharing parent pin effective.
        handle = self._create(path, 0x1 | 0x80, 1 | 2, 3, 0x02000000 | 0x00200000)
        try:
            info = _AttributeTagInfo()
            if not self.kernel.GetFileInformationByHandleEx(
                handle, 9, ctypes.byref(info), ctypes.sizeof(info)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if not info.attributes & 0x10 or info.attributes & 0x400:
                raise OSError("The compute JSON parent must be a regular directory.")
            return handle
        except BaseException as exc:
            try:
                self.close_handle(handle)
            except BaseException as failure:
                _note_cleanup(exc, "parent handle close", failure)
            raise

    def final_path(self, handle) -> str:
        needed = self.kernel.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not needed:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(needed + 1)
        written = self.kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not written:
            raise ctypes.WinError(ctypes.get_last_error())
        if written >= len(buffer):
            raise OSError("The pinned compute JSON path changed during verification.")
        return buffer.value

    def _discard_handle(self, handle) -> None:
        delete = ctypes.c_ubyte(1)
        if not self.kernel.SetFileInformationByHandle(
            handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def create_source(self, path: Path) -> int:
        # CREATE_NEW and a retained DELETE handle are the source ownership receipt.
        handle = self._create(path, 0x80000000 | 0x40000000 | 0x10000, 1, 1, 0x00200000)
        try:
            descriptor = self.crt.open_osfhandle(
                handle, os.O_RDWR | os.O_BINARY | os.O_NOINHERIT
            )
            if descriptor < 0:
                raise OSError("The compute JSON writer could not adopt its native handle.")
            return descriptor
        except BaseException as exc:
            for label, cleanup in (
                ("unadopted source removal", lambda: self._discard_handle(handle)),
                ("unadopted source close", lambda: self.close_handle(handle)),
            ):
                try:
                    cleanup()
                except BaseException as failure:
                    _note_cleanup(exc, label, failure, candidate=path)
            raise

    def publish(self, descriptor: int, target: Path) -> None:
        """Rename once using Windows 10 POSIX replacement semantics, or fail closed."""
        name = _extended_name(target).encode("utf-16-le")
        buffer = ctypes.create_string_buffer(
            max(ctypes.sizeof(_RenameInfo), _RenameInfo.name.offset + len(name) + 2)
        )
        info = _RenameInfo.from_buffer(buffer)
        info.flags = 0x1 | 0x2
        info.root = None
        info.name_length = len(name)
        ctypes.memmove(ctypes.addressof(buffer) + _RenameInfo.name.offset, name, len(name))
        if not self.kernel.SetFileInformationByHandle(
            self.crt.get_osfhandle(descriptor), 22, buffer, len(buffer)
        ):
            failure = ctypes.WinError(ctypes.get_last_error())
            failure.add_note(
                "Compute JSON publication requires FileRenameInfoEx POSIX replacement "
                "support and readers that share DELETE access; no fallback was attempted."
            )
            raise failure

    def verify_published(self, descriptor: int, target: Path) -> None:
        actual = self.final_path(self.crt.get_osfhandle(descriptor))
        if ntpath.normcase(actual) != ntpath.normcase(_extended_name(target)):
            raise OSError("The compute JSON source was not published at its expected path.")

    def discard(self, descriptor: int) -> None:
        self._discard_handle(self.crt.get_osfhandle(descriptor))


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Keep the source handle alive through flush, replacement, and verification."""
    native = _WindowsComputeIO()
    parent = descriptor = stream = None
    target, temporary = path, None
    published = False
    primary_error = cleanup_error = None
    try:
        parent = native.open_parent(path.parent)
        target = Path(native.final_path(parent)) / path.name
        if target.is_symlink():
            raise OSError("Compute-job metadata cannot use a symbolic link.")
        temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
        descriptor = native.create_source(temporary)
        metadata = os.fstat(descriptor)
        attributes = getattr(metadata, "st_file_attributes", None)
        if (
            not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
            or attributes is None or attributes & 0x400
        ):
            raise OSError("The compute JSON source must be a regular unlinked file.")
        # This scope remains the only descriptor owner if fdopen partly fails.
        stream = os.fdopen(descriptor, "wb", closefd=False)
        stream.write(content)
        stream.flush()
        os.fsync(descriptor)
        native.publish(descriptor, target)
        # A post-publication verification failure must never delete the new target.
        published = True
        native.verify_published(descriptor, target)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanups = (
            ("stream close", stream.close if stream is not None else None),
            ("unpublished source removal", (
                lambda: native.discard(descriptor)
            ) if descriptor is not None and not published else None),
            ("descriptor close", (lambda: os.close(descriptor)) if descriptor is not None else None),
            ("parent handle close", (lambda: native.close_handle(parent)) if parent is not None else None),
        )
        for label, cleanup in cleanups:
            if cleanup is None:
                continue
            try:
                cleanup()
            except BaseException as exc:
                if primary_error is None:
                    primary_error = cleanup_error = exc
                    _note_cleanup(
                        exc, label, exc, candidate=target if published else temporary,
                        published=published,
                    )
                else:
                    _note_cleanup(
                        primary_error, label, exc,
                        candidate=target if published else temporary, published=published,
                    )
        if cleanup_error is not None:
            raise cleanup_error
