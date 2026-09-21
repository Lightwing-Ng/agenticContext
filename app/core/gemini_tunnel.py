"""Gemini remote MCP configuration and its minimal OAuth 2.1 authority.

Code version: v1.2.0-codex.0

This module deliberately owns no Flask routes and opens no network listener. It
provides the security-sensitive configuration, metadata, and token primitives
that a Gemini Tunnel transport can expose through a separately authenticated
HTTPS ingress.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit

from app.core.config import default_settings_path


GEMINI_TUNNEL_CONFIG_FILENAME = "gemini-tunnel-credentials.json"
GEMINI_TUNNEL_SCHEMA_VERSION = 1
GEMINI_TUNNEL_CONFIG_MAX_BYTES = 16 * 1024

MCP_SCOPE = "mcp:tools"
AUTHORIZATION_CODE_TTL_SECONDS = 5 * 60
ACCESS_TOKEN_TTL_SECONDS = 10 * 60
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_IN_MEMORY_GRANTS = 1_024
MAX_OAUTH_TOKEN_CHARACTERS = 8 * 1024
MAX_REDIRECT_URI_CHARACTERS = 2_048

_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "public_origin",
        "client_id",
        "client_secret",
        "signing_key",
    }
)
_PRIVATE_HOST_SUFFIXES = (
    ".home.arpa",
    ".internal",
    ".lan",
    ".local",
    ".localdomain",
    ".localhost",
)
_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)
_PKCE_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)
_PKCE_VERIFIER_RE = re.compile(r"[A-Za-z0-9._~-]{43,128}", re.ASCII)
_GENERATED_VALUE_RE = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)
_ACCESS_TOKEN_HEADER = {"alg": "HS256", "typ": "at+jwt"}
_WWW_AUTHENTICATE_ERRORS = frozenset(
    {"invalid_request", "invalid_token", "insufficient_scope"}
)
_CONFIG_MUTATION_LOCK = threading.RLock()


class GeminiTunnelConfigError(ValueError):
    """Raised when Gemini Tunnel configuration is malformed or unsafe."""


class GeminiOAuthError(ValueError):
    """One OAuth error suitable for conversion to an HTTP JSON response."""

    def __init__(self, error: str, description: str, *, status_code: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status_code = status_code

    def as_dict(self) -> dict[str, str]:
        """Return the standard OAuth error response body."""
        return {"error": self.error, "error_description": self.description}


@dataclass(frozen=True, slots=True)
class GeminiTunnelConfig:
    """Persisted Gemini Tunnel origin and static OAuth credentials."""

    public_origin: str = ""
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    signing_key: str = field(default="", repr=False)
    schema_version: int = GEMINI_TUNNEL_SCHEMA_VERSION

    @property
    def configured(self) -> bool:
        """Return whether the complete generated configuration is present."""
        return bool(
            self.public_origin
            and self.client_id
            and self.client_secret
            and self.signing_key
            and self.schema_version == GEMINI_TUNNEL_SCHEMA_VERSION
        )

    @property
    def mcp_url(self) -> str:
        """Return the public MCP resource URL, or an empty value when unconfigured."""
        return f"{self.public_origin}/mcp/gemini" if self.public_origin else ""

    def snapshot(self) -> dict[str, object]:
        """Return UI-safe metadata without any secret or secret fragment."""
        return {
            "schema_version": self.schema_version,
            "configured": self.configured,
            "public_origin": self.public_origin,
            "mcp_url": self.mcp_url,
            # OAuth client identifiers are public and must be entered in Gemini.
            "client_id": self.client_id,
            "client_secret_saved": bool(self.client_secret),
            "signing_key_saved": bool(self.signing_key),
        }

    def _payload(self) -> dict[str, object]:
        """Return the complete private persistence record."""
        return {
            "schema_version": self.schema_version,
            "public_origin": self.public_origin,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "signing_key": self.signing_key,
        }


@dataclass(frozen=True, slots=True)
class _AuthorizationGrant:
    token_digest: str
    client_id: str
    redirect_uri: str
    resource: str
    scope: str
    code_challenge: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class _RefreshGrant:
    token_digest: str
    client_id: str
    resource: str
    scope: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class _AccessGrant:
    token_digest: str
    client_id: str
    resource: str
    scope: str
    expires_at: float


def default_gemini_tunnel_config_path() -> Path:
    """Return the Gemini Tunnel configuration beside the regular settings file."""
    return default_settings_path().parent / GEMINI_TUNNEL_CONFIG_FILENAME


def _reject_unsafe_text(value: str, label: str) -> str:
    if (
        value != value.strip()
        or "\\" in value
        or any(
            character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
            for character in value
        )
    ):
        raise GeminiTunnelConfigError(
            f"{label} must not contain whitespace, control characters, or backslashes."
        )
    return value


def _public_dns_hostname(raw_hostname: str, label: str) -> str:
    hostname = raw_hostname.rstrip(".").casefold()
    if not hostname or hostname != raw_hostname.casefold():
        raise GeminiTunnelConfigError(f"{label} must use a canonical DNS hostname.")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise GeminiTunnelConfigError(f"{label} must not use an IP address.")
    if hostname == "localhost" or hostname.endswith(_PRIVATE_HOST_SUFFIXES):
        raise GeminiTunnelConfigError(
            f"{label} must not use a local or private hostname."
        )
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise GeminiTunnelConfigError(f"{label} has an invalid DNS hostname.") from exc
    labels = ascii_hostname.split(".")
    if (
        len(labels) < 2
        or len(ascii_hostname) > 253
        or any(not _DNS_LABEL_RE.fullmatch(part) for part in labels)
        or not any(character.isalpha() for character in labels[-1])
    ):
        raise GeminiTunnelConfigError(f"{label} must use a public DNS hostname.")
    return ascii_hostname


def _validated_https_url(
    value: object, label: str, *, origin_only: bool
) -> tuple[str, SplitResult]:
    if not isinstance(value, str) or not value:
        raise GeminiTunnelConfigError(f"{label} must be a non-empty HTTPS URL.")
    value = _reject_unsafe_text(value, label)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise GeminiTunnelConfigError(f"{label} has an invalid port.") from exc
    if parsed.scheme.casefold() != "https":
        raise GeminiTunnelConfigError(f"{label} must use HTTPS.")
    if not parsed.netloc or not parsed.hostname:
        raise GeminiTunnelConfigError(f"{label} must include a hostname.")
    if parsed.username is not None or parsed.password is not None:
        raise GeminiTunnelConfigError(f"{label} must not contain user information.")
    if parsed.netloc.rsplit("@", 1)[-1].endswith(":"):
        raise GeminiTunnelConfigError(f"{label} has an invalid port.")
    hostname = _public_dns_hostname(parsed.hostname, label)
    if port is not None and not 1 <= port <= 65_535:
        raise GeminiTunnelConfigError(f"{label} has an invalid port.")
    if parsed.fragment:
        raise GeminiTunnelConfigError(f"{label} must not contain a fragment.")
    if origin_only and (parsed.path not in ("", "/") or parsed.query):
        raise GeminiTunnelConfigError(
            f"{label} must be an origin without a path or query."
        )
    if not origin_only and not parsed.path.startswith("/"):
        raise GeminiTunnelConfigError(f"{label} must use an absolute path.")
    authority = hostname
    if port is not None and port != 443:
        authority = f"{authority}:{port}"
    canonical = f"https://{authority}"
    if not origin_only:
        canonical = f"{canonical}{parsed.path}"
        if parsed.query:
            canonical = f"{canonical}?{parsed.query}"
    return canonical, parsed


def validate_public_origin(value: object) -> str:
    """Validate and canonicalize one public HTTPS origin.

    Literal IP addresses, local/private hostnames, user information, paths,
    queries, and fragments are rejected before any configuration is saved.
    """
    canonical, _ = _validated_https_url(value, "Public origin", origin_only=True)
    return canonical


def validate_redirect_uri(value: object) -> str:
    """Validate and preserve one public HTTPS OAuth redirect URI for exact binding."""
    if isinstance(value, str) and len(value) > MAX_REDIRECT_URI_CHARACTERS:
        raise GeminiTunnelConfigError(
            "Redirect URI must not exceed 2,048 characters."
        )
    _validated_https_url(value, "Redirect URI", origin_only=False)
    assert isinstance(value, str)
    return value


def _generated_value(prefix: str, random_bytes: int) -> str:
    return f"{prefix}{secrets.token_urlsafe(random_bytes)}"


def _valid_generated_value(value: object, prefix: str, minimum_payload: int) -> bool:
    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    payload = value[len(prefix) :]
    return (
        len(payload) >= minimum_payload
        and _GENERATED_VALUE_RE.fullmatch(payload) is not None
    )


def _parse_config(payload: object) -> GeminiTunnelConfig:
    if not isinstance(payload, dict) or set(payload) != _CONFIG_KEYS:
        raise GeminiTunnelConfigError(
            "Gemini Tunnel configuration has an invalid field set."
        )
    if type(payload.get("schema_version")) is not int:
        raise GeminiTunnelConfigError(
            "Gemini Tunnel schema_version must be an integer."
        )
    if payload["schema_version"] != GEMINI_TUNNEL_SCHEMA_VERSION:
        raise GeminiTunnelConfigError(
            f"Gemini Tunnel configuration must use schema_version {GEMINI_TUNNEL_SCHEMA_VERSION}."
        )
    values = {
        key: payload.get(key)
        for key in ("public_origin", "client_id", "client_secret", "signing_key")
    }
    if not all(isinstance(value, str) for value in values.values()):
        raise GeminiTunnelConfigError(
            "Gemini Tunnel configuration values must be strings."
        )
    if not any(values.values()):
        return GeminiTunnelConfig()
    public_origin = validate_public_origin(values["public_origin"])
    if public_origin != values["public_origin"]:
        raise GeminiTunnelConfigError("Gemini Tunnel public_origin must be canonical.")
    if not _valid_generated_value(values["client_id"], "gtc_", 32):
        raise GeminiTunnelConfigError("Gemini Tunnel client_id is invalid.")
    if not _valid_generated_value(values["client_secret"], "gts_", 64):
        raise GeminiTunnelConfigError("Gemini Tunnel client_secret is invalid.")
    if not _valid_generated_value(values["signing_key"], "gtk_", 64):
        raise GeminiTunnelConfigError("Gemini Tunnel signing_key is invalid.")
    return GeminiTunnelConfig(
        public_origin=public_origin,
        client_id=values["client_id"],
        client_secret=values["client_secret"],
        signing_key=values["signing_key"],
    )


def load_gemini_tunnel_config(path: Path | None = None) -> GeminiTunnelConfig:
    """Load a strict private configuration with owner-only mode enforced on POSIX."""
    resolved_path = path if path is not None else default_gemini_tunnel_config_path()
    try:
        metadata = resolved_path.lstat()
    except FileNotFoundError:
        return GeminiTunnelConfig()
    except OSError as exc:
        raise GeminiTunnelConfigError(
            "Gemini Tunnel configuration cannot be inspected."
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise GeminiTunnelConfigError(
            "Gemini Tunnel configuration must be a regular file."
        )
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise GeminiTunnelConfigError("Gemini Tunnel configuration must be owner-only.")
    if metadata.st_size > GEMINI_TUNNEL_CONFIG_MAX_BYTES:
        raise GeminiTunnelConfigError("Gemini Tunnel configuration is too large.")
    try:
        payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GeminiTunnelConfigError(
            "Gemini Tunnel configuration is not valid UTF-8 JSON."
        ) from exc
    return _parse_config(payload)


def _write_gemini_tunnel_config(config: GeminiTunnelConfig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(config._payload(), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
        if os.name == "posix":
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def configure_gemini_tunnel(
    public_origin: object,
    path: Path | None = None,
) -> GeminiTunnelConfig:
    """Persist a valid origin and stable high-entropy OAuth credentials.

    Re-saving the same canonical origin preserves its credentials. Changing the
    origin rotates every credential so tokens from the old origin stop verifying.
    """
    origin = validate_public_origin(public_origin)
    resolved_path = path if path is not None else default_gemini_tunnel_config_path()
    with _CONFIG_MUTATION_LOCK:
        current = load_gemini_tunnel_config(resolved_path)
        if current.configured and hmac.compare_digest(current.public_origin, origin):
            return current
        config = GeminiTunnelConfig(
            public_origin=origin,
            client_id=_generated_value("gtc_", 32),
            client_secret=_generated_value("gts_", 48),
            signing_key=_generated_value("gtk_", 48),
        )
        _write_gemini_tunnel_config(config, resolved_path)
        return config


def clear_gemini_tunnel_config(path: Path | None = None) -> GeminiTunnelConfig:
    """Atomically replace the saved configuration with an empty safe record."""
    resolved_path = path if path is not None else default_gemini_tunnel_config_path()
    config = GeminiTunnelConfig()
    with _CONFIG_MUTATION_LOCK:
        _write_gemini_tunnel_config(config, resolved_path)
    return config


def _require_configured(config: GeminiTunnelConfig) -> None:
    if not config.configured:
        raise GeminiTunnelConfigError("Gemini Tunnel is not configured.")
    validated = _parse_config(config._payload())
    if validated != config:
        raise GeminiTunnelConfigError("Gemini Tunnel configuration is not canonical.")


def protected_resource_metadata_url(config: GeminiTunnelConfig) -> str:
    """Return the RFC 9728 metadata URL for the public MCP resource."""
    _require_configured(config)
    return f"{config.public_origin}/.well-known/oauth-protected-resource/mcp/gemini"


def protected_resource_metadata(config: GeminiTunnelConfig) -> dict[str, object]:
    """Return OAuth protected-resource metadata for the public MCP endpoint."""
    _require_configured(config)
    return {
        "resource": config.mcp_url,
        "authorization_servers": [config.public_origin],
        "scopes_supported": [MCP_SCOPE],
        "bearer_methods_supported": ["header"],
    }


def authorization_server_metadata(config: GeminiTunnelConfig) -> dict[str, object]:
    """Return OAuth authorization-server metadata for Gemini client discovery."""
    _require_configured(config)
    return {
        "issuer": config.public_origin,
        "authorization_endpoint": f"{config.public_origin}/oauth/authorize",
        "token_endpoint": f"{config.public_origin}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "authorization_response_iss_parameter_supported": True,
        "token_endpoint_auth_methods_supported": [
            "client_secret_basic",
            "client_secret_post",
        ],
        "scopes_supported": [MCP_SCOPE],
    }


def _quoted_challenge_value(value: str) -> str:
    sanitized = " ".join(value.split())[:300]
    return sanitized.replace("\\", "\\\\").replace('"', '\\"')


def www_authenticate_challenge(
    config: GeminiTunnelConfig,
    *,
    error: str | None = None,
    error_description: str = "",
) -> str:
    """Return an RFC 9728 Bearer challenge without header-injection input."""
    if error is not None and error not in _WWW_AUTHENTICATE_ERRORS:
        raise ValueError("Unsupported WWW-Authenticate error.")
    parts = [
        f'resource_metadata="{protected_resource_metadata_url(config)}"',
        f'scope="{MCP_SCOPE}"',
    ]
    if error is not None:
        parts.append(f'error="{error}"')
    if error_description:
        parts.append(
            f'error_description="{_quoted_challenge_value(error_description)}"'
        )
    return f"Bearer {', '.join(parts)}"


def _opaque_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", errors="strict")).hexdigest()


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    if not value or _GENERATED_VALUE_RE.fullmatch(value) is None:
        raise ValueError("Invalid base64url value.")
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(f"{value}{padding}", altchars=b"-_", validate=True)


def _json_segment(value: dict[str, Any]) -> str:
    return _base64url_encode(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )


def _exact(actual: str, expected: str) -> bool:
    return hmac.compare_digest(actual.encode("utf-8"), expected.encode("utf-8"))


class GeminiOAuthAuthority:
    """In-memory OAuth 2.1 authorization-code and refresh-token authority."""

    def __init__(
        self,
        config: GeminiTunnelConfig,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        _require_configured(config)
        self._config = config
        self._clock = clock or time.time
        self._authorization_codes: dict[str, _AuthorizationGrant] = {}
        self._refresh_tokens: dict[str, _RefreshGrant] = {}
        self._access_tokens: dict[str, _AccessGrant] = {}
        self._lock = threading.Lock()
        self._revoked = False

    @property
    def resource(self) -> str:
        """Return the only resource accepted by this authority."""
        return self._config.mcp_url

    def revoke_all(self) -> None:
        """Permanently revoke this authority instance and every in-memory grant."""
        with self._lock:
            self._revoked = True
            self._authorization_codes.clear()
            self._refresh_tokens.clear()
            self._access_tokens.clear()

    def issue_authorization_code(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        resource: str,
        scope: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> str:
        """Issue a short-lived code after the adapter establishes owner approval.

        The authority deliberately has no public auto-approval path. The HTTP adapter
        must establish explicit, time-bounded local-owner consent before invoking this
        method for the pre-provisioned static confidential client.
        """
        canonical_redirect_uri = self.validate_authorization_request(
            client_id=client_id,
            redirect_uri=redirect_uri,
            resource=resource,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )
        code = secrets.token_urlsafe(32)
        digest = _opaque_token_digest(code)
        now = self._clock()
        grant = _AuthorizationGrant(
            token_digest=digest,
            client_id=self._config.client_id,
            redirect_uri=canonical_redirect_uri,
            resource=self.resource,
            scope=MCP_SCOPE,
            code_challenge=code_challenge,
            expires_at=now + AUTHORIZATION_CODE_TTL_SECONDS,
        )
        with self._lock:
            self._require_active_locked("invalid_request")
            self._prune_locked(now)
            self._authorization_codes[digest] = grant
            self._bound_locked(self._authorization_codes)
        return code

    def validate_authorization_request(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        resource: str,
        scope: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> str:
        """Validate an authorization request before local owner approval.

        The returned redirect URI is canonical and safe to show in the local Agent
        UI. Validation does not create a code or mutate grant state.
        """
        self._require_client_id(client_id)
        canonical_redirect_uri = self._oauth_redirect_uri(redirect_uri)
        self._require_resource_scope(resource, scope)
        if not _exact(str(code_challenge_method), "S256"):
            raise GeminiOAuthError(
                "invalid_request",
                "code_challenge_method must be S256.",
            )
        if (
            not isinstance(code_challenge, str)
            or _PKCE_CHALLENGE_RE.fullmatch(code_challenge) is None
        ):
            raise GeminiOAuthError(
                "invalid_request", "code_challenge is not a valid S256 value."
            )
        with self._lock:
            self._require_active_locked("invalid_request")
        return canonical_redirect_uri

    def exchange_authorization_code(
        self,
        *,
        code: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        resource: str,
        scope: str,
        code_verifier: str,
    ) -> dict[str, object]:
        """Exchange one authorization code and rotate it out after a successful check."""
        self._require_client(client_id, client_secret)
        canonical_redirect_uri = self._oauth_redirect_uri(redirect_uri)
        self._require_resource_scope(resource, scope)
        if (
            not isinstance(code, str)
            or not code
            or len(code) > MAX_OAUTH_TOKEN_CHARACTERS
        ):
            raise GeminiOAuthError("invalid_grant", "Authorization code is invalid.")
        if (
            not isinstance(code_verifier, str)
            or _PKCE_VERIFIER_RE.fullmatch(code_verifier) is None
        ):
            raise GeminiOAuthError("invalid_grant", "PKCE code_verifier is invalid.")
        try:
            digest = _opaque_token_digest(code)
        except UnicodeEncodeError as exc:
            raise GeminiOAuthError(
                "invalid_grant", "Authorization code is invalid."
            ) from exc
        now = self._clock()
        with self._lock:
            self._require_active_locked("invalid_grant")
            self._prune_locked(now)
            grant = self._authorization_codes.get(digest)
            if grant is None or not _exact(grant.token_digest, digest):
                raise GeminiOAuthError(
                    "invalid_grant", "Authorization code is invalid or expired."
                )
            self._require_grant_binding(
                grant.client_id,
                client_id,
                grant.redirect_uri,
                canonical_redirect_uri,
                grant.resource,
                resource,
                grant.scope,
                scope,
            )
            calculated_challenge = _base64url_encode(
                hashlib.sha256(code_verifier.encode()).digest()
            )
            if not _exact(calculated_challenge, grant.code_challenge):
                raise GeminiOAuthError("invalid_grant", "PKCE verification failed.")
            self._authorization_codes.pop(digest, None)
            return self._issue_token_response_locked(
                now, grant.client_id, grant.resource, grant.scope
            )

    def refresh_access_token(
        self,
        *,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        resource: str,
        scope: str,
    ) -> dict[str, object]:
        """Rotate one refresh token and issue a fresh short-lived access token."""
        self._require_client(client_id, client_secret)
        self._require_resource_scope(resource, scope)
        if (
            not isinstance(refresh_token, str)
            or not refresh_token
            or len(refresh_token) > MAX_OAUTH_TOKEN_CHARACTERS
        ):
            raise GeminiOAuthError("invalid_grant", "Refresh token is invalid.")
        try:
            digest = _opaque_token_digest(refresh_token)
        except UnicodeEncodeError as exc:
            raise GeminiOAuthError(
                "invalid_grant", "Refresh token is invalid."
            ) from exc
        now = self._clock()
        with self._lock:
            self._require_active_locked("invalid_grant")
            self._prune_locked(now)
            grant = self._refresh_tokens.get(digest)
            if grant is None or not _exact(grant.token_digest, digest):
                raise GeminiOAuthError(
                    "invalid_grant", "Refresh token is invalid or expired."
                )
            self._require_grant_binding(
                grant.client_id,
                client_id,
                "",
                "",
                grant.resource,
                resource,
                grant.scope,
                scope,
            )
            self._refresh_tokens.pop(digest, None)
            return self._issue_token_response_locked(
                now, grant.client_id, grant.resource, grant.scope
            )

    def verify_access_token(
        self,
        token: str,
        *,
        resource: str,
        scope: str = MCP_SCOPE,
    ) -> dict[str, Any]:
        """Verify a self-contained HMAC access token and its exact audience and scope."""
        self._require_resource_scope(resource, scope)
        if not isinstance(token, str) or len(token) > MAX_OAUTH_TOKEN_CHARACTERS:
            raise GeminiOAuthError(
                "invalid_token", "Access token is malformed.", status_code=401
            )
        segments = token.split(".")
        if len(segments) != 3:
            raise GeminiOAuthError(
                "invalid_token", "Access token is malformed.", status_code=401
            )
        encoded_header, encoded_payload, encoded_signature = segments
        try:
            signing_input = f"{encoded_header}.{encoded_payload}".encode(
                "ascii",
                errors="strict",
            )
        except UnicodeEncodeError as exc:
            raise GeminiOAuthError(
                "invalid_token", "Access token is malformed.", status_code=401
            ) from exc
        expected_signature = hmac.new(
            self._config.signing_key.encode("ascii"),
            signing_input,
            hashlib.sha256,
        ).digest()
        try:
            supplied_signature = _base64url_decode(encoded_signature)
        except (ValueError, UnicodeError) as exc:
            raise GeminiOAuthError(
                "invalid_token", "Access token is malformed.", status_code=401
            ) from exc
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise GeminiOAuthError(
                "invalid_token", "Access token signature is invalid.", status_code=401
            )
        try:
            header = json.loads(_base64url_decode(encoded_header))
            payload = json.loads(_base64url_decode(encoded_payload))
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise GeminiOAuthError(
                "invalid_token", "Access token is malformed.", status_code=401
            ) from exc
        if header != _ACCESS_TOKEN_HEADER or not isinstance(payload, dict):
            raise GeminiOAuthError(
                "invalid_token", "Access token header is invalid.", status_code=401
            )
        required_string_claims = ("iss", "sub", "aud", "client_id", "scope", "jti")
        if any(
            not isinstance(payload.get(name), str) for name in required_string_claims
        ):
            raise GeminiOAuthError(
                "invalid_token", "Access token claims are invalid.", status_code=401
            )
        if type(payload.get("iat")) is not int or type(payload.get("exp")) is not int:
            raise GeminiOAuthError(
                "invalid_token", "Access token times are invalid.", status_code=401
            )
        if not all(
            (
                _exact(payload["iss"], self._config.public_origin),
                _exact(payload["sub"], self._config.client_id),
                _exact(payload["client_id"], self._config.client_id),
                _exact(payload["aud"], resource),
                _exact(payload["scope"], scope),
            )
        ):
            raise GeminiOAuthError(
                "invalid_token", "Access token binding is invalid.", status_code=401
            )
        now = int(self._clock())
        if payload["iat"] > now or payload["exp"] <= now:
            raise GeminiOAuthError(
                "invalid_token", "Access token is expired.", status_code=401
            )
        if payload["exp"] - payload["iat"] != ACCESS_TOKEN_TTL_SECONDS:
            raise GeminiOAuthError(
                "invalid_token", "Access token lifetime is invalid.", status_code=401
            )
        token_digest = _opaque_token_digest(token)
        with self._lock:
            self._require_active_locked("invalid_token", status_code=401)
            self._prune_locked(self._clock())
            grant = self._access_tokens.get(token_digest)
            if grant is None or not _exact(grant.token_digest, token_digest):
                raise GeminiOAuthError(
                    "invalid_token",
                    "Access token is unknown or expired.",
                    status_code=401,
                )
            self._require_grant_binding(
                grant.client_id,
                payload["client_id"],
                "",
                "",
                grant.resource,
                payload["aud"],
                grant.scope,
                payload["scope"],
            )
        return dict(payload)

    def _require_client_id(self, client_id: str) -> None:
        if not isinstance(client_id, str) or not _exact(
            client_id, self._config.client_id
        ):
            raise GeminiOAuthError("unauthorized_client", "OAuth client_id is invalid.")

    def _require_client(self, client_id: str, client_secret: str) -> None:
        id_valid = isinstance(client_id, str) and _exact(
            client_id, self._config.client_id
        )
        secret_valid = isinstance(client_secret, str) and _exact(
            client_secret,
            self._config.client_secret,
        )
        if not id_valid or not secret_valid:
            raise GeminiOAuthError(
                "invalid_client",
                "OAuth client authentication failed.",
                status_code=401,
            )

    def _require_resource_scope(self, resource: str, scope: str) -> None:
        if not isinstance(resource, str) or not _exact(resource, self.resource):
            raise GeminiOAuthError("invalid_target", "OAuth resource is invalid.")
        if not isinstance(scope, str) or not _exact(scope, MCP_SCOPE):
            raise GeminiOAuthError("invalid_scope", f"OAuth scope must be {MCP_SCOPE}.")

    def _require_active_locked(self, error: str, *, status_code: int = 400) -> None:
        if self._revoked:
            raise GeminiOAuthError(
                error,
                "OAuth authority has been revoked.",
                status_code=status_code,
            )

    @staticmethod
    def _oauth_redirect_uri(redirect_uri: str) -> str:
        try:
            return validate_redirect_uri(redirect_uri)
        except GeminiTunnelConfigError as exc:
            raise GeminiOAuthError("invalid_request", str(exc)) from exc

    @staticmethod
    def _require_grant_binding(
        granted_client_id: str,
        client_id: str,
        granted_redirect_uri: str,
        redirect_uri: str,
        granted_resource: str,
        resource: str,
        granted_scope: str,
        scope: str,
    ) -> None:
        bindings = (
            (granted_client_id, client_id),
            (granted_resource, resource),
            (granted_scope, scope),
        )
        if granted_redirect_uri or redirect_uri:
            bindings = (*bindings, (granted_redirect_uri, redirect_uri))
        if not all(_exact(actual, expected) for actual, expected in bindings):
            raise GeminiOAuthError(
                "invalid_grant", "OAuth grant binding does not match."
            )

    def _issue_token_response_locked(
        self,
        now: float,
        client_id: str,
        resource: str,
        scope: str,
    ) -> dict[str, object]:
        issued_at = int(now)
        payload = {
            "iss": self._config.public_origin,
            "sub": client_id,
            "aud": resource,
            "client_id": client_id,
            "scope": scope,
            "iat": issued_at,
            "exp": issued_at + ACCESS_TOKEN_TTL_SECONDS,
            "jti": secrets.token_urlsafe(18),
        }
        encoded_header = _json_segment(_ACCESS_TOKEN_HEADER)
        encoded_payload = _json_segment(payload)
        signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
        signature = hmac.new(
            self._config.signing_key.encode("ascii"),
            signing_input,
            hashlib.sha256,
        ).digest()
        access_token = (
            f"{encoded_header}.{encoded_payload}.{_base64url_encode(signature)}"
        )
        access_digest = _opaque_token_digest(access_token)
        self._access_tokens[access_digest] = _AccessGrant(
            token_digest=access_digest,
            client_id=client_id,
            resource=resource,
            scope=scope,
            expires_at=now + ACCESS_TOKEN_TTL_SECONDS,
        )
        self._bound_locked(self._access_tokens)
        refresh_token = secrets.token_urlsafe(48)
        refresh_digest = _opaque_token_digest(refresh_token)
        self._refresh_tokens[refresh_digest] = _RefreshGrant(
            token_digest=refresh_digest,
            client_id=client_id,
            resource=resource,
            scope=scope,
            expires_at=now + REFRESH_TOKEN_TTL_SECONDS,
        )
        self._bound_locked(self._refresh_tokens)
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "refresh_token": refresh_token,
            "scope": scope,
        }

    def _prune_locked(self, now: float) -> None:
        self._authorization_codes = {
            digest: grant
            for digest, grant in self._authorization_codes.items()
            if grant.expires_at > now
        }
        self._refresh_tokens = {
            digest: grant
            for digest, grant in self._refresh_tokens.items()
            if grant.expires_at > now
        }
        self._access_tokens = {
            digest: grant
            for digest, grant in self._access_tokens.items()
            if grant.expires_at > now
        }

    @staticmethod
    def _bound_locked(grants: dict[str, Any]) -> None:
        while len(grants) > MAX_IN_MEMORY_GRANTS:
            grants.pop(next(iter(grants)))


__all__ = [
    "ACCESS_TOKEN_TTL_SECONDS",
    "AUTHORIZATION_CODE_TTL_SECONDS",
    "GEMINI_TUNNEL_CONFIG_FILENAME",
    "GEMINI_TUNNEL_SCHEMA_VERSION",
    "GeminiOAuthAuthority",
    "GeminiOAuthError",
    "GeminiTunnelConfig",
    "GeminiTunnelConfigError",
    "MCP_SCOPE",
    "REFRESH_TOKEN_TTL_SECONDS",
    "authorization_server_metadata",
    "clear_gemini_tunnel_config",
    "configure_gemini_tunnel",
    "default_gemini_tunnel_config_path",
    "load_gemini_tunnel_config",
    "protected_resource_metadata",
    "protected_resource_metadata_url",
    "validate_public_origin",
    "validate_redirect_uri",
    "www_authenticate_challenge",
]
