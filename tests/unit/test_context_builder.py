from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.agents.context_builder.repository import RepositoryContextBuilder
from issue_to_pr_agent.domain.entities import IssueContext
from issue_to_pr_agent.infrastructure.scm.local_repo import LocalRepoInspector


class RepositoryContextBuilderTests(unittest.TestCase):
    def test_build_detects_python_profile_and_ranks_relevant_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                "[project]\nname='demo'\n[tool.pytest.ini_options]\n",
                encoding="utf-8",
            )
            (root / "src").mkdir()
            (root / "src" / "config_loader.py").write_text(
                "def load_config(path):\n    return path\n",
                encoding="utf-8",
            )
            (root / "tests").mkdir()
            (root / "tests" / "test_config_loader.py").write_text(
                "def test_load_config():\n    assert True\n",
                encoding="utf-8",
            )

            snapshot = LocalRepoInspector(root).snapshot(max_files=20)
            issue = IssueContext(
                repo_full_name="acme/widgets",
                issue_number=3,
                title="Fix config loader failure",
                body="The config loader fails when the config path is missing.",
                labels=["bug"],
                url="https://example.com/issues/3",
            )

            context = RepositoryContextBuilder(max_ranked_files=5, snippet_line_limit=4).build(
                issue,
                snapshot,
            )

            self.assertEqual(context.repository_profile.primary_language, "python")
            self.assertIn("python3 -m pytest", context.suggested_test_commands)
            self.assertTrue(context.ranked_files)
            self.assertEqual(context.ranked_files[0].path, "src/config_loader.py")
            self.assertIn("load_config", context.ranked_files[0].preview)
            self.assertGreater(context.repository_index.symbol_count, 0)
            self.assertEqual(context.repository_index.top_symbols[0].name, "load_config")
            self.assertGreater(context.repository_index.complexity_score, 0)
            self.assertGreater(context.evaluation.score, 0)
