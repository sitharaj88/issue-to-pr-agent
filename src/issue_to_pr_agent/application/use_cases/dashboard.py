from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ...application.services.tenant_access import TenantAccessController
from ...domain.entities import (
    AuthenticatedPrincipal,
    DashboardRecordRef,
    DashboardSummary,
    PlatformPermission,
)
from ...infrastructure.persistence.run_repository import RunRepository


@dataclass(frozen=True)
class DashboardResult:
    summary: DashboardSummary


class DashboardUseCase:
    def __init__(
        self,
        repository: RunRepository,
        access_controller: TenantAccessController,
    ) -> None:
        self._repository = repository
        self._access_controller = access_controller

    def build(
        self,
        *,
        tenant_id: str,
        actor: str | None = None,
        team: str | None = None,
        principal: AuthenticatedPrincipal | None = None,
    ) -> DashboardResult:
        if principal is not None:
            tenant_record, payload = self._access_controller.require_tenant_permission_for_principal(
                tenant_id=tenant_id,
                principal=principal,
                permission=PlatformPermission.VIEW_DASHBOARD,
                team=team,
            )
        else:
            tenant_record, payload = self._access_controller.require_tenant_permission(
                tenant_id=tenant_id,
                actor=actor,
                permission=PlatformPermission.VIEW_DASHBOARD,
                team=team,
            )
        repo_patterns = _string_list(payload.get("repo_patterns"))
        runs = [
            item
            for item in self._repository.list_runs(limit=1000)
            if _repo_matches(item.repo_full_name, repo_patterns)
        ]
        approvals = [
            item
            for item in self._repository.list_approvals(limit=1000)
            if _repo_matches(item.repo_full_name, repo_patterns)
        ]
        deliveries = [
            item
            for item in self._repository.list_deliveries(limit=1000)
            if _repo_matches(item.repo_full_name, repo_patterns)
        ]
        notifications = self._repository.list_notifications(tenant_id=tenant_id, limit=20)

        summary = DashboardSummary(
            tenant_id=tenant_record.tenant_id,
            tenant_name=tenant_record.name,
            generated_at=datetime.now(timezone.utc).isoformat(),
            run_counts=_count_by_key(item.status.value for item in runs),
            approval_counts=_count_by_key(item.status.value for item in approvals),
            delivery_counts=_count_by_key(item.status.value for item in deliveries),
            notification_counts=_count_by_key(item.event_type.value for item in notifications),
            pending_approvals=[
                DashboardRecordRef(
                    record_type="approval",
                    record_id=item.approval_id,
                    created_at=item.updated_at,
                    status=item.status.value,
                    summary=item.summary,
                )
                for item in approvals
                if item.status.value == "pending"
            ][:5],
            recent_deliveries=[
                DashboardRecordRef(
                    record_type="delivery",
                    record_id=item.delivery_id,
                    created_at=item.created_at,
                    status=item.status.value,
                    summary=item.summary,
                )
                for item in deliveries[:5]
            ],
            recent_notifications=[
                DashboardRecordRef(
                    record_type="notification",
                    record_id=item.notification_id,
                    created_at=item.created_at,
                    status=item.status.value,
                    summary=item.summary,
                )
                for item in notifications[:5]
            ],
        )
        return DashboardResult(summary=summary)


def _count_by_key(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _repo_matches(repo_full_name: str, repo_patterns: list[str]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(repo_full_name, pattern) or repo_full_name == pattern for pattern in repo_patterns)
