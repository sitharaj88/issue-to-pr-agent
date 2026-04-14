from __future__ import annotations

from abc import ABC, abstractmethod

from ...domain.entities import IssueContext, PlanningContext, RepoSnapshot


class ContextBuilder(ABC):
    @abstractmethod
    def build(
        self,
        issue: IssueContext,
        repo_snapshot: RepoSnapshot,
        objective: str | None = None,
    ) -> PlanningContext:
        raise NotImplementedError
