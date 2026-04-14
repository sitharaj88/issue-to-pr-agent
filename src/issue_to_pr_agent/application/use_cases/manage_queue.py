from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from ...application.services.queue_budget import QueueBudgetManager
from ...application.services.tenant_access import TenantAccessController
from ...domain.entities import (
    PlatformPermission,
    QueueJobRecord,
    QueueJobStatus,
    QueueJobType,
)
from ...infrastructure.config.settings import Settings
from ...infrastructure.persistence.run_repository import RunRepository
from ...shared.exceptions import PolicyError


@dataclass(frozen=True)
class QueueJobResult:
    job_id: str
    status: QueueJobStatus
    receipt_path: Path


class ManageQueueUseCase:
    def __init__(
        self,
        repository: RunRepository,
        settings: Settings,
        access_controller: TenantAccessController,
        budget_manager: QueueBudgetManager,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._access_controller = access_controller
        self._budget_manager = budget_manager

    def enqueue_plan(
        self,
        *,
        repo_full_name: str,
        issue_number: int,
        repo_root: Path,
        provider: str,
        actor: str,
        team: str,
        objective: str | None = None,
        create_branch: bool = False,
        priority: int = 0,
        max_attempts: int | None = None,
        budget_units: int | None = None,
        output_dir: Path | None = None,
        required_worker_tags: list[str] | None = None,
        concurrency_key: str | None = None,
    ) -> QueueJobResult:
        tenant_context = self._access_controller.require_repo_permission(
            repo_full_name=repo_full_name,
            actor=actor,
            permission=PlatformPermission.OPERATE_QUEUE,
            team=team,
        )
        resolved_attempts = max_attempts or self._settings.queue_max_attempts
        resolved_budget = budget_units or self._budget_manager.default_budget_units(
            job_type=QueueJobType.PLAN,
            max_attempts=resolved_attempts,
            planner_provider=provider,
        )
        tenant_id = tenant_context[0].tenant_id if tenant_context is not None else None
        self._budget_manager.ensure_can_enqueue(tenant_id=tenant_id, budget_units=resolved_budget)
        summary = f"Queued plan for {repo_full_name}#{issue_number}"
        return self._create_job(
            job_type=QueueJobType.PLAN,
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            requested_by=actor,
            tenant_id=tenant_id,
            priority=priority,
            max_attempts=resolved_attempts,
            budget_units=resolved_budget,
            summary=summary,
            parameters={
                "repo_root": str(repo_root.resolve()),
                "provider": provider,
                "objective": objective or "",
                "create_branch": create_branch,
                "output_dir": str((output_dir or self._settings.artifact_dir).resolve()),
                "team": team,
                "required_worker_tags": _clean_worker_tags(required_worker_tags),
                "concurrency_key": concurrency_key or "",
            },
            required_worker_tags=required_worker_tags,
            concurrency_key=concurrency_key,
        )

    def enqueue_external_plan(
        self,
        *,
        repo_full_name: str,
        external_key: str,
        external_title: str,
        external_body: str,
        external_labels: list[str],
        external_url: str,
        source_system: str,
        repo_root: Path,
        provider: str,
        actor: str,
        team: str,
        objective: str | None = None,
        create_branch: bool = False,
        priority: int = 0,
        max_attempts: int | None = None,
        budget_units: int | None = None,
        output_dir: Path | None = None,
        required_worker_tags: list[str] | None = None,
        concurrency_key: str | None = None,
    ) -> QueueJobResult:
        tenant_context = self._access_controller.require_repo_permission(
            repo_full_name=repo_full_name,
            actor=actor,
            permission=PlatformPermission.OPERATE_QUEUE,
            team=team,
        )
        resolved_attempts = max_attempts or self._settings.queue_max_attempts
        resolved_budget = budget_units or self._budget_manager.default_budget_units(
            job_type=QueueJobType.PLAN,
            max_attempts=resolved_attempts,
            planner_provider=provider,
        )
        tenant_id = tenant_context[0].tenant_id if tenant_context is not None else None
        self._budget_manager.ensure_can_enqueue(tenant_id=tenant_id, budget_units=resolved_budget)
        summary = f"Queued {source_system} plan for {repo_full_name} {external_key}"
        return self._create_job(
            job_type=QueueJobType.PLAN,
            repo_full_name=repo_full_name,
            issue_number=_parse_external_issue_number(external_key),
            requested_by=actor,
            tenant_id=tenant_id,
            priority=priority,
            max_attempts=resolved_attempts,
            budget_units=resolved_budget,
            summary=summary,
            parameters={
                "repo_root": str(repo_root.resolve()),
                "provider": provider,
                "objective": objective or "",
                "create_branch": create_branch,
                "output_dir": str((output_dir or self._settings.artifact_dir).resolve()),
                "team": team,
                "required_worker_tags": _clean_worker_tags(required_worker_tags),
                "concurrency_key": concurrency_key or "",
                "external_issue": {
                    "system": source_system,
                    "key": external_key,
                    "title": external_title,
                    "body": external_body,
                    "labels": [item.strip() for item in external_labels if item.strip()],
                    "url": external_url,
                },
            },
            required_worker_tags=required_worker_tags,
            concurrency_key=concurrency_key,
        )

    def enqueue_verify(
        self,
        *,
        run_id: str | None,
        execution_id: str | None,
        repo_root: Path,
        actor: str,
        team: str,
        priority: int = 0,
        max_attempts: int | None = None,
        budget_units: int | None = None,
        verify_max_attempts: int = 3,
        timeout_seconds: int = 120,
        output_dir: Path | None = None,
        required_worker_tags: list[str] | None = None,
        concurrency_key: str | None = None,
    ) -> QueueJobResult:
        run_record = self._resolve_run_record(run_id=run_id, execution_id=execution_id)
        tenant_context = self._access_controller.require_repo_permission(
            repo_full_name=run_record.repo_full_name,
            actor=actor,
            permission=PlatformPermission.DELIVER,
            team=team,
        )
        resolved_attempts = max_attempts or self._settings.queue_max_attempts
        resolved_budget = budget_units or self._budget_manager.default_budget_units(
            job_type=QueueJobType.VERIFY,
            max_attempts=resolved_attempts,
        )
        tenant_id = tenant_context[0].tenant_id if tenant_context is not None else None
        self._budget_manager.ensure_can_enqueue(tenant_id=tenant_id, budget_units=resolved_budget)
        summary = f"Queued verification for {run_record.repo_full_name}"
        return self._create_job(
            job_type=QueueJobType.VERIFY,
            repo_full_name=run_record.repo_full_name,
            issue_number=run_record.issue_number,
            requested_by=actor,
            tenant_id=tenant_id,
            priority=priority,
            max_attempts=resolved_attempts,
            budget_units=resolved_budget,
            summary=summary,
            linked_run_id=run_id,
            linked_execution_id=execution_id,
            parameters={
                "repo_root": str(repo_root.resolve()),
                "output_dir": str((output_dir or self._settings.artifact_dir).resolve()),
                "verify_max_attempts": verify_max_attempts,
                "timeout_seconds": timeout_seconds,
                "team": team,
                "required_worker_tags": _clean_worker_tags(required_worker_tags),
                "concurrency_key": concurrency_key or "",
            },
            required_worker_tags=required_worker_tags,
            concurrency_key=concurrency_key,
        )

    def enqueue_deliver(
        self,
        *,
        run_id: str,
        execution_id: str,
        verification_id: str,
        approval_id: str | None,
        actor: str,
        team: str,
        repo_root: Path,
        priority: int = 0,
        max_attempts: int | None = None,
        budget_units: int | None = None,
        base_branch: str | None = None,
        rollout_stage: str | None = None,
        commit_message: str | None = None,
        pr_title: str | None = None,
        publish_pr_comment: bool = True,
        required_worker_tags: list[str] | None = None,
        concurrency_key: str | None = None,
    ) -> QueueJobResult:
        run = self._repository.get_run(run_id)
        if run is None:
            raise ValueError(f"Run not found: {run_id}")
        run_record, _ = run
        tenant_context = self._access_controller.require_repo_permission(
            repo_full_name=run_record.repo_full_name,
            actor=actor,
            permission=PlatformPermission.OPERATE_QUEUE,
            team=team,
        )
        resolved_attempts = max_attempts or 1
        resolved_budget = budget_units or self._budget_manager.default_budget_units(
            job_type=QueueJobType.DELIVER,
            max_attempts=resolved_attempts,
        )
        tenant_id = tenant_context[0].tenant_id if tenant_context is not None else None
        self._budget_manager.ensure_can_enqueue(tenant_id=tenant_id, budget_units=resolved_budget)
        summary = f"Queued delivery for {run_record.repo_full_name}"
        return self._create_job(
            job_type=QueueJobType.DELIVER,
            repo_full_name=run_record.repo_full_name,
            issue_number=run_record.issue_number,
            requested_by=actor,
            tenant_id=tenant_id,
            priority=priority,
            max_attempts=resolved_attempts,
            budget_units=resolved_budget,
            summary=summary,
            linked_run_id=run_id,
            linked_execution_id=execution_id,
            linked_verification_id=verification_id,
            parameters={
                "approval_id": approval_id,
                "repo_root": str(repo_root.resolve()),
                "base_branch": base_branch,
                "rollout_stage": rollout_stage,
                "commit_message": commit_message,
                "pr_title": pr_title,
                "publish_pr_comment": publish_pr_comment,
                "team": team,
                "required_worker_tags": _clean_worker_tags(required_worker_tags),
                "concurrency_key": concurrency_key or "",
            },
            required_worker_tags=required_worker_tags,
            concurrency_key=concurrency_key,
        )

    def cancel_job(self, *, job_id: str, actor: str, team: str) -> QueueJobResult:
        record, payload = self._load_mutable_job(job_id=job_id, actor=actor, team=team)
        if record.status in {QueueJobStatus.SUCCEEDED, QueueJobStatus.CANCELLED}:
            raise PolicyError(f"Queue job {job_id} is already in a final state.")
        updated_at = datetime.now(timezone.utc).isoformat()
        if record.status == QueueJobStatus.QUEUED:
            status = QueueJobStatus.CANCELLED
            summary = f"{record.summary} (cancelled by {actor})"
        else:
            status = record.status
            summary = f"{record.summary} (cancel requested by {actor})"
        updated = QueueJobRecord(
            job_id=record.job_id,
            created_at=record.created_at,
            updated_at=updated_at,
            job_type=record.job_type,
            status=status,
            repo_full_name=record.repo_full_name,
            issue_number=record.issue_number,
            priority=record.priority,
            requested_by=record.requested_by,
            tenant_id=record.tenant_id,
            worker_id=record.worker_id,
            attempt_count=record.attempt_count,
            max_attempts=record.max_attempts,
            budget_units=record.budget_units,
            budget_used=record.budget_used,
            next_run_at=record.next_run_at,
            summary=summary,
            receipt_path=record.receipt_path,
            linked_run_id=record.linked_run_id,
            linked_execution_id=record.linked_execution_id,
            linked_verification_id=record.linked_verification_id,
            concurrency_key=record.concurrency_key,
            required_worker_tags=record.required_worker_tags,
            lease_token=record.lease_token,
            lease_expires_at=record.lease_expires_at,
            rehydration_count=record.rehydration_count,
            cancel_requested=True,
            error_message=record.error_message,
        )
        updated_payload = dict(payload)
        updated_payload["updated_at"] = updated_at
        updated_payload["status"] = updated.status.value
        updated_payload["summary"] = updated.summary
        updated_payload["cancel_requested"] = True
        updated_payload["lease_token"] = updated.lease_token
        updated_payload["lease_expires_at"] = updated.lease_expires_at
        self._repository.save_queue_job(updated, updated_payload)
        return QueueJobResult(job_id=job_id, status=updated.status, receipt_path=updated.receipt_path)

    def resume_job(
        self,
        *,
        job_id: str,
        actor: str,
        team: str,
        reset_attempts: bool = False,
    ) -> QueueJobResult:
        record, payload = self._load_mutable_job(job_id=job_id, actor=actor, team=team)
        if record.status not in {QueueJobStatus.FAILED, QueueJobStatus.CANCELLED}:
            raise PolicyError(f"Queue job {job_id} is not resumable from status {record.status.value}.")
        if not reset_attempts and record.attempt_count >= record.max_attempts:
            raise PolicyError("Queue job has exhausted its attempts. Use reset_attempts to resume it.")
        updated_at = datetime.now(timezone.utc).isoformat()
        updated = QueueJobRecord(
            job_id=record.job_id,
            created_at=record.created_at,
            updated_at=updated_at,
            job_type=record.job_type,
            status=QueueJobStatus.QUEUED,
            repo_full_name=record.repo_full_name,
            issue_number=record.issue_number,
            priority=record.priority,
            requested_by=record.requested_by,
            tenant_id=record.tenant_id,
            worker_id=None,
            attempt_count=0 if reset_attempts else record.attempt_count,
            max_attempts=record.max_attempts,
            budget_units=record.budget_units,
            budget_used=0 if reset_attempts else record.budget_used,
            next_run_at=updated_at,
            summary=record.summary,
            receipt_path=record.receipt_path,
            linked_run_id=record.linked_run_id,
            linked_execution_id=record.linked_execution_id,
            linked_verification_id=record.linked_verification_id,
            concurrency_key=record.concurrency_key,
            required_worker_tags=record.required_worker_tags,
            lease_token=None,
            lease_expires_at=None,
            rehydration_count=record.rehydration_count,
            cancel_requested=False,
            error_message=None,
        )
        updated_payload = dict(payload)
        updated_payload["updated_at"] = updated_at
        updated_payload["status"] = updated.status.value
        updated_payload["attempt_count"] = updated.attempt_count
        updated_payload["budget_used"] = updated.budget_used
        updated_payload["worker_id"] = None
        updated_payload["next_run_at"] = updated.next_run_at
        updated_payload["cancel_requested"] = False
        updated_payload["error_message"] = None
        updated_payload["lease_token"] = None
        updated_payload["lease_expires_at"] = None
        self._repository.save_queue_job(updated, updated_payload)
        return QueueJobResult(job_id=job_id, status=updated.status, receipt_path=updated.receipt_path)

    def _create_job(
        self,
        *,
        job_type: QueueJobType,
        repo_full_name: str,
        issue_number: int | None,
        requested_by: str,
        tenant_id: str | None,
        priority: int,
        max_attempts: int,
        budget_units: int,
        summary: str,
        parameters: dict[str, object],
        linked_run_id: str | None = None,
        linked_execution_id: str | None = None,
        linked_verification_id: str | None = None,
        required_worker_tags: list[str] | None = None,
        concurrency_key: str | None = None,
    ) -> QueueJobResult:
        created_at = datetime.now(timezone.utc).isoformat()
        job_id = uuid4().hex[:12]
        receipt_path = self._settings.artifact_dir / "queue" / "jobs" / f"{job_id}.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        record = QueueJobRecord(
            job_id=job_id,
            created_at=created_at,
            updated_at=created_at,
            job_type=job_type,
            status=QueueJobStatus.QUEUED,
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            priority=priority,
            requested_by=requested_by,
            tenant_id=tenant_id,
            worker_id=None,
            attempt_count=0,
            max_attempts=max_attempts,
            budget_units=budget_units,
            budget_used=0,
            next_run_at=created_at,
            summary=summary,
            receipt_path=receipt_path,
            linked_run_id=linked_run_id,
            linked_execution_id=linked_execution_id,
            linked_verification_id=linked_verification_id,
            concurrency_key=(concurrency_key.strip() if isinstance(concurrency_key, str) and concurrency_key.strip() else None),
            required_worker_tags=_clean_worker_tags(required_worker_tags),
            lease_token=None,
            lease_expires_at=None,
            rehydration_count=0,
            cancel_requested=False,
            error_message=None,
        )
        payload = {
            "job_id": job_id,
            "created_at": created_at,
            "updated_at": created_at,
            "job_type": job_type.value,
            "status": QueueJobStatus.QUEUED.value,
            "repo_full_name": repo_full_name,
            "issue_number": issue_number,
            "priority": priority,
            "requested_by": requested_by,
            "tenant_id": tenant_id,
            "worker_id": None,
            "attempt_count": 0,
            "max_attempts": max_attempts,
            "budget_units": budget_units,
            "budget_used": 0,
            "next_run_at": created_at,
            "summary": summary,
            "receipt_path": str(receipt_path),
            "linked_run_id": linked_run_id,
            "linked_execution_id": linked_execution_id,
            "linked_verification_id": linked_verification_id,
            "concurrency_key": record.concurrency_key,
            "required_worker_tags": record.required_worker_tags,
            "lease_token": None,
            "lease_expires_at": None,
            "rehydration_count": 0,
            "cancel_requested": False,
            "error_message": None,
            "parameters": parameters,
        }
        receipt_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._repository.save_queue_job(record, payload)
        return QueueJobResult(job_id=job_id, status=record.status, receipt_path=receipt_path)

    def _load_mutable_job(
        self,
        *,
        job_id: str,
        actor: str,
        team: str,
    ) -> tuple[QueueJobRecord, dict[str, object]]:
        job = self._repository.get_queue_job(job_id)
        if job is None:
            raise ValueError(f"Queue job not found: {job_id}")
        record, payload = job
        self._access_controller.require_repo_permission(
            repo_full_name=record.repo_full_name,
            actor=actor,
            permission=PlatformPermission.OPERATE_QUEUE,
            team=team,
        )
        return record, payload

    def _resolve_run_record(self, *, run_id: str | None, execution_id: str | None):
        if run_id:
            run = self._repository.get_run(run_id)
            if run is None:
                raise ValueError(f"Run not found: {run_id}")
            return run[0]
        if execution_id:
            execution = self._repository.get_execution(execution_id)
            if execution is None:
                raise ValueError(f"Execution not found: {execution_id}")
            execution_record, _ = execution
            if not execution_record.linked_run_id:
                raise ValueError(f"Execution {execution_id} is not linked to a planning run.")
            run = self._repository.get_run(execution_record.linked_run_id)
            if run is None:
                raise ValueError(f"Run not found: {execution_record.linked_run_id}")
            return run[0]
        raise ValueError("Either run_id or execution_id is required.")


def _parse_external_issue_number(external_key: str) -> int:
    suffix = external_key.rsplit("-", 1)[-1].strip()
    if suffix.isdigit():
        return int(suffix)
    return 0


def _clean_worker_tags(tags: list[str] | None) -> list[str]:
    if not tags:
        return []
    return sorted({item.strip() for item in tags if item.strip()})
