from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from uuid import uuid4

from ...application.services.approval_policy import ApprovalPolicyEvaluator
from ...domain.entities import (
    ApprovalAction,
    ApprovalDecision,
    ApprovalReceipt,
    ApprovalRecord,
    ApprovalReviewerDecision,
    ApprovalRiskLevel,
    ApprovalStatus,
    PatchExecutionStatus,
    RunStatus,
    VerificationStatus,
    VerificationStopReason,
)
from ...infrastructure.persistence.run_repository import RunRepository
from ...shared.exceptions import ApprovalError


@dataclass(frozen=True)
class ApprovalWorkflowResult:
    approval_id: str
    receipt: ApprovalReceipt
    receipt_path: Path


class RequestApprovalUseCase:
    def __init__(
        self,
        run_repository: RunRepository,
        approval_policy: ApprovalPolicyEvaluator,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._run_repository = run_repository
        self._approval_policy = approval_policy
        self._logger = logger or logging.getLogger(__name__)

    def request_delivery_approval(
        self,
        *,
        run_id: str,
        execution_id: str,
        verification_id: str,
        actor: str,
        team: str,
        comment: str = "",
        expires_in_hours: int | None = None,
        assigned_reviewers: list[str] | None = None,
        assigned_reviewer_teams: list[str] | None = None,
    ) -> ApprovalWorkflowResult:
        approval_id = uuid4().hex[:12]
        created_at = datetime.now(timezone.utc).isoformat()
        updated_at = created_at
        run_record, run_payload, execution_record, execution_payload, verification_record, verification_payload = (
            _load_delivery_context(
                self._run_repository,
                run_id=run_id,
                execution_id=execution_id,
                verification_id=verification_id,
            )
        )
        receipt_path = run_record.audit_path.parent / "approvals" / f"{approval_id}.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)

        self._logger.info(
            "Creating delivery approval request",
            extra={
                "run_id": run_id,
                "execution_id": execution_id,
                "verification_id": verification_id,
                "actor": actor,
                "team": team,
                "provider": "approval_workflow",
            },
        )
        evaluation = self._approval_policy.evaluate_delivery(
            repo_full_name=run_record.repo_full_name,
            run_payload=run_payload,
            execution_payload=execution_payload,
            verification_payload=verification_payload,
            requester_team=team,
        )

        if evaluation.blocked_reasons:
            status = ApprovalStatus.REJECTED
            error_message = "; ".join(evaluation.blocked_reasons)
        elif evaluation.approval_required:
            status = ApprovalStatus.PENDING
            error_message = None
        else:
            status = ApprovalStatus.APPROVED
            error_message = None

        summary = evaluation.summary
        resolved_assigned_reviewers = _clean_string_list(assigned_reviewers)
        resolved_assigned_reviewer_teams = _clean_string_list(assigned_reviewer_teams)
        if not resolved_assigned_reviewer_teams:
            resolved_assigned_reviewer_teams = list(evaluation.required_reviewer_teams)
        expires_at = _resolve_expires_at(created_at, expires_in_hours) if status == ApprovalStatus.PENDING else None
        receipt = ApprovalReceipt(
            approval_id=approval_id,
            action=ApprovalAction.DELIVERY,
            linked_run_id=run_id,
            linked_execution_id=execution_id,
            linked_verification_id=verification_id,
            repo_full_name=run_record.repo_full_name,
            status=status,
            risk_level=evaluation.risk_level,
            requested_by=actor,
            requester_team=team,
            request_comment=comment,
            required_approvals=evaluation.required_approvals,
            approved_count=0,
            expires_at=expires_at,
            required_reviewer_teams=evaluation.required_reviewer_teams,
            assigned_reviewers=resolved_assigned_reviewers,
            assigned_reviewer_teams=resolved_assigned_reviewer_teams,
            reasons=evaluation.reasons,
            blocked_reasons=evaluation.blocked_reasons,
            decisions=[],
            summary=summary,
            error_message=error_message,
        )
        return _persist_approval(
            repository=self._run_repository,
            created_at=created_at,
            updated_at=updated_at,
            receipt=receipt,
            receipt_path=receipt_path,
            policy_snapshot=evaluation.policy_snapshot,
        )


class ReviewApprovalUseCase:
    def __init__(
        self,
        run_repository: RunRepository,
        approval_policy: ApprovalPolicyEvaluator,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._run_repository = run_repository
        self._approval_policy = approval_policy
        self._logger = logger or logging.getLogger(__name__)

    def decide(
        self,
        *,
        approval_id: str,
        actor: str,
        team: str,
        decision: ApprovalDecision,
        comment: str = "",
    ) -> ApprovalWorkflowResult:
        approval = self._run_repository.get_approval(approval_id)
        if approval is None:
            raise ValueError(f"Approval not found: {approval_id}")
        record, payload = approval
        if record.status != ApprovalStatus.PENDING:
            raise ApprovalError(f"Approval {approval_id} is already in a final state: {record.status.value}.")
        if approval_is_expired(payload):
            raise ApprovalError(f"Approval {approval_id} has expired and can no longer be reviewed.")

        self._approval_policy.ensure_reviewer_can_decide(
            repo_full_name=record.repo_full_name,
            requested_by=record.requested_by,
            actor=actor,
            reviewer_team=team,
            required_reviewer_teams=_string_list(payload.get("required_reviewer_teams")),
            assigned_reviewers=_string_list(payload.get("assigned_reviewers")),
            assigned_reviewer_teams=_string_list(payload.get("assigned_reviewer_teams")),
        )

        existing_decisions = payload.get("decisions")
        if not isinstance(existing_decisions, list):
            existing_decisions = []
        if any(isinstance(item, dict) and item.get("actor") == actor for item in existing_decisions):
            raise ApprovalError(f"Actor '{actor}' has already reviewed approval {approval_id}.")

        updated_at = datetime.now(timezone.utc).isoformat()
        reviewer_decision = ApprovalReviewerDecision(
            actor=actor,
            team=team,
            decision=decision,
            comment=comment,
            decided_at=updated_at,
        )
        decisions = [
            ApprovalReviewerDecision(
                actor=str(item.get("actor", "")),
                team=str(item.get("team", "")),
                decision=ApprovalDecision(str(item.get("decision", ApprovalDecision.APPROVE.value))),
                comment=str(item.get("comment", "")),
                decided_at=str(item.get("decided_at", "")),
            )
            for item in existing_decisions
            if isinstance(item, dict)
        ]
        decisions.append(reviewer_decision)
        approved_count = sum(1 for item in decisions if item.decision == ApprovalDecision.APPROVE)

        if decision == ApprovalDecision.REJECT:
            status = ApprovalStatus.REJECTED
            error_message = comment or f"Rejected by {actor}."
        elif approved_count >= record.required_approvals:
            status = ApprovalStatus.APPROVED
            error_message = None
        else:
            status = ApprovalStatus.PENDING
            error_message = None

        summary = _approval_status_summary(
            status=status,
            risk_level=record.risk_level,
            approved_count=approved_count,
            required_approvals=record.required_approvals,
        )
        receipt = ApprovalReceipt(
            approval_id=record.approval_id,
            action=record.action,
            linked_run_id=record.linked_run_id,
            linked_execution_id=record.linked_execution_id,
            linked_verification_id=record.linked_verification_id,
            repo_full_name=record.repo_full_name,
            status=status,
            risk_level=record.risk_level,
            requested_by=record.requested_by,
            requester_team=record.requester_team,
            request_comment=str(payload.get("request_comment", "")),
            required_approvals=record.required_approvals,
            approved_count=approved_count,
            expires_at=_optional_string(payload.get("expires_at")),
            required_reviewer_teams=_string_list(payload.get("required_reviewer_teams")),
            assigned_reviewers=_string_list(payload.get("assigned_reviewers")),
            assigned_reviewer_teams=_string_list(payload.get("assigned_reviewer_teams")),
            reasons=_string_list(payload.get("reasons")),
            blocked_reasons=_string_list(payload.get("blocked_reasons")),
            decisions=decisions,
            summary=summary,
            error_message=error_message,
        )
        return _persist_approval(
            repository=self._run_repository,
            created_at=record.created_at,
            updated_at=updated_at,
            receipt=receipt,
            receipt_path=record.receipt_path,
            policy_snapshot=_dict_value(payload.get("policy_snapshot")),
        )


def _load_delivery_context(
    repository: RunRepository,
    *,
    run_id: str,
    execution_id: str,
    verification_id: str,
):
    run = repository.get_run(run_id)
    if run is None:
        raise ValueError(f"Run not found: {run_id}")
    run_record, run_payload = run
    execution = repository.get_execution(execution_id)
    if execution is None:
        raise ValueError(f"Execution not found: {execution_id}")
    execution_record, execution_payload = execution
    verification = repository.get_verification(verification_id)
    if verification is None:
        raise ValueError(f"Verification not found: {verification_id}")
    verification_record, verification_payload = verification

    if run_record.status != RunStatus.SUCCEEDED:
        raise ApprovalError(f"Run {run_id} is not in a succeeded state.")
    if execution_record.status != PatchExecutionStatus.SUCCEEDED:
        raise ApprovalError(f"Execution {execution_id} is not in a succeeded state.")
    if execution_record.mode.value != "apply":
        raise ApprovalError(f"Execution {execution_id} must be an apply run before approval.")
    if verification_record.status != VerificationStatus.SUCCEEDED:
        raise ApprovalError(f"Verification {verification_id} is not in a succeeded state.")
    if verification_record.stop_reason != VerificationStopReason.SUCCESS:
        raise ApprovalError(f"Verification {verification_id} did not stop with success.")
    if execution_record.linked_run_id != run_id:
        raise ApprovalError(f"Execution {execution_id} is not linked to run {run_id}.")
    if verification_record.linked_run_id != run_id:
        raise ApprovalError(f"Verification {verification_id} is not linked to run {run_id}.")
    if verification_record.linked_execution_id != execution_id:
        raise ApprovalError(f"Verification {verification_id} is not linked to execution {execution_id}.")
    return run_record, run_payload, execution_record, execution_payload, verification_record, verification_payload


def _persist_approval(
    *,
    repository: RunRepository,
    created_at: str,
    updated_at: str,
    receipt: ApprovalReceipt,
    receipt_path: Path,
    policy_snapshot: dict[str, object],
) -> ApprovalWorkflowResult:
    payload = _receipt_payload(created_at=created_at, updated_at=updated_at, receipt=receipt, policy_snapshot=policy_snapshot)
    receipt_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    record = ApprovalRecord(
        approval_id=receipt.approval_id,
        created_at=created_at,
        updated_at=updated_at,
        action=receipt.action,
        linked_run_id=receipt.linked_run_id,
        linked_execution_id=receipt.linked_execution_id,
        linked_verification_id=receipt.linked_verification_id,
        repo_full_name=receipt.repo_full_name,
        status=receipt.status,
        risk_level=receipt.risk_level,
        requested_by=receipt.requested_by,
        requester_team=receipt.requester_team,
        required_approvals=receipt.required_approvals,
        approved_count=receipt.approved_count,
        summary=receipt.summary,
        receipt_path=receipt_path,
        expires_at=receipt.expires_at,
        error_message=receipt.error_message,
    )
    repository.save_approval(record, payload)
    return ApprovalWorkflowResult(
        approval_id=receipt.approval_id,
        receipt=receipt,
        receipt_path=receipt_path,
    )


def _receipt_payload(
    *,
    created_at: str,
    updated_at: str,
    receipt: ApprovalReceipt,
    policy_snapshot: dict[str, object],
) -> dict[str, object]:
    return {
        "approval_id": receipt.approval_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "action": receipt.action.value,
        "linked_run_id": receipt.linked_run_id,
        "linked_execution_id": receipt.linked_execution_id,
        "linked_verification_id": receipt.linked_verification_id,
        "repo_full_name": receipt.repo_full_name,
        "status": receipt.status.value,
        "risk_level": receipt.risk_level.value,
        "requested_by": receipt.requested_by,
        "requester_team": receipt.requester_team,
        "request_comment": receipt.request_comment,
        "required_approvals": receipt.required_approvals,
        "approved_count": receipt.approved_count,
        "expires_at": receipt.expires_at,
        "required_reviewer_teams": receipt.required_reviewer_teams,
        "assigned_reviewers": receipt.assigned_reviewers,
        "assigned_reviewer_teams": receipt.assigned_reviewer_teams,
        "reasons": receipt.reasons,
        "blocked_reasons": receipt.blocked_reasons,
        "decisions": [
            {
                "actor": item.actor,
                "team": item.team,
                "decision": item.decision.value,
                "comment": item.comment,
                "decided_at": item.decided_at,
            }
            for item in receipt.decisions
        ],
        "summary": receipt.summary,
        "policy_snapshot": policy_snapshot,
        "error_message": receipt.error_message,
    }


def _approval_status_summary(
    *,
    status: ApprovalStatus,
    risk_level: ApprovalRiskLevel,
    approved_count: int,
    required_approvals: int,
) -> str:
    if status == ApprovalStatus.APPROVED:
        return (
            f"{risk_level.value.capitalize()}-risk delivery approval completed with "
            f"{approved_count}/{required_approvals} approvals."
        )
    if status == ApprovalStatus.REJECTED:
        return f"{risk_level.value.capitalize()}-risk delivery approval was rejected."
    return (
        f"{risk_level.value.capitalize()}-risk delivery approval is pending with "
        f"{approved_count}/{required_approvals} approvals collected."
    )


def approval_is_expired(payload: dict[str, object], *, now: datetime | None = None) -> bool:
    expires_at = _optional_string(payload.get("expires_at"))
    if expires_at is None:
        return False
    return (now or datetime.now(timezone.utc)) >= _parse_timestamp(expires_at)


def _resolve_expires_at(created_at: str, expires_in_hours: int | None) -> str | None:
    if expires_in_hours is None:
        return None
    if expires_in_hours <= 0:
        raise ApprovalError("Approval expiry must be greater than zero hours.")
    created = _parse_timestamp(created_at)
    return (created + timedelta(hours=expires_in_hours)).isoformat()


def _parse_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _clean_string_list(value: list[str] | None) -> list[str]:
    if value is None:
        return []
    return [item.strip() for item in value if item.strip()]


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}
