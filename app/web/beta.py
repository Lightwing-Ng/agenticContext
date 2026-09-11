"""Optional isolated experiments, including explicitly service-backed tools.

Code version: v0.3.0-codex.1
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Blueprint, Flask, abort, jsonify, render_template, request, session

from app.core.agent import (
    AGENT_ACCESS_SESSION_KEY,
    is_allowed_agent_network_request,
    is_loopback_address,
)
from app.core.browser import browser_descriptors
from app.core.foundation import (
    BETA_STORE_ROOT,
    CacheTaskLock,
    LOCAL_STORE_ROOT,
    CrawlConfig,
    TaskState,
    load_saved_config,
)
from app.core.providers import (
    ZHIHU_EXAMPLE_PROFILE_URL,
    ZhihuAnswersService,
    ZhihuArchiveError,
    ZhihuTaskBusyError,
    build_zhihu_answers_initial_snapshot,
)


BETA_VERSION = "v0.3.0"


@dataclass(frozen=True, slots=True)
class BetaExperiment:
    """Immutable navigation metadata for one independently selectable experiment."""

    id: str
    title: str
    kicker: str
    description: str
    icon: str
    source_url: str
    source_title: str


BETA_EXPERIMENTS = (
    BetaExperiment(
        id="idea-collision",
        title="Idea Collision",
        kicker="Connect distant ideas",
        description="Cross two fragments of knowledge and turn the collision into a testable research brief.",
        icon="llm",
        source_url="https://deepmind.google/blog/co-scientist-a-multi-agent-ai-partner-to-accelerate-research/",
        source_title="Google DeepMind: AI co-scientist",
    ),
    BetaExperiment(
        id="context-capsule",
        title="Context Capsule",
        kicker="Make context portable",
        description="Select original evidence with line references and fit a handoff into a clear character budget.",
        icon="downloads",
        source_url="https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents",
        source_title="Anthropic: Effective context engineering for AI agents",
    ),
    BetaExperiment(
        id="question-radar",
        title="Question Radar",
        kicker="Find the next question",
        description="Find explicit questions, unknowns, TODOs, and blockers in your notes, ranked against your objective.",
        icon="browser",
        source_url="https://microsoft.github.io/graphrag/query/overview/",
        source_title="Microsoft GraphRAG: Query overview",
    ),
    BetaExperiment(
        id="memory-diff",
        title="Memory Diff",
        kicker="Track how thinking changes",
        description="Compare two notes to see what was added, removed, and carried forward before updating an agent's context.",
        icon="cloud",
        source_url="https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents",
        source_title="Anthropic: Effective context engineering for AI agents",
    ),
    BetaExperiment(
        id="decision-wind-tunnel",
        title="Decision Wind Tunnel",
        kicker="Stress-test a decision",
        description="Expose assumptions, counterarguments, failure signals, and reversible trials before committing to a choice.",
        icon="agent",
        source_url="https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents",
        source_title="Anthropic: Demystifying evals for AI agents",
    ),
    BetaExperiment(
        id="mission-forge",
        title="Mission Forge",
        kicker="Turn curiosity into a mission",
        description="Shape an open-ended ambition into a bounded agent brief with evidence, checkpoints, and a definition of done.",
        icon="style-tokens",
        source_url="https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents",
        source_title="Anthropic: Demystifying evals for AI agents",
    ),
    BetaExperiment(
        id="zhihu-answers-cache",
        title="Zhihu Answers Cache",
        kicker="Preserve a public answer corpus",
        description=(
            "Cache every answer exposed on one Zhihu profile, with stable-ID deduplication "
            "and a verified local snapshot."
        ),
        icon="downloads",
        source_url=ZHIHU_EXAMPLE_PROFILE_URL,
        source_title="Zhihu: Feifeimao answers",
    ),
)


def _zhihu_browser_options(config: CrawlConfig) -> tuple[dict[str, str], ...]:
    """Expose only authenticated browser transports supported by this experiment."""

    descriptors = browser_descriptors(config)
    return tuple(
        {
            "id": descriptor.browser_id,
            "label": descriptor.label,
            "icon_filename": descriptor.icon_filename,
        }
        for browser_id in ("edge", "chrome")
        if (descriptor := descriptors.get(browser_id)) is not None
        and descriptor.engine == "chromium"
    )


def register_beta(
    app: Flask,
    *,
    version: str,
    enabled: bool | None = None,
    experiment_ids: Iterable[str] | None = None,
    beta_store_root: Path | str = BETA_STORE_ROOT,
    cache_store_root: Path | str = LOCAL_STORE_ROOT,
    task_lock: CacheTaskLock | None = None,
    config_provider: Callable[[], CrawlConfig] = load_saved_config,
) -> None:
    """Register only enabled experiments and their explicitly scoped services."""
    if enabled is None:
        enabled = os.environ.get("AGENTIC_CONTEXT_BETA_ENABLED", "1").strip().lower() in {
            "1", "true", "yes", "on",
        }
    if not enabled:
        return

    experiments = BETA_EXPERIMENTS
    if experiment_ids is not None:
        if isinstance(experiment_ids, str):
            raise ValueError("beta_experiments must be a collection of experiment IDs, not a string")
        selected_ids = frozenset(experiment_ids)
        unknown_ids = selected_ids.difference(item.id for item in BETA_EXPERIMENTS)
        if unknown_ids:
            raise ValueError(f"Unknown Beta experiment IDs: {', '.join(sorted(unknown_ids))}")
        experiments = tuple(item for item in BETA_EXPERIMENTS if item.id in selected_ids)
    if not experiments:
        return

    blueprint = Blueprint("beta", __name__, url_prefix="/beta")
    by_id = {item.id: item for item in experiments}
    zhihu_service: ZhihuAnswersService | None = None
    if "zhihu-answers-cache" in by_id:
        zhihu_state = TaskState(
            version=version,
            snapshot_factory=lambda state_version: build_zhihu_answers_initial_snapshot(
                state_version,
                beta_store_root,
            ),
        )
        zhihu_service = ZhihuAnswersService(
            zhihu_state,
            beta_store_root=beta_store_root,
            cache_store_root=cache_store_root,
            task_lock=task_lock,
        )
        app.extensions["beta_zhihu_answers_service"] = zhihu_service

    def render_experiment(selected: BetaExperiment) -> str:
        browser_options: tuple[dict[str, str], ...] = ()
        selected_browser = ""
        if selected.id == "zhihu-answers-cache":
            browser_options = _zhihu_browser_options(config_provider())
            selected_browser = "edge" if any(
                option["id"] == "edge" for option in browser_options
            ) else (browser_options[0]["id"] if browser_options else "")
        return render_template(
            "beta.html",
            beta_experiments=experiments,
            beta_experiment=selected,
            beta_version=BETA_VERSION,
            beta_browser_options=browser_options,
            beta_selected_browser=selected_browser,
            zhihu_example_url=ZHIHU_EXAMPLE_PROFILE_URL,
            version=version,
        )

    @blueprint.get("", strict_slashes=False)
    def index() -> str:
        return render_experiment(experiments[0])

    @blueprint.get("/<experiment_id>")
    def experiment(experiment_id: str) -> str:
        selected = by_id.get(experiment_id)
        if selected is None:
            abort(404)
        return render_experiment(selected)

    app.register_blueprint(blueprint)

    if zhihu_service is None:
        return

    api = Blueprint(
        "beta_zhihu_api",
        __name__,
        url_prefix="/api/beta/zhihu-answers-cache",
    )

    def api_error(message: str, code: str, status: int):
        return jsonify({"error": message, "code": code}), status

    @api.after_request
    def prevent_beta_api_caching(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    @api.errorhandler(401)
    def beta_api_authentication_required(_error):
        return api_error(
            "Unlock Agent access on this LAN device, then return to Zhihu Answers Cache.",
            "agent_access_required",
            401,
        )

    @api.errorhandler(403)
    def beta_api_forbidden(_error):
        return api_error(
            "This Zhihu cache control request is not allowed from the current origin.",
            "forbidden_origin",
            403,
        )

    def require_local_control_request() -> None:
        """Protect browser authority and local archive metadata from remote callers."""

        host_name = urlsplit(f"//{request.host}").hostname
        if not is_allowed_agent_network_request(request.remote_addr, host_name):
            abort(403)
        origin = request.headers.get("Origin", "").strip()
        if origin:
            origin_parts = urlsplit(origin)
            expected_parts = urlsplit(request.host_url)
            if (
                origin_parts.scheme,
                origin_parts.hostname,
                origin_parts.port,
            ) != (
                expected_parts.scheme,
                expected_parts.hostname,
                expected_parts.port,
            ):
                abort(403)
        if not is_loopback_address(request.remote_addr) and not bool(
            session.get(AGENT_ACCESS_SESSION_KEY)
        ):
            abort(401)

    @api.get("/status", endpoint="status")
    def zhihu_status():
        require_local_control_request()
        profile_url = request.args.get("profile_url", ZHIHU_EXAMPLE_PROFILE_URL)
        try:
            return jsonify(zhihu_service.snapshot(profile_url))
        except ValueError as exc:
            return api_error(str(exc), "invalid_zhihu_profile", 400)

    @api.get("/answers", endpoint="answers")
    def zhihu_answers():
        require_local_control_request()
        profile_url = request.args.get("profile_url", ZHIHU_EXAMPLE_PROFILE_URL)
        query = request.args.get("q", "")
        try:
            page = int(request.args.get("page", "1"))
            page_size = int(request.args.get("page_size", "20"))
            return jsonify(
                zhihu_service.browse_answers(
                    profile_url,
                    query=query,
                    page=page,
                    page_size=page_size,
                )
            )
        except ValueError as exc:
            return api_error(str(exc), "invalid_zhihu_archive_query", 400)
        except (OSError, RuntimeError):
            return api_error(
                "The local Zhihu answer archive could not be read.",
                "zhihu_archive_unavailable",
                500,
            )

    @api.get("/answers/<answer_id>", endpoint="answer")
    def zhihu_answer(answer_id: str):
        require_local_control_request()
        profile_url = request.args.get("profile_url", ZHIHU_EXAMPLE_PROFILE_URL)
        try:
            answer = zhihu_service.read_answer(profile_url, answer_id)
        except ValueError as exc:
            return api_error(str(exc), "invalid_zhihu_answer", 400)
        except (OSError, RuntimeError):
            return api_error(
                "The local Zhihu answer archive could not be read.",
                "zhihu_archive_unavailable",
                500,
            )
        if answer is None:
            return api_error(
                "That answer is not present in this local archive.",
                "zhihu_answer_not_found",
                404,
            )
        return jsonify(answer)

    @api.post("/start", endpoint="start")
    def zhihu_start():
        require_local_control_request()
        if not bool(app.config.get("AGENT_EXTERNAL_OPERATIONS_ENABLED")):
            return api_error(
                "External browser operations are disabled for this isolated application.",
                "external_operations_disabled",
                409,
            )
        payload: Any = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return api_error("Send a JSON object with profile_url and browser.", "invalid_json", 400)
        try:
            zhihu_service.start(
                config_provider(),
                str(payload.get("profile_url") or ""),
                str(payload.get("browser") or ""),
            )
        except ValueError as exc:
            return api_error(str(exc), "invalid_zhihu_request", 400)
        except OSError:
            return api_error(
                "Zhihu Answers Cache could not access its local storage.",
                "zhihu_storage_error",
                500,
            )
        except ZhihuTaskBusyError as exc:
            return api_error(str(exc), "cache_task_busy", 409)
        except ZhihuArchiveError:
            return api_error(
                "Zhihu Answers Cache rejected an unsafe storage boundary.",
                "zhihu_storage_boundary",
                409,
            )
        except RuntimeError:
            return api_error(
                "Zhihu Answers Cache could not start its background worker.",
                "zhihu_start_failed",
                500,
            )
        return jsonify({"status": zhihu_service.snapshot()}), 202

    @api.post("/stop", endpoint="stop")
    def zhihu_stop():
        require_local_control_request()
        stop_requested = zhihu_service.request_stop()
        return (
            jsonify(
                {
                    "stop_requested": stop_requested,
                    "status": zhihu_service.snapshot(),
                }
            ),
            202 if stop_requested else 200,
        )

    app.register_blueprint(api)
