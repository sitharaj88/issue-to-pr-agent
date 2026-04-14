from __future__ import annotations

from pathlib import Path

from ...domain.entities import PatchOperation, PatchOperationType
from ...shared.exceptions import ExecutionError


class WorkspaceGuardrails:
    def __init__(
        self,
        *,
        blocked_roots: tuple[str, ...] = (".git", ".issue-to-pr", "__pycache__"),
        max_file_bytes: int = 512_000,
    ) -> None:
        self._blocked_roots = blocked_roots
        self._max_file_bytes = max_file_bytes

    def resolve_path(self, repo_root: Path, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise ExecutionError(f"Absolute paths are not allowed: {relative_path}")
        normalized = (repo_root / candidate).resolve()
        try:
            normalized.relative_to(repo_root.resolve())
        except ValueError as exc:
            raise ExecutionError(f"Path escapes repository root: {relative_path}") from exc
        if any(part in self._blocked_roots for part in candidate.parts):
            raise ExecutionError(f"Path is inside a blocked directory: {relative_path}")
        return normalized

    def validate_operation(
        self,
        repo_root: Path,
        operation: PatchOperation,
        *,
        allowed_existing_paths: set[str] | None = None,
    ) -> Path:
        if not operation.path:
            raise ExecutionError("Patch operations must include a relative path.")
        resolved = self.resolve_path(repo_root, operation.path)
        relative = str(resolved.relative_to(repo_root.resolve()))

        if allowed_existing_paths and resolved.exists() and relative not in allowed_existing_paths:
            raise ExecutionError(
                f"Operation targets a file outside the allowed execution set: {operation.path}"
            )

        if operation.type == PatchOperationType.REPLACE_TEXT:
            if not resolved.exists():
                raise ExecutionError(f"replace_text target does not exist: {operation.path}")
            if not operation.find_text:
                raise ExecutionError(f"replace_text requires find_text: {operation.path}")
        elif operation.type == PatchOperationType.APPEND_TEXT:
            if not resolved.exists():
                raise ExecutionError(f"append_text target does not exist: {operation.path}")
            if not operation.content:
                raise ExecutionError(f"append_text requires content: {operation.path}")
        elif operation.type == PatchOperationType.WRITE_FILE:
            if resolved.exists() and not operation.allow_overwrite:
                raise ExecutionError(
                    f"write_file would overwrite an existing file without allow_overwrite: {operation.path}"
                )
        else:
            raise ExecutionError(f"Unsupported patch operation: {operation.type.value}")

        if resolved.exists():
            self.ensure_file_is_safe(resolved)
        return resolved

    def ensure_file_is_safe(self, path: Path) -> None:
        if path.is_symlink():
            raise ExecutionError(f"Symlinks are not supported for patch execution: {path}")
        if path.stat().st_size > self._max_file_bytes:
            raise ExecutionError(f"File exceeds the execution size limit: {path}")
