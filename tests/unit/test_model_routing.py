from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.application.services.model_routing import ModelRoutingService
from issue_to_pr_agent.domain.entities import (
    PatchFileContext,
    PlanningContext,
    RepositoryIndex,
    RepositoryProfile,
)
from issue_to_pr_agent.infrastructure.config.settings import Settings


class ModelRoutingTests(unittest.TestCase):
    def test_select_planner_model_uses_complex_model_above_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(
                tmp,
                {
                    "OPENAI_MODEL": "gpt-4.1-mini",
                    "ISSUE_TO_PR_OPENAI_COMPLEX_MODEL": "gpt-5.4",
                    "ISSUE_TO_PR_ROUTER_PLANNER_COMPLEXITY_THRESHOLD": "10",
                },
            )
            context = PlanningContext(
                summary="Large repository planning context",
                repository_profile=RepositoryProfile(primary_language="python"),
                repository_index=RepositoryIndex(files_indexed=40, symbol_count=24, complexity_score=16),
            )

            decision = ModelRoutingService(settings).select_planner_model(planning_context=context)

            self.assertEqual(decision.model_name, "gpt-5.4")
            self.assertEqual(decision.complexity_score, 16)
            self.assertTrue(any("complex planner model" in reason for reason in decision.reasons))

    def test_select_patch_model_keeps_standard_model_below_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(
                tmp,
                {
                    "OPENAI_MODEL": "gpt-4.1-mini",
                    "ISSUE_TO_PR_OPENAI_COMPLEX_MODEL": "gpt-5.4",
                    "ISSUE_TO_PR_ROUTER_PATCH_COMPLEXITY_THRESHOLD": "25",
                },
            )
            context = PlanningContext(
                summary="Small repository planning context",
                repository_profile=RepositoryProfile(primary_language="python"),
                repository_index=RepositoryIndex(files_indexed=6, symbol_count=4, complexity_score=8),
            )
            files = [
                PatchFileContext(
                    path="src/module.py",
                    exists=True,
                    content="def configure():\n    return 1\n",
                    preview="def configure():",
                )
            ]

            decision = ModelRoutingService(settings).select_patch_model(
                planning_context=context,
                files=files,
            )

            self.assertEqual(decision.model_name, "gpt-4.1-mini")
            self.assertLess(decision.complexity_score, 25)
            self.assertTrue(any("standard patch model" in reason for reason in decision.reasons))


def _settings(root: str, env: dict[str, str]) -> Settings:
    with mock.patch.dict("os.environ", env, clear=False):
        settings = Settings.from_env(cwd=Path(root))
    settings.validate()
    return settings


if __name__ == "__main__":
    unittest.main()
