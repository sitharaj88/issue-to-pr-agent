from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.application.use_cases.manage_release import ManageReleaseUseCase
from issue_to_pr_agent.application.use_cases.run_smoke_test import RunSmokeTestUseCase
from issue_to_pr_agent.infrastructure.config.settings import Settings
from issue_to_pr_agent.infrastructure.persistence.run_repository import RunRepository


class ReleaseManagementTests(unittest.TestCase):
    def test_backup_and_restore_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            artifact_dir.mkdir()
            db_path = artifact_dir / "agent_runs.sqlite3"
            repository = RunRepository(db_path)
            settings = Settings.from_env(cwd=root)
            manager = ManageReleaseUseCase(repository, settings)

            backup = manager.backup_state(output_dir=root / "backups", include_artifacts=True)
            self.assertTrue(backup.manifest_path.exists())
            self.assertTrue(backup.archive_path.exists())

            restored = manager.restore_state(
                manifest_path=backup.manifest_path,
                target_dir=root / "restored",
            )
            self.assertTrue(restored.restored_database_path.exists())
            self.assertTrue(restored.restored_artifact_dir.exists())

    def test_release_manifest_contains_schema_and_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings.from_env(cwd=root)
            repository = RunRepository(settings.database_path)
            manager = ManageReleaseUseCase(repository, settings)

            result = manager.build_release_manifest(output_path=root / "release-manifest.json")

            self.assertTrue(result.manifest_path.exists())
            payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], repository.current_schema_version())
            self.assertEqual(payload["verification_runtime"], settings.verification_runtime.value)

    def test_smoke_test_exercises_core_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings.from_env(cwd=root)
            repository = RunRepository(settings.database_path)

            result = RunSmokeTestUseCase(repository, settings).run(output_dir=root / "smoke")

            self.assertTrue(result.receipt_path.exists())
            self.assertEqual(result.payload["verification_status"], "succeeded")
