from __future__ import annotations

from fnmatch import fnmatch

from ...domain.entities import (
    AuthSubjectType,
    AuthenticatedPrincipal,
    PlatformPermission,
    TenantRecord,
    TenantRole,
    TenantStatus,
)
from ...infrastructure.persistence.run_repository import RunRepository
from ...shared.exceptions import PolicyError

_ROLE_PERMISSIONS = {
    TenantRole.ADMIN: {
        PlatformPermission.MANAGE_TENANT,
        PlatformPermission.MANAGE_POLICY,
        PlatformPermission.MANAGE_MEMBERSHIP,
        PlatformPermission.OPERATE_QUEUE,
        PlatformPermission.VIEW_QUEUE,
        PlatformPermission.REQUEST_APPROVAL,
        PlatformPermission.REVIEW_APPROVAL,
        PlatformPermission.DELIVER,
        PlatformPermission.VIEW_DASHBOARD,
        PlatformPermission.VIEW_NOTIFICATIONS,
    },
    TenantRole.OPERATOR: {
        PlatformPermission.OPERATE_QUEUE,
        PlatformPermission.VIEW_QUEUE,
        PlatformPermission.REQUEST_APPROVAL,
        PlatformPermission.DELIVER,
        PlatformPermission.VIEW_DASHBOARD,
        PlatformPermission.VIEW_NOTIFICATIONS,
    },
    TenantRole.REVIEWER: {
        PlatformPermission.VIEW_QUEUE,
        PlatformPermission.REVIEW_APPROVAL,
        PlatformPermission.VIEW_DASHBOARD,
        PlatformPermission.VIEW_NOTIFICATIONS,
    },
    TenantRole.OBSERVER: {
        PlatformPermission.VIEW_QUEUE,
        PlatformPermission.VIEW_DASHBOARD,
        PlatformPermission.VIEW_NOTIFICATIONS,
    },
}


class TenantAccessController:
    def __init__(self, repository: RunRepository) -> None:
        self._repository = repository

    def resolve_tenant_for_repo(self, repo_full_name: str) -> tuple[TenantRecord, dict[str, object]] | None:
        for tenant in self._repository.list_tenants(limit=500):
            tenant_context = self._repository.get_tenant(tenant.tenant_id)
            if tenant_context is None:
                continue
            record, payload = tenant_context
            if _repo_matches(repo_full_name, payload.get("repo_patterns")):
                return record, payload
        return None

    def require_repo_permission(
        self,
        *,
        repo_full_name: str,
        actor: str | None,
        permission: PlatformPermission,
        team: str | None = None,
    ) -> tuple[TenantRecord, dict[str, object]] | None:
        tenant_context = self.resolve_tenant_for_repo(repo_full_name)
        if tenant_context is None:
            return None
        if actor is None or not actor.strip():
            raise PolicyError(
                f"Actor is required because repository '{repo_full_name}' is assigned to a tenant."
            )
        record, payload = tenant_context
        self._ensure_tenant_active(record)
        self._ensure_membership(record.tenant_id, actor=actor, permission=permission, team=team)
        return record, payload

    def require_repo_permission_for_principal(
        self,
        *,
        repo_full_name: str,
        principal: AuthenticatedPrincipal,
        permission: PlatformPermission,
        team: str | None = None,
    ) -> tuple[TenantRecord, dict[str, object]] | None:
        tenant_context = self.resolve_tenant_for_repo(repo_full_name)
        if tenant_context is None:
            return None
        record, payload = tenant_context
        self._ensure_tenant_active(record)
        self._ensure_principal_has_permission(
            tenant_id=record.tenant_id,
            principal=principal,
            permission=permission,
            team=team,
        )
        return record, payload

    def require_tenant_permission(
        self,
        *,
        tenant_id: str,
        actor: str | None,
        permission: PlatformPermission,
        team: str | None = None,
    ) -> tuple[TenantRecord, dict[str, object]]:
        tenant_context = self._repository.get_tenant(tenant_id)
        if tenant_context is None:
            raise ValueError(f"Tenant not found: {tenant_id}")
        if actor is None or not actor.strip():
            raise PolicyError(f"Actor is required for tenant-scoped permission checks on {tenant_id}.")
        record, payload = tenant_context
        self._ensure_tenant_active(record)
        self._ensure_membership(tenant_id, actor=actor, permission=permission, team=team)
        return record, payload

    def require_tenant_permission_for_principal(
        self,
        *,
        tenant_id: str,
        principal: AuthenticatedPrincipal,
        permission: PlatformPermission,
        team: str | None = None,
    ) -> tuple[TenantRecord, dict[str, object]]:
        tenant_context = self._repository.get_tenant(tenant_id)
        if tenant_context is None:
            raise ValueError(f"Tenant not found: {tenant_id}")
        record, payload = tenant_context
        self._ensure_tenant_active(record)
        self._ensure_principal_has_permission(
            tenant_id=tenant_id,
            principal=principal,
            permission=permission,
            team=team,
        )
        return record, payload

    def get_membership_role(self, *, tenant_id: str, actor: str) -> TenantRole | None:
        membership = self._repository.get_tenant_membership(tenant_id, actor)
        if membership is None:
            return None
        record, _ = membership
        return record.role

    def _ensure_tenant_active(self, record: TenantRecord) -> None:
        if record.status != TenantStatus.ACTIVE:
            raise PolicyError(f"Tenant '{record.tenant_id}' is not active.")

    def _ensure_membership(
        self,
        tenant_id: str,
        *,
        actor: str,
        permission: PlatformPermission,
        team: str | None,
    ) -> None:
        membership = self._repository.get_tenant_membership(tenant_id, actor)
        if membership is None:
            raise PolicyError(f"Actor '{actor}' is not a member of tenant '{tenant_id}'.")
        record, _ = membership
        if team is not None and team.strip() and record.team != team:
            raise PolicyError(
                f"Actor '{actor}' is registered to team '{record.team}', not '{team}'."
            )
        allowed = _ROLE_PERMISSIONS.get(record.role, set())
        if permission not in allowed:
            raise PolicyError(
                f"Actor '{actor}' with role '{record.role.value}' does not have '{permission.value}' permission."
            )

    def _ensure_principal_has_permission(
        self,
        *,
        tenant_id: str,
        principal: AuthenticatedPrincipal,
        permission: PlatformPermission,
        team: str | None,
    ) -> None:
        if principal.subject_type == AuthSubjectType.SERVICE and self._service_has_permission(
            principal=principal,
            permission=permission,
            tenant_id=tenant_id,
        ):
            return
        membership = self._repository.get_tenant_membership(tenant_id, principal.actor)
        if membership is None:
            raise PolicyError(f"Actor '{principal.actor}' is not a member of tenant '{tenant_id}'.")
        record, _ = membership
        if team is not None and team.strip() and record.team != team:
            raise PolicyError(
                f"Actor '{principal.actor}' is registered to team '{record.team}', not '{team}'."
            )
        principal_teams = {item for item in principal.groups if item}
        if principal.team:
            principal_teams.add(principal.team)
        if principal_teams and record.team not in principal_teams:
            raise PolicyError(
                f"Authenticated principal does not include required team '{record.team}' for actor '{principal.actor}'."
            )
        allowed = _ROLE_PERMISSIONS.get(record.role, set())
        if permission not in allowed:
            raise PolicyError(
                f"Actor '{principal.actor}' with role '{record.role.value}' does not have '{permission.value}' permission."
            )

    def _service_has_permission(
        self,
        *,
        principal: AuthenticatedPrincipal,
        permission: PlatformPermission,
        tenant_id: str,
    ) -> bool:
        scopes = {scope.strip() for scope in principal.scopes if scope.strip()}
        if not scopes:
            return False
        if principal.tenant_ids and tenant_id not in principal.tenant_ids:
            raise PolicyError(
                f"Service principal '{principal.subject}' is not scoped to tenant '{tenant_id}'."
            )
        if "*" in scopes:
            return True
        if permission.value in scopes:
            return True
        return f"tenant:{tenant_id}:{permission.value}" in scopes


def _repo_matches(repo_full_name: str, repo_patterns: object) -> bool:
    if not isinstance(repo_patterns, list):
        return False
    for item in repo_patterns:
        if not isinstance(item, str) or not item.strip():
            continue
        pattern = item.strip()
        if fnmatch(repo_full_name, pattern):
            return True
        if repo_full_name == pattern:
            return True
    return False
