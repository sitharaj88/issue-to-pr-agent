from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from uuid import uuid4

from ...application.services.approval_policy import ApprovalPolicyEvaluator
from ...application.services.delivery_governance import DeliveryGovernancePolicyEvaluator
from ...application.services.delivery_summary import DeliverySummaryBuilder
from ...application.use_cases.manage_approval import approval_is_expired
from ...domain.entities import (
    ApprovalStatus,
    ArtifactReference,
    DeliveryReceipt,
    DeliveryRecord,
    DeliveryStatus,
    IssueCommentSummary,
    PullRequestSummary,
    RunStatus,
    VerificationStatus,
    VerificationStopReason,
)
from ...domain.policies.safety import SafetyPolicy
from ...infrastructure.persistence.run_repository import RunRepository
from ...infrastructure.scm.local_repo import LocalRepoInspector
from ...integrations.github.client import GitHubClient
from ...shared.exceptions import DeliveryError


@dataclass(frozen=True)
class DeliveryResult:
    delivery_id: str
    receipt: DeliveryReceipt
    receipt_path: Path


class DeliverRunUseCase:
    def __init__(
        self,
        run_repository: RunRepository,
        github_client: GitHubClient,
        safety_policy: SafetyPolicy,
        *,
        approval_policy: ApprovalPolicyEvaluator | None = None,
        delivery_governance_policy: DeliveryGovernancePolicyEvaluator | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._run_repository = run_repository
        self._github_client = github_client
        self._safety_policy = safety_policy
        self._approval_policy = approval_policy or ApprovalPolicyEvaluator()
        self._delivery_governance_policy = delivery_governance_policy or DeliveryGovernancePolicyEvaluator()
        self._logger = logger or logging.getLogger(__name__)

    def deliver(
        self,
        *,
        run_id: str,
        execution_id: str,
        verification_id: str,
        approval_id: str | None,
        repo_root: Path,
        artifact_dir: Path,
        artifact_base_url: str | None,
        artifact_store_backend: str = "filesystem",
        artifact_store_dir: Path | None = None,
        artifact_store_base_url: str | None = None,
        remote_name: str,
        base_branch: str | None = None,
        rollout_stage: str | None = None,
        commit_message: str | None = None,
        pr_title: str | None = None,
        publish_pr_comment: bool = True,
    ) -> DeliveryResult:
        delivery_id = uuid4().hex[:12]
        created_at = datetime.now(timezone.utc).isoformat()

        run_context = self._run_repository.get_run(run_id)
        if run_context is None:
            raise ValueError(f"Run not found: {run_id}")
        run_record, run_payload = run_context
        receipt_path = run_record.audit_path.parent / "deliveries" / f"{delivery_id}.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)

        execution_record = None
        verification_record = None
        commit_sha: str | None = None
        pr: PullRequestSummary | None = None
        pr_comment: IssueCommentSummary | None = None
        artifacts: list[ArtifactReference] = []
        summary = "GitHub delivery"
        branch_name = run_record.branch_name
        resolved_base_branch = base_branch or ""
        resolved_rollout_stage = _normalize_optional(rollout_stage)
        rollback_base_sha: str | None = None
        branch_protection_required = False
        branch_protection_verified = False
        governance_reasons: list[str] = []
        governance_blocked_reasons: list[str] = []
        governance_policy_snapshot: dict[str, object] = {}
        linked_approval_id = approval_id

        try:
            self._logger.info(
                "Starting GitHub delivery",
                extra={
                    "run_id": run_id,
                    "execution_id": execution_id,
                    "verification_id": verification_id,
                    "provider": "github_delivery",
                },
            )
            execution_context = self._run_repository.get_execution(execution_id)
            if execution_context is None:
                raise ValueError(f"Execution not found: {execution_id}")
            execution_record, execution_payload = execution_context
            patch_proposal_context = self._run_repository.get_patch_proposal(execution_record.proposal_id)
            patch_payload = patch_proposal_context[1] if patch_proposal_context is not None else {}

            verification_context = self._run_repository.get_verification(verification_id)
            if verification_context is None:
                raise ValueError(f"Verification not found: {verification_id}")
            verification_record, verification_payload = verification_context

            self._validate_linked_records(
                run_record=run_record,
                execution_record=execution_record,
                verification_record=verification_record,
                run_payload=run_payload,
                repo_root=repo_root,
            )

            summary_builder = DeliverySummaryBuilder(
                artifact_dir=artifact_dir,
                artifact_base_url=artifact_base_url,
                artifact_store_backend=artifact_store_backend,
                artifact_store_dir=artifact_store_dir,
                artifact_store_base_url=artifact_store_base_url,
            )
            summary = summary_builder.build_summary(
                run_payload=run_payload,
                execution_payload=execution_payload,
                verification_payload=verification_payload,
            )
            approval_evaluation = self._approval_policy.evaluate_delivery(
                repo_full_name=run_record.repo_full_name,
                run_payload=run_payload,
                execution_payload=execution_payload,
                verification_payload=verification_payload,
            )
            linked_approval_id = self._ensure_delivery_approval(
                approval_id=approval_id,
                approval_required=approval_evaluation.approval_required,
                blocked_reasons=approval_evaluation.blocked_reasons,
                linked_run_id=run_id,
                linked_execution_id=execution_id,
                linked_verification_id=verification_id,
            )
            branch_name = self._resolve_branch_name(run_record.branch_name, run_payload)
            self._safety_policy.ensure_branch_name(branch_name)

            repository_info = self._github_client.fetch_repository(run_record.repo_full_name)
            resolved_base_branch = _normalize_optional(base_branch) or repository_info.default_branch
            if branch_name == resolved_base_branch:
                raise DeliveryError("Target branch cannot match the repository base branch.")
            branch_protection_verified = self._github_client.fetch_branch_protection(
                run_record.repo_full_name,
                resolved_base_branch,
            )
            governance_evaluation = self._delivery_governance_policy.evaluate_delivery(
                repo_full_name=run_record.repo_full_name,
                base_branch=resolved_base_branch,
                base_branch_protected=branch_protection_verified,
                rollout_stage=resolved_rollout_stage,
                run_payload=run_payload,
                execution_payload=execution_payload,
                patch_payload=patch_payload,
            )
            resolved_rollout_stage = governance_evaluation.rollout_stage
            branch_protection_required = governance_evaluation.branch_protection_required
            branch_protection_verified = governance_evaluation.branch_protection_verified
            governance_reasons = governance_evaluation.reasons
            governance_blocked_reasons = governance_evaluation.blocked_reasons
            governance_policy_snapshot = governance_evaluation.policy_snapshot
            if governance_blocked_reasons:
                raise DeliveryError(
                    "Delivery is blocked by governance policy: " + "; ".join(governance_blocked_reasons)
                )

            inspector = LocalRepoInspector(repo_root)
            if not inspector.snapshot().is_git_repo:
                raise DeliveryError(f"Delivery requires a git repository: {repo_root}")
            if not inspector.has_remote(remote_name):
                raise DeliveryError(f"Git remote '{remote_name}' does not exist in {repo_root}.")
            rollback_base_sha = inspector.commit_sha_for_ref(resolved_base_branch)

            changed_paths = set(_changed_paths(execution_payload))
            if not changed_paths:
                raise DeliveryError("Execution receipt does not contain any changed files to deliver.")

            current_paths = set(inspector.changed_paths())
            unexpected_paths = sorted(
                path
                for path in current_paths
                if not _path_matches_expected_change(path, changed_paths)
                and not _is_under_artifact_dir(path, repo_root=repo_root, artifact_dir=artifact_dir)
            )
            if unexpected_paths:
                raise DeliveryError(
                    "Workspace contains changes outside the execution receipt: "
                    + ", ".join(unexpected_paths)
                )

            current_branch = inspector.current_branch()
            if current_branch != branch_name:
                if inspector.branch_exists(branch_name):
                    raise DeliveryError(
                        f"Target branch '{branch_name}' already exists and is not the current branch."
                    )
                inspector.create_branch(branch_name)

            inspector.stage_paths(sorted(changed_paths))
            staged_paths = set(inspector.staged_paths())
            missing_paths = sorted(path for path in changed_paths if path not in staged_paths)
            if missing_paths:
                raise DeliveryError(
                    "Execution receipt changes are not all present in the current workspace: "
                    + ", ".join(missing_paths)
                )

            resolved_commit_message = _normalize_optional(commit_message) or summary_builder.build_commit_message(
                run_payload=run_payload
            )
            commit_sha = inspector.commit(resolved_commit_message)
            inspector.push_branch(remote_name, branch_name)

            artifacts = summary_builder.build_artifact_references(
                run_payload=run_payload,
                execution_payload=execution_payload,
                verification_payload=verification_payload,
            )
            resolved_pr_title = _normalize_optional(pr_title) or summary_builder.build_pr_title(
                run_payload=run_payload
            )
            pr_body = summary_builder.build_pr_body(
                run_payload=run_payload,
                verification_payload=verification_payload,
                artifacts=artifacts,
                branch_name=branch_name,
                base_branch=resolved_base_branch,
                commit_sha=commit_sha,
                rollout_stage=resolved_rollout_stage,
                rollback_base_sha=rollback_base_sha,
                branch_protection_verified=branch_protection_verified,
            )
            pr = self._github_client.create_pull_request(
                run_record.repo_full_name,
                title=resolved_pr_title,
                body=pr_body,
                head_branch=branch_name,
                base_branch=resolved_base_branch,
                draft=True,
            )
            if publish_pr_comment:
                pr_comment = self._github_client.add_issue_comment(
                    run_record.repo_full_name,
                    pr.number,
                    body=summary_builder.build_pr_comment(
                        run_payload=run_payload,
                        execution_payload=execution_payload,
                        verification_payload=verification_payload,
                        artifacts=artifacts,
                        commit_sha=commit_sha,
                        rollout_stage=resolved_rollout_stage,
                        rollback_base_sha=rollback_base_sha,
                        branch_protection_verified=branch_protection_verified,
                    ),
                )

            return self._finalize(
                created_at=created_at,
                receipt_path=receipt_path,
                linked_run_id=run_id,
                linked_execution_id=execution_id,
                linked_verification_id=verification_id,
                linked_approval_id=linked_approval_id,
                status=DeliveryStatus.SUCCEEDED,
                repo_root=repo_root,
                repo_full_name=run_record.repo_full_name,
                branch_name=branch_name,
                base_branch=resolved_base_branch,
                commit_sha=commit_sha,
                commit_message=resolved_commit_message,
                pr=pr,
                pr_comment=pr_comment,
                rollout_stage=resolved_rollout_stage,
                rollback_base_sha=rollback_base_sha,
                branch_protection_required=branch_protection_required,
                branch_protection_verified=branch_protection_verified,
                governance_reasons=governance_reasons,
                governance_blocked_reasons=governance_blocked_reasons,
                governance_policy_snapshot=governance_policy_snapshot,
                artifacts=artifacts,
                summary=summary,
                error_message=None,
            )
        except Exception as exc:
            log_args = {
                "extra": {
                    "run_id": run_id,
                    "execution_id": execution_id,
                    "verification_id": verification_id,
                    "provider": "github_delivery",
                }
            }
            if isinstance(exc, DeliveryError):
                self._logger.warning("GitHub delivery blocked", **log_args)
            else:
                self._logger.exception("GitHub delivery failed", **log_args)
            error_message = str(exc)
            return self._finalize(
                created_at=created_at,
                receipt_path=receipt_path,
                linked_run_id=run_id,
                linked_execution_id=execution_id,
                linked_verification_id=verification_id,
                linked_approval_id=linked_approval_id,
                status=DeliveryStatus.FAILED,
                repo_root=repo_root,
                repo_full_name=run_record.repo_full_name,
                branch_name=branch_name,
                base_branch=resolved_base_branch,
                commit_sha=commit_sha,
                commit_message=_normalize_optional(commit_message) or "",
                pr=pr,
                pr_comment=pr_comment,
                rollout_stage=resolved_rollout_stage,
                rollback_base_sha=rollback_base_sha,
                branch_protection_required=branch_protection_required,
                branch_protection_verified=branch_protection_verified,
                governance_reasons=governance_reasons,
                governance_blocked_reasons=governance_blocked_reasons,
                governance_policy_snapshot=governance_policy_snapshot,
                artifacts=artifacts,
                summary=summary,
                error_message=error_message,
            )

    def _validate_linked_records(
        self,
        *,
        run_record,
        execution_record,
        verification_record,
        run_payload: dict[str, object],
        repo_root: Path,
    ) -> None:
        if run_record.status != RunStatus.SUCCEEDED:
            raise DeliveryError(f"Run {run_record.run_id} is not in a succeeded state.")
        if execution_record.status.value != "succeeded":
            raise DeliveryError(f"Execution {execution_record.execution_id} is not in a succeeded state.")
        if execution_record.mode.value != "apply":
            raise DeliveryError(f"Execution {execution_record.execution_id} must be an apply run to deliver.")
        if verification_record.status != VerificationStatus.SUCCEEDED:
            raise DeliveryError(f"Verification {verification_record.verification_id} is not in a succeeded state.")
        if verification_record.stop_reason != VerificationStopReason.SUCCESS:
            raise DeliveryError(
                f"Verification {verification_record.verification_id} did not stop with success."
            )
        if execution_record.linked_run_id != run_record.run_id:
            raise DeliveryError(
                f"Execution {execution_record.execution_id} is not linked to run {run_record.run_id}."
            )
        if verification_record.linked_run_id != run_record.run_id:
            raise DeliveryError(
                f"Verification {verification_record.verification_id} is not linked to run {run_record.run_id}."
            )
        if verification_record.linked_execution_id != execution_record.execution_id:
            raise DeliveryError(
                f"Verification {verification_record.verification_id} is not linked to execution {execution_record.execution_id}."
            )
        run_snapshot = run_payload.get("repo_snapshot")
        if isinstance(run_snapshot, dict):
            if run_snapshot.get("is_dirty") is True:
                raise DeliveryError("Planning run started from a dirty repository state.")
            run_root = run_snapshot.get("root")
            if isinstance(run_root, str) and Path(run_root).resolve() != repo_root.resolve():
                raise DeliveryError("Delivery repo_root does not match the planning run repository root.")
        if execution_record.repo_root.resolve() != repo_root.resolve():
            raise DeliveryError("Delivery repo_root does not match the execution repository root.")
        if verification_record.repo_root.resolve() != repo_root.resolve():
            raise DeliveryError("Delivery repo_root does not match the verification repository root.")

    def _resolve_branch_name(self, branch_name: str, run_payload: dict[str, object]) -> str:
        if branch_name.strip():
            return branch_name
        plan = run_payload.get("plan")
        if isinstance(plan, dict):
            plan_branch = plan.get("branch_name")
            if isinstance(plan_branch, str) and plan_branch.strip():
                return plan_branch
        raise DeliveryError("Planning run does not contain a branch name for delivery.")

    def _ensure_delivery_approval(
        self,
        *,
        approval_id: str | None,
        approval_required: bool,
        blocked_reasons: list[str],
        linked_run_id: str,
        linked_execution_id: str,
        linked_verification_id: str,
    ) -> str | None:
        if blocked_reasons:
            raise DeliveryError("Delivery is blocked by approval policy: " + "; ".join(blocked_reasons))
        if approval_id is None:
            if approval_required:
                raise DeliveryError(
                    "Delivery requires an approved approval request. Create one with request-approval."
                )
            return None
        approval = self._run_repository.get_approval(approval_id)
        if approval is None:
            raise DeliveryError(f"Approval not found: {approval_id}")
        record, payload = approval
        if record.status != ApprovalStatus.APPROVED:
            raise DeliveryError(f"Approval {approval_id} is not approved.")
        if approval_is_expired(payload):
            raise DeliveryError(f"Approval {approval_id} has expired and must be re-requested.")
        if record.linked_run_id != linked_run_id:
            raise DeliveryError(f"Approval {approval_id} is not linked to run {linked_run_id}.")
        if record.linked_execution_id != linked_execution_id:
            raise DeliveryError(
                f"Approval {approval_id} is not linked to execution {linked_execution_id}."
            )
        if record.linked_verification_id != linked_verification_id:
            raise DeliveryError(
                f"Approval {approval_id} is not linked to verification {linked_verification_id}."
            )
        return approval_id

    def _finalize(
        self,
        *,
        created_at: str,
        receipt_path: Path,
        linked_run_id: str,
        linked_execution_id: str,
        linked_verification_id: str,
        linked_approval_id: str | None,
        status: DeliveryStatus,
        repo_root: Path,
        repo_full_name: str,
        branch_name: str,
        base_branch: str,
        commit_sha: str | None,
        commit_message: str,
        pr: PullRequestSummary | None,
        pr_comment: IssueCommentSummary | None,
        rollout_stage: str | None,
        rollback_base_sha: str | None,
        branch_protection_required: bool,
        branch_protection_verified: bool,
        governance_reasons: list[str],
        governance_blocked_reasons: list[str],
        governance_policy_snapshot: dict[str, object],
        artifacts: list[ArtifactReference],
        summary: str,
        error_message: str | None,
    ) -> DeliveryResult:
        delivery_id = receipt_path.stem
        receipt = DeliveryReceipt(
            delivery_id=delivery_id,
            linked_run_id=linked_run_id,
            linked_execution_id=linked_execution_id,
            linked_verification_id=linked_verification_id,
            linked_approval_id=linked_approval_id,
            status=status,
            repo_root=repo_root,
            repo_full_name=repo_full_name,
            branch_name=branch_name,
            base_branch=base_branch,
            commit_sha=commit_sha,
            commit_message=commit_message,
            pr=pr,
            pr_comment=pr_comment,
            rollout_stage=rollout_stage,
            rollback_base_sha=rollback_base_sha,
            branch_protection_required=branch_protection_required,
            branch_protection_verified=branch_protection_verified,
            governance_reasons=governance_reasons,
            governance_blocked_reasons=governance_blocked_reasons,
            governance_policy_snapshot=governance_policy_snapshot,
            artifacts=artifacts,
            summary=summary,
            error_message=error_message,
        )
        payload = _receipt_payload(created_at, receipt)
        receipt_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        record = DeliveryRecord(
            delivery_id=delivery_id,
            created_at=created_at,
            linked_run_id=linked_run_id,
            linked_execution_id=linked_execution_id,
            linked_verification_id=linked_verification_id,
            status=status,
            repo_full_name=repo_full_name,
            branch_name=branch_name,
            base_branch=base_branch,
            summary=summary,
            receipt_path=receipt_path,
            error_message=error_message,
        )
        self._run_repository.save_delivery(record, payload)
        return DeliveryResult(delivery_id=delivery_id, receipt=receipt, receipt_path=receipt_path)


def _changed_paths(execution_payload: dict[str, object]) -> list[str]:
    receipts = execution_payload.get("receipts")
    if not isinstance(receipts, list):
        return []
    changed: list[str] = []
    for item in receipts:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if item.get("changed") is True and isinstance(path, str) and path.strip():
            changed.append(path)
    return changed


def _is_under_artifact_dir(path: str, *, repo_root: Path, artifact_dir: Path) -> bool:
    try:
        artifact_relative = artifact_dir.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return False
    normalized_path = Path(path).as_posix()
    return normalized_path == artifact_relative or normalized_path.startswith(f"{artifact_relative}/")


def _path_matches_expected_change(path: str, changed_paths: set[str]) -> bool:
    normalized_path = Path(path).as_posix().rstrip("/")
    for changed_path in changed_paths:
        normalized_changed = Path(changed_path).as_posix().rstrip("/")
        if normalized_changed == normalized_path or normalized_changed.startswith(f"{normalized_path}/"):
            return True
    return False


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _receipt_payload(created_at: str, receipt: DeliveryReceipt) -> dict[str, object]:
    return {
        "delivery_id": receipt.delivery_id,
        "created_at": created_at,
        "linked_run_id": receipt.linked_run_id,
        "linked_execution_id": receipt.linked_execution_id,
        "linked_verification_id": receipt.linked_verification_id,
        "linked_approval_id": receipt.linked_approval_id,
        "status": receipt.status.value,
        "repo_root": str(receipt.repo_root),
        "repo_full_name": receipt.repo_full_name,
        "branch_name": receipt.branch_name,
        "base_branch": receipt.base_branch,
        "commit_sha": receipt.commit_sha,
        "commit_message": receipt.commit_message,
        "rollout_stage": receipt.rollout_stage,
        "rollback_base_sha": receipt.rollback_base_sha,
        "branch_protection_required": receipt.branch_protection_required,
        "branch_protection_verified": receipt.branch_protection_verified,
        "governance_reasons": receipt.governance_reasons,
        "governance_blocked_reasons": receipt.governance_blocked_reasons,
        "governance_policy_snapshot": receipt.governance_policy_snapshot,
        "pr": None
        if receipt.pr is None
        else {
            "number": receipt.pr.number,
            "url": receipt.pr.url,
            "html_url": receipt.pr.html_url,
            "title": receipt.pr.title,
        },
        "pr_comment": None
        if receipt.pr_comment is None
        else {
            "comment_id": receipt.pr_comment.comment_id,
            "url": receipt.pr_comment.url,
            "html_url": receipt.pr_comment.html_url,
        },
        "artifacts": [
            {
                "label": item.label,
                "path": item.path,
                "url": item.url,
            }
            for item in receipt.artifacts
        ],
        "summary": receipt.summary,
        "error_message": receipt.error_message,
    }
