"""One registry for Agent actions, page observations, and WebMCP tools.

Code version: v1.5.0-codex.1
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any

from ..brand import PRODUCT_DESCRIPTION, PRODUCT_NAME, SITE_ID


CAPABILITY_REGISTRY_VERSION = "1.4.0"
AGENT_OPTIMIZATION_CONTRACT_VERSION = "1.1.0"
AGENT_OPTIMIZATION_PROFILE = "openai-site-tools-2026-08-28"


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    """Describe one bounded capability at its authoritative application boundary."""

    key: str
    kind: str
    label: str
    description: str
    read_only: bool
    public: bool = False
    manifest_id: str = ""
    action_name: str = ""
    webmcp_name: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    handler_name: str = ""
    prompt_example: str = ""
    read_only_task_allowed: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe internal registry record."""
        return asdict(self)

    def as_manifest_record(self) -> dict[str, str]:
        """Return the small public record accepted by the shared WebMCP runtime."""
        return {
            "id": self.manifest_id or self.key.replace(".", "_")[:64],
            "label": self.label,
            "description": self.description,
        }

    def as_webmcp_record(self) -> dict[str, Any]:
        """Return the registry-owned metadata consumed by the shared runtime."""
        return {
            "name": self.webmcp_name,
            "description": self.description,
            "inputSchema": deepcopy(self.input_schema),
            "readOnlyHint": self.read_only,
        }


@dataclass(frozen=True, slots=True)
class NavigationTarget:
    """Describe one same-origin human-page destination in the Site manifest."""

    id: str
    label: str
    description: str
    path: str

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-safe navigation record."""
        return asdict(self)


def _action(
    name: str,
    label: str,
    description: str,
    *,
    read_only: bool,
    input_schema: dict[str, Any] | None = None,
    handler_name: str = "",
    prompt_example: str = "",
    read_only_task_allowed: bool = False,
) -> CapabilityDefinition:
    return CapabilityDefinition(
        key=f"agent.action.{name}",
        kind="agent_action",
        label=label,
        description=description,
        read_only=read_only,
        action_name=name,
        input_schema=input_schema or {},
        handler_name=handler_name,
        prompt_example=prompt_example,
        read_only_task_allowed=read_only_task_allowed,
    )


def _observation(key: str, label: str, description: str) -> CapabilityDefinition:
    return CapabilityDefinition(
        key=f"page.observe.{key}",
        kind="page_observation",
        label=label,
        description=description,
        read_only=True,
    )


def _webmcp(
    name: str,
    label: str,
    description: str,
    *,
    read_only: bool,
    input_schema: dict[str, Any] | None = None,
) -> CapabilityDefinition:
    return CapabilityDefinition(
        key=f"webmcp.{name}",
        kind="webmcp_tool",
        label=label,
        description=description,
        read_only=read_only,
        public=False,
        webmcp_name=name,
        input_schema=input_schema or {},
    )


def _action_schema(
    action_name: str,
    properties: dict[str, Any],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build one closed Agent Action schema from the authoritative action name."""
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "const": action_name},
            **properties,
        },
        "required": ["action", *required],
        "additionalProperties": False,
    }


def _string_property(
    description: str,
    *,
    maximum: int | None = None,
    minimum: int | None = None,
) -> dict[str, Any]:
    """Return one bounded string property for a registry schema."""
    property_schema: dict[str, Any] = {
        "type": "string",
        "description": description,
    }
    if maximum is not None:
        property_schema["maxLength"] = maximum
    if minimum is not None:
        property_schema["minLength"] = minimum
    return property_schema


def _integer_property(
    description: str,
    *,
    minimum: int,
    maximum: int,
) -> dict[str, Any]:
    """Return one bounded integer property for a registry schema."""
    return {
        "type": "integer",
        "description": description,
        "minimum": minimum,
        "maximum": maximum,
    }


class ControllerActionValidationError(ValueError):
    """Raised when an action violates its registry-owned controller schema."""


def _schema_validation_error(path: str, message: str) -> None:
    """Raise one concise, non-content-bearing action-schema validation error."""
    raise ControllerActionValidationError(f"Agent action payload {path} {message}")


def _validate_controller_schema(value: Any, schema: dict[str, Any], *, path: str) -> None:
    """Validate the bounded JSON-Schema subset used by Agent Action definitions."""
    if not isinstance(schema, dict):
        _schema_validation_error(path, "uses an unsupported registered schema.")

    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, dict):
            _schema_validation_error(path, "must be an object.")
        properties = schema.get("properties", {})
        required = schema.get("required", ())
        if not isinstance(properties, dict) or not isinstance(required, (list, tuple)):
            _schema_validation_error(path, "uses an unsupported registered schema.")
        if any(not isinstance(key, str) for key in value):
            _schema_validation_error(path, "must use string field names.")
        for required_name in required:
            if not isinstance(required_name, str):
                _schema_validation_error(path, "uses an unsupported registered schema.")
            if required_name not in value:
                _schema_validation_error(f"{path}.{required_name}", "is required.")
        if schema.get("additionalProperties") is False:
            unknown = sorted(str(key) for key in value if key not in properties)
            if unknown:
                _schema_validation_error(
                    path,
                    "contains unsupported field(s): " + ", ".join(unknown[:8]) + ".",
                )
        for key, nested_schema in properties.items():
            if key in value:
                _validate_controller_schema(
                    value[key],
                    nested_schema,
                    path=f"{path}.{key}",
                )
    elif expected_type == "string":
        if not isinstance(value, str):
            _schema_validation_error(path, "must be a string.")
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            _schema_validation_error(path, f"must contain at least {minimum:,} character(s).")
        if isinstance(maximum, int) and len(value) > maximum:
            _schema_validation_error(path, f"must contain at most {maximum:,} character(s).")
    elif expected_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            _schema_validation_error(path, "must be an integer.")
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, int) and value < minimum:
            _schema_validation_error(path, f"must be at least {minimum:,}.")
        if isinstance(maximum, int) and value > maximum:
            _schema_validation_error(path, f"must be at most {maximum:,}.")
    elif expected_type == "array":
        if not isinstance(value, list):
            _schema_validation_error(path, "must be an array.")
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            _schema_validation_error(path, f"must contain at least {minimum:,} item(s).")
        if isinstance(maximum, int) and len(value) > maximum:
            _schema_validation_error(path, f"must contain at most {maximum:,} item(s).")
        item_schema = schema.get("items")
        if item_schema is not None:
            if not isinstance(item_schema, dict):
                _schema_validation_error(path, "uses an unsupported registered schema.")
            for index, item in enumerate(value):
                _validate_controller_schema(
                    item,
                    item_schema,
                    path=f"{path}[{index}]",
                )
    else:
        _schema_validation_error(path, "uses an unsupported registered schema.")

    if "const" in schema and value != schema["const"]:
        _schema_validation_error(path, "does not match the registered action value.")


def validate_controller_action_payload(
    capability: CapabilityDefinition,
    payload: Any,
) -> None:
    """Enforce one Agent Action's registry schema at the controller boundary."""
    if capability.kind != "agent_action" or not capability.action_name:
        raise ControllerActionValidationError(
            "The requested capability is not a registered Agent Action."
        )
    if not isinstance(payload, dict):
        raise ControllerActionValidationError("Agent action payload must be an object.")
    _validate_controller_schema(payload, capability.input_schema, path="root")


CAPABILITY_REGISTRY: tuple[CapabilityDefinition, ...] = (
    CapabilityDefinition(
        key="site.cache_review",
        kind="site_capability",
        label="Cache review",
        description="Review source-specific cache status and controls through the existing human interface.",
        read_only=True,
        public=True,
        manifest_id="cache_review",
    ),
    CapabilityDefinition(
        key="site.local_resources",
        kind="site_capability",
        label="Local resources",
        description="Search and inspect locally cached media, messages, and saved prompts through the existing human interface.",
        read_only=True,
        public=True,
        manifest_id="local_resources",
    ),
    CapabilityDefinition(
        key="site.agent_workspace",
        kind="site_capability",
        label="Agent workspace",
        description="Configure and supervise the existing signed-in Web Agent workflow without granting Site tools direct task-execution authority.",
        read_only=False,
        public=True,
        manifest_id="agent_workspace",
    ),
    CapabilityDefinition(
        key="site.configuration",
        kind="site_capability",
        label="Configuration",
        description="Review local runtime, storage, browser, and Agent settings through the existing human interface.",
        read_only=True,
        public=True,
        manifest_id="configuration",
    ),
    CapabilityDefinition(
        key="agent.run",
        kind="agent_lifecycle",
        label="Agent run lifecycle",
        description="Start, observe, verify, and complete one bounded browser-mediated Agent run.",
        read_only=False,
    ),
    CapabilityDefinition(
        key="agent.recovery.doctor",
        kind="agent_recovery",
        label="Agent doctor recovery",
        description=(
            "Diagnose and explicitly reconcile local Agent runtime state. An eligible "
            "interrupted Edge and ChatGPT task may continue only after an explicit user action."
        ),
        read_only=False,
    ),
    _action(
        "list",
        "List files",
        "List bounded, non-sensitive paths inside the selected Agent workspace.",
        read_only=True,
        handler_name="_list",
        read_only_task_allowed=True,
        prompt_example='{"action":"list","path":".","depth":2}',
        input_schema=_action_schema(
            "list",
            {
                "path": _string_property(
                    "Workspace-relative directory path.",
                    maximum=1_000,
                ),
                "depth": _integer_property(
                    "Maximum directory depth to inspect.",
                    minimum=1,
                    maximum=6,
                ),
            },
        ),
    ),
    _action(
        "read",
        "Read a file",
        "Read a bounded text range from one non-sensitive workspace file.",
        read_only=True,
        handler_name="_read",
        read_only_task_allowed=True,
        prompt_example='{"action":"read","path":"relative/file","start_line":1,"end_line":240}',
        input_schema=_action_schema(
            "read",
            {
                "path": _string_property(
                    "Workspace-relative file path.",
                    maximum=1_000,
                    minimum=1,
                ),
                "start_line": _integer_property(
                    "First one-based line to read.",
                    minimum=1,
                    maximum=1_000_000,
                ),
                "end_line": _integer_property(
                    "Last one-based line to read.",
                    minimum=1,
                    maximum=1_000_000,
                ),
            },
            required=("path",),
        ),
    ),
    _action(
        "search",
        "Search files",
        "Search literal text inside the selected workspace with bounded results.",
        read_only=True,
        handler_name="_search",
        read_only_task_allowed=True,
        prompt_example='{"action":"search","query":"literal text","path":".","glob":"*.py","max_results":80}',
        input_schema=_action_schema(
            "search",
            {
                "query": _string_property(
                    "Literal search text, never a regular expression.",
                    maximum=8_000,
                    minimum=1,
                ),
                "path": _string_property(
                    "Workspace-relative search root.",
                    maximum=1_000,
                ),
                "glob": _string_property(
                    "Inclusive file glob.",
                    maximum=1_000,
                ),
                "max_results": _integer_property(
                    "Maximum returned matches.",
                    minimum=1,
                    maximum=300,
                ),
            },
            required=("query",),
        ),
    ),
    _action(
        "replace",
        "Replace text",
        "Replace one exact occurrence in an existing workspace file.",
        read_only=False,
        handler_name="_replace",
        prompt_example='{"action":"replace","path":"relative/file","old":"exact text appearing once","new":"replacement text"}',
        input_schema=_action_schema(
            "replace",
            {
                "path": _string_property(
                    "Workspace-relative existing file path.",
                    maximum=1_000,
                    minimum=1,
                ),
                "old": _string_property(
                    "Exact existing text to replace.",
                    maximum=120_000,
                    minimum=1,
                ),
                "new": _string_property(
                    "Replacement text; an empty string is allowed by the controller.",
                    maximum=120_000,
                ),
            },
            required=("path", "old", "new"),
        ),
    ),
    _action(
        "replace_base64",
        "Replace encoded text",
        "Replace one exact occurrence using base64 transport for quote-safe content.",
        read_only=False,
        handler_name="_replace_base64",
        prompt_example='{"action":"replace_base64","path":"relative/file","old_base64":"base64-of-old","new_base64":"base64-of-new"}',
        input_schema=_action_schema(
            "replace_base64",
            {
                "path": _string_property(
                    "Workspace-relative existing file path.",
                    maximum=1_000,
                    minimum=1,
                ),
                "old_base64": _string_property(
                    "Base64-encoded exact existing text.",
                    maximum=160_000,
                    minimum=1,
                ),
                "new_base64": _string_property(
                    "Base64-encoded replacement text.",
                    maximum=160_000,
                ),
            },
            required=("path", "old_base64", "new_base64"),
        ),
    ),
    _action(
        "write",
        "Write a file",
        "Create one new workspace file with bounded text content.",
        read_only=False,
        handler_name="_write",
        prompt_example='{"action":"write","path":"relative/new-file","content":"complete content"}',
        input_schema=_action_schema(
            "write",
            {
                "path": _string_property(
                    "Workspace-relative new file path.",
                    maximum=1_000,
                    minimum=1,
                ),
                "content": _string_property(
                    "Complete UTF-8 file content.",
                    maximum=120_000,
                    minimum=1,
                ),
            },
            required=("path", "content"),
        ),
    ),
    _action(
        "write_base64",
        "Write encoded file",
        "Create one new workspace file using base64 transport for quote-safe content.",
        read_only=False,
        handler_name="_write_base64",
        prompt_example='{"action":"write_base64","path":"relative/new-file","content_base64":"base64-of-content"}',
        input_schema=_action_schema(
            "write_base64",
            {
                "path": _string_property(
                    "Workspace-relative new file path.",
                    maximum=1_000,
                    minimum=1,
                ),
                "content_base64": _string_property(
                    "Base64-encoded UTF-8 file content.",
                    maximum=160_000,
                    minimum=1,
                ),
            },
            required=("path", "content_base64"),
        ),
    ),
    _action(
        "delete",
        "Delete a file",
        "Delete one previously read workspace file only when its current SHA-256 matches the supplied value.",
        read_only=False,
        handler_name="_delete",
        prompt_example='{"action":"delete","path":"relative/obsolete-file","expected_sha256":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}',
        input_schema=_action_schema(
            "delete",
            {
                "path": _string_property(
                    "Workspace-relative existing regular file path.",
                    maximum=1_000,
                    minimum=1,
                ),
                "expected_sha256": _string_property(
                    "Lowercase SHA-256 reported by a current read action.",
                    maximum=64,
                    minimum=64,
                ),
            },
            required=("path", "expected_sha256"),
        ),
    ),
    _action(
        "run",
        "Run verification",
        "Run one approved, bounded inspection or verification command and fingerprint the workspace before and after it.",
        read_only=True,
        handler_name="_run",
        prompt_example='{"action":"run","command":"focused inspection, build, lint, or test command"}',
        input_schema=_action_schema(
            "run",
            {
                "command": _string_property(
                    "One approved inspection, build, lint, or test command.",
                    maximum=4_000,
                    minimum=1,
                ),
            },
            required=("command",),
        ),
    ),
    _action(
        "job_start",
        "Start compute job",
        "Start one approved durable optimization worker outside the provider-turn and verification timeout lifecycles.",
        read_only=False,
        handler_name="_job_start",
        prompt_example='{"action":"job_start","entrypoint":"optimizer","config_path":"optimizer-config.json","idempotency_key":"stable-request-key"}',
        input_schema=_action_schema(
            "job_start",
            {
                "entrypoint": _string_property(
                    "Approved entrypoint id from .agenticContext-compute.json.",
                    maximum=64,
                    minimum=1,
                ),
                "config_path": _string_property(
                    "Workspace-relative JSON configuration path.",
                    maximum=1_000,
                    minimum=1,
                ),
                "idempotency_key": _string_property(
                    "Stable retry key for this exact compute request.",
                    maximum=128,
                    minimum=8,
                ),
                "resume_job_id": _string_property(
                    "Optional terminal job id whose complete checkpoint should be resumed.",
                    maximum=32,
                    minimum=32,
                ),
            },
            required=("entrypoint", "config_path", "idempotency_key"),
        ),
    ),
    _action(
        "job_status",
        "Inspect compute job",
        "Read bounded durable job metadata, progress, and the rolling log tail.",
        read_only=True,
        handler_name="_job_status",
        read_only_task_allowed=True,
        prompt_example='{"action":"job_status","job_id":"optional-32-character-job-id"}',
        input_schema=_action_schema(
            "job_status",
            {
                "job_id": _string_property(
                    "Optional exact compute job id; omission selects the latest job.",
                    maximum=32,
                    minimum=32,
                ),
            },
        ),
    ),
    _action(
        "job_stop",
        "Stop compute job",
        "Stop only the identity-verified process group owned by one durable compute job.",
        read_only=False,
        handler_name="_job_stop",
        prompt_example='{"action":"job_stop","job_id":"32-character-job-id"}',
        input_schema=_action_schema(
            "job_stop",
            {
                "job_id": _string_property(
                    "Exact compute job id to stop.",
                    maximum=32,
                    minimum=32,
                ),
            },
            required=("job_id",),
        ),
    ),
    _action(
        "bodycheck",
        "Run bodycheck",
        "Check the current bounded diff and repository instruction files before final publication.",
        read_only=True,
        handler_name="_bodycheck",
        read_only_task_allowed=True,
        prompt_example='{"action":"bodycheck"}',
        input_schema=_action_schema("bodycheck", {}),
    ),
    _action(
        "final",
        "Publish final",
        "Publish a final summary only after current verification and bodycheck evidence exists.",
        read_only=False,
        prompt_example='{"action":"final","summary":"concise Markdown outcome","verification":["check and result"],"limitations":["remaining limitation"]}',
        input_schema=_action_schema(
            "final",
            {
                "summary": _string_property(
                    "Concise Markdown outcome.",
                    maximum=8_000,
                    minimum=1,
                ),
                "verification": {
                    "type": "array",
                    "items": _string_property("One verification result.", maximum=1_000, minimum=1),
                    "maxItems": 20,
                },
                "limitations": {
                    "type": "array",
                    "items": _string_property("One remaining limitation.", maximum=1_000, minimum=1),
                    "maxItems": 20,
                },
            },
            required=("summary",),
        ),
    ),
    _observation(
        "provider_turn",
        "Provider turn observation",
        "Observe one bounded provider turn for controller parsing without persisting page content.",
    ),
    _observation(
        "browser_interruption",
        "Browser interruption observation",
        "Observe a bounded browser interruption and recovery state without resubmitting the turn.",
    ),
    _observation(
        "agent_status",
        "Agent status observation",
        "Expose bounded Agent lifecycle state to the local human interface and doctor diagnostics.",
    ),
    _observation(
        "browser_session",
        "Browser session observation",
        "Observe bounded browser/provider readiness metadata without reading page content.",
    ),
    _observation(
        "agent_response",
        "Agent response observation",
        "Observe a bounded local Agent response summary without adding raw provider transcript data to the event ledger.",
    ),
    _webmcp(
        "get_site_capabilities",
        "Discover site capabilities",
        "Read the trusted, bounded capability and navigation inventory for this local application.",
        read_only=True,
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    ),
    _webmcp(
        "get_page_context",
        "Observe page context",
        "Read bounded metadata for the current top-level page without reading page content.",
        read_only=True,
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    ),
    _webmcp(
        "navigate_to_site_target",
        "Navigate to a site target",
        "Navigate the current top-level tab to one allowlisted same-origin human page.",
        read_only=False,
        input_schema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Stable identifier of an allowlisted destination.",
                },
            },
            "required": ["target"],
            "additionalProperties": False,
        },
    ),
)


CAPABILITIES_BY_KEY = {capability.key: capability for capability in CAPABILITY_REGISTRY}
AGENT_ACTIONS = {
    capability.action_name: capability
    for capability in CAPABILITY_REGISTRY
    if capability.kind == "agent_action" and capability.action_name
}
PAGE_OBSERVATIONS = {
    capability.key.removeprefix("page.observe."): capability
    for capability in CAPABILITY_REGISTRY
    if capability.kind == "page_observation"
}
WEBMCP_TOOLS = {
    capability.webmcp_name: capability
    for capability in CAPABILITY_REGISTRY
    if capability.kind == "webmcp_tool" and capability.webmcp_name
}


NAVIGATION_TARGETS: tuple[NavigationTarget, ...] = (
    NavigationTarget(
        "x_cache",
        "X cache",
        "Open the X Likes cache overview without starting or stopping a cache job.",
        "/cache/x",
    ),
    NavigationTarget(
        "grok_cache",
        "Grok cache",
        "Open the Grok cache overview without starting or stopping a cache job.",
        "/cache/grok",
    ),
    NavigationTarget(
        "chatgpt_cache",
        "ChatGPT cache",
        "Open the ChatGPT cache overview without starting or stopping a cache job.",
        "/cache/chatgpt",
    ),
    NavigationTarget(
        "gemini_cache",
        "Gemini cache",
        "Open the Gemini cache overview without starting or stopping a cache job.",
        "/cache/gemini",
    ),
    NavigationTarget(
        "claude_cache",
        "Claude cache",
        "Open the Claude history cache overview without starting or stopping a cache job.",
        "/cache/claude",
    ),
    NavigationTarget(
        "zhihu_cache",
        "Zhihu cache",
        "Open the Zhihu answer cache overview without starting or stopping a cache job.",
        "/cache/zhihu",
    ),
    NavigationTarget(
        "local_resources",
        "Local resources",
        "Open the local cached-resource browser without deleting, restoring, or exporting data.",
        "/browser",
    ),
    NavigationTarget(
        "agent_workspace",
        "Agent workspace",
        "Open the Agent workspace without submitting a prompt or authorizing a terminal command.",
        "/agent",
    ),
    NavigationTarget(
        "settings",
        "Settings",
        "Open Settings without changing persisted configuration.",
        "/settings",
    ),
)


def capability_for_action(action_name: str) -> CapabilityDefinition | None:
    """Return the registered Agent Action for one protocol action name."""
    return AGENT_ACTIONS.get(str(action_name or "").strip().lower())


def capability_for_observation(observation_name: str) -> CapabilityDefinition | None:
    """Return the registered page observation by its stable short name."""
    return PAGE_OBSERVATIONS.get(str(observation_name or "").strip().lower())


def public_manifest_capabilities() -> list[dict[str, str]]:
    """Return bounded public groups derived from the detailed registry."""
    records = [
        capability.as_manifest_record()
        for capability in CAPABILITY_REGISTRY
        if capability.public
    ]
    records.extend(
        [
            {
                "id": "agent_actions",
                "label": "Agent actions",
                "description": (
                    f"The bounded local Agent Action protocol ({len(AGENT_ACTIONS)} registered actions) "
                    "for reading, editing, verification, durable compute jobs, bodycheck, and final publication."
                ),
            },
            {
                "id": "page_observation",
                "label": "Page observation",
                "description": (
                    f"The bounded page-observation layer ({len(PAGE_OBSERVATIONS)} registered observations) "
                    "used to inspect provider, browser, and Agent state without persisting page content."
                ),
            },
            {
                "id": "webmcp_tools",
                "label": "WebMCP tools",
                "description": (
                    f"The shared WebMCP surface ({len(WEBMCP_TOOLS)} registered tools) for capability discovery, "
                    "bounded page context, and allowlisted same-origin navigation."
                ),
            },
        ]
    )
    return records


def controller_action_prompt_schema() -> str:
    """Return the provider-facing action examples from the same registry."""
    examples = [
        capability.prompt_example
        for capability in CAPABILITY_REGISTRY
        if capability.kind == "agent_action" and capability.prompt_example
    ]
    return "\n\nUse one of these actions:\n" + "\n".join(examples) + "\n"


def webmcp_manifest_definitions() -> list[dict[str, Any]]:
    """Return the bounded WebMCP definitions consumed by both browser adapters."""
    return [
        capability.as_webmcp_record()
        for capability in CAPABILITY_REGISTRY
        if capability.kind == "webmcp_tool"
    ]


def build_agent_optimization_manifest() -> dict[str, Any]:
    """Build the project-owned manifest from the single capability registry."""
    return {
        "contractVersion": AGENT_OPTIMIZATION_CONTRACT_VERSION,
        "profile": AGENT_OPTIMIZATION_PROFILE,
        "status": "project-convention",
        "site": {
            "id": SITE_ID,
            "name": PRODUCT_NAME,
            "description": PRODUCT_DESCRIPTION,
            "privacyBoundary": (
                "Local cached content, settings, authenticated sessions, and Agent context remain "
                "behind the existing application routes and are not returned by the v1 Site tools."
            ),
        },
        "capabilities": public_manifest_capabilities(),
        "navigation": [target.as_dict() for target in NAVIGATION_TARGETS],
        "webmcpTools": webmcp_manifest_definitions(),
    }


def capability_registry_snapshot() -> dict[str, Any]:
    """Return the internal registry for local diagnostics and tests."""
    return {
        "version": CAPABILITY_REGISTRY_VERSION,
        "capabilities": [capability.as_dict() for capability in CAPABILITY_REGISTRY],
        "navigation": [target.as_dict() for target in NAVIGATION_TARGETS],
    }
