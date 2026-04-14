from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ...domain.entities import QueueJobStatus, QueueJobType
from ...infrastructure.config.settings import Settings
from ...infrastructure.persistence.run_repository import RunRepository
from ...shared.exceptions import PolicyError


class QueueBudgetManager:
    def __init__(self, settings: Settings, repository: RunRepository) -> None:
        self._settings = settings
        self._repository = repository

    def estimate_units(self, *, job_type: QueueJobType, planner_provider: str | None = None) -> int:
        if job_type == QueueJobType.PLAN:
            return (
                self._settings.budget_cost_plan_openai
                if planner_provider == "openai"
                else self._settings.budget_cost_plan_heuristic
            )
        if job_type == QueueJobType.VERIFY:
            return self._settings.budget_cost_verify
        if job_type == QueueJobType.DELIVER:
            return self._settings.budget_cost_deliver
        raise ValueError(f"Unsupported queue job type: {job_type.value}")

    def default_budget_units(
        self,
        *,
        job_type: QueueJobType,
        max_attempts: int,
        planner_provider: str | None = None,
    ) -> int:
        return self.estimate_units(job_type=job_type, planner_provider=planner_provider) * max_attempts

    def ensure_can_enqueue(
        self,
        *,
        tenant_id: str | None,
        budget_units: int,
    ) -> None:
        if budget_units <= 0:
            raise PolicyError("Queue job budget units must be greater than zero.")
        if budget_units > self._settings.budget_max_units_per_job:
            raise PolicyError(
                "Queue job budget exceeds ISSUE_TO_PR_BUDGET_MAX_UNITS_PER_JOB."
            )
        active_statuses = [QueueJobStatus.QUEUED, QueueJobStatus.RUNNING]
        if self._repository.count_queue_jobs(statuses=active_statuses) >= self._settings.budget_max_pending_jobs:
            raise PolicyError("Global pending queue budget has been exhausted.")
        if tenant_id is not None:
            tenant_active = self._repository.count_queue_jobs(statuses=active_statuses, tenant_id=tenant_id)
            if tenant_active >= self._settings.budget_max_pending_jobs_per_tenant:
                raise PolicyError(f"Tenant '{tenant_id}' has exhausted its pending queue budget.")

    def ensure_attempt_within_budget(
        self,
        *,
        budget_units: int,
        budget_used: int,
        job_type: QueueJobType,
        planner_provider: str | None = None,
    ) -> int:
        next_cost = self.estimate_units(job_type=job_type, planner_provider=planner_provider)
        if budget_used + next_cost > budget_units:
            raise PolicyError("Queue job budget would be exceeded by the next attempt.")
        return next_cost

    def next_retry_at(self, *, attempt_count: int, now: datetime | None = None) -> str:
        base = now or datetime.now(timezone.utc)
        backoff_seconds = self._settings.queue_backoff_seconds * max(attempt_count, 1)
        return (base + timedelta(seconds=backoff_seconds)).isoformat()
