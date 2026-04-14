from __future__ import annotations

from ...shared.exceptions import PolicyError
from ..entities import CommandAssessment, CommandDecision


class SafetyPolicy:
    def __init__(self, *, branch_prefix: str) -> None:
        self._branch_prefix = branch_prefix

    def ensure_branch_name(self, branch_name: str) -> None:
        if not branch_name.startswith(self._branch_prefix):
            raise PolicyError(
                f"Branch '{branch_name}' violates policy. Expected prefix '{self._branch_prefix}'."
            )

    def assess_commands(self, commands: list[str]) -> list[CommandAssessment]:
        return [self._assess_command(command) for command in commands]

    def _assess_command(self, command: str) -> CommandAssessment:
        normalized = command.strip()
        lowered = normalized.lower()
        if not normalized:
            return CommandAssessment(
                command=command,
                decision=CommandDecision.REVIEW,
                reason="Empty command requires manual review.",
            )

        blocked_terms = (
            "rm -rf",
            "git reset --hard",
            "git clean -fd",
            "sudo ",
            "curl ",
            "wget ",
            "chmod 777",
        )
        if any(term in lowered for term in blocked_terms):
            return CommandAssessment(
                command=command,
                decision=CommandDecision.BLOCK,
                reason="Command is destructive or pulls remote code without review.",
            )

        allowed_prefixes = (
            "git status",
            "git diff",
            "git rev-parse",
            "git ls-files",
            "rg ",
            "ls",
            "find ",
            "python3 -m pytest",
            "python -m pytest",
            "python3 -m unittest",
            "python -m unittest",
            "pytest",
            "npm test",
            "go test",
            "cargo test",
        )
        if lowered.startswith(allowed_prefixes):
            return CommandAssessment(
                command=command,
                decision=CommandDecision.ALLOW,
                reason="Command is read-only or an approved local test execution.",
            )

        return CommandAssessment(
            command=command,
            decision=CommandDecision.REVIEW,
            reason="Command is not in the approved allowlist and needs operator review.",
        )
