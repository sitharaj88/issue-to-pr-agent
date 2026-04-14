from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.application.use_cases.run_autofix import RunAutofixUseCase
from issue_to_pr_agent.domain.entities import (
    ExecutionMode,
    PatchOperation,
    PatchOperationType,
    PatchProposal,
    PatcherProvider,
    PlannerProvider,
    RunRecord,
    RunStatus,
)
from issue_to_pr_agent.domain.policies.safety import SafetyPolicy
from issue_to_pr_agent.infrastructure.persistence.run_repository import RunRepository


class SequencedPatcher:
    provider = PatcherProvider.OPENAI

    def __init__(self, replacements: list[str]) -> None:
        self._replacements = replacements
        self.objectives: list[str] = []
        self.calls = 0

    def generate(
        self,
        *,
        linked_run_id: str,
        issue,
        plan,
        planning_context,
        repo_root: Path,
        files,
        allowed_existing_paths: list[str],
        suggested_new_file_directories: list[str],
        objective: str | None = None,
    ) -> PatchProposal:
        self.objectives.append(objective or "")
        self.calls += 1
        file_context = next(item for item in files if item.path == "flag_module.py")
        current_line = _flag_line(file_context.content)
        replacement = self._replacements[min(self.calls - 1, len(self._replacements) - 1)]
        return PatchProposal(
            proposal_id=f"{linked_run_id}-proposal-{self.calls}",
            linked_run_id=linked_run_id,
            summary=f"Set module flag to {replacement.split('=', 1)[1].strip()}",
            rationale="Update the flag module to satisfy the failing test.",
            operations=[
                PatchOperation(
                    type=PatchOperationType.REPLACE_TEXT,
                    path="flag_module.py",
                    find_text=current_line,
                    replace_text=replacement,
                )
            ],
        )


class AutofixUseCaseTests(unittest.TestCase):
    def test_autofix_succeeds_on_first_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            _write_flag_fixture(root)
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            run_id = _create_planning_run(repository, artifact_dir)

            patcher = SequencedPatcher(["FLAG = True"])
            result = RunAutofixUseCase(
                repository,
                patcher,
                SafetyPolicy(branch_prefix="agent/"),
            ).run(
                run_id=run_id,
                repo_root=root,
                artifact_dir=artifact_dir,
                max_attempts=2,
                verify_max_attempts=1,
                timeout_seconds=30,
            )

            self.assertEqual(result.status.value, "succeeded")
            self.assertEqual(len(result.receipt.attempts), 1)
            self.assertIn("FLAG = True", (root / "flag_module.py").read_text(encoding="utf-8"))
            stored = repository.get_autofix_run(result.autofix_id)
            self.assertIsNotNone(stored)
            attempts = repository.list_autofix_attempts(autofix_id=result.autofix_id)
            self.assertEqual(len(attempts), 1)

    def test_autofix_retries_after_failed_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            _write_flag_fixture(root)
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            run_id = _create_planning_run(repository, artifact_dir)

            patcher = SequencedPatcher(["FLAG = None", "FLAG = True"])
            result = RunAutofixUseCase(
                repository,
                patcher,
                SafetyPolicy(branch_prefix="agent/"),
            ).run(
                run_id=run_id,
                repo_root=root,
                artifact_dir=artifact_dir,
                max_attempts=3,
                verify_max_attempts=1,
                timeout_seconds=30,
                objective="Make the test pass with the smallest possible patch.",
            )

            self.assertEqual(result.status.value, "succeeded")
            self.assertEqual(len(result.receipt.attempts), 2)
            self.assertIn("FLAG = True", (root / "flag_module.py").read_text(encoding="utf-8"))
            self.assertIn("Verification stop reason: max_attempts_reached", patcher.objectives[1])
            self.assertIn("Last failing command: python3 -m unittest discover -s tests -v", patcher.objectives[1])

    def test_autofix_records_failure_after_max_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            _write_flag_fixture(root)
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            run_id = _create_planning_run(repository, artifact_dir)

            patcher = SequencedPatcher(["FLAG = None"])
            result = RunAutofixUseCase(
                repository,
                patcher,
                SafetyPolicy(branch_prefix="agent/"),
            ).run(
                run_id=run_id,
                repo_root=root,
                artifact_dir=artifact_dir,
                max_attempts=2,
                verify_max_attempts=1,
                timeout_seconds=30,
            )

            self.assertEqual(result.status.value, "failed")
            self.assertEqual(len(result.receipt.attempts), 2)
            self.assertIn("FLAG = None", (root / "flag_module.py").read_text(encoding="utf-8"))
            stored = repository.get_autofix_run(result.autofix_id)
            self.assertIsNotNone(stored)
            _, payload = stored or (None, None)
            self.assertEqual(payload["status"], "failed")


def _write_flag_fixture(root: Path) -> None:
    (root / "flag_module.py").write_text("FLAG = False\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_flag_module.py").write_text(
        "from pathlib import Path\n"
        "import unittest\n\n"
        "class FlagModuleTests(unittest.TestCase):\n"
        "    def test_flag_is_true(self):\n"
        "        namespace = {}\n"
        "        exec(Path('flag_module.py').read_text(encoding='utf-8'), namespace)\n"
        "        self.assertIs(namespace['FLAG'], True)\n",
        encoding="utf-8",
    )


def _create_planning_run(repository: RunRepository, artifact_dir: Path, *, run_id: str = "run-autofix") -> str:
    run_record = RunRecord(
        run_id=run_id,
        created_at="2026-04-14T10:00:00+00:00",
        repo_full_name="acme/widgets",
        issue_number=21,
        planner_provider=PlannerProvider.HEURISTIC,
        execution_mode=ExecutionMode.PLAN_ONLY,
        status=RunStatus.SUCCEEDED,
        branch_name="agent/issue-21",
        summary="Turn the module flag on.",
        issue_url="https://example.com/issues/21",
        report_path=artifact_dir / "runs" / run_id / "plan.md",
        pr_draft_path=artifact_dir / "runs" / run_id / "pr.md",
        audit_path=artifact_dir / "runs" / run_id / "run.json",
    )
    run_record.audit_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "issue": {
            "repo_full_name": "acme/widgets",
            "issue_number": 21,
            "title": "Flag should be enabled",
            "body": "The module flag should be enabled so the test passes.",
            "labels": ["bug"],
            "url": "https://example.com/issues/21",
        },
        "plan": {
            "summary": "Turn the module flag on.",
            "files_to_inspect": ["flag_module.py"],
            "tests": ["python3 -m unittest discover -s tests -v"],
            "branch_name": "agent/issue-21",
            "pr_title": "Fix module flag",
            "pr_body": "Enable the module flag and verify the unit test.",
        },
        "planning_context": {
            "summary": "The issue affects a single module-level flag.",
            "issue_keywords": ["flag", "enabled"],
            "repository_profile": {
                "primary_language": "python",
                "detected_languages": ["python"],
                "detected_frameworks": [],
                "build_systems": [],
                "test_commands": ["python3 -m unittest discover -s tests -v"],
            },
            "ranked_files": [
                {
                    "path": "flag_module.py",
                    "score": 10,
                    "reasons": ["Matches the issue keyword and failing behavior."],
                    "preview": "FLAG = False",
                }
            ],
            "suggested_test_commands": ["python3 -m unittest discover -s tests -v"],
        },
    }
    repository.save_run(run_record, payload)
    return run_id


def _flag_line(content: str) -> str:
    for line in content.splitlines():
        if line.startswith("FLAG ="):
            return line
    raise AssertionError("FLAG line not found in file context.")


if __name__ == "__main__":
    unittest.main()
