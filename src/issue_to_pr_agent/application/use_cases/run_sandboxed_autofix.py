from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path

from .manage_sandbox import ManageSandboxUseCase, SandboxResult
from .run_autofix import AutofixRunResult, RunAutofixUseCase


@dataclass(frozen=True)
class SandboxedAutofixResult:
    sandbox: SandboxResult
    autofix: AutofixRunResult


class RunSandboxedAutofixUseCase:
    def __init__(
        self,
        sandboxes: ManageSandboxUseCase,
        autofix: RunAutofixUseCase,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._sandboxes = sandboxes
        self._autofix = autofix
        self._logger = logger or logging.getLogger(__name__)

    def run(
        self,
        *,
        run_id: str,
        source_repo_root: Path,
        artifact_dir: Path,
        sandbox_dir: Path,
        max_attempts: int = 3,
        verify_max_attempts: int = 3,
        timeout_seconds: int = 120,
        objective: str | None = None,
    ) -> SandboxedAutofixResult:
        sandbox = self._sandboxes.prepare(
            repo_root=source_repo_root,
            sandbox_dir=sandbox_dir,
            artifact_dir=artifact_dir,
            linked_run_id=run_id,
            summary="Sandbox prepared for isolated autofix execution.",
        )
        autofix = self._autofix.run(
            run_id=run_id,
            repo_root=sandbox.receipt.workspace_root,
            artifact_dir=artifact_dir,
            max_attempts=max_attempts,
            verify_max_attempts=verify_max_attempts,
            timeout_seconds=timeout_seconds,
            objective=objective,
        )
        sandbox = self._sandboxes.mark_used(
            sandbox_id=sandbox.sandbox_id,
            linked_autofix_id=autofix.autofix_id,
            summary=f"Sandbox used by autofix {autofix.autofix_id}.",
        )
        return SandboxedAutofixResult(sandbox=sandbox, autofix=autofix)
