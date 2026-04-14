from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.application.use_cases.generate_patch_proposal import GeneratePatchProposalUseCase
from issue_to_pr_agent.application.use_cases.plan_issue_to_pr import IssueToPRAgent
from issue_to_pr_agent.agents.planner.heuristic import HeuristicPlanner
from issue_to_pr_agent.domain.entities import (
    IssueContext,
    PatchOperation,
    PatchOperationType,
    PatchProposal,
    PatcherProvider,
)
from issue_to_pr_agent.domain.policies.safety import SafetyPolicy
from issue_to_pr_agent.infrastructure.persistence.run_repository import RunRepository


class FakeGitHubClient:
    def fetch_issue(self, repo_full_name: str, issue_number: int) -> IssueContext:
        return IssueContext(
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            title="Fix planner output",
            body="Need a clearer implementation in the planner.",
            labels=["bug"],
            url=f"https://example.com/{repo_full_name}/issues/{issue_number}",
        )


class FakePatcher:
    provider = PatcherProvider.OPENAI
    model_name = "fake-standard-model"

    def __init__(self) -> None:
        self.seen_paths: list[str] = []

    def generate(
        self,
        *,
        linked_run_id: str,
        issue: IssueContext,
        plan,
        planning_context,
        repo_root: Path,
        files,
        allowed_existing_paths: list[str],
        suggested_new_file_directories: list[str],
        objective: str | None = None,
    ) -> PatchProposal:
        self.seen_paths = [item.path for item in files]
        return PatchProposal(
            proposal_id=f"{linked_run_id}-auto",
            linked_run_id=linked_run_id,
            summary="Generated planner patch",
            rationale="Replace placeholder implementation.",
            operations=[
                PatchOperation(
                    type=PatchOperationType.REPLACE_TEXT,
                    path="src/planner.py",
                    find_text="pass\n",
                    replace_text="return 'configured'\n",
                )
            ],
        )


class InvalidPathPatcher(FakePatcher):
    def generate(self, **kwargs) -> PatchProposal:  # type: ignore[override]
        proposal = super().generate(**kwargs)
        return PatchProposal(
            proposal_id=proposal.proposal_id,
            linked_run_id=proposal.linked_run_id,
            summary=proposal.summary,
            rationale=proposal.rationale,
            operations=[
                PatchOperation(
                    type=PatchOperationType.REPLACE_TEXT,
                    path="outside.py",
                    find_text="old",
                    replace_text="new",
                )
            ],
        )


class PatchGenerationTests(unittest.TestCase):
    def test_generate_persists_autonomous_patch_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            (root / "src").mkdir()
            (root / "src" / "planner.py").write_text("def configure():\n    pass\n", encoding="utf-8")
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            run = IssueToPRAgent(
                FakeGitHubClient(),
                HeuristicPlanner(),
                repository,
                SafetyPolicy(branch_prefix="agent/"),
            ).run(
                repo_full_name="acme/widgets",
                issue_number=11,
                repo_root=root,
                output_dir=artifact_dir,
            )

            patcher = FakePatcher()
            result = GeneratePatchProposalUseCase(repository, patcher).generate(
                run_id=run.run_id,
                repo_root=root,
            )

            self.assertEqual(result.proposal.linked_run_id, run.run_id)
            self.assertEqual(len(result.proposal.operations), 1)
            self.assertTrue(result.proposal_path.exists())
            self.assertIn("src/planner.py", patcher.seen_paths)

            stored = repository.get_patch_proposal(result.proposal_id)
            self.assertIsNotNone(stored)
            record, payload = stored or (None, None)
            self.assertEqual(record.proposal_id, result.proposal_id)
            self.assertEqual(payload["linked_run_id"], run.run_id)
            self.assertEqual(payload["operations"][0]["path"], "src/planner.py")
            self.assertEqual(payload["model"], "fake-standard-model")
            self.assertGreater(payload["evaluation"]["score"], 0)

    def test_generate_rejects_paths_outside_allowed_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            (root / "src").mkdir()
            (root / "src" / "planner.py").write_text("def configure():\n    pass\n", encoding="utf-8")
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            run = IssueToPRAgent(
                FakeGitHubClient(),
                HeuristicPlanner(),
                repository,
                SafetyPolicy(branch_prefix="agent/"),
            ).run(
                repo_full_name="acme/widgets",
                issue_number=12,
                repo_root=root,
                output_dir=artifact_dir,
            )

            with self.assertRaises(ValueError):
                GeneratePatchProposalUseCase(repository, InvalidPathPatcher()).generate(
                    run_id=run.run_id,
                    repo_root=root,
                )


if __name__ == "__main__":
    unittest.main()
