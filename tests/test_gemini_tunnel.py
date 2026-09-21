"""Gemini Tunnel configuration and OAuth authority tests.

Code version: v1.2.0-codex.0
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core import gemini_tunnel
from app.core.gemini_tunnel import (
    ACCESS_TOKEN_TTL_SECONDS,
    AUTHORIZATION_CODE_TTL_SECONDS,
    GEMINI_TUNNEL_SCHEMA_VERSION,
    MCP_SCOPE,
    REFRESH_TOKEN_TTL_SECONDS,
    GeminiOAuthAuthority,
    GeminiOAuthError,
    GeminiTunnelConfig,
    GeminiTunnelConfigError,
    authorization_server_metadata,
    clear_gemini_tunnel_config,
    configure_gemini_tunnel,
    default_gemini_tunnel_config_path,
    load_gemini_tunnel_config,
    protected_resource_metadata,
    protected_resource_metadata_url,
    validate_public_origin,
    validate_redirect_uri,
    www_authenticate_challenge,
)


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTIC_CONTEXT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    return default_gemini_tunnel_config_path()


@pytest.fixture
def config(config_path: Path) -> GeminiTunnelConfig:
    return configure_gemini_tunnel("https://mcp.example.com", config_path)


@pytest.fixture
def clock() -> list[float]:
    return [1_800_000_000.0]


@pytest.fixture
def authority(config: GeminiTunnelConfig, clock: list[float]) -> GeminiOAuthAuthority:
    return GeminiOAuthAuthority(config, clock=lambda: clock[0])


def _pkce(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )


def _authorization_code(
    authority: GeminiOAuthAuthority,
    config: GeminiTunnelConfig,
    *,
    verifier: str = "A" * 43,
) -> str:
    return authority.issue_authorization_code(
        client_id=config.client_id,
        redirect_uri="https://gemini.example/callback",
        resource=config.mcp_url,
        scope=MCP_SCOPE,
        code_challenge=_pkce(verifier),
        code_challenge_method="S256",
    )


def _exchange(
    authority: GeminiOAuthAuthority,
    config: GeminiTunnelConfig,
    code: str,
    *,
    verifier: str = "A" * 43,
) -> dict[str, object]:
    return authority.exchange_authorization_code(
        code=code,
        client_id=config.client_id,
        client_secret=config.client_secret,
        redirect_uri="https://gemini.example/callback",
        resource=config.mcp_url,
        scope=MCP_SCOPE,
        code_verifier=verifier,
    )


def test_configure_persists_owner_only_static_credentials_and_safe_snapshot(
    config_path: Path,
) -> None:
    assert config_path.name == "gemini-tunnel-credentials.json"
    config = configure_gemini_tunnel("https://MCP.Example.COM:443/", config_path)

    assert config.public_origin == "https://mcp.example.com"
    assert config.mcp_url == "https://mcp.example.com/mcp/gemini"
    assert re.fullmatch(r"gtc_[A-Za-z0-9_-]{32,}", config.client_id)
    assert re.fullmatch(r"gts_[A-Za-z0-9_-]{64,}", config.client_secret)
    assert re.fullmatch(r"gtk_[A-Za-z0-9_-]{64,}", config.signing_key)
    assert load_gemini_tunnel_config(config_path) == config
    if os.name == "posix":
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert not list(config_path.parent.glob(f".{config_path.name}.*.tmp"))

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert set(payload) == {
        "schema_version",
        "public_origin",
        "client_id",
        "client_secret",
        "signing_key",
    }
    snapshot = config.snapshot()
    assert snapshot == {
        "schema_version": GEMINI_TUNNEL_SCHEMA_VERSION,
        "configured": True,
        "public_origin": "https://mcp.example.com",
        "mcp_url": "https://mcp.example.com/mcp/gemini",
        "client_id": config.client_id,
        "client_secret_saved": True,
        "signing_key_saved": True,
    }
    serialized_snapshot = json.dumps(snapshot)
    assert config.client_secret not in serialized_snapshot
    assert config.signing_key not in serialized_snapshot
    assert 'client_secret"' not in serialized_snapshot
    assert 'signing_key"' not in serialized_snapshot
    assert config.client_secret not in repr(config)
    assert config.signing_key not in repr(config)

    same = configure_gemini_tunnel("https://mcp.example.com", config_path)
    assert same == config
    rotated = configure_gemini_tunnel("https://other.example.com", config_path)
    assert rotated.public_origin == "https://other.example.com"
    assert rotated.client_id != config.client_id
    assert rotated.client_secret != config.client_secret
    assert rotated.signing_key != config.signing_key

    assert clear_gemini_tunnel_config(config_path) == GeminiTunnelConfig()
    assert load_gemini_tunnel_config(config_path) == GeminiTunnelConfig()
    if os.name == "posix":
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "value",
    (
        "http://mcp.example.com",
        "https://user@mcp.example.com",
        "https://user:secret@mcp.example.com",
        "https://mcp.example.com/path",
        "https://mcp.example.com?query=yes",
        "https://mcp.example.com#fragment",
        "https://localhost",
        "https://service.localhost",
        "https://service.local",
        "https://service.internal",
        "https://service.home.arpa",
        "https://intranet",
        "https://*.example.com",
        "https://127.0.0.1",
        "https://10.0.0.1",
        "https://192.168.1.1",
        "https://169.254.1.1",
        "https://8.8.8.8",
        "https://[::1]",
        "https://127.1",
        " https://mcp.example.com",
        "https://mcp.example.com\n",
        "https://mcp.example.com/\\evil",
        "https://mcp.example.com:",
        "https://mcp.example.com:70000",
    ),
)
def test_public_origin_rejects_non_public_or_non_origin_values(value: str) -> None:
    with pytest.raises(GeminiTunnelConfigError):
        validate_public_origin(value)


def test_public_origin_accepts_only_canonical_public_https_origins(
    config_path: Path,
) -> None:
    assert (
        validate_public_origin("https://MCP.Example.COM:443/")
        == "https://mcp.example.com"
    )
    assert validate_public_origin("https://mcp.example.com:8443") == (
        "https://mcp.example.com:8443"
    )
    with pytest.raises(GeminiTunnelConfigError):
        configure_gemini_tunnel("https://localhost", config_path)
    assert not config_path.exists()


def test_redirect_uri_is_public_https_and_preserves_path_and_query() -> None:
    assert validate_redirect_uri(
        "https://Gemini.Example/oauth/callback?flow=custom"
    ) == ("https://Gemini.Example/oauth/callback?flow=custom")
    for value in (
        "http://gemini.example/callback",
        "https://localhost/callback",
        "https://127.0.0.1/callback",
        "https://user@gemini.example/callback",
        "https://gemini.example/callback#fragment",
        f"https://gemini.example/{'a' * 2_048}",
    ):
        with pytest.raises(GeminiTunnelConfigError):
            validate_redirect_uri(value)


def test_load_rejects_broad_permissions_unknown_fields_and_invalid_secrets(
    config_path: Path,
) -> None:
    config = configure_gemini_tunnel("https://mcp.example.com", config_path)
    if os.name == "posix":
        config_path.chmod(0o644)
        with pytest.raises(GeminiTunnelConfigError, match="owner-only"):
            load_gemini_tunnel_config(config_path)
        config_path.chmod(0o600)

    payload = config._payload()
    payload["unexpected"] = True
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    config_path.chmod(0o600)
    with pytest.raises(GeminiTunnelConfigError, match="field set"):
        load_gemini_tunnel_config(config_path)

    payload.pop("unexpected")
    payload["client_secret"] = "short"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    config_path.chmod(0o600)
    with pytest.raises(GeminiTunnelConfigError, match="client_secret"):
        load_gemini_tunnel_config(config_path)


def test_atomic_save_failure_preserves_the_previous_configuration(
    config_path: Path,
) -> None:
    original = configure_gemini_tunnel("https://mcp.example.com", config_path)
    original_bytes = config_path.read_bytes()

    with patch("app.core.gemini_tunnel.os.replace", side_effect=OSError("blocked")):
        with pytest.raises(OSError, match="blocked"):
            configure_gemini_tunnel("https://other.example.com", config_path)

    assert config_path.read_bytes() == original_bytes
    assert load_gemini_tunnel_config(config_path) == original
    assert not list(config_path.parent.glob(f".{config_path.name}.*.tmp"))


def test_concurrent_same_origin_configuration_returns_one_static_credential_set(
    config_path: Path,
) -> None:
    barrier = threading.Barrier(8)
    results: list[GeminiTunnelConfig] = []
    results_lock = threading.Lock()

    def configure() -> None:
        barrier.wait()
        saved = configure_gemini_tunnel("https://mcp.example.com", config_path)
        with results_lock:
            results.append(saved)

    threads = [threading.Thread(target=configure) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 8
    assert all(result == results[0] for result in results)
    assert load_gemini_tunnel_config(config_path) == results[0]


def test_oauth_metadata_and_www_authenticate_contain_no_secrets(
    config: GeminiTunnelConfig,
) -> None:
    protected = protected_resource_metadata(config)
    assert protected == {
        "resource": "https://mcp.example.com/mcp/gemini",
        "authorization_servers": ["https://mcp.example.com"],
        "scopes_supported": [MCP_SCOPE],
        "bearer_methods_supported": ["header"],
    }
    authority = authorization_server_metadata(config)
    assert authority["issuer"] == "https://mcp.example.com"
    assert (
        authority["authorization_endpoint"] == "https://mcp.example.com/oauth/authorize"
    )
    assert authority["token_endpoint"] == "https://mcp.example.com/oauth/token"
    assert authority["code_challenge_methods_supported"] == ["S256"]
    assert authority["authorization_response_iss_parameter_supported"] is True
    assert authority["scopes_supported"] == [MCP_SCOPE]
    assert protected_resource_metadata_url(config) == (
        "https://mcp.example.com/.well-known/oauth-protected-resource/mcp/gemini"
    )

    challenge = www_authenticate_challenge(
        config,
        error="invalid_token",
        error_description='Expired\r\n"token"',
    )
    assert challenge == (
        'Bearer resource_metadata="https://mcp.example.com/'
        '.well-known/oauth-protected-resource/mcp/gemini", scope="mcp:tools", '
        'error="invalid_token", error_description="Expired \\"token\\""'
    )
    serialized = json.dumps([protected, authority, challenge])
    assert config.client_secret not in serialized
    assert config.signing_key not in serialized
    with pytest.raises(ValueError, match="Unsupported"):
        www_authenticate_challenge(config, error="made_up")


def test_pkce_authorization_code_exchange_and_hmac_access_token(
    authority: GeminiOAuthAuthority,
    config: GeminiTunnelConfig,
) -> None:
    code = _authorization_code(authority, config)
    token_response = _exchange(authority, config, code)

    assert token_response["token_type"] == "Bearer"
    assert token_response["expires_in"] == ACCESS_TOKEN_TTL_SECONDS
    assert token_response["scope"] == MCP_SCOPE
    assert isinstance(token_response["refresh_token"], str)
    access_token = str(token_response["access_token"])
    assert access_token.count(".") == 2
    claims = authority.verify_access_token(access_token, resource=config.mcp_url)
    assert claims["iss"] == config.public_origin
    assert claims["sub"] == config.client_id
    assert claims["client_id"] == config.client_id
    assert claims["aud"] == config.mcp_url
    assert claims["scope"] == MCP_SCOPE
    assert claims["exp"] - claims["iat"] == ACCESS_TOKEN_TTL_SECONDS

    with pytest.raises(GeminiOAuthError, match="invalid or expired") as replay:
        _exchange(authority, config, code)
    assert replay.value.error == "invalid_grant"

    restarted = GeminiOAuthAuthority(config, clock=lambda: float(claims["iat"]))
    with pytest.raises(GeminiOAuthError, match="unknown or expired"):
        restarted.verify_access_token(access_token, resource=config.mcp_url)


def test_authorization_requires_exact_client_redirect_resource_scope_and_s256(
    authority: GeminiOAuthAuthority,
    config: GeminiTunnelConfig,
) -> None:
    valid = {
        "client_id": config.client_id,
        "redirect_uri": "https://gemini.example/callback",
        "resource": config.mcp_url,
        "scope": MCP_SCOPE,
        "code_challenge": _pkce("A" * 43),
        "code_challenge_method": "S256",
    }
    assert authority.validate_authorization_request(**valid) == (
        "https://gemini.example/callback"
    )
    mutations = (
        ("client_id", f"{config.client_id}x", "unauthorized_client"),
        ("redirect_uri", "http://gemini.example/callback", "invalid_request"),
        ("resource", f"{config.public_origin}/other", "invalid_target"),
        ("scope", "mcp:tools extra", "invalid_scope"),
        ("code_challenge", "short", "invalid_request"),
        ("code_challenge_method", "plain", "invalid_request"),
    )
    for field, value, expected_error in mutations:
        arguments = {**valid, field: value}
        with pytest.raises(GeminiOAuthError) as captured:
            authority.validate_authorization_request(**arguments)
        assert captured.value.error == expected_error


def test_failed_token_checks_do_not_consume_the_code(
    authority: GeminiOAuthAuthority,
    config: GeminiTunnelConfig,
) -> None:
    code = _authorization_code(authority, config)
    base = {
        "code": code,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "redirect_uri": "https://gemini.example/callback",
        "resource": config.mcp_url,
        "scope": MCP_SCOPE,
        "code_verifier": "A" * 43,
    }
    mutations = (
        ("client_secret", f"{config.client_secret}x", "invalid_client", 401),
        ("redirect_uri", "https://gemini.example/other", "invalid_grant", 400),
        ("redirect_uri", "https://GEMINI.example/callback", "invalid_grant", 400),
        ("resource", f"{config.public_origin}/other", "invalid_target", 400),
        ("scope", "other", "invalid_scope", 400),
        ("code_verifier", "B" * 43, "invalid_grant", 400),
    )
    for field, value, expected_error, expected_status in mutations:
        arguments = {**base, field: value}
        with pytest.raises(GeminiOAuthError) as captured:
            authority.exchange_authorization_code(**arguments)
        assert captured.value.error == expected_error
        assert captured.value.status_code == expected_status

    assert _exchange(authority, config, code)["token_type"] == "Bearer"


def test_authorization_code_and_access_token_expire(
    authority: GeminiOAuthAuthority,
    config: GeminiTunnelConfig,
    clock: list[float],
) -> None:
    expired_code = _authorization_code(authority, config)
    clock[0] += AUTHORIZATION_CODE_TTL_SECONDS
    with pytest.raises(GeminiOAuthError) as captured:
        _exchange(authority, config, expired_code)
    assert captured.value.error == "invalid_grant"

    code = _authorization_code(authority, config)
    response = _exchange(authority, config, code)
    access_token = str(response["access_token"])
    authority.verify_access_token(access_token, resource=config.mcp_url)
    clock[0] += ACCESS_TOKEN_TTL_SECONDS
    with pytest.raises(GeminiOAuthError) as expired:
        authority.verify_access_token(access_token, resource=config.mcp_url)
    assert expired.value.error == "invalid_token"
    assert expired.value.status_code == 401


def test_refresh_token_rotates_and_stays_exactly_bound(
    authority: GeminiOAuthAuthority,
    config: GeminiTunnelConfig,
) -> None:
    response = _exchange(authority, config, _authorization_code(authority, config))
    original_refresh = str(response["refresh_token"])
    refreshed = authority.refresh_access_token(
        refresh_token=original_refresh,
        client_id=config.client_id,
        client_secret=config.client_secret,
        resource=config.mcp_url,
        scope=MCP_SCOPE,
    )
    assert refreshed["refresh_token"] != original_refresh
    authority.verify_access_token(
        str(refreshed["access_token"]), resource=config.mcp_url
    )

    with pytest.raises(GeminiOAuthError) as reused:
        authority.refresh_access_token(
            refresh_token=original_refresh,
            client_id=config.client_id,
            client_secret=config.client_secret,
            resource=config.mcp_url,
            scope=MCP_SCOPE,
        )
    assert reused.value.error == "invalid_grant"

    with pytest.raises(GeminiOAuthError) as expanded:
        authority.refresh_access_token(
            refresh_token=str(refreshed["refresh_token"]),
            client_id=config.client_id,
            client_secret=config.client_secret,
            resource=config.mcp_url,
            scope=f"{MCP_SCOPE} extra",
        )
    assert expanded.value.error == "invalid_scope"


def test_refresh_token_expires(
    authority: GeminiOAuthAuthority,
    config: GeminiTunnelConfig,
    clock: list[float],
) -> None:
    response = _exchange(authority, config, _authorization_code(authority, config))
    clock[0] += REFRESH_TOKEN_TTL_SECONDS
    with pytest.raises(GeminiOAuthError) as captured:
        authority.refresh_access_token(
            refresh_token=str(response["refresh_token"]),
            client_id=config.client_id,
            client_secret=config.client_secret,
            resource=config.mcp_url,
            scope=MCP_SCOPE,
        )
    assert captured.value.error == "invalid_grant"


def test_access_token_rejects_tampering_and_wrong_resource(
    authority: GeminiOAuthAuthority,
    config: GeminiTunnelConfig,
) -> None:
    response = _exchange(authority, config, _authorization_code(authority, config))
    access_token = str(response["access_token"])
    header, payload, signature = access_token.split(".")
    replacement = "A" if payload[0] != "A" else "B"
    tampered = f"{header}.{replacement}{payload[1:]}.{signature}"
    with pytest.raises(GeminiOAuthError, match="signature"):
        authority.verify_access_token(tampered, resource=config.mcp_url)

    with pytest.raises(GeminiOAuthError) as wrong_resource:
        authority.verify_access_token(
            access_token,
            resource=f"{config.public_origin}/other",
        )
    assert wrong_resource.value.error == "invalid_target"

    with pytest.raises(GeminiOAuthError) as malformed:
        authority.verify_access_token("not-a-token", resource=config.mcp_url)
    assert malformed.value.error == "invalid_token"
    assert malformed.value.status_code == 401


def test_revoke_all_invalidates_tokens_and_disables_the_old_authority(
    authority: GeminiOAuthAuthority,
    config: GeminiTunnelConfig,
) -> None:
    response = _exchange(authority, config, _authorization_code(authority, config))
    authority.revoke_all()

    with pytest.raises(GeminiOAuthError, match="revoked") as access:
        authority.verify_access_token(
            str(response["access_token"]),
            resource=config.mcp_url,
        )
    assert access.value.error == "invalid_token"
    assert access.value.status_code == 401

    with pytest.raises(GeminiOAuthError, match="revoked") as refresh:
        authority.refresh_access_token(
            refresh_token=str(response["refresh_token"]),
            client_id=config.client_id,
            client_secret=config.client_secret,
            resource=config.mcp_url,
            scope=MCP_SCOPE,
        )
    assert refresh.value.error == "invalid_grant"

    with pytest.raises(GeminiOAuthError, match="revoked") as authorize:
        _authorization_code(authority, config)
    assert authorize.value.error == "invalid_request"


def test_authorization_and_refresh_grant_stores_are_bounded(
    authority: GeminiOAuthAuthority,
    config: GeminiTunnelConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gemini_tunnel, "MAX_IN_MEMORY_GRANTS", 2)
    codes = [_authorization_code(authority, config) for _ in range(3)]
    with pytest.raises(GeminiOAuthError) as evicted_code:
        _exchange(authority, config, codes[0])
    assert evicted_code.value.error == "invalid_grant"

    first = _exchange(authority, config, codes[1])
    second = _exchange(authority, config, codes[2])
    third = _exchange(authority, config, _authorization_code(authority, config))
    with pytest.raises(GeminiOAuthError) as evicted_access:
        authority.verify_access_token(
            str(first["access_token"]),
            resource=config.mcp_url,
        )
    assert evicted_access.value.error == "invalid_token"
    with pytest.raises(GeminiOAuthError) as evicted_refresh:
        authority.refresh_access_token(
            refresh_token=str(first["refresh_token"]),
            client_id=config.client_id,
            client_secret=config.client_secret,
            resource=config.mcp_url,
            scope=MCP_SCOPE,
        )
    assert evicted_refresh.value.error == "invalid_grant"
    assert second["refresh_token"] != third["refresh_token"]
