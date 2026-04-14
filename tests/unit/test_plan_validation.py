from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.application.services.plan_validation import PlanValidator
from issue_to_pr_agent.domain.entities import AgentPlan, IssueContext
from issue_to_pr_agent.agents.context_builder.repository import RepositoryContextBuilder
from issue_to_pr_agent.infrastructure.scm.local_repo import LocalRepoInspector


class PlanValidatorTests(unittest.TestCase):
    def test_normalize_filters_unknown_files_and_fills_test_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "agent_core.py").write_text("class AgentCore:\n    pass\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_agent_core.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")

            snapshot = LocalRepoInspector(root).snapshot(max_files=20)
            issue = IssueContext(
                repo_full_name="acme/widgets",
                issue_number=12,
                title="Agent core config bug",
                body="Investigate the config bug in the agent core.",
                labels=["bug"],
                url="https://example.com/issues/12",
            )
            context = RepositoryContextBuilder().build(issue, snapshot)
            plan = AgentPlan(
                summary="Investigate the issue.",
                files_to_inspect=["missing.py"],
                commands=["git status --short", "git status --short"],
                tests=[],
                branch_name="agent/issue-12",
                pr_title="Fix #12",
                pr_body="Summary",
            )

            normalized = PlanValidator().normalize(plan, snapshot, context)

            self.assertNotIn("missing.py", normalized.files_to_inspect)
            self.assertTrue(normalized.files_to_inspect)
            self.assertTrue(normalized.tests)
            self.assertEqual(normalized.commands, ["git status --short"])
            self.assertTrue(any("removed" in risk for risk in normalized.risks))
