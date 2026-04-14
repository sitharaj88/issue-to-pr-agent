from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from uuid import uuid4

from ...domain.entities import SandboxReceipt, SandboxRecord, SandboxStatus
from ...infrastructure.persistence.run_repository import RunRepository
from ...infrastructure.sandbox import LocalSandboxManager


@dataclass(frozen=True)
class SandboxResult:
    sandbox_id: str
    receipt: SandboxReceipt
    receipt_path: Path


class ManageSandboxUseCase:
    def __init__(
        self,
        run_repository: RunRepository,
        sandbox_manager: LocalSandboxManager,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._run_repository = run_repository
        self._sandbox_manager = sandbox_manager
        self._logger = logger or logging.getLogger(__name__)

    def prepare(
        self,
        *,
        repo_root: Path,
        sandbox_dir: Path,
        artifact_dir: Path,
        linked_run_id: str | None = None,
        summary: str | None = None,
    ) -> SandboxResult:
        sandbox_id = uuid4().hex[:12]
        created_at = _now()
        sandbox_root = sandbox_dir / sandbox_id
        receipt_path = sandbox_root / "sandbox.json"
        workspace_root = sandbox_root / "workspace"
        sandbox_root.mkdir(parents=True, exist_ok=True)

        try:
            materialization = self._sandbox_manager.prepare(
                source_repo_root=repo_root,
                workspace_root=workspace_root,
                artifact_dir=artifact_dir,
            )
            receipt = SandboxReceipt(
                sandbox_id=sandbox_id,
                linked_run_id=linked_run_id,
                linked_autofix_id=None,
                linked_execution_id=None,
                linked_delivery_id=None,
                status=SandboxStatus.PREPARED,
                source_repo_root=repo_root.resolve(),
                workspace_root=materialization.workspace_root,
                copied_file_count=materialization.copied_file_count,
                skipped_entry_count=materialization.skipped_entry_count,
                total_bytes=materialization.total_bytes,
                materialization_strategy=materialization.materialization_strategy,
                source_branch=materialization.source_branch,
                source_head_sha=materialization.source_head_sha,
                skipped_entries=materialization.skipped_entries,
                summary=summary
                or (
                    "Sandbox prepared with "
                    f"{materialization.copied_file_count} copied file(s) using "
                    f"{materialization.materialization_strategy}."
                ),
            )
            self._save(
                created_at=created_at,
                updated_at=created_at,
                receipt=receipt,
                receipt_path=receipt_path,
            )
            return SandboxResult(sandbox_id=sandbox_id, receipt=receipt, receipt_path=receipt_path)
        except Exception as exc:
            receipt = SandboxReceipt(
                sandbox_id=sandbox_id,
                linked_run_id=linked_run_id,
                linked_autofix_id=None,
                linked_execution_id=None,
                linked_delivery_id=None,
                status=SandboxStatus.FAILED,
                source_repo_root=repo_root.resolve(),
                workspace_root=workspace_root,
                copied_file_count=0,
                skipped_entry_count=0,
                total_bytes=0,
                skipped_entries=[],
                summary=summary or "Sandbox preparation failed.",
                error_message=str(exc),
            )
            self._save(
                created_at=created_at,
                updated_at=created_at,
                receipt=receipt,
                receipt_path=receipt_path,
            )
            self._logger.exception("Sandbox preparation failed", extra={"sandbox_id": sandbox_id})
            raise

    def mark_used(
        self,
        *,
        sandbox_id: str,
        linked_autofix_id: str | None = None,
        linked_execution_id: str | None = None,
        linked_delivery_id: str | None = None,
        summary: str,
    ) -> SandboxResult:
        record_payload = self._run_repository.get_sandbox(sandbox_id)
        if record_payload is None:
            raise ValueError(f"Sandbox not found: {sandbox_id}")
        record, payload = record_payload
        receipt = SandboxReceipt(
            sandbox_id=record.sandbox_id,
            linked_run_id=record.linked_run_id,
            linked_autofix_id=linked_autofix_id or _optional_string(payload.get("linked_autofix_id")),
            linked_execution_id=linked_execution_id or _optional_string(payload.get("linked_execution_id")),
            linked_delivery_id=linked_delivery_id or _optional_string(payload.get("linked_delivery_id")),
            status=SandboxStatus.USED,
            source_repo_root=record.source_repo_root,
            workspace_root=record.workspace_root,
            copied_file_count=record.copied_file_count,
            skipped_entry_count=record.skipped_entry_count,
            total_bytes=record.total_bytes,
            materialization_strategy=_optional_string(payload.get("materialization_strategy")) or "copy",
            source_branch=_optional_string(payload.get("source_branch")),
            source_head_sha=_optional_string(payload.get("source_head_sha")),
            skipped_entries=_string_list(payload.get("skipped_entries")),
            summary=summary,
        )
        updated_at = _now()
        self._save(
            created_at=record.created_at,
            updated_at=updated_at,
            receipt=receipt,
            receipt_path=record.receipt_path,
        )
        return SandboxResult(sandbox_id=sandbox_id, receipt=receipt, receipt_path=record.receipt_path)

    def cleanup(self, *, sandbox_id: str, remove_workspace: bool = True) -> SandboxResult:
        record_payload = self._run_repository.get_sandbox(sandbox_id)
        if record_payload is None:
            raise ValueError(f"Sandbox not found: {sandbox_id}")
        record, payload = record_payload
        if remove_workspace:
            self._sandbox_manager.cleanup(workspace_root=record.workspace_root)
        receipt = SandboxReceipt(
            sandbox_id=record.sandbox_id,
            linked_run_id=record.linked_run_id,
            linked_autofix_id=record.linked_autofix_id,
            linked_execution_id=_optional_string(payload.get("linked_execution_id")),
            linked_delivery_id=_optional_string(payload.get("linked_delivery_id")),
            status=SandboxStatus.CLEANED_UP,
            source_repo_root=record.source_repo_root,
            workspace_root=record.workspace_root,
            copied_file_count=record.copied_file_count,
            skipped_entry_count=record.skipped_entry_count,
            total_bytes=record.total_bytes,
            materialization_strategy=_optional_string(payload.get("materialization_strategy")) or "copy",
            source_branch=_optional_string(payload.get("source_branch")),
            source_head_sha=_optional_string(payload.get("source_head_sha")),
            skipped_entries=_string_list(payload.get("skipped_entries")),
            summary="Sandbox cleaned up." if remove_workspace else "Sandbox marked as cleaned up.",
        )
        updated_at = _now()
        self._save(
            created_at=record.created_at,
            updated_at=updated_at,
            receipt=receipt,
            receipt_path=record.receipt_path,
        )
        return SandboxResult(sandbox_id=sandbox_id, receipt=receipt, receipt_path=record.receipt_path)

    def _save(
        self,
        *,
        created_at: str,
        updated_at: str,
        receipt: SandboxReceipt,
        receipt_path: Path,
    ) -> None:
        payload = {
            "sandbox_id": receipt.sandbox_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "linked_run_id": receipt.linked_run_id,
            "linked_autofix_id": receipt.linked_autofix_id,
            "linked_execution_id": receipt.linked_execution_id,
            "linked_delivery_id": receipt.linked_delivery_id,
            "status": receipt.status.value,
            "source_repo_root": str(receipt.source_repo_root),
            "workspace_root": str(receipt.workspace_root),
            "copied_file_count": receipt.copied_file_count,
            "skipped_entry_count": receipt.skipped_entry_count,
            "total_bytes": receipt.total_bytes,
            "materialization_strategy": receipt.materialization_strategy,
            "source_branch": receipt.source_branch,
            "source_head_sha": receipt.source_head_sha,
            "skipped_entries": receipt.skipped_entries,
            "summary": receipt.summary,
            "error_message": receipt.error_message,
        }
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._run_repository.save_sandbox(
            SandboxRecord(
                sandbox_id=receipt.sandbox_id,
                created_at=created_at,
                updated_at=updated_at,
                linked_run_id=receipt.linked_run_id,
                linked_autofix_id=receipt.linked_autofix_id,
                status=receipt.status,
                source_repo_root=receipt.source_repo_root,
                workspace_root=receipt.workspace_root,
                copied_file_count=receipt.copied_file_count,
                skipped_entry_count=receipt.skipped_entry_count,
                total_bytes=receipt.total_bytes,
                summary=receipt.summary,
                receipt_path=receipt_path,
                error_message=receipt.error_message,
            ),
            payload,
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
