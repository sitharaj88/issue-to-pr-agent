from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import zipfile

from ...infrastructure.persistence.run_repository import RunRepository


@dataclass(frozen=True)
class AuditExportResult:
    export_id: str
    run_id: str
    created_at: str
    bundle_path: Path
    manifest_path: Path
    archive_path: Path


class RunAuditExporter:
    def __init__(self, repository: RunRepository) -> None:
        self._repository = repository

    def export_run(self, *, run_id: str, output_dir: Path) -> AuditExportResult:
        run = self._repository.get_run(run_id)
        if run is None:
            raise ValueError(f"Run not found: {run_id}")
        run_record, run_payload = run
        created_at = datetime.now(timezone.utc).isoformat()
        export_id = hashlib.sha256(f"{run_id}\0{created_at}".encode("utf-8")).hexdigest()[:12]
        destination = output_dir / run_id / export_id
        destination.mkdir(parents=True, exist_ok=True)

        bundle = {
            "export_id": export_id,
            "created_at": created_at,
            "run": {"record": _record_to_dict(run_record), "payload": run_payload},
            "patch_proposals": self._collect_payloads(
                self._repository.list_patch_proposals(limit=1000),
                lambda item: item.linked_run_id == run_id,
                self._repository.get_patch_proposal,
                lambda item: item.proposal_id,
            ),
            "executions": self._collect_payloads(
                self._repository.list_executions(limit=1000),
                lambda item: item.linked_run_id == run_id,
                self._repository.get_execution,
                lambda item: item.execution_id,
            ),
            "verifications": self._collect_payloads(
                self._repository.list_verifications(limit=1000),
                lambda item: item.linked_run_id == run_id,
                self._repository.get_verification,
                lambda item: item.verification_id,
            ),
            "deliveries": self._collect_payloads(
                self._repository.list_deliveries(limit=1000),
                lambda item: item.linked_run_id == run_id,
                self._repository.get_delivery,
                lambda item: item.delivery_id,
            ),
            "approvals": self._collect_payloads(
                self._repository.list_approvals(limit=1000),
                lambda item: item.linked_run_id == run_id,
                self._repository.get_approval,
                lambda item: item.approval_id,
            ),
            "autofix_runs": self._collect_payloads(
                self._repository.list_autofix_runs(limit=1000),
                lambda item: item.linked_run_id == run_id,
                self._repository.get_autofix_run,
                lambda item: item.autofix_id,
            ),
            "sandboxes": self._collect_payloads(
                self._repository.list_sandboxes(limit=1000),
                lambda item: item.linked_run_id == run_id,
                self._repository.get_sandbox,
                lambda item: item.sandbox_id,
            ),
            "queue_jobs": [
                _record_to_dict(item)
                for item in self._repository.list_queue_jobs(limit=1000)
                if item.linked_run_id == run_id
            ],
            "trace_events": [
                _record_to_dict(item)
                for item in self._repository.list_trace_events(linked_run_id=run_id, limit=1000)
            ],
        }

        manifest = {
            "export_id": export_id,
            "run_id": run_id,
            "created_at": created_at,
            "artifacts": [_artifact_manifest(path) for path in _extract_existing_paths(bundle)],
        }

        bundle_path = destination / "bundle.json"
        manifest_path = destination / "manifest.json"
        archive_path = destination / "audit-export.zip"
        bundle_path.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(bundle_path, arcname="bundle.json")
            archive.write(manifest_path, arcname="manifest.json")
        return AuditExportResult(
            export_id=export_id,
            run_id=run_id,
            created_at=created_at,
            bundle_path=bundle_path,
            manifest_path=manifest_path,
            archive_path=archive_path,
        )

    def _collect_payloads(self, items, predicate, loader, key_getter):
        payloads: list[dict[str, object]] = []
        for item in items:
            if not predicate(item):
                continue
            loaded = loader(key_getter(item))
            if loaded is None:
                continue
            record, payload = loaded
            payloads.append({"record": _record_to_dict(record), "payload": payload})
        return payloads


def _record_to_dict(record: object) -> dict[str, object]:
    data = {}
    for key, value in getattr(record, "__dict__", {}).items():
        if isinstance(value, Path):
            data[key] = str(value)
        else:
            data[key] = value
    return data


def _extract_existing_paths(value: object) -> list[Path]:
    paths: list[Path] = []
    if isinstance(value, dict):
        for item in value.values():
            paths.extend(_extract_existing_paths(item))
        return paths
    if isinstance(value, list):
        for item in value:
            paths.extend(_extract_existing_paths(item))
        return paths
    if isinstance(value, str):
        path = Path(value)
        if path.is_absolute() and path.exists() and path.is_file():
            return [path]
    return paths


def _artifact_manifest(path: Path) -> dict[str, object]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": digest,
    }
