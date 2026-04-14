from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from ...infrastructure.config.settings import Settings
from ...infrastructure.persistence.run_repository import RunRepository


@dataclass(frozen=True)
class SchemaStatusResult:
    current_version: int
    migrations: list[dict[str, object]]


@dataclass(frozen=True)
class BackupStateResult:
    backup_dir: Path
    manifest_path: Path
    archive_path: Path


@dataclass(frozen=True)
class RestoreStateResult:
    target_dir: Path
    restored_database_path: Path
    restored_artifact_dir: Path
    manifest_path: Path


@dataclass(frozen=True)
class ReleaseManifestResult:
    manifest_path: Path
    manifest: dict[str, object]


class ManageReleaseUseCase:
    def __init__(self, repository: RunRepository, settings: Settings) -> None:
        self._repository = repository
        self._settings = settings

    def schema_status(self) -> SchemaStatusResult:
        migrations = [
            {"version": item.version, "name": item.name, "applied_at": item.applied_at}
            for item in self._repository.list_schema_migrations()
        ]
        return SchemaStatusResult(
            current_version=self._repository.current_schema_version(),
            migrations=migrations,
        )

    def backup_state(self, *, output_dir: Path, include_artifacts: bool = True) -> BackupStateResult:
        created_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = output_dir.resolve() / f"backup-{created_at}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        restored_db = backup_dir / self._settings.database_path.name
        shutil.copy2(self._settings.database_path, restored_db)

        artifacts_target = backup_dir / "artifacts"
        artifact_included = False
        if include_artifacts and self._settings.artifact_dir.exists():
            shutil.copytree(
                self._settings.artifact_dir,
                artifacts_target,
                dirs_exist_ok=True,
            )
            artifact_included = True

        manifest = {
            "created_at": created_at,
            "database_path": str(restored_db),
            "schema_version": self._repository.current_schema_version(),
            "artifact_dir": str(artifacts_target),
            "artifact_included": artifact_included,
            "source_database_path": str(self._settings.database_path),
            "source_artifact_dir": str(self._settings.artifact_dir),
        }
        manifest_path = backup_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        archive_base = backup_dir.parent / backup_dir.name
        archive_path = Path(shutil.make_archive(str(archive_base), "zip", root_dir=backup_dir.parent, base_dir=backup_dir.name))
        return BackupStateResult(
            backup_dir=backup_dir,
            manifest_path=manifest_path,
            archive_path=archive_path,
        )

    def restore_state(self, *, manifest_path: Path, target_dir: Path) -> RestoreStateResult:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Backup manifest must be a JSON object.")
        backup_dir = manifest_path.parent
        restored_root = target_dir.resolve()
        restored_root.mkdir(parents=True, exist_ok=True)
        database_source = backup_dir / Path(str(payload.get("database_path", ""))).name
        if not database_source.exists():
            raise FileNotFoundError(f"Backup database not found: {database_source}")
        restored_database_path = restored_root / database_source.name
        shutil.copy2(database_source, restored_database_path)

        artifacts_source = backup_dir / "artifacts"
        restored_artifact_dir = restored_root / "artifacts"
        if artifacts_source.exists():
            shutil.copytree(artifacts_source, restored_artifact_dir, dirs_exist_ok=True)
        else:
            restored_artifact_dir.mkdir(parents=True, exist_ok=True)
        return RestoreStateResult(
            target_dir=restored_root,
            restored_database_path=restored_database_path,
            restored_artifact_dir=restored_artifact_dir,
            manifest_path=manifest_path,
        )

    def build_release_manifest(self, *, output_path: Path) -> ReleaseManifestResult:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        schema = self.schema_status()
        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "environment": self._settings.environment,
            "database_backend": self._settings.database_backend,
            "database_path": str(self._settings.database_path),
            "artifact_dir": str(self._settings.artifact_dir),
            "notification_dir": str(self._settings.notification_dir),
            "metrics_dir": str(self._settings.metrics_dir),
            "telemetry_dir": str(self._settings.telemetry_dir),
            "schema_version": schema.current_version,
            "migrations": schema.migrations,
            "verification_runtime": self._settings.verification_runtime.value,
            "docker_image": self._settings.docker_image,
            "api_host": self._settings.api_host,
            "api_port": self._settings.api_port,
            "routes": [
                "/healthz",
                "/ui",
                "/v1/plan",
                "/v1/verify",
                "/v1/deliver",
                "/v1/queue/plan",
                "/v1/queue/verify",
                "/v1/queue/deliver",
            ],
        }
        output_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return ReleaseManifestResult(manifest_path=output_path, manifest=manifest)
