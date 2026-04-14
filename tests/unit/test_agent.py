from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.application.use_cases.plan_issue_to_pr import IssueToPRAgent
from issue_to_pr_agent.agents.planner.heuristic import HeuristicPlanner
from issue_to_pr_agent.domain.entities import CommandDecision, IssueContext
from issue_to_pr_agent.domain.policies.safety import SafetyPolicy
from issue_to_pr_agent.infrastructure.persistence.run_repository import RunRepository


class FakeGitHubClient:
    def fetch_issue(self, repo_full_name: str, issue_number: int) -> IssueContext:
        return IssueContext(
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            title="Handle missing config",
            body="The agent should fail with a clear error when config is missing.",
            labels=["bug"],
            url=f"https://example.com/{repo_full_name}/issues/{issue_number}",
        )


class AgentTests(unittest.TestCase):
    def test_agent_writes_run_artifacts_and_persists_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("demo\n", encoding="utf-8")
            artifact_dir = root / ".issue-to-pr"

            agent = IssueToPRAgent(
                FakeGitHubClient(),
                HeuristicPlanner(),
                RunRepository(artifact_dir / "agent_runs.sqlite3"),
                SafetyPolicy(branch_prefix="agent/"),
                max_repo_files=25,
            )
            result = agent.run(
                repo_full_name="acme/widgets",
                issue_number=7,
                repo_root=root,
                output_dir=artifact_dir,
            )

            self.assertTrue(result.report_path.exists())
            self.assertTrue(result.pr_draft_path.exists())
            self.assertTrue(result.audit_path.exists())
            self.assertTrue(result.run_directory.exists())
            self.assertTrue(result.planning_context.ranked_files)
            self.assertGreater(len(result.command_assessments), 0)
            self.assertIn(
                CommandDecision.ALLOW,
                {item.decision for item in result.command_assessments},
            )
            report = result.report_path.read_text(encoding="utf-8")
            self.assertIn("Issue 7: Handle missing config", report)
            self.assertIn("README.md", report)
            self.assertIn("Planning Context", report)
            audit = result.audit_path.read_text(encoding="utf-8")
            self.assertIn(result.run_id, audit)
            self.assertIn("\"planning_context\"", audit)

            stored = RunRepository(artifact_dir / "agent_runs.sqlite3").get_run(result.run_id)
            self.assertIsNotNone(stored)
            record, payload = stored or (None, None)
            self.assertEqual(record.run_id, result.run_id)
            self.assertEqual(payload["status"], "succeeded")
