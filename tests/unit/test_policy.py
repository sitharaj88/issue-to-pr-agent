from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.domain.entities import CommandDecision
from issue_to_pr_agent.domain.policies.safety import SafetyPolicy
from issue_to_pr_agent.shared.exceptions import PolicyError


class SafetyPolicyTests(unittest.TestCase):
    def test_assess_commands_blocks_destructive_inputs(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        assessments = policy.assess_commands(["git status --short", "rm -rf /tmp/build"])
        self.assertEqual(assessments[0].decision, CommandDecision.ALLOW)
        self.assertEqual(assessments[1].decision, CommandDecision.BLOCK)

    def test_branch_prefix_is_enforced(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        with self.assertRaises(PolicyError):
            policy.ensure_branch_name("feature/custom")
