from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.agents.planner.heuristic import HeuristicPlanner
from issue_to_pr_agent.application.use_cases.execute_patch_proposal import ExecutePatchProposalUseCase
from issue_to_pr_agent.application.use_cases.plan_issue_to_pr import IssueToPRAgent
from issue_to_pr_agent.domain.entities import IssueContext, PatchExecutionMode, PatchOperation, PatchOperationType, PatchProposal
from issue_to_pr_agent.domain.policies.safety import SafetyPolicy
from issue_to_pr_agent.domain.policies.workspace import WorkspaceGuardrails
from issue_to_pr_agent.infrastructure.persistence.run_repository import RunRepository


class FakeGitHubClient:
    def fetch_issue(self, repo_full_name: str, issue_number: int) -> IssueContext:
        return IssueContext(
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            title="Fix config path handling",
            body="Config path handling should use a fallback.",
            labels=["bug"],
            url=f"https://example.com/{repo_full_name}/issues/{issue_number}",
        )


class PatchExecutionTests(unittest.TestCase):
    def test_dry_run_does_not_mutate_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            source = root / "service.py"
            source.write_text("value = 'old'\n", encoding="utf-8")
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")

            proposal = PatchProposal(
                proposal_id="dry-run-proposal",
                summary="Update service value",
                operations=[
                    PatchOperation(
                        type=PatchOperationType.REPLACE_TEXT,
                        path="service.py",
                        find_text="'old'",
                        replace_text="'new'",
                    )
                ],
            )

            result = ExecutePatchProposalUseCase(repository).execute(
                proposal=proposal,
                repo_root=root,
                artifact_dir=artifact_dir,
                mode=PatchExecutionMode.DRY_RUN,
            )

            self.assertEqual(source.read_text(encoding="utf-8"), "value = 'old'\n")
            self.assertTrue(result.receipt_path.exists())
            self.assertEqual(result.receipt.status.value, "succeeded")
            self.assertTrue(result.receipt.receipts[0].changed)

    def test_apply_with_linked_run_mutates_allowed_file_and_stores_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            (root / "src").mkdir()
            source = root / "src" / "config_loader.py"
            source.write_text("DEFAULT = None\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_config_loader.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")

            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            planning_result = IssueToPRAgent(
                FakeGitHubClient(),
                HeuristicPlanner(),
                repository,
                SafetyPolicy(branch_prefix="agent/"),
            ).run(
                repo_full_name="acme/widgets",
                issue_number=9,
                repo_root=root,
                output_dir=artifact_dir,
            )

            proposal = PatchProposal(
                proposal_id="apply-proposal",
                linked_run_id=planning_result.run_id,
                summary="Update fallback value",
                operations=[
                    PatchOperation(
                        type=PatchOperationType.REPLACE_TEXT,
                        path="src/config_loader.py",
                        find_text="None",
                        replace_text="'fallback'",
                    )
                ],
            )

            result = ExecutePatchProposalUseCase(repository).execute(
                proposal=proposal,
                repo_root=root,
                artifact_dir=artifact_dir,
                mode=PatchExecutionMode.APPLY,
            )

            self.assertIn("'fallback'", source.read_text(encoding="utf-8"))
            self.assertIn("executions", str(result.receipt_path))
            stored = repository.get_execution(result.execution_id)
            self.assertIsNotNone(stored)

    def test_guardrails_block_path_escape(self) -> None:
        guardrails = WorkspaceGuardrails()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            operation = PatchOperation(
                type=PatchOperationType.WRITE_FILE,
                path="../escape.py",
                content="x = 1\n",
            )
            with self.assertRaises(Exception):
                guardrails.validate_operation(root, operation)
