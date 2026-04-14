from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ...domain.entities import AgentPlan, IssueContext, PatchFileContext, PatchProposal, PatcherProvider, PlanningContext


class PatcherClient(ABC):
    provider: PatcherProvider
    model_name: str | None = None

    @abstractmethod
    def generate(
        self,
        *,
        linked_run_id: str,
        issue: IssueContext,
        plan: AgentPlan,
        planning_context: PlanningContext,
        repo_root: Path,
        files: list[PatchFileContext],
        allowed_existing_paths: list[str],
        suggested_new_file_directories: list[str],
        objective: str | None = None,
    ) -> PatchProposal:
        raise NotImplementedError
