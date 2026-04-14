from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from ...domain.entities import (
    AlertRecord,
    AlertSeverity,
    AlertStatus,
    ApprovalAction,
    ApprovalRecord,
    ApprovalRiskLevel,
    ApprovalStatus,
    AutofixAttemptRecord,
    AutofixAttemptStatus,
    AutofixRunRecord,
    AutofixStatus,
    NotificationEventType,
    NotificationRecord,
    NotificationStatus,
    PlatformPermission,
    TenantMembershipRecord,
    TenantRecord,
    TenantRole,
    TenantStatus,
    DeliveryRecord,
    DeliveryStatus,
    ExecutionMode,
    PatchExecutionMode,
    PatchExecutionRecord,
    PatchExecutionStatus,
    PatchProposalRecord,
    PatcherProvider,
    PlannerProvider,
    QueueAttemptRecord,
    QueueAttemptStatus,
    QueueJobRecord,
    QueueJobStatus,
    QueueJobType,
    RunRecord,
    RunStatus,
    SandboxRecord,
    SandboxStatus,
    SchemaMigrationRecord,
    TraceEventRecord,
    WorkerHeartbeatRecord,
    WorkerStatus,
    VerificationRecord,
    VerificationStatus,
    VerificationStopReason,
)
from ...shared.exceptions import StorageError

_SCHEMA_MIGRATIONS: list[tuple[int, str]] = [
    (1, "bootstrap_core_tables"),
    (2, "queue_leases_and_worker_affinity"),
    (3, "observability_and_compliance"),
    (4, "phase21_release_and_intelligence"),
]


class RunRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def list_schema_migrations(self) -> list[SchemaMigrationRecord]:
        sql = """
        SELECT version, name, applied_at
        FROM schema_migrations
        ORDER BY version ASC
        """
        with self._managed_connection() as conn:
            rows = conn.execute(sql).fetchall()
        return [
            SchemaMigrationRecord(version=int(row[0]), name=str(row[1]), applied_at=str(row[2]))
            for row in rows
        ]

    def current_schema_version(self) -> int:
        migrations = self.list_schema_migrations()
        return migrations[-1].version if migrations else 0

    def save_run(self, record: RunRecord, payload: dict[str, object]) -> None:
        sql = """
        INSERT OR REPLACE INTO agent_runs (
            run_id,
            created_at,
            repo_full_name,
            issue_number,
            planner_provider,
            execution_mode,
            status,
            branch_name,
            summary,
            issue_url,
            report_path,
            pr_draft_path,
            audit_path,
            error_message,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            record.run_id,
            record.created_at,
            record.repo_full_name,
            record.issue_number,
            record.planner_provider.value,
            record.execution_mode.value,
            record.status.value,
            record.branch_name,
            record.summary,
            record.issue_url,
            str(record.report_path),
            str(record.pr_draft_path),
            str(record.audit_path),
            record.error_message,
            json.dumps(payload, indent=2, sort_keys=True),
        )
        with self._managed_connection() as conn:
            conn.execute(sql, values)
            conn.commit()

    def list_runs(self, *, limit: int = 20) -> list[RunRecord]:
        sql = """
        SELECT
            run_id,
            created_at,
            repo_full_name,
            issue_number,
            planner_provider,
            execution_mode,
            status,
            branch_name,
            summary,
            issue_url,
            report_path,
            pr_draft_path,
            audit_path,
            error_message
        FROM agent_runs
        ORDER BY created_at DESC
        LIMIT ?
        """
        with self._managed_connection() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [self._row_to_record(row) for row in rows]

    def get_run(self, run_id: str) -> tuple[RunRecord, dict[str, object]] | None:
        sql = """
        SELECT
            run_id,
            created_at,
            repo_full_name,
            issue_number,
            planner_provider,
            execution_mode,
            status,
            branch_name,
            summary,
            issue_url,
            report_path,
            pr_draft_path,
            audit_path,
            error_message,
            payload_json
        FROM agent_runs
        WHERE run_id = ?
        """
        with self._managed_connection() as conn:
            row = conn.execute(sql, (run_id,)).fetchone()
        if row is None:
            return None
        record = self._row_to_record(row[:-1])
        payload = json.loads(row[-1])
        if not isinstance(payload, dict):
            raise StorageError(f"Stored payload for run {run_id} is not a JSON object.")
        return record, payload

    def save_execution(self, record: PatchExecutionRecord, payload: dict[str, object]) -> None:
        sql = """
        INSERT OR REPLACE INTO patch_executions (
            execution_id,
            created_at,
            proposal_id,
            linked_run_id,
            mode,
            status,
            summary,
            repo_root,
            receipt_path,
            error_message,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            record.execution_id,
            record.created_at,
            record.proposal_id,
            record.linked_run_id,
            record.mode.value,
            record.status.value,
            record.summary,
            str(record.repo_root),
            str(record.receipt_path),
            record.error_message,
            json.dumps(payload, indent=2, sort_keys=True),
        )
        with self._managed_connection() as conn:
            conn.execute(sql, values)
            conn.commit()

    def list_executions(self, *, limit: int = 20) -> list[PatchExecutionRecord]:
        sql = """
        SELECT
            execution_id,
            created_at,
            proposal_id,
            linked_run_id,
            mode,
            status,
            summary,
            repo_root,
            receipt_path,
            error_message
        FROM patch_executions
        ORDER BY created_at DESC
        LIMIT ?
        """
        with self._managed_connection() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [self._row_to_execution_record(row) for row in rows]

    def get_execution(self, execution_id: str) -> tuple[PatchExecutionRecord, dict[str, object]] | None:
        sql = """
        SELECT
            execution_id,
            created_at,
            proposal_id,
            linked_run_id,
            mode,
            status,
            summary,
            repo_root,
            receipt_path,
            error_message,
            payload_json
        FROM patch_executions
        WHERE execution_id = ?
        """
        with self._managed_connection() as conn:
            row = conn.execute(sql, (execution_id,)).fetchone()
        if row is None:
            return None
        record = self._row_to_execution_record(row[:-1])
        payload = json.loads(row[-1])
        if not isinstance(payload, dict):
            raise StorageError(f"Stored payload for execution {execution_id} is not a JSON object.")
        return record, payload

    def save_patch_proposal(self, record: PatchProposalRecord, payload: dict[str, object]) -> None:
        sql = """
        INSERT OR REPLACE INTO patch_proposals (
            proposal_id,
            created_at,
            linked_run_id,
            provider,
            summary,
            proposal_path,
            error_message,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            record.proposal_id,
            record.created_at,
            record.linked_run_id,
            record.provider.value,
            record.summary,
            str(record.proposal_path),
            record.error_message,
            json.dumps(payload, indent=2, sort_keys=True),
        )
        with self._managed_connection() as conn:
            conn.execute(sql, values)
            conn.commit()

    def list_patch_proposals(self, *, limit: int = 20) -> list[PatchProposalRecord]:
        sql = """
        SELECT
            proposal_id,
            created_at,
            linked_run_id,
            provider,
            summary,
            proposal_path,
            error_message
        FROM patch_proposals
        ORDER BY created_at DESC
        LIMIT ?
        """
        with self._managed_connection() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [self._row_to_patch_proposal_record(row) for row in rows]

    def get_patch_proposal(self, proposal_id: str) -> tuple[PatchProposalRecord, dict[str, object]] | None:
        sql = """
        SELECT
            proposal_id,
            created_at,
            linked_run_id,
            provider,
            summary,
            proposal_path,
            error_message,
            payload_json
        FROM patch_proposals
        WHERE proposal_id = ?
        """
        with self._managed_connection() as conn:
            row = conn.execute(sql, (proposal_id,)).fetchone()
        if row is None:
            return None
        record = self._row_to_patch_proposal_record(row[:-1])
        payload = json.loads(row[-1])
        if not isinstance(payload, dict):
            raise StorageError(f"Stored payload for patch proposal {proposal_id} is not a JSON object.")
        return record, payload

    def save_autofix_run(self, record: AutofixRunRecord, payload: dict[str, object]) -> None:
        sql = """
        INSERT OR REPLACE INTO autofix_runs (
            autofix_id,
            created_at,
            updated_at,
            linked_run_id,
            provider,
            status,
            summary,
            repo_root,
            max_attempts,
            attempt_count,
            latest_proposal_id,
            latest_execution_id,
            latest_verification_id,
            receipt_path,
            error_message,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            record.autofix_id,
            record.created_at,
            record.updated_at,
            record.linked_run_id,
            record.provider.value,
            record.status.value,
            record.summary,
            str(record.repo_root),
            record.max_attempts,
            record.attempt_count,
            record.latest_proposal_id,
            record.latest_execution_id,
            record.latest_verification_id,
            str(record.receipt_path),
            record.error_message,
            json.dumps(payload, indent=2, sort_keys=True),
        )
        with self._managed_connection() as conn:
            conn.execute(sql, values)
            conn.commit()

    def list_autofix_runs(self, *, limit: int = 20) -> list[AutofixRunRecord]:
        sql = """
        SELECT
            autofix_id,
            created_at,
            updated_at,
            linked_run_id,
            provider,
            status,
            summary,
            repo_root,
            max_attempts,
            attempt_count,
            latest_proposal_id,
            latest_execution_id,
            latest_verification_id,
            receipt_path,
            error_message
        FROM autofix_runs
        ORDER BY updated_at DESC
        LIMIT ?
        """
        with self._managed_connection() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [self._row_to_autofix_run_record(row) for row in rows]

    def get_autofix_run(self, autofix_id: str) -> tuple[AutofixRunRecord, dict[str, object]] | None:
        sql = """
        SELECT
            autofix_id,
            created_at,
            updated_at,
            linked_run_id,
            provider,
            status,
            summary,
            repo_root,
            max_attempts,
            attempt_count,
            latest_proposal_id,
            latest_execution_id,
            latest_verification_id,
            receipt_path,
            error_message,
            payload_json
        FROM autofix_runs
        WHERE autofix_id = ?
        """
        with self._managed_connection() as conn:
            row = conn.execute(sql, (autofix_id,)).fetchone()
        if row is None:
            return None
        record = self._row_to_autofix_run_record(row[:-1])
        payload = json.loads(row[-1])
        if not isinstance(payload, dict):
            raise StorageError(f"Stored payload for autofix run {autofix_id} is not a JSON object.")
        return record, payload

    def save_autofix_attempt(self, record: AutofixAttemptRecord, payload: dict[str, object]) -> None:
        sql = """
        INSERT OR REPLACE INTO autofix_attempts (
            attempt_id,
            autofix_id,
            attempt_index,
            created_at,
            status,
            summary,
            objective,
            proposal_id,
            execution_id,
            verification_id,
            verification_stop_reason,
            payload_path,
            error_message,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            record.attempt_id,
            record.autofix_id,
            record.attempt_index,
            record.created_at,
            record.status.value,
            record.summary,
            record.objective,
            record.proposal_id,
            record.execution_id,
            record.verification_id,
            None if record.verification_stop_reason is None else record.verification_stop_reason.value,
            str(record.payload_path),
            record.error_message,
            json.dumps(payload, indent=2, sort_keys=True),
        )
        with self._managed_connection() as conn:
            conn.execute(sql, values)
            conn.commit()

    def list_autofix_attempts(self, *, autofix_id: str, limit: int = 50) -> list[AutofixAttemptRecord]:
        sql = """
        SELECT
            attempt_id,
            autofix_id,
            attempt_index,
            created_at,
            status,
            summary,
            objective,
            proposal_id,
            execution_id,
            verification_id,
            verification_stop_reason,
            payload_path,
            error_message
        FROM autofix_attempts
        WHERE autofix_id = ?
        ORDER BY attempt_index ASC
        LIMIT ?
        """
        with self._managed_connection() as conn:
            rows = conn.execute(sql, (autofix_id, limit)).fetchall()
        return [self._row_to_autofix_attempt_record(row) for row in rows]

    def save_sandbox(self, record: SandboxRecord, payload: dict[str, object]) -> None:
        sql = """
        INSERT OR REPLACE INTO sandboxes (
            sandbox_id,
            created_at,
            updated_at,
            linked_run_id,
            linked_autofix_id,
            status,
            source_repo_root,
            workspace_root,
            copied_file_count,
            skipped_entry_count,
            total_bytes,
            summary,
            receipt_path,
            error_message,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            record.sandbox_id,
            record.created_at,
            record.updated_at,
            record.linked_run_id,
            record.linked_autofix_id,
            record.status.value,
            str(record.source_repo_root),
            str(record.workspace_root),
            record.copied_file_count,
            record.skipped_entry_count,
            record.total_bytes,
            record.summary,
            str(record.receipt_path),
            record.error_message,
            json.dumps(payload, indent=2, sort_keys=True),
        )
        with self._managed_connection() as conn:
            conn.execute(sql, values)
            conn.commit()

    def list_sandboxes(self, *, limit: int = 20) -> list[SandboxRecord]:
        sql = """
        SELECT
            sandbox_id,
            created_at,
            updated_at,
            linked_run_id,
            linked_autofix_id,
            status,
            source_repo_root,
            workspace_root,
            copied_file_count,
            skipped_entry_count,
            total_bytes,
            summary,
            receipt_path,
            error_message
        FROM sandboxes
        ORDER BY updated_at DESC
        LIMIT ?
        """
        with self._managed_connection() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [self._row_to_sandbox_record(row) for row in rows]

    def get_sandbox(self, sandbox_id: str) -> tuple[SandboxRecord, dict[str, object]] | None:
        sql = """
        SELECT
            sandbox_id,
            created_at,
            updated_at,
            linked_run_id,
            linked_autofix_id,
            status,
            source_repo_root,
            workspace_root,
            copied_file_count,
            skipped_entry_count,
            total_bytes,
            summary,
            receipt_path,
            error_message,
            payload_json
        FROM sandboxes
        WHERE sandbox_id = ?
        """
        with self._managed_connection() as conn:
            row = conn.execute(sql, (sandbox_id,)).fetchone()
        if row is None:
            return None
        record = self._row_to_sandbox_record(row[:-1])
        payload = json.loads(row[-1])
        if not isinstance(payload, dict):
            raise StorageError(f"Stored payload for sandbox {sandbox_id} is not a JSON object.")
        return record, payload

    def save_verification(self, record: VerificationRecord, payload: dict[str, object]) -> None:
        sql = """
        INSERT OR REPLACE INTO verifications (
            verification_id,
            created_at,
            linked_run_id,
            linked_execution_id,
            status,
            stop_reason,
            summary,
            repo_root,
            receipt_path,
            error_message,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            record.verification_id,
            record.created_at,
            record.linked_run_id,
            record.linked_execution_id,
            record.status.value,
            record.stop_reason.value,
            record.summary,
            str(record.repo_root),
            str(record.receipt_path),
            record.error_message,
            json.dumps(payload, indent=2, sort_keys=True),
        )
        with self._managed_connection() as conn:
            conn.execute(sql, values)
            conn.commit()

    def list_verifications(self, *, limit: int = 20) -> list[VerificationRecord]:
        sql = """
        SELECT
            verification_id,
            created_at,
            linked_run_id,
            linked_execution_id,
            status,
            stop_reason,
            summary,
            repo_root,
            receipt_path,
            error_message
        FROM verifications
        ORDER BY created_at DESC
        LIMIT ?
        """
        with self._managed_connection() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [self._row_to_verification_record(row) for row in rows]

    def get_verification(self, verification_id: str) -> tuple[VerificationRecord, dict[str, object]] | None:
        sql = """
        SELECT
            verification_id,
            created_at,
            linked_run_id,
            linked_execution_id,
            status,
            stop_reason,
            summary,
            repo_root,
            receipt_path,
            error_message,
            payload_json
        FROM verifications
        WHERE verification_id = ?
        """
        with self._managed_connection() as conn:
            row = conn.execute(sql, (verification_id,)).fetchone()
        if row is None:
            return None
        record = self._row_to_verification_record(row[:-1])
        payload = json.loads(row[-1])
        if not isinstance(payload, dict):
            raise StorageError(f"Stored payload for verification {verification_id} is not a JSON object.")
        return record, payload

    def save_delivery(self, record: DeliveryRecord, payload: dict[str, object]) -> None:
        sql = """
        INSERT OR REPLACE INTO deliveries (
            delivery_id,
            created_at,
            linked_run_id,
            linked_execution_id,
            linked_verification_id,
            status,
            repo_full_name,
            branch_name,
            base_branch,
            summary,
            receipt_path,
            error_message,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            record.delivery_id,
            record.created_at,
            record.linked_run_id,
            record.linked_execution_id,
            record.linked_verification_id,
            record.status.value,
            record.repo_full_name,
            record.branch_name,
            record.base_branch,
            record.summary,
            str(record.receipt_path),
            record.error_message,
            json.dumps(payload, indent=2, sort_keys=True),
        )
        with self._managed_connection() as conn:
            conn.execute(sql, values)
            conn.commit()

    def list_deliveries(self, *, limit: int = 20) -> list[DeliveryRecord]:
        sql = """
        SELECT
            delivery_id,
            created_at,
            linked_run_id,
            linked_execution_id,
            linked_verification_id,
            status,
            repo_full_name,
            branch_name,
            base_branch,
            summary,
            receipt_path,
            error_message
        FROM deliveries
        ORDER BY created_at DESC
        LIMIT ?
        """
        with self._managed_connection() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [self._row_to_delivery_record(row) for row in rows]

    def get_delivery(self, delivery_id: str) -> tuple[DeliveryRecord, dict[str, object]] | None:
        sql = """
        SELECT
            delivery_id,
            created_at,
            linked_run_id,
            linked_execution_id,
            linked_verification_id,
            status,
            repo_full_name,
            branch_name,
            base_branch,
            summary,
            receipt_path,
            error_message,
            payload_json
        FROM deliveries
        WHERE delivery_id = ?
        """
        with self._managed_connection() as conn:
            row = conn.execute(sql, (delivery_id,)).fetchone()
        if row is None:
            return None
        record = self._row_to_delivery_record(row[:-1])
        payload = json.loads(row[-1])
        if not isinstance(payload, dict):
            raise StorageError(f"Stored payload for delivery {delivery_id} is not a JSON object.")
        return record, payload

    def save_approval(self, record: ApprovalRecord, payload: dict[str, object]) -> None:
        sql = """
        INSERT OR REPLACE INTO approvals (
            approval_id,
            created_at,
            updated_at,
            action,
            linked_run_id,
            linked_execution_id,
            linked_verification_id,
            repo_full_name,
            status,
            risk_level,
            requested_by,
            requester_team,
            required_approvals,
            approved_count,
            summary,
            receipt_path,
            error_message,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            record.approval_id,
            record.created_at,
            record.updated_at,
            record.action.value,
            record.linked_run_id,
            record.linked_execution_id,
            record.linked_verification_id,
            record.repo_full_name,
            record.status.value,
            record.risk_level.value,
            record.requested_by,
            record.requester_team,
            record.required_approvals,
            record.approved_count,
            record.summary,
            str(record.receipt_path),
            record.error_message,
            json.dumps(payload, indent=2, sort_keys=True),
        )
        with self._managed_connection() as conn:
            conn.execute(sql, values)
            conn.commit()

    def list_approvals(
        self,
        *,
        limit: int = 20,
        status: ApprovalStatus | None = None,
    ) -> list[ApprovalRecord]:
        sql = """
        SELECT
            approval_id,
            created_at,
            updated_at,
            action,
            linked_run_id,
            linked_execution_id,
            linked_verification_id,
            repo_full_name,
            status,
            risk_level,
            requested_by,
            requester_team,
            required_approvals,
            approved_count,
            summary,
            receipt_path,
            error_message
        FROM approvals
        """
        params: tuple[object, ...]
        if status is not None:
            sql += " WHERE status = ?"
            params = (status.value, limit)
        else:
            params = (limit,)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        with self._managed_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_approval_record(row) for row in rows]

    def get_approval(self, approval_id: str) -> tuple[ApprovalRecord, dict[str, object]] | None:
        sql = """
        SELECT
            approval_id,
            created_at,
            updated_at,
            action,
            linked_run_id,
            linked_execution_id,
            linked_verification_id,
            repo_full_name,
            status,
            risk_level,
            requested_by,
            requester_team,
            required_approvals,
            approved_count,
            summary,
            receipt_path,
            error_message,
            payload_json
        FROM approvals
        WHERE approval_id = ?
        """
        with self._managed_connection() as conn:
            row = conn.execute(sql, (approval_id,)).fetchone()
        if row is None:
            return None
        record = self._row_to_approval_record(row[:-1])
        payload = json.loads(row[-1])
        if not isinstance(payload, dict):
            raise StorageError(f"Stored payload for approval {approval_id} is not a JSON object.")
        return record, payload

    def save_tenant(self, record: TenantRecord, payload: dict[str, object]) -> None:
        sql = """
        INSERT OR REPLACE INTO tenants (
            tenant_id,
            created_at,
            updated_at,
            name,
            status,
            summary,
            config_path,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            record.tenant_id,
            record.created_at,
            record.updated_at,
            record.name,
            record.status.value,
            record.summary,
            str(record.config_path),
            json.dumps(payload, indent=2, sort_keys=True),
        )
        with self._managed_connection() as conn:
            conn.execute(sql, values)
            conn.commit()

    def list_tenants(self, *, limit: int = 100) -> list[TenantRecord]:
        sql = """
        SELECT
            tenant_id,
            created_at,
            updated_at,
            name,
            status,
            summary,
            config_path
        FROM tenants
        ORDER BY updated_at DESC
        LIMIT ?
        """
        with self._managed_connection() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [self._row_to_tenant_record(row) for row in rows]

    def get_tenant(self, tenant_id: str) -> tuple[TenantRecord, dict[str, object]] | None:
        sql = """
        SELECT
            tenant_id,
            created_at,
            updated_at,
            name,
            status,
            summary,
            config_path,
            payload_json
        FROM tenants
        WHERE tenant_id = ?
        """
        with self._managed_connection() as conn:
            row = conn.execute(sql, (tenant_id,)).fetchone()
        if row is None:
            return None
        record = self._row_to_tenant_record(row[:-1])
        payload = json.loads(row[-1])
        if not isinstance(payload, dict):
            raise StorageError(f"Stored payload for tenant {tenant_id} is not a JSON object.")
        return record, payload

    def save_tenant_membership(self, record: TenantMembershipRecord, payload: dict[str, object]) -> None:
        sql = """
        INSERT OR REPLACE INTO tenant_memberships (
            tenant_id,
            actor,
            role,
            team,
            created_at,
            updated_at,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            record.tenant_id,
            record.actor,
            record.role.value,
            record.team,
            record.created_at,
            record.updated_at,
            json.dumps(payload, indent=2, sort_keys=True),
        )
        with self._managed_connection() as conn:
            conn.execute(sql, values)
            conn.commit()

    def list_tenant_memberships(self, tenant_id: str) -> list[TenantMembershipRecord]:
        sql = """
        SELECT
            tenant_id,
            actor,
            role,
            team,
            created_at,
            updated_at
        FROM tenant_memberships
        WHERE tenant_id = ?
        ORDER BY actor ASC
        """
        with self._managed_connection() as conn:
            rows = conn.execute(sql, (tenant_id,)).fetchall()
        return [self._row_to_tenant_membership_record(row) for row in rows]

    def get_tenant_membership(self, tenant_id: str, actor: str) -> tuple[TenantMembershipRecord, dict[str, object]] | None:
        sql = """
        SELECT
            tenant_id,
            actor,
            role,
            team,
            created_at,
            updated_at,
            payload_json
        FROM tenant_memberships
        WHERE tenant_id = ? AND actor = ?
        """
        with self._managed_connection() as conn:
            row = conn.execute(sql, (tenant_id, actor)).fetchone()
        if row is None:
            return None
        record = self._row_to_tenant_membership_record(row[:-1])
        payload = json.loads(row[-1])
        if not isinstance(payload, dict):
            raise StorageError(
                f"Stored payload for tenant membership {tenant_id}/{actor} is not a JSON object."
            )
        return record, payload

    def delete_tenant_memberships_except(self, tenant_id: str, actors: list[str]) -> int:
        actor_list = [item for item in actors if item]
        with self._managed_connection() as conn:
            if actor_list:
                placeholders = ", ".join("?" for _ in actor_list)
                sql = (
                    "DELETE FROM tenant_memberships "
                    f"WHERE tenant_id = ? AND actor NOT IN ({placeholders})"
                )
                cursor = conn.execute(sql, (tenant_id, *actor_list))
            else:
                cursor = conn.execute("DELETE FROM tenant_memberships WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return int(cursor.rowcount)

    def save_notification(self, record: NotificationRecord, payload: dict[str, object]) -> None:
        sql = """
        INSERT OR REPLACE INTO notifications (
            notification_id,
            created_at,
            tenant_id,
            event_type,
            status,
            summary,
            payload_path,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            record.notification_id,
            record.created_at,
            record.tenant_id,
            record.event_type.value,
            record.status.value,
            record.summary,
            str(record.payload_path),
            json.dumps(payload, indent=2, sort_keys=True),
        )
        with self._managed_connection() as conn:
            conn.execute(sql, values)
            conn.commit()

    def list_notifications(self, *, tenant_id: str | None = None, limit: int = 20) -> list[NotificationRecord]:
        sql = """
        SELECT
            notification_id,
            created_at,
            tenant_id,
            event_type,
            status,
            summary,
            payload_path
        FROM notifications
        """
        params: tuple[object, ...]
        if tenant_id is not None:
            sql += " WHERE tenant_id = ?"
            params = (tenant_id, limit)
        else:
            params = (limit,)
        sql += " ORDER BY created_at DESC LIMIT ?"
        with self._managed_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_notification_record(row) for row in rows]

    def get_notification(self, notification_id: str) -> tuple[NotificationRecord, dict[str, object]] | None:
        sql = """
        SELECT
            notification_id,
            created_at,
            tenant_id,
            event_type,
            status,
            summary,
            payload_path,
            payload_json
        FROM notifications
        WHERE notification_id = ?
        """
        with self._managed_connection() as conn:
            row = conn.execute(sql, (notification_id,)).fetchone()
        if row is None:
            return None
        record = self._row_to_notification_record(row[:-1])
        payload = json.loads(row[-1])
        if not isinstance(payload, dict):
            raise StorageError(f"Stored payload for notification {notification_id} is not a JSON object.")
        return record, payload

    def save_alert(self, record: AlertRecord, payload: dict[str, object]) -> None:
        sql = """
        INSERT OR REPLACE INTO alerts (
            alert_id,
            created_at,
            tenant_id,
            severity,
            source,
            status,
            summary,
            payload_path,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            record.alert_id,
            record.created_at,
            record.tenant_id,
            record.severity.value,
            record.source,
            record.status.value,
            record.summary,
            str(record.payload_path),
            json.dumps(payload, indent=2, sort_keys=True),
        )
        with self._managed_connection() as conn:
            conn.execute(sql, values)
            conn.commit()

    def list_alerts(
        self,
        *,
        tenant_id: str | None = None,
        severity: AlertSeverity | None = None,
        status: AlertStatus | None = None,
        limit: int = 20,
    ) -> list[AlertRecord]:
        sql = """
        SELECT
            alert_id,
            created_at,
            tenant_id,
            severity,
            source,
            status,
            summary,
            payload_path
        FROM alerts
        """
        clauses: list[str] = []
        params: list[object] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity.value)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._managed_connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_alert_record(row) for row in rows]

    def get_alert(self, alert_id: str) -> tuple[AlertRecord, dict[str, object]] | None:
        sql = """
        SELECT
            alert_id,
            created_at,
            tenant_id,
            severity,
            source,
            status,
            summary,
            payload_path,
            payload_json
        FROM alerts
        WHERE alert_id = ?
        """
        with self._managed_connection() as conn:
            row = conn.execute(sql, (alert_id,)).fetchone()
        if row is None:
            return None
        record = self._row_to_alert_record(row[:-1])
        payload = json.loads(row[-1])
        if not isinstance(payload, dict):
            raise StorageError(f"Stored payload for alert {alert_id} is not a JSON object.")
        return record, payload

    def save_trace_event(self, record: TraceEventRecord, payload: dict[str, object]) -> None:
        sql = """
        INSERT OR REPLACE INTO trace_events (
            event_id,
            trace_id,
            recorded_at,
            source,
            span_name,
            status,
            payload_path,
            linked_run_id,
            linked_job_id,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            record.event_id,
            record.trace_id,
            record.recorded_at,
            record.source,
            record.span_name,
            record.status,
            str(record.payload_path),
            record.linked_run_id,
            record.linked_job_id,
            json.dumps(payload, indent=2, sort_keys=True),
        )
        with self._managed_connection() as conn:
            conn.execute(sql, values)
            conn.commit()

    def list_trace_events(
        self,
        *,
        trace_id: str | None = None,
        linked_run_id: str | None = None,
        linked_job_id: str | None = None,
        limit: int = 50,
    ) -> list[TraceEventRecord]:
        sql = """
        SELECT
            event_id,
            trace_id,
            recorded_at,
            source,
            span_name,
            status,
            payload_path,
            linked_run_id,
            linked_job_id
        FROM trace_events
        """
        clauses: list[str] = []
        params: list[object] = []
        if trace_id is not None:
            clauses.append("trace_id = ?")
            params.append(trace_id)
        if linked_run_id is not None:
            clauses.append("linked_run_id = ?")
            params.append(linked_run_id)
        if linked_job_id is not None:
            clauses.append("linked_job_id = ?")
            params.append(linked_job_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY recorded_at DESC LIMIT ?"
        params.append(limit)
        with self._managed_connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_trace_event_record(row) for row in rows]

    def save_queue_job(self, record: QueueJobRecord, payload: dict[str, object]) -> None:
        sql = """
        INSERT OR REPLACE INTO queue_jobs (
            job_id,
            created_at,
            updated_at,
            job_type,
            status,
            repo_full_name,
            issue_number,
            priority,
            requested_by,
            tenant_id,
            worker_id,
            attempt_count,
            max_attempts,
            budget_units,
            budget_used,
            next_run_at,
            summary,
            receipt_path,
            linked_run_id,
            linked_execution_id,
            linked_verification_id,
            concurrency_key,
            required_worker_tags_json,
            lease_token,
            lease_expires_at,
            rehydration_count,
            cancel_requested,
            error_message,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            record.job_id,
            record.created_at,
            record.updated_at,
            record.job_type.value,
            record.status.value,
            record.repo_full_name,
            record.issue_number,
            record.priority,
            record.requested_by,
            record.tenant_id,
            record.worker_id,
            record.attempt_count,
            record.max_attempts,
            record.budget_units,
            record.budget_used,
            record.next_run_at,
            record.summary,
            str(record.receipt_path),
            record.linked_run_id,
            record.linked_execution_id,
            record.linked_verification_id,
            record.concurrency_key,
            json.dumps(record.required_worker_tags, sort_keys=True),
            record.lease_token,
            record.lease_expires_at,
            record.rehydration_count,
            1 if record.cancel_requested else 0,
            record.error_message,
            json.dumps(payload, indent=2, sort_keys=True),
        )
        with self._managed_connection() as conn:
            conn.execute(sql, values)
            conn.commit()

    def list_queue_jobs(
        self,
        *,
        limit: int = 20,
        status: QueueJobStatus | None = None,
        job_type: QueueJobType | None = None,
        tenant_id: str | None = None,
    ) -> list[QueueJobRecord]:
        sql = """
        SELECT
            job_id,
            created_at,
            updated_at,
            job_type,
            status,
            repo_full_name,
            issue_number,
            priority,
            requested_by,
            tenant_id,
            worker_id,
            attempt_count,
            max_attempts,
            budget_units,
            budget_used,
            next_run_at,
            summary,
            receipt_path,
            linked_run_id,
            linked_execution_id,
            linked_verification_id,
            concurrency_key,
            required_worker_tags_json,
            lease_token,
            lease_expires_at,
            rehydration_count,
            cancel_requested,
            error_message
        FROM queue_jobs
        """
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if job_type is not None:
            clauses.append("job_type = ?")
            params.append(job_type.value)
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY priority DESC, created_at ASC LIMIT ?"
        params.append(limit)
        with self._managed_connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_queue_job_record(row) for row in rows]

    def count_queue_jobs(
        self,
        *,
        statuses: list[QueueJobStatus] | None = None,
        tenant_id: str | None = None,
    ) -> int:
        sql = "SELECT COUNT(*) FROM queue_jobs"
        clauses: list[str] = []
        params: list[object] = []
        if statuses:
            clauses.append("status IN (" + ", ".join("?" for _ in statuses) + ")")
            params.extend(item.value for item in statuses)
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._managed_connection() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
        return int(row[0]) if row is not None else 0

    def get_queue_job(self, job_id: str) -> tuple[QueueJobRecord, dict[str, object]] | None:
        sql = """
        SELECT
            job_id,
            created_at,
            updated_at,
            job_type,
            status,
            repo_full_name,
            issue_number,
            priority,
            requested_by,
            tenant_id,
            worker_id,
            attempt_count,
            max_attempts,
            budget_units,
            budget_used,
            next_run_at,
            summary,
            receipt_path,
            linked_run_id,
            linked_execution_id,
            linked_verification_id,
            concurrency_key,
            required_worker_tags_json,
            lease_token,
            lease_expires_at,
            rehydration_count,
            cancel_requested,
            error_message,
            payload_json
        FROM queue_jobs
        WHERE job_id = ?
        """
        with self._managed_connection() as conn:
            row = conn.execute(sql, (job_id,)).fetchone()
        if row is None:
            return None
        record = self._row_to_queue_job_record(row[:-1])
        payload = json.loads(row[-1])
        if not isinstance(payload, dict):
            raise StorageError(f"Stored payload for queue job {job_id} is not a JSON object.")
        return record, payload

    def claim_next_queue_job(
        self,
        *,
        worker_id: str,
        now: str,
        allowed_types: list[QueueJobType] | None = None,
        worker_tags: list[str] | None = None,
        lease_seconds: int = 900,
        max_running_jobs_per_worker: int = 4,
        max_running_jobs_per_tenant: int = 2,
        candidate_scan_limit: int = 200,
    ) -> tuple[QueueJobRecord, dict[str, object]] | None:
        with self._managed_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._requeue_expired_queue_jobs_conn(conn, now=now)
            if (
                max_running_jobs_per_worker > 0
                and self._count_running_jobs_conn(conn, worker_id=worker_id) >= max_running_jobs_per_worker
            ):
                conn.commit()
                return None
            row, payload = self._select_claimable_queue_job(
                conn,
                now=now,
                allowed_types=allowed_types,
                worker_tags=worker_tags or [],
                max_running_jobs_per_tenant=max_running_jobs_per_tenant,
                candidate_scan_limit=candidate_scan_limit,
            )
            if row is None:
                conn.commit()
                return None
            record = self._row_to_queue_job_record(row)
            lease_token = uuid4().hex
            lease_expires_at = (datetime.fromisoformat(now) + timedelta(seconds=lease_seconds)).isoformat()
            updated_record = QueueJobRecord(
                job_id=record.job_id,
                created_at=record.created_at,
                updated_at=now,
                job_type=record.job_type,
                status=QueueJobStatus.RUNNING,
                repo_full_name=record.repo_full_name,
                issue_number=record.issue_number,
                priority=record.priority,
                requested_by=record.requested_by,
                tenant_id=record.tenant_id,
                worker_id=worker_id,
                attempt_count=record.attempt_count + 1,
                max_attempts=record.max_attempts,
                budget_units=record.budget_units,
                budget_used=record.budget_used,
                next_run_at=record.next_run_at,
                summary=record.summary,
                receipt_path=record.receipt_path,
                linked_run_id=record.linked_run_id,
                linked_execution_id=record.linked_execution_id,
                linked_verification_id=record.linked_verification_id,
                concurrency_key=record.concurrency_key,
                required_worker_tags=record.required_worker_tags,
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
                rehydration_count=record.rehydration_count,
                cancel_requested=record.cancel_requested,
                error_message=None,
            )
            updated_payload = dict(payload)
            updated_payload["status"] = QueueJobStatus.RUNNING.value
            updated_payload["worker_id"] = worker_id
            updated_payload["attempt_count"] = updated_record.attempt_count
            updated_payload["updated_at"] = now
            updated_payload["lease_token"] = lease_token
            updated_payload["lease_expires_at"] = lease_expires_at
            updated_payload["required_worker_tags"] = updated_record.required_worker_tags
            updated_payload["concurrency_key"] = updated_record.concurrency_key
            conn.execute(
                """
                UPDATE queue_jobs
                SET
                    updated_at = ?,
                    status = ?,
                    worker_id = ?,
                    attempt_count = ?,
                    error_message = ?,
                    lease_token = ?,
                    lease_expires_at = ?,
                    payload_json = ?
                WHERE job_id = ?
                """,
                (
                    updated_record.updated_at,
                    updated_record.status.value,
                    updated_record.worker_id,
                    updated_record.attempt_count,
                    updated_record.error_message,
                    updated_record.lease_token,
                    updated_record.lease_expires_at,
                    json.dumps(updated_payload, indent=2, sort_keys=True),
                    updated_record.job_id,
                ),
            )
            conn.commit()
        return updated_record, updated_payload

    def requeue_expired_queue_jobs(self, *, now: str) -> int:
        with self._managed_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reclaimed = self._requeue_expired_queue_jobs_conn(conn, now=now)
            conn.commit()
        return reclaimed

    def count_running_queue_jobs(
        self,
        *,
        tenant_id: str | None = None,
        worker_id: str | None = None,
        concurrency_key: str | None = None,
    ) -> int:
        with self._managed_connection() as conn:
            return self._count_running_jobs_conn(
                conn,
                tenant_id=tenant_id,
                worker_id=worker_id,
                concurrency_key=concurrency_key,
            )

    def _select_claimable_queue_job(
        self,
        conn: sqlite3.Connection,
        *,
        now: str,
        allowed_types: list[QueueJobType] | None,
        worker_tags: list[str],
        max_running_jobs_per_tenant: int,
        candidate_scan_limit: int,
    ) -> tuple[tuple[object, ...] | None, dict[str, object]]:
        sql = """
        SELECT
            job_id,
            created_at,
            updated_at,
            job_type,
            status,
            repo_full_name,
            issue_number,
            priority,
            requested_by,
            tenant_id,
            worker_id,
            attempt_count,
            max_attempts,
            budget_units,
            budget_used,
            next_run_at,
            summary,
            receipt_path,
            linked_run_id,
            linked_execution_id,
            linked_verification_id,
            concurrency_key,
            required_worker_tags_json,
            lease_token,
            lease_expires_at,
            rehydration_count,
            cancel_requested,
            error_message,
            payload_json
        FROM queue_jobs
        WHERE status = ? AND cancel_requested = 0 AND next_run_at <= ?
        """
        params: list[object] = [QueueJobStatus.QUEUED.value, now]
        if allowed_types:
            sql += " AND job_type IN (" + ", ".join("?" for _ in allowed_types) + ")"
            params.extend(item.value for item in allowed_types)
        sql += " ORDER BY priority DESC, created_at ASC LIMIT ?"
        params.append(candidate_scan_limit)
        running_by_tenant = self._running_counts_by_tenant_conn(conn)
        rows = conn.execute(sql, tuple(params)).fetchall()
        selected: tuple[tuple[object, ...], dict[str, object], tuple[object, ...]] | None = None
        normalized_worker_tags = {item.strip() for item in worker_tags if item.strip()}
        for row in rows:
            payload = json.loads(row[-1])
            if not isinstance(payload, dict):
                raise StorageError("Stored queue payload is not a JSON object.")
            record = self._row_to_queue_job_record(row[:-1])
            required_tags = set(record.required_worker_tags)
            if required_tags and not required_tags.issubset(normalized_worker_tags):
                continue
            if record.concurrency_key and self._count_running_jobs_conn(
                conn,
                concurrency_key=record.concurrency_key,
            ) > 0:
                continue
            tenant_running = running_by_tenant.get(record.tenant_id or "", 0)
            if record.tenant_id and max_running_jobs_per_tenant > 0 and tenant_running >= max_running_jobs_per_tenant:
                continue
            fairness_key = (-record.priority, tenant_running, record.created_at)
            if selected is None or fairness_key < selected[2]:
                selected = (row[:-1], payload, fairness_key)
        if selected is None:
            return None, {}
        return selected[0], selected[1]

    def _count_running_jobs_conn(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str | None = None,
        worker_id: str | None = None,
        concurrency_key: str | None = None,
    ) -> int:
        sql = "SELECT COUNT(*) FROM queue_jobs WHERE status = ?"
        params: list[object] = [QueueJobStatus.RUNNING.value]
        if tenant_id is not None:
            sql += " AND tenant_id = ?"
            params.append(tenant_id)
        if worker_id is not None:
            sql += " AND worker_id = ?"
            params.append(worker_id)
        if concurrency_key is not None:
            sql += " AND concurrency_key = ?"
            params.append(concurrency_key)
        row = conn.execute(sql, tuple(params)).fetchone()
        return int(row[0]) if row is not None else 0

    def _running_counts_by_tenant_conn(self, conn: sqlite3.Connection) -> dict[str, int]:
        rows = conn.execute(
            """
            SELECT tenant_id, COUNT(*)
            FROM queue_jobs
            WHERE status = ?
            GROUP BY tenant_id
            """,
            (QueueJobStatus.RUNNING.value,),
        ).fetchall()
        counts: dict[str, int] = {}
        for tenant_id, count in rows:
            counts["" if tenant_id is None else str(tenant_id)] = int(count)
        return counts

    def _requeue_expired_queue_jobs_conn(self, conn: sqlite3.Connection, *, now: str) -> int:
        rows = conn.execute(
            """
            SELECT
                job_id,
                created_at,
                updated_at,
                job_type,
                status,
                repo_full_name,
                issue_number,
                priority,
                requested_by,
                tenant_id,
                worker_id,
                attempt_count,
                max_attempts,
                budget_units,
                budget_used,
                next_run_at,
                summary,
                receipt_path,
                linked_run_id,
                linked_execution_id,
                linked_verification_id,
                concurrency_key,
                required_worker_tags_json,
                lease_token,
                lease_expires_at,
                rehydration_count,
                cancel_requested,
                error_message,
                payload_json
            FROM queue_jobs
            WHERE status = ? AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
            """,
            (QueueJobStatus.RUNNING.value, now),
        ).fetchall()
        reclaimed = 0
        for row in rows:
            record = self._row_to_queue_job_record(row[:-1])
            payload = json.loads(row[-1])
            if not isinstance(payload, dict):
                raise StorageError(f"Stored payload for queue job {record.job_id} is not a JSON object.")
            rehydration_count = record.rehydration_count + 1
            updated_payload = dict(payload)
            updated_payload["status"] = QueueJobStatus.QUEUED.value
            updated_payload["updated_at"] = now
            updated_payload["worker_id"] = None
            updated_payload["lease_token"] = None
            updated_payload["lease_expires_at"] = None
            updated_payload["rehydration_count"] = rehydration_count
            updated_payload["resume_state"] = {
                "last_worker_id": record.worker_id,
                "last_lease_token": record.lease_token,
                "last_lease_expires_at": record.lease_expires_at,
                "reclaimed_at": now,
            }
            conn.execute(
                """
                UPDATE queue_jobs
                SET
                    updated_at = ?,
                    status = ?,
                    worker_id = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    rehydration_count = ?,
                    payload_json = ?
                WHERE job_id = ?
                """,
                (
                    now,
                    QueueJobStatus.QUEUED.value,
                    rehydration_count,
                    json.dumps(updated_payload, indent=2, sort_keys=True),
                    record.job_id,
                ),
            )
            reclaimed += 1
        return reclaimed

    def save_queue_attempt(self, record: QueueAttemptRecord, payload: dict[str, object]) -> None:
        sql = """
        INSERT OR REPLACE INTO queue_attempts (
            attempt_id,
            job_id,
            attempt_index,
            created_at,
            finished_at,
            worker_id,
            status,
            summary,
            payload_path,
            error_message,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            record.attempt_id,
            record.job_id,
            record.attempt_index,
            record.created_at,
            record.finished_at,
            record.worker_id,
            record.status.value,
            record.summary,
            str(record.payload_path),
            record.error_message,
            json.dumps(payload, indent=2, sort_keys=True),
        )
        with self._managed_connection() as conn:
            conn.execute(sql, values)
            conn.commit()

    def list_queue_attempts(self, job_id: str) -> list[QueueAttemptRecord]:
        sql = """
        SELECT
            attempt_id,
            job_id,
            attempt_index,
            created_at,
            finished_at,
            worker_id,
            status,
            summary,
            payload_path,
            error_message
        FROM queue_attempts
        WHERE job_id = ?
        ORDER BY attempt_index ASC
        """
        with self._managed_connection() as conn:
            rows = conn.execute(sql, (job_id,)).fetchall()
        return [self._row_to_queue_attempt_record(row) for row in rows]

    def save_worker_heartbeat(self, record: WorkerHeartbeatRecord, payload: dict[str, object]) -> None:
        sql = """
        INSERT OR REPLACE INTO worker_heartbeats (
            worker_id,
            recorded_at,
            status,
            current_job_id,
            summary,
            processed_jobs,
            succeeded_jobs,
            failed_jobs,
            cancelled_jobs,
            payload_path,
            advertised_worker_tags_json,
            active_lease_token,
            queue_capacity,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            record.worker_id,
            record.recorded_at,
            record.status.value,
            record.current_job_id,
            record.summary,
            record.processed_jobs,
            record.succeeded_jobs,
            record.failed_jobs,
            record.cancelled_jobs,
            str(record.payload_path),
            json.dumps(record.advertised_worker_tags, sort_keys=True),
            record.active_lease_token,
            record.queue_capacity,
            json.dumps(payload, indent=2, sort_keys=True),
        )
        with self._managed_connection() as conn:
            conn.execute(sql, values)
            conn.commit()

    def list_worker_heartbeats(
        self,
        *,
        worker_id: str | None = None,
        limit: int = 20,
    ) -> list[WorkerHeartbeatRecord]:
        sql = """
        SELECT
            worker_id,
            recorded_at,
            status,
            current_job_id,
            summary,
            processed_jobs,
            succeeded_jobs,
            failed_jobs,
            cancelled_jobs,
            payload_path,
            advertised_worker_tags_json,
            active_lease_token,
            queue_capacity
        FROM worker_heartbeats
        """
        params: tuple[object, ...]
        if worker_id is not None:
            sql += " WHERE worker_id = ?"
            params = (worker_id, limit)
        else:
            params = (limit,)
        sql += " ORDER BY recorded_at DESC LIMIT ?"
        with self._managed_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_worker_heartbeat_record(row) for row in rows]

    def prune_notifications_before(self, cutoff: str) -> list[NotificationRecord]:
        with self._managed_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    notification_id,
                    created_at,
                    tenant_id,
                    event_type,
                    status,
                    summary,
                    payload_path
                FROM notifications
                WHERE created_at < ?
                ORDER BY created_at ASC
                """,
                (cutoff,),
            ).fetchall()
            records = [self._row_to_notification_record(row) for row in rows]
            conn.execute("DELETE FROM notifications WHERE created_at < ?", (cutoff,))
            conn.commit()
        return records

    def prune_worker_heartbeats_before(self, cutoff: str) -> list[WorkerHeartbeatRecord]:
        with self._managed_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    worker_id,
                    recorded_at,
                    status,
                    current_job_id,
                    summary,
                    processed_jobs,
                    succeeded_jobs,
                    failed_jobs,
                    cancelled_jobs,
                    payload_path,
                    advertised_worker_tags_json,
                    active_lease_token,
                    queue_capacity
                FROM worker_heartbeats
                WHERE recorded_at < ?
                ORDER BY recorded_at ASC
                """,
                (cutoff,),
            ).fetchall()
            records = [self._row_to_worker_heartbeat_record(row) for row in rows]
            conn.execute("DELETE FROM worker_heartbeats WHERE recorded_at < ?", (cutoff,))
            conn.commit()
        return records

    def prune_alerts_before(self, cutoff: str) -> list[AlertRecord]:
        with self._managed_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    alert_id,
                    created_at,
                    tenant_id,
                    severity,
                    source,
                    status,
                    summary,
                    payload_path
                FROM alerts
                WHERE created_at < ?
                ORDER BY created_at ASC
                """,
                (cutoff,),
            ).fetchall()
            records = [self._row_to_alert_record(row) for row in rows]
            conn.execute("DELETE FROM alerts WHERE created_at < ?", (cutoff,))
            conn.commit()
        return records

    def prune_trace_events_before(self, cutoff: str) -> list[TraceEventRecord]:
        with self._managed_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    event_id,
                    trace_id,
                    recorded_at,
                    source,
                    span_name,
                    status,
                    payload_path,
                    linked_run_id,
                    linked_job_id
                FROM trace_events
                WHERE recorded_at < ?
                ORDER BY recorded_at ASC
                """,
                (cutoff,),
            ).fetchall()
            records = [self._row_to_trace_event_record(row) for row in rows]
            conn.execute("DELETE FROM trace_events WHERE recorded_at < ?", (cutoff,))
            conn.commit()
        return records

    def _initialize(self) -> None:
        schema_migrations_sql = """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
        run_sql = """
        CREATE TABLE IF NOT EXISTS agent_runs (
            run_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            repo_full_name TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            planner_provider TEXT NOT NULL,
            execution_mode TEXT NOT NULL,
            status TEXT NOT NULL,
            branch_name TEXT NOT NULL,
            summary TEXT NOT NULL,
            issue_url TEXT NOT NULL,
            report_path TEXT NOT NULL,
            pr_draft_path TEXT NOT NULL,
            audit_path TEXT NOT NULL,
            error_message TEXT,
            payload_json TEXT NOT NULL
        )
        """
        execution_sql = """
        CREATE TABLE IF NOT EXISTS patch_executions (
            execution_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            proposal_id TEXT NOT NULL,
            linked_run_id TEXT,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            repo_root TEXT NOT NULL,
            receipt_path TEXT NOT NULL,
            error_message TEXT,
            payload_json TEXT NOT NULL
        )
        """
        proposal_sql = """
        CREATE TABLE IF NOT EXISTS patch_proposals (
            proposal_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            linked_run_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            summary TEXT NOT NULL,
            proposal_path TEXT NOT NULL,
            error_message TEXT,
            payload_json TEXT NOT NULL
        )
        """
        autofix_run_sql = """
        CREATE TABLE IF NOT EXISTS autofix_runs (
            autofix_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            linked_run_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            repo_root TEXT NOT NULL,
            max_attempts INTEGER NOT NULL,
            attempt_count INTEGER NOT NULL,
            latest_proposal_id TEXT,
            latest_execution_id TEXT,
            latest_verification_id TEXT,
            receipt_path TEXT NOT NULL,
            error_message TEXT,
            payload_json TEXT NOT NULL
        )
        """
        autofix_attempt_sql = """
        CREATE TABLE IF NOT EXISTS autofix_attempts (
            attempt_id TEXT PRIMARY KEY,
            autofix_id TEXT NOT NULL,
            attempt_index INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            objective TEXT NOT NULL,
            proposal_id TEXT,
            execution_id TEXT,
            verification_id TEXT,
            verification_stop_reason TEXT,
            payload_path TEXT NOT NULL,
            error_message TEXT,
            payload_json TEXT NOT NULL
        )
        """
        sandbox_sql = """
        CREATE TABLE IF NOT EXISTS sandboxes (
            sandbox_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            linked_run_id TEXT,
            linked_autofix_id TEXT,
            status TEXT NOT NULL,
            source_repo_root TEXT NOT NULL,
            workspace_root TEXT NOT NULL,
            copied_file_count INTEGER NOT NULL,
            skipped_entry_count INTEGER NOT NULL,
            total_bytes INTEGER NOT NULL,
            summary TEXT NOT NULL,
            receipt_path TEXT NOT NULL,
            error_message TEXT,
            payload_json TEXT NOT NULL
        )
        """
        verification_sql = """
        CREATE TABLE IF NOT EXISTS verifications (
            verification_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            linked_run_id TEXT,
            linked_execution_id TEXT,
            status TEXT NOT NULL,
            stop_reason TEXT NOT NULL,
            summary TEXT NOT NULL,
            repo_root TEXT NOT NULL,
            receipt_path TEXT NOT NULL,
            error_message TEXT,
            payload_json TEXT NOT NULL
        )
        """
        delivery_sql = """
        CREATE TABLE IF NOT EXISTS deliveries (
            delivery_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            linked_run_id TEXT NOT NULL,
            linked_execution_id TEXT NOT NULL,
            linked_verification_id TEXT NOT NULL,
            status TEXT NOT NULL,
            repo_full_name TEXT NOT NULL,
            branch_name TEXT NOT NULL,
            base_branch TEXT NOT NULL,
            summary TEXT NOT NULL,
            receipt_path TEXT NOT NULL,
            error_message TEXT,
            payload_json TEXT NOT NULL
        )
        """
        approval_sql = """
        CREATE TABLE IF NOT EXISTS approvals (
            approval_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            action TEXT NOT NULL,
            linked_run_id TEXT NOT NULL,
            linked_execution_id TEXT NOT NULL,
            linked_verification_id TEXT NOT NULL,
            repo_full_name TEXT NOT NULL,
            status TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            requester_team TEXT NOT NULL,
            required_approvals INTEGER NOT NULL,
            approved_count INTEGER NOT NULL,
            summary TEXT NOT NULL,
            receipt_path TEXT NOT NULL,
            error_message TEXT,
            payload_json TEXT NOT NULL
        )
        """
        tenant_sql = """
        CREATE TABLE IF NOT EXISTS tenants (
            tenant_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            config_path TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
        tenant_membership_sql = """
        CREATE TABLE IF NOT EXISTS tenant_memberships (
            tenant_id TEXT NOT NULL,
            actor TEXT NOT NULL,
            role TEXT NOT NULL,
            team TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (tenant_id, actor)
        )
        """
        notification_sql = """
        CREATE TABLE IF NOT EXISTS notifications (
            notification_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            payload_path TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
        alert_sql = """
        CREATE TABLE IF NOT EXISTS alerts (
            alert_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            tenant_id TEXT,
            severity TEXT NOT NULL,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            payload_path TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
        trace_event_sql = """
        CREATE TABLE IF NOT EXISTS trace_events (
            event_id TEXT PRIMARY KEY,
            trace_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            source TEXT NOT NULL,
            span_name TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_path TEXT NOT NULL,
            linked_run_id TEXT,
            linked_job_id TEXT,
            payload_json TEXT NOT NULL
        )
        """
        queue_job_sql = """
        CREATE TABLE IF NOT EXISTS queue_jobs (
            job_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            job_type TEXT NOT NULL,
            status TEXT NOT NULL,
            repo_full_name TEXT NOT NULL,
            issue_number INTEGER,
            priority INTEGER NOT NULL,
            requested_by TEXT,
            tenant_id TEXT,
            worker_id TEXT,
            attempt_count INTEGER NOT NULL,
            max_attempts INTEGER NOT NULL,
            budget_units INTEGER NOT NULL,
            budget_used INTEGER NOT NULL,
            next_run_at TEXT NOT NULL,
            summary TEXT NOT NULL,
            receipt_path TEXT NOT NULL,
            linked_run_id TEXT,
            linked_execution_id TEXT,
            linked_verification_id TEXT,
            concurrency_key TEXT,
            required_worker_tags_json TEXT NOT NULL DEFAULT '[]',
            lease_token TEXT,
            lease_expires_at TEXT,
            rehydration_count INTEGER NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL,
            error_message TEXT,
            payload_json TEXT NOT NULL
        )
        """
        queue_attempt_sql = """
        CREATE TABLE IF NOT EXISTS queue_attempts (
            attempt_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            attempt_index INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            worker_id TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            payload_path TEXT NOT NULL,
            error_message TEXT,
            payload_json TEXT NOT NULL
        )
        """
        worker_heartbeat_sql = """
        CREATE TABLE IF NOT EXISTS worker_heartbeats (
            worker_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            status TEXT NOT NULL,
            current_job_id TEXT,
            summary TEXT NOT NULL,
            processed_jobs INTEGER NOT NULL,
            succeeded_jobs INTEGER NOT NULL,
            failed_jobs INTEGER NOT NULL,
            cancelled_jobs INTEGER NOT NULL,
            payload_path TEXT NOT NULL,
            advertised_worker_tags_json TEXT NOT NULL DEFAULT '[]',
            active_lease_token TEXT,
            queue_capacity INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (worker_id, recorded_at)
        )
        """
        with self._managed_connection() as conn:
            conn.execute(schema_migrations_sql)
            conn.execute(run_sql)
            conn.execute(execution_sql)
            conn.execute(proposal_sql)
            conn.execute(autofix_run_sql)
            conn.execute(autofix_attempt_sql)
            conn.execute(sandbox_sql)
            conn.execute(verification_sql)
            conn.execute(delivery_sql)
            conn.execute(approval_sql)
            conn.execute(tenant_sql)
            conn.execute(tenant_membership_sql)
            conn.execute(notification_sql)
            conn.execute(alert_sql)
            conn.execute(trace_event_sql)
            conn.execute(queue_job_sql)
            conn.execute(queue_attempt_sql)
            conn.execute(worker_heartbeat_sql)
            self._ensure_column(
                conn,
                table_name="queue_jobs",
                column_name="concurrency_key",
                column_sql="TEXT",
            )
            self._ensure_column(
                conn,
                table_name="queue_jobs",
                column_name="required_worker_tags_json",
                column_sql="TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column(
                conn,
                table_name="queue_jobs",
                column_name="lease_token",
                column_sql="TEXT",
            )
            self._ensure_column(
                conn,
                table_name="queue_jobs",
                column_name="lease_expires_at",
                column_sql="TEXT",
            )
            self._ensure_column(
                conn,
                table_name="queue_jobs",
                column_name="rehydration_count",
                column_sql="INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                conn,
                table_name="worker_heartbeats",
                column_name="advertised_worker_tags_json",
                column_sql="TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column(
                conn,
                table_name="worker_heartbeats",
                column_name="active_lease_token",
                column_sql="TEXT",
            )
            self._ensure_column(
                conn,
                table_name="worker_heartbeats",
                column_name="queue_capacity",
                column_sql="INTEGER NOT NULL DEFAULT 0",
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_runs_repo_issue ON agent_runs(repo_full_name, issue_number)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_runs_created_at ON agent_runs(created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_patch_executions_created_at ON patch_executions(created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_patch_executions_run_id ON patch_executions(linked_run_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_patch_proposals_run_id ON patch_proposals(linked_run_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_patch_proposals_created_at ON patch_proposals(created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_autofix_runs_updated_at ON autofix_runs(updated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_autofix_runs_run_id ON autofix_runs(linked_run_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_autofix_attempts_run ON autofix_attempts(autofix_id, attempt_index ASC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sandboxes_updated_at ON sandboxes(updated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sandboxes_run_id ON sandboxes(linked_run_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_verifications_created_at ON verifications(created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_verifications_run_id ON verifications(linked_run_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_deliveries_created_at ON deliveries(created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_deliveries_run_id ON deliveries(linked_run_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_approvals_updated_at ON approvals(updated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_approvals_run_id ON approvals(linked_run_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tenants_updated_at ON tenants(updated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tenant_memberships_tenant ON tenant_memberships(tenant_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notifications_tenant_id ON notifications(tenant_id, created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON alerts(created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_alerts_tenant_status ON alerts(tenant_id, status, created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trace_events_trace_id ON trace_events(trace_id, recorded_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trace_events_run_id ON trace_events(linked_run_id, recorded_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trace_events_job_id ON trace_events(linked_job_id, recorded_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_queue_jobs_status_next_run ON queue_jobs(status, next_run_at ASC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_queue_jobs_tenant_status ON queue_jobs(tenant_id, status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_queue_jobs_type_status ON queue_jobs(job_type, status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_queue_jobs_lease_expires ON queue_jobs(status, lease_expires_at ASC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_queue_jobs_concurrency ON queue_jobs(concurrency_key, status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_queue_attempts_job ON queue_attempts(job_id, attempt_index ASC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_worker_heartbeats_recorded_at ON worker_heartbeats(recorded_at DESC)"
            )
            self._record_schema_migrations(conn)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        try:
            return sqlite3.connect(self._db_path)
        except sqlite3.Error as exc:
            raise StorageError(f"Unable to open run database at {self._db_path}: {exc}") from exc

    @contextmanager
    def _managed_connection(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        *,
        table_name: str,
        column_name: str,
        column_sql: str,
    ) -> None:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {str(row[1]) for row in rows}
        if column_name in existing:
            return
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")

    def _record_schema_migrations(self, conn: sqlite3.Connection) -> None:
        existing_rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        existing = {int(row[0]) for row in existing_rows}
        applied_at = datetime.now(timezone.utc).isoformat()
        for version, name in _SCHEMA_MIGRATIONS:
            if version in existing:
                continue
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, applied_at),
            )

    def _row_to_record(self, row: tuple[object, ...]) -> RunRecord:
        return RunRecord(
            run_id=str(row[0]),
            created_at=str(row[1]),
            repo_full_name=str(row[2]),
            issue_number=int(row[3]),
            planner_provider=PlannerProvider(str(row[4])),
            execution_mode=ExecutionMode(str(row[5])),
            status=RunStatus(str(row[6])),
            branch_name=str(row[7]),
            summary=str(row[8]),
            issue_url=str(row[9]),
            report_path=Path(str(row[10])),
            pr_draft_path=Path(str(row[11])),
            audit_path=Path(str(row[12])),
            error_message=None if row[13] is None else str(row[13]),
        )

    def _row_to_execution_record(self, row: tuple[object, ...]) -> PatchExecutionRecord:
        return PatchExecutionRecord(
            execution_id=str(row[0]),
            created_at=str(row[1]),
            proposal_id=str(row[2]),
            linked_run_id=None if row[3] is None else str(row[3]),
            mode=PatchExecutionMode(str(row[4])),
            status=PatchExecutionStatus(str(row[5])),
            summary=str(row[6]),
            repo_root=Path(str(row[7])),
            receipt_path=Path(str(row[8])),
            error_message=None if row[9] is None else str(row[9]),
        )

    def _row_to_patch_proposal_record(self, row: tuple[object, ...]) -> PatchProposalRecord:
        return PatchProposalRecord(
            proposal_id=str(row[0]),
            created_at=str(row[1]),
            linked_run_id=str(row[2]),
            provider=PatcherProvider(str(row[3])),
            summary=str(row[4]),
            proposal_path=Path(str(row[5])),
            error_message=None if row[6] is None else str(row[6]),
        )

    def _row_to_autofix_run_record(self, row: tuple[object, ...]) -> AutofixRunRecord:
        return AutofixRunRecord(
            autofix_id=str(row[0]),
            created_at=str(row[1]),
            updated_at=str(row[2]),
            linked_run_id=str(row[3]),
            provider=PatcherProvider(str(row[4])),
            status=AutofixStatus(str(row[5])),
            summary=str(row[6]),
            repo_root=Path(str(row[7])),
            max_attempts=int(row[8]),
            attempt_count=int(row[9]),
            latest_proposal_id=None if row[10] is None else str(row[10]),
            latest_execution_id=None if row[11] is None else str(row[11]),
            latest_verification_id=None if row[12] is None else str(row[12]),
            receipt_path=Path(str(row[13])),
            error_message=None if row[14] is None else str(row[14]),
        )

    def _row_to_autofix_attempt_record(self, row: tuple[object, ...]) -> AutofixAttemptRecord:
        return AutofixAttemptRecord(
            attempt_id=str(row[0]),
            autofix_id=str(row[1]),
            attempt_index=int(row[2]),
            created_at=str(row[3]),
            status=AutofixAttemptStatus(str(row[4])),
            summary=str(row[5]),
            objective=str(row[6]),
            proposal_id=None if row[7] is None else str(row[7]),
            execution_id=None if row[8] is None else str(row[8]),
            verification_id=None if row[9] is None else str(row[9]),
            verification_stop_reason=None if row[10] is None else VerificationStopReason(str(row[10])),
            payload_path=Path(str(row[11])),
            error_message=None if row[12] is None else str(row[12]),
        )

    def _row_to_sandbox_record(self, row: tuple[object, ...]) -> SandboxRecord:
        return SandboxRecord(
            sandbox_id=str(row[0]),
            created_at=str(row[1]),
            updated_at=str(row[2]),
            linked_run_id=None if row[3] is None else str(row[3]),
            linked_autofix_id=None if row[4] is None else str(row[4]),
            status=SandboxStatus(str(row[5])),
            source_repo_root=Path(str(row[6])),
            workspace_root=Path(str(row[7])),
            copied_file_count=int(row[8]),
            skipped_entry_count=int(row[9]),
            total_bytes=int(row[10]),
            summary=str(row[11]),
            receipt_path=Path(str(row[12])),
            error_message=None if row[13] is None else str(row[13]),
        )

    def _row_to_verification_record(self, row: tuple[object, ...]) -> VerificationRecord:
        return VerificationRecord(
            verification_id=str(row[0]),
            created_at=str(row[1]),
            linked_run_id=None if row[2] is None else str(row[2]),
            linked_execution_id=None if row[3] is None else str(row[3]),
            status=VerificationStatus(str(row[4])),
            stop_reason=VerificationStopReason(str(row[5])),
            summary=str(row[6]),
            repo_root=Path(str(row[7])),
            receipt_path=Path(str(row[8])),
            error_message=None if row[9] is None else str(row[9]),
        )

    def _row_to_delivery_record(self, row: tuple[object, ...]) -> DeliveryRecord:
        return DeliveryRecord(
            delivery_id=str(row[0]),
            created_at=str(row[1]),
            linked_run_id=str(row[2]),
            linked_execution_id=str(row[3]),
            linked_verification_id=str(row[4]),
            status=DeliveryStatus(str(row[5])),
            repo_full_name=str(row[6]),
            branch_name=str(row[7]),
            base_branch=str(row[8]),
            summary=str(row[9]),
            receipt_path=Path(str(row[10])),
            error_message=None if row[11] is None else str(row[11]),
        )

    def _row_to_approval_record(self, row: tuple[object, ...]) -> ApprovalRecord:
        return ApprovalRecord(
            approval_id=str(row[0]),
            created_at=str(row[1]),
            updated_at=str(row[2]),
            action=ApprovalAction(str(row[3])),
            linked_run_id=str(row[4]),
            linked_execution_id=str(row[5]),
            linked_verification_id=str(row[6]),
            repo_full_name=str(row[7]),
            status=ApprovalStatus(str(row[8])),
            risk_level=ApprovalRiskLevel(str(row[9])),
            requested_by=str(row[10]),
            requester_team=str(row[11]),
            required_approvals=int(row[12]),
            approved_count=int(row[13]),
            summary=str(row[14]),
            receipt_path=Path(str(row[15])),
            error_message=None if row[16] is None else str(row[16]),
        )

    def _row_to_tenant_record(self, row: tuple[object, ...]) -> TenantRecord:
        return TenantRecord(
            tenant_id=str(row[0]),
            created_at=str(row[1]),
            updated_at=str(row[2]),
            name=str(row[3]),
            status=TenantStatus(str(row[4])),
            summary=str(row[5]),
            config_path=Path(str(row[6])),
        )

    def _row_to_tenant_membership_record(self, row: tuple[object, ...]) -> TenantMembershipRecord:
        return TenantMembershipRecord(
            tenant_id=str(row[0]),
            actor=str(row[1]),
            role=TenantRole(str(row[2])),
            team=str(row[3]),
            created_at=str(row[4]),
            updated_at=str(row[5]),
        )

    def _row_to_notification_record(self, row: tuple[object, ...]) -> NotificationRecord:
        return NotificationRecord(
            notification_id=str(row[0]),
            created_at=str(row[1]),
            tenant_id=str(row[2]),
            event_type=NotificationEventType(str(row[3])),
            status=NotificationStatus(str(row[4])),
            summary=str(row[5]),
            payload_path=Path(str(row[6])),
        )

    def _row_to_alert_record(self, row: tuple[object, ...]) -> AlertRecord:
        return AlertRecord(
            alert_id=str(row[0]),
            created_at=str(row[1]),
            tenant_id=None if row[2] is None else str(row[2]),
            severity=AlertSeverity(str(row[3])),
            source=str(row[4]),
            status=AlertStatus(str(row[5])),
            summary=str(row[6]),
            payload_path=Path(str(row[7])),
        )

    def _row_to_trace_event_record(self, row: tuple[object, ...]) -> TraceEventRecord:
        return TraceEventRecord(
            event_id=str(row[0]),
            trace_id=str(row[1]),
            recorded_at=str(row[2]),
            source=str(row[3]),
            span_name=str(row[4]),
            status=str(row[5]),
            payload_path=Path(str(row[6])),
            linked_run_id=None if row[7] is None else str(row[7]),
            linked_job_id=None if row[8] is None else str(row[8]),
        )

    def _row_to_queue_job_record(self, row: tuple[object, ...]) -> QueueJobRecord:
        issue_number = None if row[6] is None else int(row[6])
        raw_tags = row[22]
        required_tags = json.loads(raw_tags) if isinstance(raw_tags, str) else []
        if not isinstance(required_tags, list):
            required_tags = []
        return QueueJobRecord(
            job_id=str(row[0]),
            created_at=str(row[1]),
            updated_at=str(row[2]),
            job_type=QueueJobType(str(row[3])),
            status=QueueJobStatus(str(row[4])),
            repo_full_name=str(row[5]),
            issue_number=issue_number,
            priority=int(row[7]),
            requested_by=None if row[8] is None else str(row[8]),
            tenant_id=None if row[9] is None else str(row[9]),
            worker_id=None if row[10] is None else str(row[10]),
            attempt_count=int(row[11]),
            max_attempts=int(row[12]),
            budget_units=int(row[13]),
            budget_used=int(row[14]),
            next_run_at=str(row[15]),
            summary=str(row[16]),
            receipt_path=Path(str(row[17])),
            linked_run_id=None if row[18] is None else str(row[18]),
            linked_execution_id=None if row[19] is None else str(row[19]),
            linked_verification_id=None if row[20] is None else str(row[20]),
            concurrency_key=None if row[21] is None else str(row[21]),
            required_worker_tags=[
                str(item) for item in required_tags if isinstance(item, str) and item.strip()
            ],
            lease_token=None if row[23] is None else str(row[23]),
            lease_expires_at=None if row[24] is None else str(row[24]),
            rehydration_count=int(row[25]),
            cancel_requested=bool(int(row[26])),
            error_message=None if row[27] is None else str(row[27]),
        )

    def _row_to_queue_attempt_record(self, row: tuple[object, ...]) -> QueueAttemptRecord:
        return QueueAttemptRecord(
            attempt_id=str(row[0]),
            job_id=str(row[1]),
            attempt_index=int(row[2]),
            created_at=str(row[3]),
            finished_at=None if row[4] is None else str(row[4]),
            worker_id=str(row[5]),
            status=QueueAttemptStatus(str(row[6])),
            summary=str(row[7]),
            payload_path=Path(str(row[8])),
            error_message=None if row[9] is None else str(row[9]),
        )

    def _row_to_worker_heartbeat_record(self, row: tuple[object, ...]) -> WorkerHeartbeatRecord:
        raw_tags = row[10]
        advertised_tags = json.loads(raw_tags) if isinstance(raw_tags, str) else []
        if not isinstance(advertised_tags, list):
            advertised_tags = []
        return WorkerHeartbeatRecord(
            worker_id=str(row[0]),
            recorded_at=str(row[1]),
            status=WorkerStatus(str(row[2])),
            current_job_id=None if row[3] is None else str(row[3]),
            summary=str(row[4]),
            processed_jobs=int(row[5]),
            succeeded_jobs=int(row[6]),
            failed_jobs=int(row[7]),
            cancelled_jobs=int(row[8]),
            payload_path=Path(str(row[9])),
            advertised_worker_tags=[
                str(item) for item in advertised_tags if isinstance(item, str) and item.strip()
            ],
            active_lease_token=None if row[11] is None else str(row[11]),
            queue_capacity=int(row[12]),
        )
