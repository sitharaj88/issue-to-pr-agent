from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from uuid import uuid4

from ...agents.patcher.base import PatcherClient
from ...application.services.patch_reflection import PatchReflectionService
from ...domain.entities import (
    AutofixAttemptReceipt,
    AutofixAttemptRecord,
    AutofixAttemptStatus,
    AutofixReceipt,
    AutofixRunRecord,
    AutofixStatus,
    PatchExecutionMode,
    VerificationStatus,
)
from ...domain.policies.safety import SafetyPolicy
from ...infrastructure.persistence.run_repository import RunRepository
from .execute_patch_proposal import ExecutePatchProposalUseCase, PatchExecutionFailedError, PatchExecutionResult
from .generate_patch_proposal import GeneratePatchProposalUseCase, PatchProposalGenerationResult
from .verify_run import VerificationResult, VerifyRunUseCase


@dataclass(frozen=True)
class AutofixRunResult:
    autofix_id: str
    status: AutofixStatus
    receipt: AutofixReceipt
    receipt_path: Path


class RunAutofixUseCase:
    def __init__(
        self,
        run_repository: RunRepository,
        patcher: PatcherClient,
        safety_policy: SafetyPolicy,
        *,
        generator: GeneratePatchProposalUseCase | None = None,
        executor: ExecutePatchProposalUseCase | None = None,
        verifier: VerifyRunUseCase | None = None,
        reflector: PatchReflectionService | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._run_repository = run_repository
        self._patcher = patcher
        self._generator = generator or GeneratePatchProposalUseCase(run_repository, patcher)
        self._executor = executor or ExecutePatchProposalUseCase(run_repository)
        self._verifier = verifier or VerifyRunUseCase(run_repository, safety_policy)
        self._reflector = reflector or PatchReflectionService()
        self._logger = logger or logging.getLogger(__name__)

    def run(
        self,
        *,
        run_id: str,
        repo_root: Path,
        artifact_dir: Path,
        max_attempts: int = 3,
        verify_max_attempts: int = 3,
        timeout_seconds: int = 120,
        objective: str | None = None,
    ) -> AutofixRunResult:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be greater than zero.")
        if verify_max_attempts <= 0:
            raise ValueError("verify_max_attempts must be greater than zero.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero.")

        run = self._run_repository.get_run(run_id)
        if run is None:
            raise ValueError(f"Run not found: {run_id}")
        record, _ = run

        autofix_id = uuid4().hex[:12]
        created_at = _now()
        receipt_path = record.audit_path.parent / "autofix" / f"{autofix_id}.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)

        attempts: list[AutofixAttemptReceipt] = []
        latest_proposal_id: str | None = None
        latest_execution_id: str | None = None
        latest_verification_id: str | None = None
        next_objective = objective or ""

        receipt = AutofixReceipt(
            autofix_id=autofix_id,
            linked_run_id=run_id,
            provider=self._patcher.provider,
            status=AutofixStatus.RUNNING,
            repo_root=repo_root,
            max_attempts=max_attempts,
            objective=objective or "",
            attempts=attempts,
            summary="Autofix started.",
        )
        self._save_run_state(
            created_at=created_at,
            updated_at=created_at,
            receipt=receipt,
            receipt_path=receipt_path,
        )

        for attempt_index in range(1, max_attempts + 1):
            attempt_created_at = _now()
            attempt_id = uuid4().hex[:12]
            attempt_objective = next_objective
            proposal_result: PatchProposalGenerationResult | None = None
            execution_result: PatchExecutionResult | None = None
            verification_result: VerificationResult | None = None
            error_message: str | None = None
            summary = "Autofix attempt"

            self._logger.info(
                "Starting autofix attempt",
                extra={"run_id": run_id, "provider": "autofix", "attempt": attempt_index},
            )

            try:
                proposal_result = self._generator.generate(
                    run_id=run_id,
                    repo_root=repo_root,
                    objective=attempt_objective or None,
                )
                latest_proposal_id = proposal_result.proposal_id
                summary = proposal_result.proposal.summary
                execution_result = self._executor.execute(
                    proposal=proposal_result.proposal,
                    repo_root=repo_root,
                    artifact_dir=artifact_dir,
                    mode=PatchExecutionMode.APPLY,
                )
                latest_execution_id = execution_result.execution_id
                verification_result = self._verifier.verify(
                    repo_root=repo_root,
                    artifact_dir=artifact_dir,
                    execution_id=execution_result.execution_id,
                    max_attempts=verify_max_attempts,
                    timeout_seconds=timeout_seconds,
                )
                latest_verification_id = verification_result.verification_id
                if verification_result.receipt.status == VerificationStatus.SUCCEEDED:
                    attempt_status = AutofixAttemptStatus.SUCCEEDED
                else:
                    attempt_status = AutofixAttemptStatus.FAILED
                    error_message = verification_result.receipt.error_message or (
                        "Verification failed: "
                        f"{verification_result.receipt.stop_reason.value}"
                    )
            except PatchExecutionFailedError as exc:
                execution_result = exc.result
                latest_execution_id = exc.result.execution_id
                summary = proposal_result.proposal.summary if proposal_result is not None else summary
                attempt_status = AutofixAttemptStatus.FAILED
                error_message = exc.result.receipt.error_message or str(exc)
            except Exception as exc:
                attempt_status = AutofixAttemptStatus.FAILED
                error_message = str(exc)
                if proposal_result is None:
                    summary = "Patch generation failed"

            attempt = AutofixAttemptReceipt(
                attempt_id=attempt_id,
                created_at=attempt_created_at,
                attempt_index=attempt_index,
                status=attempt_status,
                summary=summary,
                objective=attempt_objective,
                proposal_id=None if proposal_result is None else proposal_result.proposal_id,
                execution_id=None if execution_result is None else execution_result.execution_id,
                verification_id=None if verification_result is None else verification_result.verification_id,
                verification_stop_reason=None
                if verification_result is None
                else verification_result.receipt.stop_reason,
                error_message=error_message,
            )
            attempts.append(attempt)
            self._save_attempt(
                autofix_id=autofix_id,
                attempt=attempt,
                receipt_path=receipt_path.parent / "attempts" / f"{attempt_index:02d}-{attempt_id}.json",
            )

            if attempt.status == AutofixAttemptStatus.SUCCEEDED:
                final_receipt = AutofixReceipt(
                    autofix_id=autofix_id,
                    linked_run_id=run_id,
                    provider=self._patcher.provider,
                    status=AutofixStatus.SUCCEEDED,
                    repo_root=repo_root,
                    max_attempts=max_attempts,
                    objective=objective or "",
                    attempts=list(attempts),
                    latest_proposal_id=latest_proposal_id,
                    latest_execution_id=latest_execution_id,
                    latest_verification_id=latest_verification_id,
                    summary=f"Autofix succeeded after {len(attempts)} attempt(s).",
                )
                updated_at = _now()
                self._save_run_state(
                    created_at=created_at,
                    updated_at=updated_at,
                    receipt=final_receipt,
                    receipt_path=receipt_path,
                )
                return AutofixRunResult(
                    autofix_id=autofix_id,
                    status=final_receipt.status,
                    receipt=final_receipt,
                    receipt_path=receipt_path,
                )

            has_remaining_attempts = attempt_index < max_attempts
            current_receipt = AutofixReceipt(
                autofix_id=autofix_id,
                linked_run_id=run_id,
                provider=self._patcher.provider,
                status=AutofixStatus.RUNNING if has_remaining_attempts else AutofixStatus.FAILED,
                repo_root=repo_root,
                max_attempts=max_attempts,
                objective=objective or "",
                attempts=list(attempts),
                latest_proposal_id=latest_proposal_id,
                latest_execution_id=latest_execution_id,
                latest_verification_id=latest_verification_id,
                summary=(
                    f"Autofix will retry after attempt {attempt_index}."
                    if has_remaining_attempts
                    else f"Autofix failed after {len(attempts)} attempt(s)."
                ),
                error_message=None if has_remaining_attempts else error_message,
            )
            updated_at = _now()
            self._save_run_state(
                created_at=created_at,
                updated_at=updated_at,
                receipt=current_receipt,
                receipt_path=receipt_path,
            )

            if not has_remaining_attempts:
                return AutofixRunResult(
                    autofix_id=autofix_id,
                    status=current_receipt.status,
                    receipt=current_receipt,
                    receipt_path=receipt_path,
                )

            next_objective = self._reflector.build_retry_objective(
                next_attempt_index=attempt_index + 1,
                base_objective=objective,
                proposal=None if proposal_result is None else proposal_result.proposal,
                execution_result=execution_result,
                verification_result=verification_result,
                error_message=error_message,
            )

        raise RuntimeError("Autofix loop exited unexpectedly.")

    def _save_attempt(
        self,
        *,
        autofix_id: str,
        attempt: AutofixAttemptReceipt,
        receipt_path: Path,
    ) -> None:
        payload = {
            "attempt_id": attempt.attempt_id,
            "autofix_id": autofix_id,
            "created_at": attempt.created_at,
            "attempt_index": attempt.attempt_index,
            "status": attempt.status.value,
            "summary": attempt.summary,
            "objective": attempt.objective,
            "proposal_id": attempt.proposal_id,
            "execution_id": attempt.execution_id,
            "verification_id": attempt.verification_id,
            "verification_stop_reason": None
            if attempt.verification_stop_reason is None
            else attempt.verification_stop_reason.value,
            "error_message": attempt.error_message,
        }
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._run_repository.save_autofix_attempt(
            AutofixAttemptRecord(
                attempt_id=attempt.attempt_id,
                autofix_id=autofix_id,
                attempt_index=attempt.attempt_index,
                created_at=attempt.created_at,
                status=attempt.status,
                summary=attempt.summary,
                objective=attempt.objective,
                proposal_id=attempt.proposal_id,
                execution_id=attempt.execution_id,
                verification_id=attempt.verification_id,
                verification_stop_reason=attempt.verification_stop_reason,
                payload_path=receipt_path,
                error_message=attempt.error_message,
            ),
            payload,
        )

    def _save_run_state(
        self,
        *,
        created_at: str,
        updated_at: str,
        receipt: AutofixReceipt,
        receipt_path: Path,
    ) -> None:
        payload = {
            "autofix_id": receipt.autofix_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "linked_run_id": receipt.linked_run_id,
            "provider": receipt.provider.value,
            "status": receipt.status.value,
            "repo_root": str(receipt.repo_root),
            "max_attempts": receipt.max_attempts,
            "attempt_count": len(receipt.attempts),
            "objective": receipt.objective,
            "latest_proposal_id": receipt.latest_proposal_id,
            "latest_execution_id": receipt.latest_execution_id,
            "latest_verification_id": receipt.latest_verification_id,
            "summary": receipt.summary,
            "error_message": receipt.error_message,
            "attempts": [
                {
                    "attempt_id": item.attempt_id,
                    "created_at": item.created_at,
                    "attempt_index": item.attempt_index,
                    "status": item.status.value,
                    "summary": item.summary,
                    "objective": item.objective,
                    "proposal_id": item.proposal_id,
                    "execution_id": item.execution_id,
                    "verification_id": item.verification_id,
                    "verification_stop_reason": None
                    if item.verification_stop_reason is None
                    else item.verification_stop_reason.value,
                    "error_message": item.error_message,
                }
                for item in receipt.attempts
            ],
        }
        receipt_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._run_repository.save_autofix_run(
            AutofixRunRecord(
                autofix_id=receipt.autofix_id,
                created_at=created_at,
                updated_at=updated_at,
                linked_run_id=receipt.linked_run_id,
                provider=receipt.provider,
                status=receipt.status,
                summary=receipt.summary,
                repo_root=receipt.repo_root,
                max_attempts=receipt.max_attempts,
                attempt_count=len(receipt.attempts),
                latest_proposal_id=receipt.latest_proposal_id,
                latest_execution_id=receipt.latest_execution_id,
                latest_verification_id=receipt.latest_verification_id,
                receipt_path=receipt_path,
                error_message=receipt.error_message,
            ),
            payload,
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
