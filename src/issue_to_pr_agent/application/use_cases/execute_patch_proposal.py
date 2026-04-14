from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from uuid import uuid4

from ...domain.entities import (
    PatchExecutionMode,
    PatchExecutionReceipt,
    PatchExecutionRecord,
    PatchExecutionStatus,
    PatchProposal,
)
from ...domain.policies.workspace import WorkspaceGuardrails
from ...infrastructure.persistence.run_repository import RunRepository
from ...infrastructure.workspace.mutator import LocalWorkspaceMutator


@dataclass(frozen=True)
class PatchExecutionResult:
    execution_id: str
    linked_run_id: str | None
    mode: PatchExecutionMode
    receipt: PatchExecutionReceipt
    receipt_path: Path


class PatchExecutionFailedError(RuntimeError):
    def __init__(self, result: PatchExecutionResult) -> None:
        self.result = result
        super().__init__(result.receipt.error_message or "Patch execution failed.")


class ExecutePatchProposalUseCase:
    def __init__(
        self,
        run_repository: RunRepository,
        *,
        guardrails: WorkspaceGuardrails | None = None,
        mutator: LocalWorkspaceMutator | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._run_repository = run_repository
        self._guardrails = guardrails or WorkspaceGuardrails()
        self._mutator = mutator or LocalWorkspaceMutator()
        self._logger = logger or logging.getLogger(__name__)

    def execute(
        self,
        *,
        proposal: PatchProposal,
        repo_root: Path,
        artifact_dir: Path,
        mode: PatchExecutionMode,
    ) -> PatchExecutionResult:
        execution_id = uuid4().hex[:12]
        created_at = datetime.now(timezone.utc).isoformat()
        receipt_path = self._receipt_path(
            artifact_dir=artifact_dir,
            linked_run_id=proposal.linked_run_id,
            execution_id=execution_id,
        )
        receipt_path.parent.mkdir(parents=True, exist_ok=True)

        if not proposal.operations:
            raise ValueError("Patch proposal must contain at least one operation.")

        allowed_existing_paths = self._allowed_existing_paths(proposal.linked_run_id)
        blocked_paths: list[str] = []
        mutation_receipts = []

        try:
            self._logger.info(
                "Starting patch execution",
                extra={"run_id": proposal.linked_run_id, "provider": "patch_executor"},
            )
            for index, operation in enumerate(proposal.operations):
                target_path = self._guardrails.validate_operation(
                    repo_root,
                    operation,
                    allowed_existing_paths=allowed_existing_paths,
                )
                if (
                    allowed_existing_paths
                    and not target_path.exists()
                    and not self._is_allowed_new_file(target_path, repo_root, allowed_existing_paths)
                ):
                    blocked_paths.append(operation.path)
                    raise ValueError(
                        f"New file path is outside allowed directories for linked run: {operation.path}"
                    )
                mutation_receipts.append(
                    self._mutator.apply_operation(
                        operation_index=index,
                        mode=mode,
                        target_path=target_path,
                        operation=operation,
                    )
                )

            receipt = PatchExecutionReceipt(
                execution_id=execution_id,
                proposal_id=proposal.proposal_id,
                linked_run_id=proposal.linked_run_id,
                mode=mode,
                status=PatchExecutionStatus.SUCCEEDED,
                repo_root=repo_root,
                summary=proposal.summary,
                receipts=mutation_receipts,
                blocked_paths=blocked_paths,
            )
            payload = _receipt_payload(created_at, receipt)
            receipt_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            record = PatchExecutionRecord(
                execution_id=execution_id,
                created_at=created_at,
                proposal_id=proposal.proposal_id,
                linked_run_id=proposal.linked_run_id,
                mode=mode,
                status=PatchExecutionStatus.SUCCEEDED,
                summary=proposal.summary,
                repo_root=repo_root,
                receipt_path=receipt_path,
            )
            self._run_repository.save_execution(record, payload)
            return PatchExecutionResult(
                execution_id=execution_id,
                linked_run_id=proposal.linked_run_id,
                mode=mode,
                receipt=receipt,
                receipt_path=receipt_path,
            )
        except Exception as exc:
            receipt = PatchExecutionReceipt(
                execution_id=execution_id,
                proposal_id=proposal.proposal_id,
                linked_run_id=proposal.linked_run_id,
                mode=mode,
                status=PatchExecutionStatus.FAILED,
                repo_root=repo_root,
                summary=proposal.summary,
                receipts=mutation_receipts,
                blocked_paths=blocked_paths,
                error_message=str(exc),
            )
            payload = _receipt_payload(created_at, receipt)
            receipt_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            record = PatchExecutionRecord(
                execution_id=execution_id,
                created_at=created_at,
                proposal_id=proposal.proposal_id,
                linked_run_id=proposal.linked_run_id,
                mode=mode,
                status=PatchExecutionStatus.FAILED,
                summary=proposal.summary,
                repo_root=repo_root,
                receipt_path=receipt_path,
                error_message=str(exc),
            )
            self._run_repository.save_execution(record, payload)
            self._logger.exception(
                "Patch execution failed",
                extra={"run_id": proposal.linked_run_id, "provider": "patch_executor"},
            )
            result = PatchExecutionResult(
                execution_id=execution_id,
                linked_run_id=proposal.linked_run_id,
                mode=mode,
                receipt=receipt,
                receipt_path=receipt_path,
            )
            raise PatchExecutionFailedError(result) from exc

    def _allowed_existing_paths(self, linked_run_id: str | None) -> set[str] | None:
        if not linked_run_id:
            return None
        result = self._run_repository.get_run(linked_run_id)
        if result is None:
            raise ValueError(f"Linked run not found: {linked_run_id}")
        _, payload = result
        plan = payload.get("plan", {})
        context = payload.get("planning_context", {})
        allowed_paths: list[str] = []
        if isinstance(plan, dict):
            allowed_paths.extend(
                path for path in plan.get("files_to_inspect", []) if isinstance(path, str)
            )
        if isinstance(context, dict):
            for item in context.get("ranked_files", []):
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    allowed_paths.append(item["path"])
        return set(allowed_paths)

    def _is_allowed_new_file(self, target_path: Path, repo_root: Path, allowed_existing_paths: set[str]) -> bool:
        relative = str(target_path.resolve().relative_to(repo_root.resolve()))
        allowed_dirs = {
            str((repo_root / path).resolve().relative_to(repo_root.resolve()).parent)
            for path in allowed_existing_paths
        }
        allowed_dirs.discard(".")
        allowed_dirs.add("tests")
        parent = str(Path(relative).parent)
        return parent in allowed_dirs

    def _receipt_path(self, *, artifact_dir: Path, linked_run_id: str | None, execution_id: str) -> Path:
        if linked_run_id:
            run = self._run_repository.get_run(linked_run_id)
            if run is None:
                raise ValueError(f"Linked run not found: {linked_run_id}")
            record, _ = run
            return record.audit_path.parent / "executions" / f"{execution_id}.json"
        return artifact_dir / "executions" / f"{execution_id}.json"


def _receipt_payload(created_at: str, receipt: PatchExecutionReceipt) -> dict[str, object]:
    return {
        "execution_id": receipt.execution_id,
        "created_at": created_at,
        "proposal_id": receipt.proposal_id,
        "linked_run_id": receipt.linked_run_id,
        "mode": receipt.mode.value,
        "status": receipt.status.value,
        "repo_root": str(receipt.repo_root),
        "summary": receipt.summary,
        "blocked_paths": receipt.blocked_paths,
        "receipts": [
            {
                "operation_index": item.operation_index,
                "operation_type": item.operation_type.value,
                "path": item.path,
                "changed": item.changed,
                "before_sha256": item.before_sha256,
                "after_sha256": item.after_sha256,
                "before_bytes": item.before_bytes,
                "after_bytes": item.after_bytes,
                "detail": item.detail,
            }
            for item in receipt.receipts
        ],
        "error_message": receipt.error_message,
    }
