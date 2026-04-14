from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path

from ...application.services.tenant_access import TenantAccessController
from ...domain.entities import (
    TenantMembershipRecord,
    TenantRecord,
    TenantRole,
    TenantStatus,
)
from ...domain.entities import PlatformPermission
from ...infrastructure.persistence.run_repository import RunRepository


@dataclass(frozen=True)
class TenantAdminResult:
    tenant_id: str
    config_path: Path


class ManageTenantUseCase:
    def __init__(
        self,
        repository: RunRepository,
        access_controller: TenantAccessController,
    ) -> None:
        self._repository = repository
        self._access_controller = access_controller

    def register_tenant(
        self,
        *,
        tenant_id: str,
        name: str,
        repo_patterns: list[str],
        admin_actor: str,
        admin_team: str,
        artifact_dir: Path,
        policy_overrides: dict[str, object] | None = None,
    ) -> TenantAdminResult:
        created_at = datetime.now(timezone.utc).isoformat()
        config_path = artifact_dir / "tenants" / tenant_id / "tenant.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tenant_id": tenant_id,
            "name": name,
            "status": TenantStatus.ACTIVE.value,
            "repo_patterns": repo_patterns,
            "policy_overrides": policy_overrides or {},
        }
        config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        record = TenantRecord(
            tenant_id=tenant_id,
            created_at=created_at,
            updated_at=created_at,
            name=name,
            status=TenantStatus.ACTIVE,
            summary=f"Tenant {name} manages {len(repo_patterns)} repository pattern(s).",
            config_path=config_path,
        )
        self._repository.save_tenant(record, payload)
        membership = TenantMembershipRecord(
            tenant_id=tenant_id,
            actor=admin_actor,
            role=TenantRole.ADMIN,
            team=admin_team,
            created_at=created_at,
            updated_at=created_at,
        )
        self._repository.save_tenant_membership(
            membership,
            {
                "tenant_id": tenant_id,
                "actor": admin_actor,
                "role": TenantRole.ADMIN.value,
                "team": admin_team,
            },
        )
        return TenantAdminResult(tenant_id=tenant_id, config_path=config_path)

    def set_policy_overrides(
        self,
        *,
        tenant_id: str,
        actor: str,
        policy_overrides: dict[str, object],
    ) -> TenantAdminResult:
        tenant_record, payload = self._access_controller.require_tenant_permission(
            tenant_id=tenant_id,
            actor=actor,
            permission=PlatformPermission.MANAGE_POLICY,
        )
        updated_at = datetime.now(timezone.utc).isoformat()
        updated_payload = dict(payload)
        updated_payload["policy_overrides"] = policy_overrides
        tenant_record = TenantRecord(
            tenant_id=tenant_record.tenant_id,
            created_at=tenant_record.created_at,
            updated_at=updated_at,
            name=tenant_record.name,
            status=tenant_record.status,
            summary=tenant_record.summary,
            config_path=tenant_record.config_path,
        )
        tenant_record.config_path.write_text(
            json.dumps(updated_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._repository.save_tenant(tenant_record, updated_payload)
        return TenantAdminResult(tenant_id=tenant_id, config_path=tenant_record.config_path)

    def set_status(
        self,
        *,
        tenant_id: str,
        actor: str,
        status: TenantStatus,
    ) -> TenantAdminResult:
        tenant_record, payload = self._access_controller.require_tenant_permission(
            tenant_id=tenant_id,
            actor=actor,
            permission=PlatformPermission.MANAGE_TENANT,
        )
        updated_at = datetime.now(timezone.utc).isoformat()
        updated_payload = dict(payload)
        updated_payload["status"] = status.value
        updated_payload["name"] = tenant_record.name
        tenant_record = TenantRecord(
            tenant_id=tenant_record.tenant_id,
            created_at=tenant_record.created_at,
            updated_at=updated_at,
            name=tenant_record.name,
            status=status,
            summary=tenant_record.summary,
            config_path=tenant_record.config_path,
        )
        tenant_record.config_path.write_text(
            json.dumps(updated_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._repository.save_tenant(tenant_record, updated_payload)
        return TenantAdminResult(tenant_id=tenant_id, config_path=tenant_record.config_path)

    def add_membership(
        self,
        *,
        tenant_id: str,
        actor: str,
        member_actor: str,
        role: TenantRole,
        team: str,
    ) -> TenantMembershipRecord:
        self._access_controller.require_tenant_permission(
            tenant_id=tenant_id,
            actor=actor,
            permission=PlatformPermission.MANAGE_MEMBERSHIP,
        )
        existing = self._repository.get_tenant_membership(tenant_id, member_actor)
        created_at = datetime.now(timezone.utc).isoformat()
        membership = TenantMembershipRecord(
            tenant_id=tenant_id,
            actor=member_actor,
            role=role,
            team=team,
            created_at=created_at if existing is None else existing[0].created_at,
            updated_at=created_at,
        )
        self._repository.save_tenant_membership(
            membership,
            {
                "tenant_id": tenant_id,
                "actor": member_actor,
                "role": role.value,
                "team": team,
            },
        )
        return membership
