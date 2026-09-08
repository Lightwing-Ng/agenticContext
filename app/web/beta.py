"""Optional presentation-only experiments with no production service dependencies.

Code version: v0.1.0
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import os

from flask import Blueprint, Flask, abort, render_template


BETA_VERSION = "v0.1.0"


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
)


def register_beta(
    app: Flask,
    *,
    version: str,
    enabled: bool | None = None,
    experiment_ids: Iterable[str] | None = None,
) -> None:
    """Register only enabled experiments without touching existing app services."""
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

    def render_experiment(selected: BetaExperiment) -> str:
        return render_template(
            "beta.html",
            beta_experiments=experiments,
            beta_experiment=selected,
            beta_version=BETA_VERSION,
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
