"""Native Windows identities and lifetime containment for compute workers.

Code version: v1.0.1-codex.1
"""

from __future__ import annotations

import ctypes
import math
import os
import time


_DWORD = ctypes.c_uint32
_HANDLE = ctypes.c_void_p
_SIZE_T = ctypes.c_size_t
_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x100000
_PROCESS_TERMINATE = 0x0001
_JOB_QUERY = 0x0004
_JOB_TERMINATE = 0x0008
_KILL_ON_JOB_CLOSE = 0x2000
_WAIT_TIMEOUT = 258


class _FileTime(ctypes.Structure):
    _fields_ = [("low", _DWORD), ("high", _DWORD)]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", _DWORD),
        ("minimum_working_set", _SIZE_T),
        ("maximum_working_set", _SIZE_T),
        ("active_process_limit", _DWORD),
        ("affinity", _SIZE_T),
        ("priority", _DWORD),
        ("scheduling", _DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "read_operations", "write_operations", "other_operations",
        "read_bytes", "write_bytes", "other_bytes",
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", _IoCounters),
        ("process_memory", _SIZE_T),
        ("job_memory", _SIZE_T),
        ("peak_process_memory", _SIZE_T),
        ("peak_job_memory", _SIZE_T),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("user_time", ctypes.c_int64), ("kernel_time", ctypes.c_int64),
        ("period_user_time", ctypes.c_int64), ("period_kernel_time", ctypes.c_int64),
        ("page_faults", _DWORD), ("total_processes", _DWORD),
        ("active_processes", _DWORD), ("terminated_processes", _DWORD),
    ]


def _kernel():
    """Bind pointer-sized handles explicitly; import remains safe on other hosts."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "OpenProcess": ([_DWORD, ctypes.c_int, _DWORD], _HANDLE),
        "CloseHandle": ([_HANDLE], ctypes.c_int),
        "GetProcessTimes": ([_HANDLE, *([ctypes.POINTER(_FileTime)] * 4)], ctypes.c_int),
        "WaitForSingleObject": ([_HANDLE, _DWORD], _DWORD),
        "CreateJobObjectW": ([ctypes.c_void_p, ctypes.c_wchar_p], _HANDLE),
        "OpenJobObjectW": ([_DWORD, ctypes.c_int, ctypes.c_wchar_p], _HANDLE),
        "SetInformationJobObject": ([_HANDLE, ctypes.c_int, ctypes.c_void_p, _DWORD], ctypes.c_int),
        "QueryInformationJobObject": ([_HANDLE, ctypes.c_int, ctypes.c_void_p, _DWORD, ctypes.c_void_p], ctypes.c_int),
        "AssignProcessToJobObject": ([_HANDLE, _HANDLE], ctypes.c_int),
        "GetCurrentProcess": ([], _HANDLE),
        "TerminateJobObject": ([_HANDLE, ctypes.c_uint], ctypes.c_int),
        "TerminateProcess": ([_HANDLE, ctypes.c_uint], ctypes.c_int),
        "IsProcessInJob": ([_HANDLE, _HANDLE, ctypes.POINTER(ctypes.c_int)], ctypes.c_int),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes = arguments
        function.restype = result
    return kernel


def process_identity(pid: int) -> str:
    """Read a live process creation time without invoking a shell or parsing ps."""
    kernel = _kernel()
    handle = kernel.OpenProcess(_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
    if not handle:
        return ""
    try:
        return _handle_identity(kernel, handle)
    finally:
        kernel.CloseHandle(handle)


def _handle_identity(kernel, handle) -> str:
    if kernel.WaitForSingleObject(handle, 0) != _WAIT_TIMEOUT:
        return ""
    created, exited, kernel_time, user_time = (_FileTime() for _ in range(4))
    if not kernel.GetProcessTimes(
        handle, ctypes.byref(created), ctypes.byref(exited),
        ctypes.byref(kernel_time), ctypes.byref(user_time),
    ):
        return ""
    return f"windows:{(created.high << 32) | created.low:016x}"


def _check(success: int) -> None:
    if not success:
        raise ctypes.WinError(ctypes.get_last_error())


def job_name(job_id: str) -> str:
    return "Local\\agenticContext-compute-" + job_id


class WorkerJob:
    """Contain descendants if the supervisor exits, including abrupt termination.

    This controls process lifetime only. It grants no filesystem or network isolation.
    """

    def __init__(self, name: str) -> None:
        self.kernel = _kernel()
        ctypes.set_last_error(0)
        self.handle = self.kernel.CreateJobObjectW(None, name)
        _check(bool(self.handle))
        if ctypes.get_last_error() == 183:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
            raise OSError("Compute job object name is already in use.")
        try:
            self._set_kill_on_close(True)
            _check(self.kernel.AssignProcessToJobObject(self.handle, self.kernel.GetCurrentProcess()))
        except BaseException:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
            raise

    def _set_kill_on_close(self, enabled: bool) -> None:
        limits = _ExtendedLimits()
        limits.basic.flags = _KILL_ON_JOB_CLOSE if enabled else 0
        _check(self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))

    def _process_ids(self) -> list[int]:
        capacity = 64
        while capacity <= 65_536:
            class ProcessIds(ctypes.Structure):
                _fields_ = [
                    ("assigned", _DWORD), ("count", _DWORD),
                    ("ids", _SIZE_T * capacity),
                ]

            record = ProcessIds()
            if self.kernel.QueryInformationJobObject(self.handle, 3, ctypes.byref(record), ctypes.sizeof(record), None):
                return [int(pid) for pid in record.ids[:record.count]]
            if ctypes.get_last_error() != 234:
                _check(False)
            capacity *= 2
        raise OSError("Compute job process inventory exceeds its safe bound.")

    def terminate_descendants(self, *, timeout: float = 5.0) -> None:
        """Keep the supervisor alive to publish metadata after descendants exit."""
        deadline = time.monotonic() + timeout
        while True:
            descendants = [pid for pid in self._process_ids() if pid != os.getpid()]
            if not descendants:
                return
            for pid in descendants:
                process = self.kernel.OpenProcess(_PROCESS_TERMINATE | _QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
                if not process:
                    continue
                try:
                    member = ctypes.c_int()
                    _check(self.kernel.IsProcessInJob(process, self.handle, ctypes.byref(member)))
                    if member.value:
                        terminated = self.kernel.TerminateProcess(process, 1)
                        if not terminated and self.kernel.WaitForSingleObject(process, 0) != 0:
                            _check(False)
                finally:
                    self.kernel.CloseHandle(process)
            if time.monotonic() >= deadline:
                raise OSError("Compute job descendants did not terminate within the deadline.")
            time.sleep(0.05)

    def close_after_cleanup(self) -> None:
        """Disarm only after no child remains; an exceptional exit keeps containment."""
        self.terminate_descendants()
        self._set_kill_on_close(False)
        _check(self.kernel.CloseHandle(self.handle))
        self.handle = None


def terminate_job(name: str, *, pid: int, expected_identity: str, timeout: float = 5.0) -> None:
    """Wait for the verified worker to exit and its job to report no active members."""
    kernel = _kernel()
    handle = kernel.OpenJobObjectW(_JOB_QUERY | _JOB_TERMINATE, False, name)
    _check(bool(handle))
    worker = None
    try:
        worker = kernel.OpenProcess(_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
        _check(bool(worker))
        if not expected_identity or _handle_identity(kernel, worker) != expected_identity:
            raise OSError("Compute worker identity changed before job termination.")
        member = ctypes.c_int()
        _check(kernel.IsProcessInJob(worker, handle, ctypes.byref(member)))
        if not member.value:
            raise OSError("Compute worker no longer belongs to its recorded job.")
        _check(kernel.TerminateJobObject(handle, 1))
        deadline = time.monotonic() + timeout
        # Job accounting can reach zero before asynchronous process teardown ends.
        # Keep the verified handle open so waiting cannot target a reused PID.
        milliseconds = min(0xFFFFFFFE, max(0, math.ceil((deadline - time.monotonic()) * 1000)))
        result = kernel.WaitForSingleObject(worker, milliseconds)
        if result == 0xFFFFFFFF:
            _check(False)
        if result != 0:
            raise OSError("Compute worker termination could not be confirmed.")
        while True:
            accounting = _Accounting()
            _check(kernel.QueryInformationJobObject(handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None))
            if accounting.active_processes == 0:
                return
            if time.monotonic() >= deadline:
                raise OSError("Compute job termination could not be confirmed.")
            time.sleep(0.05)
    finally:
        if worker:
            kernel.CloseHandle(worker)
        kernel.CloseHandle(handle)
