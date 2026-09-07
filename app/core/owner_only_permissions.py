"""Private Agent artifacts with POSIX modes or verified Windows owner DACLs.

Code version: v1.0.1-codex.1

Native contracts:
https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-setsecurityinfo
https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-getsecurityinfo
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew
https://learn.microsoft.com/en-us/windows/win32/secauthz/security-descriptor-string-format
https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_rename_info
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfinalpathnamebyhandlew
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import ctypes
import logging
import ntpath
import os
from pathlib import Path
import stat
import tempfile
from uuid import uuid4


LOGGER = logging.getLogger(__name__)
_READ_CONTROL = 0x00020000
_MAXIMUM_ALLOWED = 0x02000000
_FILE_LIST_DIRECTORY = 1
_FILE_READ_ATTRIBUTES = 0x80
_FILE_ALL_ACCESS = 0x001F01FF
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_SE_DACL_PROTECTED = 0x1000
_OWNER_SECURITY_INFORMATION = 1
_DACL_SECURITY_INFORMATION = 4
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_FILE_OBJECT = 1
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int32),
    ]


class _Acl(ctypes.Structure):
    _fields_ = [
        ("AclRevision", ctypes.c_ubyte),
        ("Sbz1", ctypes.c_ubyte),
        ("AclSize", ctypes.c_uint16),
        ("AceCount", ctypes.c_uint16),
        ("Sbz2", ctypes.c_uint16),
    ]


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", ctypes.c_uint16),
    ]


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _FileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", ctypes.c_uint32),
        ("creation", _FileTime),
        ("access", _FileTime),
        ("write", _FileTime),
        ("volume", ctypes.c_uint32),
        ("size_high", ctypes.c_uint32),
        ("size_low", ctypes.c_uint32),
        ("links", ctypes.c_uint32),
        ("index_high", ctypes.c_uint32),
        ("index_low", ctypes.c_uint32),
    ]


class _FileRenameInformation(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("root", ctypes.c_void_p),
        ("name_length", ctypes.c_uint32),
        ("name", ctypes.c_uint16 * 1),
    ]


def _checked_path(path: Path) -> Path:
    target = Path(path).expanduser().absolute()
    if ".." in target.parts:
        raise OSError("Private Agent paths cannot contain parent traversal.")
    for component in (*reversed(target.parents), target):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        if (
            stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0)
            & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise OSError(
                f"Refusing private Agent permissions through a linked path: {component}."
            )
    return target


class _WindowsPermissions:
    """Use current-token SIDs and handle-based security descriptor operations."""

    def __init__(self):
        if os.name != "nt":
            raise OSError(
                "Native Windows owner permissions are unavailable on this host."
            )
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        definitions = (
            (
                self.kernel,
                "GetFinalPathNameByHandleW",
                [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32],
                ctypes.c_uint32,
            ),
            (self.kernel, "GetCurrentProcess", [], ctypes.c_void_p),
            (self.kernel, "CloseHandle", [ctypes.c_void_p], ctypes.c_int32),
            (self.kernel, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p),
            (
                self.kernel,
                "CreateFileW",
                [
                    ctypes.c_wchar_p,
                    ctypes.c_uint32,
                    ctypes.c_uint32,
                    ctypes.POINTER(_SecurityAttributes),
                    ctypes.c_uint32,
                    ctypes.c_uint32,
                    ctypes.c_void_p,
                ],
                ctypes.c_void_p,
            ),
            (
                self.kernel,
                "CreateDirectoryW",
                [ctypes.c_wchar_p, ctypes.POINTER(_SecurityAttributes)],
                ctypes.c_int32,
            ),
            (
                self.kernel,
                "GetFileInformationByHandle",
                [ctypes.c_void_p, ctypes.POINTER(_FileInformation)],
                ctypes.c_int32,
            ),
            (
                self.kernel,
                "SetFileInformationByHandle",
                [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32],
                ctypes.c_int32,
            ),
            (
                self.security,
                "OpenProcessToken",
                [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
                ctypes.c_int32,
            ),
            (
                self.security,
                "GetTokenInformation",
                [
                    ctypes.c_void_p,
                    ctypes.c_int32,
                    ctypes.c_void_p,
                    ctypes.c_uint32,
                    ctypes.POINTER(ctypes.c_uint32),
                ],
                ctypes.c_int32,
            ),
            (
                self.security,
                "ConvertSidToStringSidW",
                [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                ctypes.c_int32,
            ),
            (
                self.security,
                "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                [
                    ctypes.c_wchar_p,
                    ctypes.c_uint32,
                    ctypes.POINTER(ctypes.c_void_p),
                    ctypes.c_void_p,
                ],
                ctypes.c_int32,
            ),
            (
                self.security,
                "GetSecurityDescriptorDacl",
                [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_int32),
                    ctypes.POINTER(ctypes.c_void_p),
                    ctypes.POINTER(ctypes.c_int32),
                ],
                ctypes.c_int32,
            ),
            (
                self.security,
                "GetSecurityDescriptorOwner",
                [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p),
                    ctypes.POINTER(ctypes.c_int32),
                ],
                ctypes.c_int32,
            ),
            (
                self.security,
                "GetSecurityDescriptorControl",
                [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_uint16),
                    ctypes.POINTER(ctypes.c_uint32),
                ],
                ctypes.c_int32,
            ),
            (
                self.security,
                "GetSecurityInfo",
                [
                    ctypes.c_void_p,
                    ctypes.c_int32,
                    ctypes.c_uint32,
                    ctypes.POINTER(ctypes.c_void_p),
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p),
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p),
                ],
                ctypes.c_uint32,
            ),
            (
                self.security,
                "SetSecurityInfo",
                [
                    ctypes.c_void_p,
                    ctypes.c_int32,
                    ctypes.c_uint32,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                ],
                ctypes.c_uint32,
            ),
            (
                self.security,
                "GetAce",
                [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
                ctypes.c_int32,
            ),
        )
        for library, name, arguments, result in definitions:
            function = getattr(library, name)
            function.argtypes = arguments
            function.restype = result
        token = ctypes.c_void_p()
        if not self.security.OpenProcessToken(
            self.kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.user_sid = self._token_sid(token, 1)
            self.owner_sid = self._token_sid(token, 4)
        finally:
            self.kernel.CloseHandle(token)

    def _sid_text(self, sid) -> str:
        output = ctypes.c_void_p()
        if not self.security.ConvertSidToStringSidW(sid, ctypes.byref(output)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.wstring_at(output.value)
        finally:
            self.kernel.LocalFree(output)

    def _token_sid(self, token, information_class) -> str:
        length = ctypes.c_uint32()
        self.security.GetTokenInformation(
            token, information_class, None, 0, ctypes.byref(length)
        )
        if length.value == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(length.value)
        if not self.security.GetTokenInformation(
            token, information_class, buffer, length, ctypes.byref(length)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return self._sid_text(ctypes.c_void_p.from_buffer(buffer).value)

    @staticmethod
    def _native_name(path: Path) -> str:
        name = str(path)
        if name.startswith("\\\\?\\"):
            return name
        return (
            "\\\\?\\UNC\\" + name[2:] if name.startswith("\\\\") else "\\\\?\\" + name
        )

    @contextmanager
    def _descriptor(self, directory: bool):
        descriptor = ctypes.c_void_p()
        inherit = "OICI" if directory else ""
        sddl = f"O:{self.user_sid}D:P(A;{inherit};FA;;;{self.user_sid})"
        if not self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl,
            1,
            ctypes.byref(descriptor),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield descriptor
        finally:
            self.kernel.LocalFree(descriptor)

    @contextmanager
    def _existing(self, path: Path, *, directory: bool, change: bool):
        # Attribute-only opens do not participate in file sharing checks.
        # Directory data access is required to make the missing DELETE share real.
        access = (
            _MAXIMUM_ALLOWED
            if change
            else (
                _READ_CONTROL
                | _FILE_READ_ATTRIBUTES
                | (_FILE_LIST_DIRECTORY if directory else 0)
            )
        )
        handle = self.kernel.CreateFileW(
            self._native_name(path),
            access,
            0 if change else 3,
            None,
            3,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle in (None, _INVALID_HANDLE):
            raise ctypes.WinError(ctypes.get_last_error())
        primary_error = None
        try:
            metadata = _FileInformation()
            if not self.kernel.GetFileInformationByHandle(
                handle, ctypes.byref(metadata)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if (
                metadata.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
                or bool(metadata.attributes & _FILE_ATTRIBUTE_DIRECTORY) != directory
                or (not directory and metadata.links != 1)
            ):
                raise OSError(
                    "Private Agent permissions require an unlinked file or plain directory."
                )
            yield handle
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if not self.kernel.CloseHandle(handle):
                error = ctypes.WinError(ctypes.get_last_error())
                if primary_error is not None:
                    primary_error.add_note(
                        f"Closing the private Agent permission handle also failed: {error}"
                    )
                else:
                    raise error

    def _read_acl(self, handle) -> dict:
        owner, dacl, descriptor = (
            ctypes.c_void_p(),
            ctypes.c_void_p(),
            ctypes.c_void_p(),
        )
        status = self.security.GetSecurityInfo(
            handle,
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if status:
            raise ctypes.WinError(status)
        try:
            if not owner.value or not dacl.value:
                raise OSError("Windows returned an absent owner or unrestricted DACL.")
            control, revision = ctypes.c_uint16(), ctypes.c_uint32()
            if not self.security.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            entries = []
            for index in range(
                ctypes.cast(dacl, ctypes.POINTER(_Acl)).contents.AceCount
            ):
                ace = ctypes.c_void_p()
                if not self.security.GetAce(dacl, index, ctypes.byref(ace)):
                    raise ctypes.WinError(ctypes.get_last_error())
                header = ctypes.cast(ace, ctypes.POINTER(_AceHeader)).contents
                if header.AceType != 0 or header.AceSize < 12:
                    entries.append(
                        {"type": int(header.AceType), "flags": int(header.AceFlags)}
                    )
                    continue
                entries.append(
                    {
                        "type": 0,
                        "flags": int(header.AceFlags),
                        "mask": int(ctypes.c_uint32.from_address(ace.value + 4).value),
                        "sid": self._sid_text(ace.value + 8),
                    }
                )
            return {
                "owner": self._sid_text(owner),
                "current_user": self.user_sid,
                "protected": bool(control.value & _SE_DACL_PROTECTED),
                "entries": entries,
            }
        finally:
            self.kernel.LocalFree(descriptor)

    def inspect(self, path: Path, *, directory: bool) -> dict:
        with self._existing(path, directory=directory, change=False) as handle:
            return self._read_acl(handle)

    def protect(self, path: Path, *, directory: bool) -> None:
        # Routine writes share private roots. Do not reacquire exclusive mutation
        # access when their owner and DACL already satisfy the contract.
        current = self.inspect(path, directory=directory)
        try:
            _assert_acl(current, directory=directory)
        except OSError:
            pass
        else:
            return
        # SetSecurityInfo documents no child propagation for MAXIMUM_ALLOWED
        # handles. Existing linked children must retain their original ACLs.
        with self._existing(path, directory=directory, change=True) as handle:
            current = self._read_acl(handle)
            if current["owner"] not in {self.user_sid, self.owner_sid}:
                raise OSError(
                    "Refusing to take ownership of another user's Agent artifact."
                )
            with self._descriptor(directory) as descriptor:
                owner, dacl = ctypes.c_void_p(), ctypes.c_void_p()
                defaulted, present = ctypes.c_int32(), ctypes.c_int32()
                if not self.security.GetSecurityDescriptorOwner(
                    descriptor, ctypes.byref(owner), ctypes.byref(defaulted)
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                if (
                    not self.security.GetSecurityDescriptorDacl(
                        descriptor,
                        ctypes.byref(present),
                        ctypes.byref(dacl),
                        ctypes.byref(defaulted),
                    )
                    or not present.value
                    or not dacl.value
                ):
                    raise OSError(
                        "The private Agent descriptor has no restrictive DACL."
                    )
                status = self.security.SetSecurityInfo(
                    handle,
                    _SE_FILE_OBJECT,
                    _OWNER_SECURITY_INFORMATION
                    | _DACL_SECURITY_INFORMATION
                    | _PROTECTED_DACL_SECURITY_INFORMATION,
                    owner,
                    None,
                    dacl,
                    None,
                )
                if status:
                    raise ctypes.WinError(status)
            _assert_acl(self._read_acl(handle), directory=directory)

    def create_directory(self, path: Path) -> None:
        with self._descriptor(True) as descriptor:
            attributes = _SecurityAttributes(
                ctypes.sizeof(_SecurityAttributes), descriptor, 0
            )
            if not self.kernel.CreateDirectoryW(
                self._native_name(path), ctypes.byref(attributes)
            ):
                error = ctypes.get_last_error()
                if error != 183:
                    raise ctypes.WinError(error)
        self.protect(path, directory=True)

    def create_file(self, path: Path) -> int:
        import msvcrt

        with self._descriptor(False) as descriptor:
            attributes = _SecurityAttributes(
                ctypes.sizeof(_SecurityAttributes), descriptor, 0
            )
            handle = self.kernel.CreateFileW(
                self._native_name(path),
                0xC0000000 | _READ_CONTROL | 0x10000,
                0,
                ctypes.byref(attributes),
                1,
                0x80,
                None,
            )
            creation_error = ctypes.get_last_error()
        if handle in (None, _INVALID_HANDLE):
            raise ctypes.WinError(creation_error)
        try:
            _assert_acl(self._read_acl(handle), directory=False)
            descriptor = msvcrt.open_osfhandle(
                handle, os.O_WRONLY | os.O_BINARY | os.O_NOINHERIT
            )
        except BaseException as exc:
            try:
                self._discard_handle(handle)
            except OSError as cleanup_error:
                exc.add_note(
                    f"Removing the empty private Agent file also failed: {cleanup_error}"
                )
            if not self.kernel.CloseHandle(handle):
                exc.add_note(
                    "The private Agent file handle could not be closed after creation failed."
                )
            raise
        return descriptor

    def _rename(self, handle, target: Path) -> None:
        encoded = self._native_name(target).encode("utf-16-le")
        offset = _FileRenameInformation.name.offset
        buffer = ctypes.create_string_buffer(
            max(ctypes.sizeof(_FileRenameInformation), offset + len(encoded) + 2)
        )
        information = _FileRenameInformation.from_buffer(buffer)
        information.flags = 1
        information.root = None
        information.name_length = len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
        if not self.kernel.SetFileInformationByHandle(handle, 3, buffer, len(buffer)):
            raise ctypes.WinError(ctypes.get_last_error())

    def _final_name(self, handle) -> str:
        length = self.kernel.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not length:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(length)
        written = self.kernel.GetFinalPathNameByHandleW(handle, buffer, length, 0)
        if not written:
            raise ctypes.WinError(ctypes.get_last_error())
        if written >= length:
            raise OSError(
                "The retained Agent artifact name changed during verification."
            )
        return ntpath.normcase(ntpath.normpath(buffer.value))

    def _verify_published_name(self, handle, target: Path) -> None:
        expected = ntpath.normcase(ntpath.normpath(self._native_name(target)))
        if self._final_name(handle) != expected:
            raise OSError(
                "Windows renamed the private Agent artifact to an unexpected path."
            )
        parent_handle = getattr(self, "_pinned_parent_handle", None)
        expected_parent = ntpath.normcase(
            ntpath.normpath(self._native_name(target.parent))
        )
        if parent_handle is None or self._final_name(parent_handle) != expected_parent:
            raise OSError(
                "The private Agent destination parent changed during publication."
            )

    def publish(self, descriptor: int, target: Path) -> None:
        """Rename the still-exclusive creation handle, never reopen its path."""
        import msvcrt

        handle = msvcrt.get_osfhandle(descriptor)
        _assert_acl(self._read_acl(handle), directory=False)
        self._rename(handle, target)
        self._verify_published_name(handle, target)
        _assert_acl(self._read_acl(handle), directory=False)

    def _discard_handle(self, handle) -> None:
        disposition = ctypes.c_ubyte(1)
        if not self.kernel.SetFileInformationByHandle(
            handle, 4, ctypes.byref(disposition), 1
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def discard(self, descriptor: int) -> None:
        """Remove only the retained creation handle when its write fails."""
        import msvcrt

        self._discard_handle(msvcrt.get_osfhandle(descriptor))

    @contextmanager
    def pin_parent_chain(self, path: Path):
        """Keep parent names fixed without changing their existing security descriptors."""
        with ExitStack() as stack:
            for parent in (*reversed(path.parent.parents), path.parent):
                parent_handle = stack.enter_context(
                    self._existing(parent, directory=True, change=False)
                )
            previous_parent = getattr(self, "_pinned_parent_handle", None)
            self._pinned_parent_handle = parent_handle
            try:
                yield
            finally:
                self._pinned_parent_handle = previous_parent


def _assert_acl(evidence: dict, *, directory: bool) -> None:
    expected = {
        "type": 0,
        "flags": 3 if directory else 0,
        "mask": _FILE_ALL_ACCESS,
        "sid": evidence["current_user"],
    }
    if (
        evidence["owner"] != evidence["current_user"]
        or not evidence["protected"]
        or evidence["entries"] != [expected]
    ):
        raise OSError(
            "Windows did not establish a protected DACL granting access only to the current user."
        )


def ensure_owner_only_directory(path: Path) -> None:
    """Protect this app-owned directory, without changing existing parent ACLs."""
    target = _checked_path(path)
    if os.name == "nt":
        target.parent.mkdir(parents=True, exist_ok=True)
        api = _WindowsPermissions()
        with api.pin_parent_chain(target):
            api.create_directory(target)
    else:
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.chmod(0o700)


def ensure_owner_only_file(path: Path) -> None:
    """Tighten one existing app-owned file and reject links or foreign ownership."""
    target = _checked_path(path)
    if os.name == "nt":
        api = _WindowsPermissions()
        with api.pin_parent_chain(target):
            api.protect(target, directory=False)
    else:
        metadata = target.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError(
                "Private Agent permissions require one unlinked regular file."
            )
        target.chmod(0o600)


def read_owner_only_acl(path: Path) -> dict:
    """Read actual Windows owner, DACL protection, and ACEs without changing them."""
    target = _checked_path(path)
    return _WindowsPermissions().inspect(target, directory=target.is_dir())


def assert_owner_only_path(path: Path, *, directory: bool) -> None:
    """Verify the native privacy contract rather than Windows' synthetic mode bits."""
    target = _checked_path(path)
    if os.name == "nt":
        _assert_acl(
            _WindowsPermissions().inspect(target, directory=directory),
            directory=directory,
        )
    else:
        metadata = target.lstat()
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected_type(metadata.st_mode) or metadata.st_mode & 0o777 != (
            0o700 if directory else 0o600
        ):
            raise OSError(
                "The Agent artifact does not have owner-only POSIX permissions."
            )


def atomic_write_owner_only_bytes(path: Path, content: bytes) -> None:
    """Write through a private unique sibling whose permissions precede its bytes."""
    target = _checked_path(path)
    if target.exists():
        metadata = target.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("Refusing to replace a linked or non-regular Agent artifact.")
    target.parent.mkdir(parents=True, exist_ok=True)
    api = _WindowsPermissions() if os.name == "nt" else None
    with ExitStack() as parents:
        if api is not None:
            parents.enter_context(api.pin_parent_chain(target))
        _write_private_sibling(target, content, api)


def _write_private_sibling(
    target: Path, content: bytes, api: _WindowsPermissions | None
) -> None:
    """Write and replace while the caller retains native parent handles."""
    if api is not None:
        _write_native_sibling(target, content, api)
        return
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(raw_temporary)
    primary_error = None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            _checked_path(target)
            if target.exists():
                metadata = target.lstat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise OSError(
                        "The Agent destination became linked or non-regular before replacement."
                    )
        os.replace(temporary, target)
        assert_owner_only_path(target, directory=False)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_errors = []
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_errors.append(exc)
        try:
            temporary.unlink(missing_ok=True)
        except OSError as exc:
            cleanup_errors.append(exc)
        if cleanup_errors:
            message = (
                f"Private Agent temporary-file cleanup failed: {cleanup_errors[0]}"
            )
            LOGGER.warning(message)
            if primary_error is not None:
                primary_error.add_note(message)
            else:
                raise OSError(message) from cleanup_errors[0]


def _write_native_sibling(
    target: Path, content: bytes, api: _WindowsPermissions
) -> None:
    def check_destination():
        _checked_path(target)
        if target.exists():
            current = api.inspect(target, directory=False)
            if current["owner"] not in {api.user_sid, api.owner_sid}:
                raise OSError("Refusing to replace another user's Agent artifact.")

    check_destination()
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    descriptor = api.create_file(temporary)
    writer = None
    primary_error = None
    published = False
    try:
        writer = os.fdopen(descriptor, "wb", closefd=False)
        writer.write(content)
        writer.flush()
        os.fsync(descriptor)
        check_destination()
        api.publish(descriptor, target)
        published = True
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_errors = []
        if writer is not None:
            try:
                writer.close()
            except OSError as exc:
                cleanup_errors.append(exc)
        if not published:
            try:
                api.discard(descriptor)
            except OSError as exc:
                cleanup_errors.append(exc)
        try:
            os.close(descriptor)
        except OSError as exc:
            cleanup_errors.append(exc)
        if cleanup_errors:
            message = (
                f"Private Agent temporary-file cleanup failed: {cleanup_errors[0]}"
            )
            LOGGER.warning(message)
            if primary_error is not None:
                primary_error.add_note(message)
            else:
                raise OSError(message) from cleanup_errors[0]


def atomic_write_owner_only_text(path: Path, content: str) -> None:
    """Persist UTF-8 text without platform newline translation."""
    atomic_write_owner_only_bytes(path, content.encode("utf-8"))
