from __future__ import annotations

from pathlib import Path

from ...domain.entities import PatchProposal
from ..use_cases.execute_patch_proposal import PatchExecutionResult
from ..use_cases.verify_run import VerificationResult


class PatchReflectionService:
    def build_retry_objective(
        self,
        *,
        next_attempt_index: int,
        base_objective: str | None,
        proposal: PatchProposal | None,
        execution_result: PatchExecutionResult | None,
        verification_result: VerificationResult | None,
        error_message: str | None,
    ) -> str:
        lines = [
            f"Autofix retry attempt {next_attempt_index}. Keep any correct existing edits and make the smallest additional change needed.",
            "Do not revert successful prior changes unless they directly caused the failure.",
        ]
        if base_objective:
            lines.append(f"Original objective: {base_objective}")
        if proposal is not None:
            lines.append(f"Previous proposal summary: {proposal.summary}")
            if proposal.rationale:
                lines.append(f"Previous proposal rationale: {proposal.rationale}")
        if execution_result is not None:
            lines.append(f"Previous execution status: {execution_result.receipt.status.value}")
            changed_paths = sorted({item.path for item in execution_result.receipt.receipts if item.changed})
            if changed_paths:
                lines.append(f"Files already changed: {', '.join(changed_paths)}")
            if execution_result.receipt.error_message:
                lines.append(f"Execution error: {execution_result.receipt.error_message}")
        if verification_result is not None:
            lines.append(
                f"Verification stop reason: {verification_result.receipt.stop_reason.value}"
            )
            failed_attempt = next(
                (
                    item
                    for item in reversed(verification_result.receipt.attempts)
                    if item.exit_code not in (None, 0)
                ),
                None,
            )
            if failed_attempt is not None:
                lines.append(f"Last failing command: {failed_attempt.command}")
                stderr_excerpt = _tail_text(failed_attempt.stderr_path)
                stdout_excerpt = _tail_text(failed_attempt.stdout_path)
                if stderr_excerpt:
                    lines.append("stderr excerpt:")
                    lines.append(stderr_excerpt)
                elif stdout_excerpt:
                    lines.append("stdout excerpt:")
                    lines.append(stdout_excerpt)
        if error_message:
            lines.append(f"Failure summary: {error_message}")
        return "\n".join(lines).strip()


def _tail_text(path: Path | None, *, max_chars: int = 1200) -> str:
    if path is None or not path.exists() or not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]
