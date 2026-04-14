from __future__ import annotations

from dataclasses import dataclass, field

from ...domain.entities import PatchFileContext, PlanningContext
from ...infrastructure.config.settings import Settings


@dataclass(frozen=True)
class ModelRoutingDecision:
    model_name: str
    complexity_score: int
    reasons: list[str] = field(default_factory=list)


class ModelRoutingService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def select_planner_model(self, *, planning_context: PlanningContext) -> ModelRoutingDecision:
        complexity = planning_context.repository_index.complexity_score
        reasons = [f"Repository complexity scored {complexity} for planning."]
        model_name = self._settings.openai_model
        if complexity >= self._settings.router_planner_complexity_threshold:
            model_name = self._settings.openai_complex_model
            reasons.append("Selected complex planner model due to repository complexity.")
        else:
            reasons.append("Selected standard planner model for bounded planning cost.")
        return ModelRoutingDecision(
            model_name=model_name,
            complexity_score=complexity,
            reasons=reasons,
        )

    def select_patch_model(
        self,
        *,
        planning_context: PlanningContext,
        files: list[PatchFileContext],
    ) -> ModelRoutingDecision:
        file_weight = sum(len(item.content) for item in files) // 800
        complexity = planning_context.repository_index.complexity_score + file_weight
        reasons = [
            f"Repository complexity scored {planning_context.repository_index.complexity_score} for patching.",
            f"Patch context added {file_weight} complexity points from file content volume.",
        ]
        model_name = self._settings.openai_model
        if complexity >= self._settings.router_patch_complexity_threshold:
            model_name = self._settings.openai_complex_model
            reasons.append("Selected complex patch model due to repo and patch-context complexity.")
        else:
            reasons.append("Selected standard patch model for bounded patch generation cost.")
        return ModelRoutingDecision(
            model_name=model_name,
            complexity_score=complexity,
            reasons=reasons,
        )
