from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from uuid import uuid4

from ...application.services.verification_reflection import VerificationReflector
from ...application.services.verification_strategy import VerificationStrategyResolver
from ...domain.entities import (
    CommandAssessment,
    CommandDecision,
    TestCommandCandidate,
    VerificationAttempt,
    VerificationAttemptStatus,
    VerificationReceipt,
    VerificationRecord,
    VerificationStatus,
    VerificationStopReason,
)
from ...domain.policies.safety import SafetyPolicy
from ...infrastructure.persistence.run_repository import RunRepository
from ...infrastructure.verification.command_runner import CommandRunner, LocalCommandRunner


@dataclass(frozen=True)
class VerificationResult:
    verification_id: str
    linked_run_id: str | None
    linked_execution_id: str | None
    receipt: VerificationReceipt
    receipt_path: Path


class VerifyRunUseCase:
    def __init__(
        self,
        run_repository: RunRepository,
        safety_policy: SafetyPolicy,
        *,
        strategy_resolver: VerificationStrategyResolver | None = None,
        reflector: VerificationReflector | None = None,
        command_runner: CommandRunner | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._run_repository = run_repository
        self._safety_policy = safety_policy
        self._strategy_resolver = strategy_resolver or VerificationStrategyResolver()
        self._reflector = reflector or VerificationReflector()
        self._command_runner = command_runner or LocalCommandRunner()
        self._logger = logger or logging.getLogger(__name__)

    def verify(
        self,
        *,
        repo_root: Path,
        artifact_dir: Path,
        run_id: str | None = None,
        execution_id: str | None = None,
        max_attempts: int = 3,
        timeout_seconds: int = 120,
    ) -> VerificationResult:
        if not run_id and not execution_id:
            raise ValueError("Verification requires a run_id or execution_id.")
        if run_id and execution_id:
            raise ValueError("Verification accepts only one of run_id or execution_id.")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be greater than zero.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero.")

        verification_id = uuid4().hex[:12]
        created_at = datetime.now(timezone.utc).isoformat()
        linked_run_id, linked_execution_id, payload = self._load_context(run_id=run_id, execution_id=execution_id)
        summary = _resolve_summary(payload)
        receipt_path = self._receipt_path(
            artifact_dir=artifact_dir,
            verification_id=verification_id,
            linked_run_id=linked_run_id,
        )
        receipt_path.parent.mkdir(parents=True, exist_ok=True)

        candidates = self._strategy_resolver.resolve(payload)
        if not candidates:
            return self._finalize(
                created_at=created_at,
                verification_id=verification_id,
                linked_run_id=linked_run_id,
                linked_execution_id=linked_execution_id,
                repo_root=repo_root,
                summary=summary,
                receipt_path=receipt_path,
                attempts=[],
                skipped_commands=[],
                status=VerificationStatus.FAILED,
                stop_reason=VerificationStopReason.NO_CANDIDATE_COMMANDS,
                error_message="No candidate test commands were available.",
            )

        candidate_assessments = self._safety_policy.assess_commands([item.command for item in candidates])
        allowed_candidates: list[TestCommandCandidate] = []
        skipped_commands: list[CommandAssessment] = []
        for candidate, assessment in zip(candidates, candidate_assessments):
            if assessment.decision == CommandDecision.ALLOW:
                allowed_candidates.append(candidate)
            else:
                skipped_commands.append(assessment)

        if not allowed_candidates:
            return self._finalize(
                created_at=created_at,
                verification_id=verification_id,
                linked_run_id=linked_run_id,
                linked_execution_id=linked_execution_id,
                repo_root=repo_root,
                summary=summary,
                receipt_path=receipt_path,
                attempts=[],
                skipped_commands=skipped_commands,
                status=VerificationStatus.FAILED,
                stop_reason=VerificationStopReason.NO_ALLOWED_COMMANDS,
                error_message="No candidate test commands passed the command safety policy.",
            )

        attempts: list[VerificationAttempt] = []
        stop_reason = VerificationStopReason.CANDIDATE_COMMANDS_EXHAUSTED
        error_message: str | None = None
        final_status = VerificationStatus.FAILED

        self._logger.info(
            "Starting verification run",
            extra={"run_id": linked_run_id, "provider": "verifier"},
        )

        try:
            for attempt_index, candidate in enumerate(allowed_candidates[:max_attempts], start=1):
                stdout_path = receipt_path.parent / f"attempt-{attempt_index}.stdout.log"
                stderr_path = receipt_path.parent / f"attempt-{attempt_index}.stderr.log"
                result = self._command_runner.run(
                    command=candidate.command,
                    cwd=repo_root,
                    timeout_seconds=timeout_seconds,
                )
                stdout_path.write_text(result.stdout, encoding="utf-8")
                stderr_path.write_text(result.stderr, encoding="utf-8")

                continue_running, reflected_stop_reason, note = self._reflector.reflect(
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    has_remaining_candidates=attempt_index < len(allowed_candidates),
                    attempts_used=attempt_index,
                    max_attempts=max_attempts,
                )
                attempts.append(
                    VerificationAttempt(
                        attempt_index=attempt_index,
                        command=candidate.command,
                        source=candidate.source,
                        status=VerificationAttemptStatus.PASSED
                        if result.exit_code == 0
                        else VerificationAttemptStatus.FAILED,
                        exit_code=result.exit_code,
                        duration_ms=result.duration_ms,
                        stdout_path=stdout_path,
                        stderr_path=stderr_path,
                        note=note,
                    )
                )

                if result.exit_code == 0:
                    final_status = VerificationStatus.SUCCEEDED
                    stop_reason = VerificationStopReason.SUCCESS
                    error_message = None
                    break

                error_message = f"Verification command failed: {candidate.command}"
                if continue_running:
                    continue
                if reflected_stop_reason is not None:
                    stop_reason = reflected_stop_reason
                break
        except Exception as exc:
            error_message = str(exc)
            return self._finalize(
                created_at=created_at,
                verification_id=verification_id,
                linked_run_id=linked_run_id,
                linked_execution_id=linked_execution_id,
                repo_root=repo_root,
                summary=summary,
                receipt_path=receipt_path,
                attempts=attempts,
                skipped_commands=skipped_commands,
                status=VerificationStatus.FAILED,
                stop_reason=VerificationStopReason.EXECUTION_ERROR,
                error_message=error_message,
            )

        if final_status != VerificationStatus.SUCCEEDED and len(attempts) >= max_attempts:
            stop_reason = VerificationStopReason.MAX_ATTEMPTS_REACHED

        return self._finalize(
            created_at=created_at,
            verification_id=verification_id,
            linked_run_id=linked_run_id,
            linked_execution_id=linked_execution_id,
            repo_root=repo_root,
            summary=summary,
            receipt_path=receipt_path,
            attempts=attempts,
            skipped_commands=skipped_commands,
            status=final_status,
            stop_reason=stop_reason,
            error_message=error_message,
        )

    def _load_context(
        self,
        *,
        run_id: str | None,
        execution_id: str | None,
    ) -> tuple[str | None, str | None, dict[str, object]]:
        if execution_id:
            execution = self._run_repository.get_execution(execution_id)
            if execution is None:
                raise ValueError(f"Execution not found: {execution_id}")
            _, execution_payload = execution
            linked_run_id = execution_payload.get("linked_run_id")
            if not isinstance(linked_run_id, str) or not linked_run_id:
                raise ValueError("Execution is not linked to a planning run, so no verification strategy is available.")
            run = self._run_repository.get_run(linked_run_id)
            if run is None:
                raise ValueError(f"Linked run not found: {linked_run_id}")
            _, run_payload = run
            return linked_run_id, execution_id, run_payload

        run = self._run_repository.get_run(run_id or "")
        if run is None:
            raise ValueError(f"Run not found: {run_id}")
        _, run_payload = run
        return run_id, None, run_payload

    def _receipt_path(self, *, artifact_dir: Path, verification_id: str, linked_run_id: str | None) -> Path:
        if linked_run_id:
            run = self._run_repository.get_run(linked_run_id)
            if run is None:
                raise ValueError(f"Linked run not found: {linked_run_id}")
            record, _ = run
            return record.audit_path.parent / "verification" / f"{verification_id}.json"
        return artifact_dir / "verification" / f"{verification_id}.json"

    def _finalize(
        self,
        *,
        created_at: str,
        verification_id: str,
        linked_run_id: str | None,
        linked_execution_id: str | None,
        repo_root: Path,
        summary: str,
        receipt_path: Path,
        attempts: list[VerificationAttempt],
        skipped_commands: list[CommandAssessment],
        status: VerificationStatus,
        stop_reason: VerificationStopReason,
        error_message: str | None,
    ) -> VerificationResult:
        receipt = VerificationReceipt(
            verification_id=verification_id,
            linked_run_id=linked_run_id,
            linked_execution_id=linked_execution_id,
            status=status,
            stop_reason=stop_reason,
            repo_root=repo_root,
            summary=summary,
            attempts=attempts,
            skipped_commands=skipped_commands,
            error_message=error_message,
        )
        payload = _receipt_payload(created_at, receipt)
        receipt_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        record = VerificationRecord(
            verification_id=verification_id,
            created_at=created_at,
            linked_run_id=linked_run_id,
            linked_execution_id=linked_execution_id,
            status=status,
            stop_reason=stop_reason,
            summary=summary,
            repo_root=repo_root,
            receipt_path=receipt_path,
            error_message=error_message,
        )
        self._run_repository.save_verification(record, payload)
        return VerificationResult(
            verification_id=verification_id,
            linked_run_id=linked_run_id,
            linked_execution_id=linked_execution_id,
            receipt=receipt,
            receipt_path=receipt_path,
        )


def _resolve_summary(payload: dict[str, object]) -> str:
    plan = payload.get("plan", {})
    if isinstance(plan, dict):
        summary = plan.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary
    return "Verification run"


def _receipt_payload(created_at: str, receipt: VerificationReceipt) -> dict[str, object]:
    return {
        "verification_id": receipt.verification_id,
        "created_at": created_at,
        "linked_run_id": receipt.linked_run_id,
        "linked_execution_id": receipt.linked_execution_id,
        "status": receipt.status.value,
        "stop_reason": receipt.stop_reason.value,
        "repo_root": str(receipt.repo_root),
        "summary": receipt.summary,
        "attempts": [
            {
                "attempt_index": item.attempt_index,
                "command": item.command,
                "source": item.source,
                "status": item.status.value,
                "exit_code": item.exit_code,
                "duration_ms": item.duration_ms,
                "stdout_path": None if item.stdout_path is None else str(item.stdout_path),
                "stderr_path": None if item.stderr_path is None else str(item.stderr_path),
                "note": item.note,
            }
            for item in receipt.attempts
        ],
        "skipped_commands": [
            {
                "command": item.command,
                "decision": item.decision.value,
                "reason": item.reason,
            }
            for item in receipt.skipped_commands
        ],
        "error_message": receipt.error_message,
    }
