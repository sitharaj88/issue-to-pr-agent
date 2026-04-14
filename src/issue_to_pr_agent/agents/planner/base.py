from __future__ import annotations

from abc import ABC, abstractmethod

from ...domain.entities import AgentPlan, IssueContext, PlannerProvider, PlanningContext, RepoSnapshot


class PlannerClient(ABC):
    provider: PlannerProvider
    model_name: str | None = None

    @abstractmethod
    def plan(
        self,
        issue: IssueContext,
        repo_snapshot: RepoSnapshot,
        planning_context: PlanningContext,
        objective: str | None = None,
    ) -> AgentPlan:
        raise NotImplementedError
