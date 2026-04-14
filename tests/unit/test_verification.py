from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.application.use_cases.verify_run import VerifyRunUseCase
from issue_to_pr_agent.domain.entities import (
    ExecutionMode,
    PlannerProvider,
    RunRecord,
    RunStatus,
    VerificationStopReason,
    VerificationStatus,
)
from issue_to_pr_agent.domain.policies.safety import SafetyPolicy
from issue_to_pr_agent.infrastructure.persistence.run_repository import RunRepository


class VerificationUseCaseTests(unittest.TestCase):
    def test_verify_run_succeeds_and_writes_attempt_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            (root / "tests").mkdir()
            (root / "tests" / "test_sample.py").write_text(
                "import unittest\n\nclass SampleTest(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            run_record = RunRecord(
                run_id="run-success",
                created_at="2026-04-13T10:00:00+00:00",
                repo_full_name="acme/widgets",
                issue_number=1,
                planner_provider=PlannerProvider.HEURISTIC,
                execution_mode=ExecutionMode.PLAN_ONLY,
                status=RunStatus.SUCCEEDED,
                branch_name="agent/issue-1",
                summary="Verify tests",
                issue_url="https://example.com/issues/1",
                report_path=artifact_dir / "runs" / "run-success" / "plan.md",
                pr_draft_path=artifact_dir / "runs" / "run-success" / "pr.md",
                audit_path=artifact_dir / "runs" / "run-success" / "run.json",
            )
            run_record.audit_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "plan": {"summary": "Verify tests", "tests": ["python3 -m unittest discover -s tests -v"]},
                "planning_context": {
                    "suggested_test_commands": ["python3 -m unittest discover -s tests -v"],
                    "repository_profile": {"test_commands": ["python3 -m unittest discover -s tests -v"]},
                },
            }
            repository.save_run(run_record, payload)

            result = VerifyRunUseCase(repository, SafetyPolicy(branch_prefix="agent/")).verify(
                repo_root=root,
                artifact_dir=artifact_dir,
                run_id="run-success",
                max_attempts=2,
                timeout_seconds=30,
            )

            self.assertEqual(result.receipt.status, VerificationStatus.SUCCEEDED)
            self.assertEqual(result.receipt.stop_reason, VerificationStopReason.SUCCESS)
            self.assertEqual(len(result.receipt.attempts), 1)
            self.assertTrue(result.receipt_path.exists())
            self.assertTrue(result.receipt.attempts[0].stdout_path.exists())

    def test_verify_run_reflects_and_tries_next_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            (root / "tests").mkdir()
            (root / "tests" / "test_sample.py").write_text(
                "import unittest\n\nclass SampleTest(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            run_record = RunRecord(
                run_id="run-fallback",
                created_at="2026-04-13T10:00:00+00:00",
                repo_full_name="acme/widgets",
                issue_number=2,
                planner_provider=PlannerProvider.HEURISTIC,
                execution_mode=ExecutionMode.PLAN_ONLY,
                status=RunStatus.SUCCEEDED,
                branch_name="agent/issue-2",
                summary="Verify fallback tests",
                issue_url="https://example.com/issues/2",
                report_path=artifact_dir / "runs" / "run-fallback" / "plan.md",
                pr_draft_path=artifact_dir / "runs" / "run-fallback" / "pr.md",
                audit_path=artifact_dir / "runs" / "run-fallback" / "run.json",
            )
            run_record.audit_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "plan": {
                    "summary": "Verify fallback tests",
                    "tests": [
                        "python3 -m unittest discover -s missing_tests -v",
                        "python3 -m unittest discover -s tests -v",
                    ],
                },
                "planning_context": {
                    "suggested_test_commands": [],
                    "repository_profile": {"test_commands": ["python3 -m unittest discover -s tests -v"]},
                },
            }
            repository.save_run(run_record, payload)

            result = VerifyRunUseCase(repository, SafetyPolicy(branch_prefix="agent/")).verify(
                repo_root=root,
                artifact_dir=artifact_dir,
                run_id="run-fallback",
                max_attempts=3,
                timeout_seconds=30,
            )

            self.assertEqual(result.receipt.status, VerificationStatus.SUCCEEDED)
            self.assertEqual(len(result.receipt.attempts), 2)
            self.assertIn("trying the next verification candidate", result.receipt.attempts[0].note.lower())

    def test_verify_run_fails_when_no_allowed_commands_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            run_record = RunRecord(
                run_id="run-blocked",
                created_at="2026-04-13T10:00:00+00:00",
                repo_full_name="acme/widgets",
                issue_number=3,
                planner_provider=PlannerProvider.HEURISTIC,
                execution_mode=ExecutionMode.PLAN_ONLY,
                status=RunStatus.SUCCEEDED,
                branch_name="agent/issue-3",
                summary="Verify blocked commands",
                issue_url="https://example.com/issues/3",
                report_path=artifact_dir / "runs" / "run-blocked" / "plan.md",
                pr_draft_path=artifact_dir / "runs" / "run-blocked" / "pr.md",
                audit_path=artifact_dir / "runs" / "run-blocked" / "run.json",
            )
            run_record.audit_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "plan": {"summary": "Blocked verify", "tests": ["echo should-not-run"]},
                "planning_context": {"suggested_test_commands": [], "repository_profile": {"test_commands": []}},
            }
            repository.save_run(run_record, payload)

            result = VerifyRunUseCase(repository, SafetyPolicy(branch_prefix="agent/")).verify(
                repo_root=root,
                artifact_dir=artifact_dir,
                run_id="run-blocked",
            )

            self.assertEqual(result.receipt.status, VerificationStatus.FAILED)
            self.assertEqual(result.receipt.stop_reason, VerificationStopReason.NO_ALLOWED_COMMANDS)
            self.assertEqual(len(result.receipt.skipped_commands), 1)
