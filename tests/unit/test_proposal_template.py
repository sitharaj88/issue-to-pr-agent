from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.application.services.proposal_template import ProposalTemplateBuilder
from issue_to_pr_agent.application.use_cases.plan_issue_to_pr import IssueToPRAgent
from issue_to_pr_agent.agents.planner.heuristic import HeuristicPlanner
from issue_to_pr_agent.domain.entities import IssueContext
from issue_to_pr_agent.domain.policies.safety import SafetyPolicy
from issue_to_pr_agent.infrastructure.persistence.run_repository import RunRepository


class FakeGitHubClient:
    def fetch_issue(self, repo_full_name: str, issue_number: int) -> IssueContext:
        return IssueContext(
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            title="Fix planner template output",
            body="Need a reusable patch proposal template.",
            labels=["enhancement"],
            url=f"https://example.com/{repo_full_name}/issues/{issue_number}",
        )


class ProposalTemplateBuilderTests(unittest.TestCase):
    def test_build_includes_allowed_paths_from_run_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            (root / "src").mkdir()
            (root / "src" / "planner.py").write_text("def plan():\n    pass\n", encoding="utf-8")
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            result = IssueToPRAgent(
                FakeGitHubClient(),
                HeuristicPlanner(),
                repository,
                SafetyPolicy(branch_prefix="agent/"),
            ).run(
                repo_full_name="acme/widgets",
                issue_number=4,
                repo_root=root,
                output_dir=artifact_dir,
            )
            stored = repository.get_run(result.run_id)
            self.assertIsNotNone(stored)
            _, payload = stored or (None, None)

            template = ProposalTemplateBuilder().build(run_id=result.run_id, payload=payload)

            self.assertEqual(template["linked_run_id"], result.run_id)
            self.assertIn("operations", template)
            self.assertIn("allowed_existing_paths", template)
