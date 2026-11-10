"""Compatible WCDB decoding retains framing, hashes and bounded private transport.

Code version: v1.0.1-codex.0
"""

import base64
from hashlib import sha256
from io import BytesIO
import importlib
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from app.core import wechat_wcdb_compat as compatible
from app.core.wechat_wcdb import (
    MissingDictionaryError, WCDBBackendUnavailableError, WCDBDecodeError,
)
from app.core.wechat_mac_refresh_source import RefreshError


FRAME = base64.b64decode("KLUv/SAc4QAAU3ludGhldGljIHJldGFpbmVkIHJvbGUg8J+YgA==")
TEXT = "Synthetic retained role 😀"


@pytest.fixture(autouse=True)
def empty_decode_cache():
    compatible._CACHE.clear()
    compatible._CACHE_BYTES = 0


def _missing_backend(*_args, **_kwargs):
    raise WCDBBackendUnavailableError("Synthetic unavailable backend")


def _response(raw=FRAME, text=TEXT, **extra):
    return json.dumps({
        "schema_version": 1, "native_sha256": sha256(raw).hexdigest(),
        "status": "ok", "decoded_text": text,
        "decoded_sha256": sha256(text.encode()).hexdigest(), **extra,
    }).encode()


def has_real_compatible_decoder():
    """Detect a capability without requiring Python 3.14 on every CI machine."""
    try:
        importlib.import_module("compression.zstd")
    except ImportError:
        try:
            compatible._runtime(os.environ.get("AGENTIC_CONTEXT_WECHAT_PYTHON", ""))
        except RefreshError:
            return False
    return True


def test_real_compressed_cell_uses_available_backend_or_fails_explicitly():
    if has_real_compatible_decoder():
        assert compatible.decode_wcdb_text_compatible(FRAME, 4) == TEXT
    else:
        with pytest.raises(WCDBBackendUnavailableError):
            compatible.decode_wcdb_text_compatible(FRAME, 4)
        assert not compatible._CACHE


def test_python_3_13_with_no_configured_compatible_worker_fails_closed(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(compatible, "decode_wcdb_text", _missing_backend)
    monkeypatch.setenv("AGENTIC_CONTEXT_WECHAT_PYTHON", str(tmp_path / "absent-python"))
    monkeypatch.setattr(
        compatible.subprocess, "run",
        lambda *_args, **_kwargs: pytest.fail("An absent interpreter must not run"),
    )
    with pytest.raises(WCDBBackendUnavailableError):
        compatible.decode_wcdb_text_compatible(FRAME, 4)
    assert not compatible._CACHE


def test_plain_cells_do_not_resolve_or_spawn_a_worker(monkeypatch):
    monkeypatch.setattr(
        compatible, "_delegated", lambda *_args: pytest.fail("Plain cells must stay local")
    )
    assert compatible.decode_wcdb_text_compatible("Literal source text", 0) == "Literal source text"


@pytest.mark.parametrize("failure", [WCDBDecodeError("Invalid frame"), MissingDictionaryError(5)])
def test_existing_decoder_rejections_never_delegate_or_relax(failure, monkeypatch):
    def reject(*_args, **_kwargs):
        raise failure
    monkeypatch.setattr(compatible, "decode_wcdb_text", reject)
    monkeypatch.setattr(
        compatible, "_delegated", lambda *_args: pytest.fail("Rejected frames must stay rejected")
    )
    with pytest.raises(type(failure)):
        compatible.decode_wcdb_text_compatible(FRAME, 4)


def test_worker_transport_keeps_cell_bytes_out_of_command_arguments(monkeypatch):
    monkeypatch.setattr(compatible, "decode_wcdb_text", _missing_backend)
    monkeypatch.setattr(compatible, "_runtime", lambda *_args: "/controlled/python3.14")
    captured = {}
    def run(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout=_response())
    monkeypatch.setattr(compatible.subprocess, "run", run)
    assert compatible.decode_wcdb_text_compatible(FRAME, 4) == TEXT
    assert captured["argv"] == [
        "/controlled/python3.14", "-B", "-m", "app.core.wechat_wcdb_compat"
    ]
    request = json.loads(captured["kwargs"]["input"])
    assert base64.b64decode(request["value_base64"]) == FRAME
    assert request["native_sha256"] == sha256(FRAME).hexdigest()
    assert captured["kwargs"]["timeout"] == compatible.MAX_WORKER_SECONDS
    assert captured["kwargs"]["stderr"] is subprocess.DEVNULL


@pytest.mark.parametrize("changes", [
    {"native_sha256": "0" * 64},
    {"decoded_sha256": "0" * 64},
    {"schema_version": True},
    {"status": "partial"},
])
def test_unverified_or_partial_worker_results_are_rejected(changes, monkeypatch):
    monkeypatch.setattr(compatible, "decode_wcdb_text", _missing_backend)
    monkeypatch.setattr(compatible, "_runtime", lambda *_args: "/controlled/python3.14")
    monkeypatch.setattr(compatible.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout=_response(**changes)
    ))
    with pytest.raises(WCDBDecodeError):
        compatible.decode_wcdb_text_compatible(FRAME, 4)


def test_worker_missing_dictionary_remains_an_explicit_dictionary_gap(monkeypatch):
    monkeypatch.setattr(compatible, "decode_wcdb_text", _missing_backend)
    monkeypatch.setattr(compatible, "_runtime", lambda *_args: "/controlled/python3.14")
    monkeypatch.setattr(compatible.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout=_response(status="missing_dictionary", dictionary_id=5)
    ))
    with pytest.raises(MissingDictionaryError) as error:
        compatible.decode_wcdb_text_compatible(FRAME, 4)
    assert error.value.dictionary_id == 5


def test_worker_timeout_cannot_return_cached_or_partial_text(monkeypatch):
    monkeypatch.setattr(compatible, "decode_wcdb_text", _missing_backend)
    monkeypatch.setattr(compatible, "_runtime", lambda *_args: "/controlled/python3.14")
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("fixed worker", compatible.MAX_WORKER_SECONDS)
    monkeypatch.setattr(compatible.subprocess, "run", timeout)
    with pytest.raises(WCDBBackendUnavailableError):
        compatible.decode_wcdb_text_compatible(FRAME, 4)
    assert not compatible._CACHE


def test_worker_and_cache_budgets_are_enforced(monkeypatch):
    monkeypatch.setattr(compatible, "decode_wcdb_text", _missing_backend)
    monkeypatch.setattr(compatible, "MAX_CACHE_ENTRIES", 2)
    monkeypatch.setattr(compatible, "MAX_CACHE_BYTES", 4)
    calls = []
    def decode(raw, _marker, _limit):
        calls.append(raw)
        return raw.decode() + "x"
    monkeypatch.setattr(compatible, "_delegated", decode)
    for raw in (b"1", b"2", b"3", b"3"):
        compatible.decode_wcdb_text_compatible(raw, 4)
    assert calls == [b"1", b"2", b"3"]
    assert len(compatible._CACHE) == 2
    assert compatible._CACHE_BYTES == 4
    with pytest.raises(WCDBDecodeError):
        compatible.decode_wcdb_text_compatible(b"x" * (compatible.MAX_INPUT_BYTES + 1), 4)


def test_worker_cli_rejects_unbound_request_without_source_text(monkeypatch):
    output = BytesIO()
    monkeypatch.setattr(compatible.sys, "stdin", SimpleNamespace(buffer=BytesIO(b'{}')))
    monkeypatch.setattr(compatible.sys, "stdout", SimpleNamespace(buffer=output))
    assert compatible._worker_main() == 0
    result = json.loads(output.getvalue())
    assert result["status"] == "rejected"
    assert "decoded_text" not in result
