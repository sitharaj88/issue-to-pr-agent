from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from ...domain.entities import FileMutationReceipt, PatchExecutionMode, PatchOperation, PatchOperationType
from ...shared.exceptions import ExecutionError


class LocalWorkspaceMutator:
    def apply_operation(
        self,
        *,
        operation_index: int,
        mode: PatchExecutionMode,
        target_path: Path,
        operation: PatchOperation,
    ) -> FileMutationReceipt:
        before_exists = target_path.exists()
        before_text = self._read_text(target_path) if before_exists else ""
        before_bytes = len(before_text.encode("utf-8")) if before_exists else 0
        before_sha = _digest(before_text) if before_exists else None

        after_text, detail = self._compute_after_text(before_text, operation, before_exists=before_exists)
        after_bytes = len(after_text.encode("utf-8"))
        after_sha = _digest(after_text)
        changed = before_text != after_text

        if mode == PatchExecutionMode.APPLY and changed:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(after_text, encoding="utf-8")

        return FileMutationReceipt(
            operation_index=operation_index,
            operation_type=operation.type,
            path=str(target_path),
            changed=changed,
            before_sha256=before_sha,
            after_sha256=after_sha,
            before_bytes=before_bytes,
            after_bytes=after_bytes,
            detail=detail,
        )

    def _compute_after_text(
        self,
        before_text: str,
        operation: PatchOperation,
        *,
        before_exists: bool,
    ) -> tuple[str, str]:
        if operation.type == PatchOperationType.WRITE_FILE:
            detail = "create file" if not before_exists else "overwrite file"
            return operation.content, detail
        if operation.type == PatchOperationType.APPEND_TEXT:
            detail = "append text"
            return before_text + operation.content, detail
        if operation.type == PatchOperationType.REPLACE_TEXT:
            occurrences = before_text.count(operation.find_text)
            if occurrences == 0:
                raise ExecutionError(f"Text to replace was not found in {operation.path}")
            if occurrences > 1:
                raise ExecutionError(
                    f"Text replacement is ambiguous in {operation.path}; found {occurrences} occurrences"
                )
            detail = "replace text"
            return before_text.replace(operation.find_text, operation.replace_text, 1), detail
        raise ExecutionError(f"Unsupported patch operation: {operation.type.value}")

    def _read_text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ExecutionError(f"Binary or non-UTF8 files are not supported: {path}") from exc


def _digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()
