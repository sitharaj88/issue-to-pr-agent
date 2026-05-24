from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Callable
from uuid import uuid4

from ...agents.planner.base import PlannerClient
from ...agents.planner.heuristic import HeuristicPlanner
from ...application.services.approval_policy import ApprovalPolicyEvaluator
from ...application.services.delivery_governance import DeliveryGovernancePolicyEvaluator
from ...application.services.queue_budget import QueueBudgetManager
from ...application.services.tenant_access import TenantAccessController
from ...application.use_cases.deliver_run import DeliverRunUseCase
from ...application.use_cases.plan_issue_to_pr import IssueToPRAgent
from ...application.use_cases.verify_run import VerifyRunUseCase
from ...domain.entities import (
    AlertSeverity,
    DeliveryStatus,
    IssueContext,
    NotificationEventType,
    PlatformPermission,
    QueueAttemptRecord,
    QueueAttemptStatus,
    QueueJobRecord,
    QueueJobStatus,
    QueueJobType,
    WorkerHeartbeatRecord,
    WorkerStatus,
    )
from ...domain.policies.safety import SafetyPolicy
from ...infrastructure.config.settings import Settings
from ...infrastructure.notifications import FileNotificationOutbox
from ...infrastructure.persistence.run_repository import RunRepository
from ...infrastructure.verification import build_command_runner
from ...integrations.github.client import GitHubClient
from ...integrations.openai.planner import OpenAIPlanner
from ...observability.metrics import QueueMetricsReporter
from ...observability.tracing import TraceRecorder
from ...observability.alerts import AlertManager
from ...shared.exceptions import PolicyError


@dataclass(frozen=True)
class WorkerRunResult:
    worker_id: str
    processed_jobs: int
    succeeded_jobs: int
    failed_jobs: int
    cancelled_jobs: int
    heartbeat_path: Path
    metrics_json_path: Path
    metrics_prom_path: Path


@dataclass(frozen=True)
class _DispatchResult:
    summary: str
    linked_run_id: str | None = None
    linked_execution_id: str | None = None
    linked_verification_id: str | None = None
    payload: dict[str, object] | None = None


class ProcessQueueUseCase:
    def __init__(
        self,
        repository: RunRepository,
        settings: Settings,
        access_controller: TenantAccessController,
        budget_manager: QueueBudgetManager,
        metrics_reporter: QueueMetricsReporter,
        *,
        github_client: GitHubClient | None = None,
        planner_overrides: dict[str, PlannerClient] | None = None,
        notification_outbox: FileNotificationOutbox | None = None,
        trace_recorder: TraceRecorder | None = None,
        alert_manager: AlertManager | None = None,
        logger: logging.Logger | None = None,
        shutdown_requested: Callable[[], bool] | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._access_controller = access_controller
        self._budget_manager = budget_manager
        self._metrics_reporter = metrics_reporter
        self._github_client = github_client or GitHubClient(settings)
        self._planner_overrides = planner_overrides or {}
        self._notification_outbox = notification_outbox
        self._trace_recorder = trace_recorder
        self._alert_manager = alert_manager
        self._logger = logger or logging.getLogger(__name__)
        self._shutdown_requested = shutdown_requested or (lambda: False)

    def process(
        self,
        *,
        worker_id: str,
        max_jobs: int = 1,
        allowed_types: list[QueueJobType] | None = None,
        worker_tags: list[str] | None = None,
    ) -> WorkerRunResult:
        processed_jobs = 0
        succeeded_jobs = 0
        failed_jobs = 0
        cancelled_jobs = 0
        normalized_worker_tags = sorted({item.strip() for item in (worker_tags or []) if item.strip()})
        trace_id = uuid4().hex

        heartbeat_path = self._emit_heartbeat(
            worker_id=worker_id,
            status=WorkerStatus.IDLE,
            current_job_id=None,
            summary="Worker started.",
            processed_jobs=processed_jobs,
            succeeded_jobs=succeeded_jobs,
            failed_jobs=failed_jobs,
            cancelled_jobs=cancelled_jobs,
            worker_tags=normalized_worker_tags,
            active_lease_token=None,
        )
        self._trace(
            trace_id=trace_id,
            span_name="worker.run.started",
            status="started",
            payload={"worker_id": worker_id, "allowed_types": [item.value for item in allowed_types or []]},
            linked_job_id=None,
            linked_run_id=None,
        )

        for _ in range(max(max_jobs, 0)):
            if self._shutdown_requested():
                self._logger.info("Shutdown requested, stopping queue processing.")
                break
            now = datetime.now(timezone.utc).isoformat()
            self._repository.requeue_expired_queue_jobs(now=now)

            claimed = self._repository.claim_next_queue_job(
                worker_id=worker_id,
                now=now,
                allowed_types=allowed_types,
                worker_tags=normalized_worker_tags,
                lease_seconds=self._settings.queue_lease_seconds,
                max_running_jobs_per_worker=self._settings.queue_max_running_jobs_per_worker,
                max_running_jobs_per_tenant=self._settings.queue_max_running_jobs_per_tenant,
                candidate_scan_limit=self._settings.queue_candidate_scan_limit,
            )
            if claimed is None:
                break
            job_record, job_payload = claimed
            heartbeat_path = self._emit_heartbeat(
                worker_id=worker_id,
                status=WorkerStatus.RUNNING,
                current_job_id=job_record.job_id,
                summary=f"Processing {job_record.job_type.value} job {job_record.job_id}.",
                processed_jobs=processed_jobs,
                succeeded_jobs=succeeded_jobs,
                failed_jobs=failed_jobs,
                cancelled_jobs=cancelled_jobs,
                worker_tags=normalized_worker_tags,
                active_lease_token=job_record.lease_token,
            )
            status = self._process_job(
                worker_id=worker_id,
                job_record=job_record,
                job_payload=job_payload,
                trace_id=trace_id,
            )
            processed_jobs += 1
            if status == QueueJobStatus.SUCCEEDED:
                succeeded_jobs += 1
            elif status == QueueJobStatus.CANCELLED:
                cancelled_jobs += 1
            else:
                failed_jobs += 1

        heartbeat_path = self._emit_heartbeat(
            worker_id=worker_id,
            status=WorkerStatus.STOPPED,
            current_job_id=None,
            summary=f"Worker stopped after processing {processed_jobs} job(s).",
            processed_jobs=processed_jobs,
            succeeded_jobs=succeeded_jobs,
            failed_jobs=failed_jobs,
            cancelled_jobs=cancelled_jobs,
            worker_tags=normalized_worker_tags,
            active_lease_token=None,
        )
        metrics_json_path, metrics_prom_path = self._metrics_reporter.write_snapshot(self._settings.metrics_dir)
        if self._alert_manager is not None:
            snapshot = self._metrics_reporter.build_snapshot()
            self._alert_manager.evaluate_queue_snapshot(
                snapshot,
                output_dir=self._settings.telemetry_dir / "alerts",
            )
        self._trace(
            trace_id=trace_id,
            span_name="worker.run.completed",
            status="completed",
            payload={
                "worker_id": worker_id,
                "processed_jobs": processed_jobs,
                "succeeded_jobs": succeeded_jobs,
                "failed_jobs": failed_jobs,
                "cancelled_jobs": cancelled_jobs,
            },
            linked_job_id=None,
            linked_run_id=None,
        )
        return WorkerRunResult(
            worker_id=worker_id,
            processed_jobs=processed_jobs,
            succeeded_jobs=succeeded_jobs,
            failed_jobs=failed_jobs,
            cancelled_jobs=cancelled_jobs,
            heartbeat_path=heartbeat_path,
            metrics_json_path=metrics_json_path,
            metrics_prom_path=metrics_prom_path,
        )

    def _process_job(
        self,
        *,
        worker_id: str,
        job_record: QueueJobRecord,
        job_payload: dict[str, object],
        trace_id: str,
    ) -> QueueJobStatus:
        parameters = _dict_value(job_payload.get("parameters"))
        attempt_started_at = datetime.now(timezone.utc).isoformat()
        attempt_path = job_record.receipt_path.parent / job_record.job_id / f"attempt-{job_record.attempt_count:02d}.json"
        attempt_path.parent.mkdir(parents=True, exist_ok=True)
        team = _string_value(parameters.get("team"))
        tenant_context: tuple[object, dict[str, object]] | None = None
        charge_units = 0
        self._trace(
            trace_id=trace_id,
            span_name="queue.job.started",
            status="started",
            payload={"job_type": job_record.job_type.value, "worker_id": worker_id},
            linked_job_id=job_record.job_id,
            linked_run_id=job_record.linked_run_id,
        )

        try:
            required_permission = (
                PlatformPermission.DELIVER
                if job_record.job_type == QueueJobType.DELIVER
                else PlatformPermission.OPERATE_QUEUE
            )
            tenant_context = self._access_controller.require_repo_permission(
                repo_full_name=job_record.repo_full_name,
                actor=job_record.requested_by,
                permission=required_permission,
                team=team,
            )
            latest_job = self._repository.get_queue_job(job_record.job_id)
            if latest_job is not None and latest_job[0].cancel_requested:
                return self._finalize_cancelled(
                    worker_id=worker_id,
                    job_record=latest_job[0],
                    job_payload=latest_job[1],
                    summary="Queue job was cancelled before execution began.",
                    attempt_path=attempt_path,
                    attempt_started_at=attempt_started_at,
                )

            planner_provider = _string_value(parameters.get("provider"))
            charge_units = self._budget_manager.ensure_attempt_within_budget(
                budget_units=job_record.budget_units,
                budget_used=job_record.budget_used,
                job_type=job_record.job_type,
                planner_provider=planner_provider,
            )
            dispatch_result = self._dispatch_job(
                job_record=job_record,
                parameters=parameters,
                tenant_context=tenant_context,
            )
            status = QueueJobStatus.SUCCEEDED
            summary = dispatch_result.summary
            error_message = None
            next_run_at = job_record.next_run_at
            budget_used = job_record.budget_used + charge_units
            self._emit_delivery_notification(
                tenant_context=tenant_context,
                job_type=job_record.job_type,
                success=True,
                payload={
                    "run_id": dispatch_result.linked_run_id or job_record.linked_run_id,
                    **(dispatch_result.payload or {}),
                },
                summary=f"Queue job {job_record.job_id} succeeded for {job_record.repo_full_name}.",
            )
        except Exception as exc:
            latest_job = self._repository.get_queue_job(job_record.job_id)
            latest_record = latest_job[0] if latest_job is not None else job_record
            latest_payload = latest_job[1] if latest_job is not None else job_payload
            if latest_record.cancel_requested:
                return self._finalize_cancelled(
                    worker_id=worker_id,
                    job_record=latest_record,
                    job_payload=latest_payload,
                    summary=f"Queue job cancelled after failure: {exc}",
                    attempt_path=attempt_path,
                    attempt_started_at=attempt_started_at,
                )
            should_retry = latest_record.attempt_count < latest_record.max_attempts
            status = QueueJobStatus.QUEUED if should_retry else QueueJobStatus.FAILED
            summary = (
                f"Retry scheduled after attempt {latest_record.attempt_count}."
                if should_retry
                else f"Queue job failed after {latest_record.attempt_count} attempt(s)."
            )
            error_message = str(exc)
            next_run_at = (
                self._budget_manager.next_retry_at(attempt_count=latest_record.attempt_count)
                if should_retry
                else latest_record.next_run_at
            )
            budget_used = latest_record.budget_used + charge_units
            self._logger.exception(
                "Queue job processing failed",
                extra={
                    "provider": "queue_worker",
                    "run_id": latest_record.linked_run_id,
                    "repo_full_name": latest_record.repo_full_name,
                },
            )
            self._emit_delivery_notification(
                tenant_context=tenant_context,
                job_type=job_record.job_type,
                success=False,
                payload={
                    "run_id": latest_record.linked_run_id,
                    "error_message": error_message,
                    "job_id": job_record.job_id,
                },
                summary=f"Queue job {job_record.job_id} failed for {job_record.repo_full_name}.",
            )
            dispatch_result = _DispatchResult(summary=summary, payload={"error_message": error_message})

        updated_at = datetime.now(timezone.utc).isoformat()
        finished_payload = dict(job_payload)
        finished_payload.update(
            {
                "updated_at": updated_at,
                "status": status.value,
                "summary": summary,
                "worker_id": worker_id,
                "budget_used": budget_used,
                "next_run_at": next_run_at,
                "error_message": error_message,
            }
        )
        if dispatch_result.linked_run_id is not None:
            finished_payload["linked_run_id"] = dispatch_result.linked_run_id
        if dispatch_result.linked_execution_id is not None:
            finished_payload["linked_execution_id"] = dispatch_result.linked_execution_id
        if dispatch_result.linked_verification_id is not None:
            finished_payload["linked_verification_id"] = dispatch_result.linked_verification_id
        if dispatch_result.payload:
            finished_payload["result"] = dispatch_result.payload
        updated_record = QueueJobRecord(
            job_id=job_record.job_id,
            created_at=job_record.created_at,
            updated_at=updated_at,
            job_type=job_record.job_type,
            status=status,
            repo_full_name=job_record.repo_full_name,
            issue_number=job_record.issue_number,
            priority=job_record.priority,
            requested_by=job_record.requested_by,
            tenant_id=job_record.tenant_id,
            worker_id=worker_id,
            attempt_count=job_record.attempt_count,
            max_attempts=job_record.max_attempts,
            budget_units=job_record.budget_units,
            budget_used=budget_used,
            next_run_at=next_run_at,
            summary=summary,
            receipt_path=job_record.receipt_path,
            linked_run_id=dispatch_result.linked_run_id or job_record.linked_run_id,
            linked_execution_id=dispatch_result.linked_execution_id or job_record.linked_execution_id,
            linked_verification_id=dispatch_result.linked_verification_id or job_record.linked_verification_id,
            concurrency_key=job_record.concurrency_key,
            required_worker_tags=job_record.required_worker_tags,
            lease_token=None,
            lease_expires_at=None,
            rehydration_count=job_record.rehydration_count,
            cancel_requested=False if status == QueueJobStatus.SUCCEEDED else job_record.cancel_requested,
            error_message=error_message,
        )
        finished_payload["lease_token"] = None
        finished_payload["lease_expires_at"] = None
        self._persist_job(record=updated_record, payload=finished_payload)
        self._persist_attempt(
            worker_id=worker_id,
            job_record=updated_record,
            started_at=attempt_started_at,
            finished_at=updated_at,
            attempt_path=attempt_path,
            status=(
                QueueAttemptStatus.SUCCEEDED
                if status == QueueJobStatus.SUCCEEDED
                else QueueAttemptStatus.FAILED
            ),
            summary=summary,
            payload=finished_payload,
            error_message=error_message,
        )
        self._trace(
            trace_id=trace_id,
            span_name="queue.job.completed",
            status=status.value,
            payload={
                "job_type": job_record.job_type.value,
                "worker_id": worker_id,
                "summary": summary,
                "error_message": error_message,
            },
            linked_job_id=job_record.job_id,
            linked_run_id=updated_record.linked_run_id,
        )
        if (
            status == QueueJobStatus.FAILED
            and self._alert_manager is not None
        ):
            self._alert_manager.emit(
                tenant_id=updated_record.tenant_id,
                severity=AlertSeverity.ERROR,
                source="queue_worker",
                summary=f"Queue job {job_record.job_id} failed for {job_record.repo_full_name}.",
                payload={
                    "job_id": job_record.job_id,
                    "job_type": job_record.job_type.value,
                    "repo_full_name": job_record.repo_full_name,
                    "run_id": updated_record.linked_run_id,
                    "error_message": error_message,
                },
                output_dir=self._settings.telemetry_dir / "alerts",
            )
        return status

    def _dispatch_job(
        self,
        *,
        job_record: QueueJobRecord,
        parameters: dict[str, object],
        tenant_context: tuple[object, dict[str, object]] | None,
    ) -> _DispatchResult:
        if job_record.job_type == QueueJobType.PLAN:
            repo_root = Path(_required_string(parameters, "repo_root")).resolve()
            provider = _required_string(parameters, "provider")
            output_dir = Path(_required_string(parameters, "output_dir")).resolve()
            planner = self._resolve_planner(provider)
            agent = IssueToPRAgent(
                self._github_client,
                planner,
                self._repository,
                SafetyPolicy(branch_prefix=self._settings.branch_prefix),
                max_repo_files=self._settings.max_repo_files,
            )
            external_issue = _dict_value(parameters.get("external_issue"))
            if external_issue:
                external_key = _required_string(external_issue, "key")
                issue = IssueContext(
                    repo_full_name=job_record.repo_full_name,
                    issue_number=_external_issue_number(
                        external_key,
                        fallback=int(job_record.issue_number or 0),
                    ),
                    title=_string_value(external_issue.get("title")) or external_key,
                    body=_string_value(external_issue.get("body")),
                    labels=_string_list(external_issue.get("labels")),
                    url=_string_value(external_issue.get("url")) or external_key,
                )
                external_ticket = {
                    "system": _string_value(external_issue.get("system")) or "external",
                    "key": external_key,
                    "title": issue.title,
                    "url": issue.url,
                }
                result = agent.run_with_issue(
                    issue=issue,
                    repo_root=repo_root,
                    output_dir=output_dir,
                    objective=_string_value(parameters.get("objective")) or None,
                    create_branch=bool(parameters.get("create_branch", False)),
                    external_ticket=external_ticket,
                )
            else:
                result = agent.run(
                    repo_full_name=job_record.repo_full_name,
                    issue_number=int(job_record.issue_number or 0),
                    repo_root=repo_root,
                    output_dir=output_dir,
                    objective=_string_value(parameters.get("objective")) or None,
                    create_branch=bool(parameters.get("create_branch", False)),
                )
            return _DispatchResult(
                summary=result.plan.summary,
                linked_run_id=result.run_id,
                payload={
                    "run_id": result.run_id,
                    "report_path": str(result.report_path),
                    "pr_draft_path": str(result.pr_draft_path),
                    "audit_path": str(result.audit_path),
                },
            )

        if job_record.job_type == QueueJobType.VERIFY:
            verifier = VerifyRunUseCase(
                self._repository,
                SafetyPolicy(branch_prefix=self._settings.branch_prefix),
                command_runner=build_command_runner(self._settings),
            )
            result = verifier.verify(
                repo_root=Path(_required_string(parameters, "repo_root")).resolve(),
                artifact_dir=Path(_required_string(parameters, "output_dir")).resolve(),
                run_id=job_record.linked_run_id,
                execution_id=job_record.linked_execution_id,
                max_attempts=int(parameters.get("verify_max_attempts", 3)),
                timeout_seconds=int(parameters.get("timeout_seconds", 120)),
            )
            return _DispatchResult(
                summary=result.receipt.summary,
                linked_run_id=result.receipt.linked_run_id,
                linked_execution_id=result.receipt.linked_execution_id,
                linked_verification_id=result.verification_id,
                payload={
                    "verification_id": result.verification_id,
                    "receipt_path": str(result.receipt_path),
                    "status": result.receipt.status.value,
                },
            )

        if job_record.job_type == QueueJobType.DELIVER:
            self._settings.require_github_token()
            approval_policy = ApprovalPolicyEvaluator(
                self._settings.approval_policy_path,
                policy_overrides=_tenant_policy_overrides(tenant_context),
            )
            delivery_governance_policy = DeliveryGovernancePolicyEvaluator(
                self._settings.delivery_governance_policy_path,
                policy_overrides=_tenant_policy_overrides(tenant_context),
            )
            deliverer = DeliverRunUseCase(
                self._repository,
                self._github_client,
                SafetyPolicy(branch_prefix=self._settings.branch_prefix),
                approval_policy=approval_policy,
                delivery_governance_policy=delivery_governance_policy,
            )
            result = deliverer.deliver(
                run_id=_required_string_from_optional(job_record.linked_run_id, "linked_run_id"),
                execution_id=_required_string_from_optional(job_record.linked_execution_id, "linked_execution_id"),
                verification_id=_required_string_from_optional(
                    job_record.linked_verification_id,
                    "linked_verification_id",
                ),
                approval_id=_string_value(parameters.get("approval_id")) or None,
                repo_root=Path(_required_string(parameters, "repo_root")).resolve(),
                artifact_dir=self._settings.artifact_dir,
                artifact_base_url=self._settings.artifact_base_url,
                artifact_store_backend=self._settings.artifact_store_backend,
                artifact_store_dir=self._settings.artifact_store_dir,
                artifact_store_base_url=self._settings.artifact_store_base_url,
                remote_name=self._settings.git_remote_name,
                base_branch=_string_value(parameters.get("base_branch")) or None,
                rollout_stage=_string_value(parameters.get("rollout_stage")) or None,
                commit_message=_string_value(parameters.get("commit_message")) or None,
                pr_title=_string_value(parameters.get("pr_title")) or None,
                publish_pr_comment=bool(parameters.get("publish_pr_comment", True)),
            )
            if result.receipt.status != DeliveryStatus.SUCCEEDED:
                raise RuntimeError(result.receipt.error_message or "Delivery failed.")
            return _DispatchResult(
                summary=result.receipt.summary,
                linked_run_id=result.receipt.linked_run_id,
                linked_execution_id=result.receipt.linked_execution_id,
                linked_verification_id=result.receipt.linked_verification_id,
                payload={
                    "delivery_id": result.delivery_id,
                    "receipt_path": str(result.receipt_path),
                    "status": result.receipt.status.value,
                    "pr_url": result.receipt.pr.html_url if result.receipt.pr is not None else None,
                },
            )

        raise ValueError(f"Unsupported queue job type: {job_record.job_type.value}")

    def _resolve_planner(self, provider: str) -> PlannerClient:
        override = self._planner_overrides.get(provider)
        if override is not None:
            return override
        if provider == "heuristic":
            return HeuristicPlanner()
        if provider == "openai":
            return OpenAIPlanner(self._settings)
        raise ValueError(f"Unsupported planner provider: {provider}")

    def _persist_job(self, *, record: QueueJobRecord, payload: dict[str, object]) -> None:
        record.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        record.receipt_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._repository.save_queue_job(record, payload)

    def _persist_attempt(
        self,
        *,
        worker_id: str,
        job_record: QueueJobRecord,
        started_at: str,
        finished_at: str,
        attempt_path: Path,
        status: QueueAttemptStatus,
        summary: str,
        payload: dict[str, object],
        error_message: str | None,
    ) -> None:
        attempt_payload = {
            "attempt_id": uuid4().hex[:12],
            "job_id": job_record.job_id,
            "attempt_index": job_record.attempt_count,
            "created_at": started_at,
            "finished_at": finished_at,
            "worker_id": worker_id,
            "status": status.value,
            "summary": summary,
            "error_message": error_message,
            "job_status": job_record.status.value,
            "job_payload": payload,
        }
        attempt_path.write_text(json.dumps(attempt_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._repository.save_queue_attempt(
            QueueAttemptRecord(
                attempt_id=attempt_payload["attempt_id"],
                job_id=job_record.job_id,
                attempt_index=job_record.attempt_count,
                created_at=started_at,
                finished_at=finished_at,
                worker_id=worker_id,
                status=status,
                summary=summary,
                payload_path=attempt_path,
                error_message=error_message,
            ),
            attempt_payload,
        )

    def _finalize_cancelled(
        self,
        *,
        worker_id: str,
        job_record: QueueJobRecord,
        job_payload: dict[str, object],
        summary: str,
        attempt_path: Path | None = None,
        attempt_started_at: str | None = None,
    ) -> QueueJobStatus:
        updated_at = datetime.now(timezone.utc).isoformat()
        payload = dict(job_payload)
        payload.update(
            {
                "updated_at": updated_at,
                "status": QueueJobStatus.CANCELLED.value,
                "summary": summary,
                "cancel_requested": True,
                "error_message": None,
            }
        )
        updated_record = QueueJobRecord(
            job_id=job_record.job_id,
            created_at=job_record.created_at,
            updated_at=updated_at,
            job_type=job_record.job_type,
            status=QueueJobStatus.CANCELLED,
            repo_full_name=job_record.repo_full_name,
            issue_number=job_record.issue_number,
            priority=job_record.priority,
            requested_by=job_record.requested_by,
            tenant_id=job_record.tenant_id,
            worker_id=worker_id,
            attempt_count=job_record.attempt_count,
            max_attempts=job_record.max_attempts,
            budget_units=job_record.budget_units,
            budget_used=job_record.budget_used,
            next_run_at=job_record.next_run_at,
            summary=summary,
            receipt_path=job_record.receipt_path,
            linked_run_id=job_record.linked_run_id,
            linked_execution_id=job_record.linked_execution_id,
            linked_verification_id=job_record.linked_verification_id,
            concurrency_key=job_record.concurrency_key,
            required_worker_tags=job_record.required_worker_tags,
            lease_token=None,
            lease_expires_at=None,
            rehydration_count=job_record.rehydration_count,
            cancel_requested=True,
            error_message=None,
        )
        payload["lease_token"] = None
        payload["lease_expires_at"] = None
        self._persist_job(record=updated_record, payload=payload)
        if attempt_path is not None and attempt_started_at is not None:
            self._persist_attempt(
                worker_id=worker_id,
                job_record=updated_record,
                started_at=attempt_started_at,
                finished_at=updated_at,
                attempt_path=attempt_path,
                status=QueueAttemptStatus.CANCELLED,
                summary=summary,
                payload=payload,
                error_message=None,
            )
        return QueueJobStatus.CANCELLED

    def _emit_heartbeat(
        self,
        *,
        worker_id: str,
        status: WorkerStatus,
        current_job_id: str | None,
        summary: str,
        processed_jobs: int,
        succeeded_jobs: int,
        failed_jobs: int,
        cancelled_jobs: int,
        worker_tags: list[str],
        active_lease_token: str | None,
    ) -> Path:
        recorded_at = datetime.now(timezone.utc).isoformat()
        worker_dir = self._settings.metrics_dir / "workers" / worker_id
        worker_dir.mkdir(parents=True, exist_ok=True)
        safe_timestamp = recorded_at.replace(":", "-")
        payload_path = worker_dir / f"{safe_timestamp}.json"
        payload = {
            "worker_id": worker_id,
            "recorded_at": recorded_at,
            "status": status.value,
            "current_job_id": current_job_id,
            "summary": summary,
            "processed_jobs": processed_jobs,
            "succeeded_jobs": succeeded_jobs,
            "failed_jobs": failed_jobs,
            "cancelled_jobs": cancelled_jobs,
            "advertised_worker_tags": worker_tags,
            "active_lease_token": active_lease_token,
            "queue_capacity": self._settings.queue_max_running_jobs_per_worker,
            "payload_path": str(payload_path),
        }
        payload_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._repository.save_worker_heartbeat(
            WorkerHeartbeatRecord(
                worker_id=worker_id,
                recorded_at=recorded_at,
                status=status,
                current_job_id=current_job_id,
                summary=summary,
                processed_jobs=processed_jobs,
                succeeded_jobs=succeeded_jobs,
                failed_jobs=failed_jobs,
                cancelled_jobs=cancelled_jobs,
                payload_path=payload_path,
                advertised_worker_tags=worker_tags,
                active_lease_token=active_lease_token,
                queue_capacity=self._settings.queue_max_running_jobs_per_worker,
            ),
            payload,
        )
        return payload_path

    def _trace(
        self,
        *,
        trace_id: str,
        span_name: str,
        status: str,
        payload: dict[str, object],
        linked_job_id: str | None,
        linked_run_id: str | None,
    ) -> None:
        if self._trace_recorder is None:
            return
        self._trace_recorder.record(
            trace_id=trace_id,
            source="queue_worker",
            span_name=span_name,
            status=status,
            payload=payload,
            linked_job_id=linked_job_id,
            linked_run_id=linked_run_id,
            output_dir=self._settings.telemetry_dir / "traces",
        )

    def _emit_delivery_notification(
        self,
        *,
        tenant_context: tuple[object, dict[str, object]] | None,
        job_type: QueueJobType,
        success: bool,
        payload: dict[str, object],
        summary: str,
    ) -> None:
        if self._notification_outbox is None or tenant_context is None or job_type != QueueJobType.DELIVER:
            return
        tenant_record, _ = tenant_context
        self._notification_outbox.emit(
            tenant_id=tenant_record.tenant_id,
            event_type=(
                NotificationEventType.DELIVERY_SUCCEEDED
                if success
                else NotificationEventType.DELIVERY_BLOCKED
            ),
            summary=summary,
            payload=payload,
            output_dir=self._settings.notification_dir,
        )


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _string_value(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _required_string(data: dict[str, object], key: str) -> str:
    value = _string_value(data.get(key))
    if not value:
        raise ValueError(f"Queue job parameter is required: {key}")
    return value


def _required_string_from_optional(value: str | None, field_name: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"Queue job is missing required field: {field_name}")
    return value


def _tenant_policy_overrides(tenant_context: tuple[object, dict[str, object]] | None) -> dict[str, object] | None:
    if tenant_context is None:
        return None
    _, tenant_payload = tenant_context
    overrides = tenant_payload.get("policy_overrides")
    return overrides if isinstance(overrides, dict) else None


def _external_issue_number(external_key: str, *, fallback: int) -> int:
    suffix = external_key.rsplit("-", 1)[-1].strip()
    if suffix.isdigit():
        return int(suffix)
    return fallback
