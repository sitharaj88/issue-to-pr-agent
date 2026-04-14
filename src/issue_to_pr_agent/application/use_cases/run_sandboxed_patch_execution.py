from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path

from .execute_patch_proposal import ExecutePatchProposalUseCase, PatchExecutionFailedError, PatchExecutionResult
from .manage_sandbox import ManageSandboxUseCase, SandboxResult
from ...domain.entities import PatchExecutionMode, PatchProposal


@dataclass(frozen=True)
class SandboxedPatchExecutionResult:
    sandbox: SandboxResult
    execution: PatchExecutionResult


class SandboxedPatchExecutionFailedError(RuntimeError):
    def __init__(self, result: SandboxedPatchExecutionResult) -> None:
        self.result = result
        super().__init__(result.execution.receipt.error_message or "Sandboxed patch execution failed.")


class RunSandboxedPatchExecutionUseCase:
    def __init__(
        self,
        sandboxes: ManageSandboxUseCase,
        executor: ExecutePatchProposalUseCase,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._sandboxes = sandboxes
        self._executor = executor
        self._logger = logger or logging.getLogger(__name__)

    def run(
        self,
        *,
        proposal: PatchProposal,
        source_repo_root: Path,
        artifact_dir: Path,
        sandbox_dir: Path,
        mode: PatchExecutionMode,
    ) -> SandboxedPatchExecutionResult:
        sandbox = self._sandboxes.prepare(
            repo_root=source_repo_root,
            sandbox_dir=sandbox_dir,
            artifact_dir=artifact_dir,
            linked_run_id=proposal.linked_run_id,
            summary="Sandbox prepared for isolated patch execution.",
        )
        try:
            execution = self._executor.execute(
                proposal=proposal,
                repo_root=sandbox.receipt.workspace_root,
                artifact_dir=artifact_dir,
                mode=mode,
            )
        except PatchExecutionFailedError as exc:
            sandbox = self._sandboxes.mark_used(
                sandbox_id=sandbox.sandbox_id,
                linked_execution_id=exc.result.execution_id,
                summary=f"Sandbox used by failed execution {exc.result.execution_id}.",
            )
            self._logger.warning(
                "Sandboxed patch execution failed",
                extra={"run_id": proposal.linked_run_id, "execution_id": exc.result.execution_id},
            )
            raise SandboxedPatchExecutionFailedError(
                SandboxedPatchExecutionResult(sandbox=sandbox, execution=exc.result)
            ) from exc

        sandbox = self._sandboxes.mark_used(
            sandbox_id=sandbox.sandbox_id,
            linked_execution_id=execution.execution_id,
            summary=f"Sandbox used by execution {execution.execution_id}.",
        )
        return SandboxedPatchExecutionResult(sandbox=sandbox, execution=execution)
