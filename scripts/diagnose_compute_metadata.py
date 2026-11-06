"""Capture one real Windows rename/read boundary without changing production source.

Code version: v1.0.0-codex.0
"""
from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.agent import compute_jobs

assert os.name == "nt", "This probe requires the real Windows kernel."
output = Path("test-results/compute-metadata-diagnostic")
output.mkdir(parents=True, exist_ok=True)
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel.CreateFileW.restype = wintypes.HANDLE
kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
kernel.SetFileInformationByHandle.restype = wintypes.BOOL
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
kernel.CloseHandle.restype = wintypes.BOOL
record = {
    "version": "v1.0.0-codex.0", "head": os.environ["GITHUB_SHA"],
    "run": os.environ["GITHUB_RUN_ID"], "attempt": os.environ["GITHUB_RUN_ATTEMPT"],
    "python": sys.version, "platform": platform.platform(), "timezone": os.environ.get("TZ"),
    "pytest": importlib.metadata.version("pytest"),
    "source_sha256": hashlib.sha256(Path(compute_jobs.__file__).read_bytes()).hexdigest(),
    "source_mutated": False, "results": [],
}
with tempfile.TemporaryDirectory(prefix="compute-metadata-native-") as temporary:
    root = Path(temporary)
    workspace = root / "workspace"
    workspace.mkdir()
    manager = compute_jobs.ComputeJobManager(workspace, root / "runtime")
    job_id = "a" * 32
    path = manager._metadata_path(job_id)
    payload = {"schema_version": 1, "job_id": job_id, "workspace": str(workspace), "workspace_device": manager.workspace_device, "workspace_inode": manager.workspace_inode, "state": "succeeded", "revision": 0}
    compute_jobs._atomic_write_json(path, payload)
    replacement = path.with_name("replacement.json")
    updated = {**payload, "revision": 1}
    compute_jobs._atomic_write_json(replacement, updated)
    held, release = Event(), Event()

    def publish_and_hold_rename_handle():
        with compute_jobs._metadata_file_lock(path):
            handle = kernel.CreateFileW(str(replacement), 0x00010000, 0x00000007, None, 3, 0, None)
            if handle is None or handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                destination = str(path.absolute())
                class RenameInfo(ctypes.Structure):
                    _fields_ = [("Flags", wintypes.DWORD), ("RootDirectory", wintypes.HANDLE), ("FileNameLength", wintypes.DWORD), ("FileName", wintypes.WCHAR * len(destination))]
                info = RenameInfo()
                info.Flags = 1
                info.FileNameLength = len(destination.encode("utf-16-le"))
                info.FileName = destination
                if not kernel.SetFileInformationByHandle(handle, 3, ctypes.byref(info), ctypes.sizeof(info)):
                    raise ctypes.WinError(ctypes.get_last_error())
                held.set()
                assert release.wait(10), "The reader did not release the controlled rename boundary."
            finally:
                assert kernel.CloseHandle(handle)

    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(publish_and_hold_rename_handle)
        try:
            assert held.wait(5), "The native writer did not reach its rename boundary."
            for name, read in (("crt_read", lambda: path.read_text(encoding="utf-8")), ("manager_load_metadata", lambda: manager._load_metadata(job_id)), ("workspace_scan", manager._all_metadata)):
                try:
                    read()
                except (OSError, compute_jobs.ComputeJobError) as error:
                    cause = error.__cause__ or error
                    item = {"reader": name, "error": type(error).__name__, "cause": type(cause).__name__, "errno": getattr(cause, "errno", None), "winerror": getattr(cause, "winerror", None)}
                    assert item["errno"] == 13 and item["winerror"] == 32, item
                    record["results"].append(item)
                else:
                    raise AssertionError(f"Unprotected reader {name} unexpectedly crossed the native DELETE handle.")
        finally:
            release.set()
        writer.result(timeout=5)
    assert manager._load_metadata(job_id) == updated
    assert manager._all_metadata() == [updated]
    assert not replacement.exists()
    record["after_writer_handle_closed"] = "Both original readers return the complete published revision."
    record["deterministic_old_source_red"] = True
    record["original_ci_errno13_winerror"] = "Not recorded in the original log; this probe proves the matching source defect only."
(output / "native-rename-read.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
print(json.dumps(record, indent=2))
