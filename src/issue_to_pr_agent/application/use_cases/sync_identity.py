from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ...application.services.tenant_access import TenantAccessController
from ...domain.entities import (
    AuthenticatedPrincipal,
    IdentitySyncMembership,
    IdentitySyncReceipt,
    PlatformPermission,
    TenantMembershipRecord,
)
from ...infrastructure.persistence.run_repository import RunRepository


@dataclass(frozen=True)
class IdentitySyncResult:
    receipt: IdentitySyncReceipt


class SyncIdentityUseCase:
    def __init__(
        self,
        repository: RunRepository,
        access_controller: TenantAccessController,
    ) -> None:
        self._repository = repository
        self._access_controller = access_controller

    def sync_tenant_memberships(
        self,
        *,
        tenant_id: str,
        memberships: list[IdentitySyncMembership],
        replace_existing: bool,
        actor: str | None = None,
        team: str | None = None,
        principal: AuthenticatedPrincipal | None = None,
    ) -> IdentitySyncResult:
        if principal is not None:
            self._access_controller.require_tenant_permission_for_principal(
                tenant_id=tenant_id,
                principal=principal,
                permission=PlatformPermission.MANAGE_MEMBERSHIP,
                team=team,
            )
            synced_by = principal.actor
        else:
            self._access_controller.require_tenant_permission(
                tenant_id=tenant_id,
                actor=actor,
                permission=PlatformPermission.MANAGE_MEMBERSHIP,
                team=team,
            )
            synced_by = actor or ""

        existing = {record.actor: record for record in self._repository.list_tenant_memberships(tenant_id)}
        synced_at = datetime.now(timezone.utc).isoformat()
        created_count = 0
        updated_count = 0
        desired_actors: list[str] = []

        for membership in memberships:
            desired_actors.append(membership.actor)
            existing_record = existing.get(membership.actor)
            is_update = (
                existing_record is not None
                and existing_record.role == membership.role
                and existing_record.team == membership.team
            )
            record = TenantMembershipRecord(
                tenant_id=tenant_id,
                actor=membership.actor,
                role=membership.role,
                team=membership.team,
                created_at=synced_at if existing_record is None else existing_record.created_at,
                updated_at=synced_at,
            )
            self._repository.save_tenant_membership(
                record,
                {
                    "tenant_id": tenant_id,
                    "actor": membership.actor,
                    "role": membership.role.value,
                    "team": membership.team,
                    "synced_at": synced_at,
                    "synced_by": synced_by,
                },
            )
            if existing_record is None:
                created_count += 1
            elif not is_update:
                updated_count += 1

        removed_count = 0
        if replace_existing:
            removed_count = self._repository.delete_tenant_memberships_except(tenant_id, desired_actors)

        receipt = IdentitySyncReceipt(
            tenant_id=tenant_id,
            synced_at=synced_at,
            synced_by=synced_by,
            replace_existing=replace_existing,
            created_count=created_count,
            updated_count=updated_count,
            removed_count=removed_count,
            membership_count=len(desired_actors),
        )
        return IdentitySyncResult(receipt=receipt)
