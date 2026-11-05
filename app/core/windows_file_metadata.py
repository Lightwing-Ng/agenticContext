"""Read Windows change metadata without opening a file's payload for reading.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _native_api():
    import ctypes
    from ctypes import wintypes

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return ctypes, kernel32, FileBasicInfo


def file_change_time(path: Path, *, descriptor: int | None = None) -> int:
    """Compare the same native ChangeTime for a pathname and an existing handle.

    Python's Windows stat(path).ctime and fstat(fd).ctime currently have different
    meanings. FILE_BASIC_INFO supplies the mutation token directly. The temporary
    pathname handle requests metadata only and never follows a reparse point.
    """
    ctypes, kernel32, info_type = _native_api()
    own_handle = descriptor is None
    if own_handle:
        handle = kernel32.CreateFileW(
            str(path), 0x0080, 0x00000001 | 0x00000002 | 0x00000004,
            None, 3, 0x00200000, None,
        )
        if handle is None or handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        import msvcrt

        handle = msvcrt.get_osfhandle(descriptor)
    try:
        info = info_type()
        if not kernel32.GetFileInformationByHandleEx(
            handle, 0, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.FileAttributes & (0x00000400 | 0x00000010):
            raise OSError("Media metadata requires a regular non-reparse file.")
        return int(info.ChangeTime)
    finally:
        if own_handle:
            kernel32.CloseHandle(handle)
