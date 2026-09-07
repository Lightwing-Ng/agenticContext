"""Exercise Windows directory metadata limitations. Code version: v1.0.0-codex.1."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import computer_use_agent as agent


@pytest.mark.parametrize("filename", ["sample.py", "verify.cmd"])
def test_fingerprint_reads_authoritative_identity_when_directory_cache_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    source = workspace / filename
    source.write_bytes(b"value = 1\n")
    scandir = agent.os.scandir

    def windows_directory_entries(directory):
        entries = []
        with scandir(directory) as listing:
            for entry in listing:
                metadata = entry.stat(follow_symlinks=False)
                cached = SimpleNamespace(**{
                    name: getattr(metadata, name)
                    for name in dir(metadata) if name.startswith("st_")
                })
                cached.st_ino = cached.st_dev = cached.st_nlink = 0
                entries.append(SimpleNamespace(
                    path=entry.path, name=entry.name,
                    stat=lambda *, follow_symlinks, cached=cached: cached,
                ))
        return iter(entries)

    monkeypatch.setattr(agent.os, "scandir", windows_directory_entries)
    first, first_complete = agent._workspace_mutation_fingerprint(workspace)
    source.write_bytes(b"value = 2\n")
    second, second_complete = agent._workspace_mutation_fingerprint(workspace)

    assert first_complete and second_complete
    assert first != second
