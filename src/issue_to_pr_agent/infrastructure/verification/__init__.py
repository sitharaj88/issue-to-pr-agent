"""Verification adapters."""

from .command_runner import CommandRunner, CommandRunnerResult, DockerCommandRunner, LocalCommandRunner
from .runtime import build_command_runner

__all__ = [
    "CommandRunner",
    "CommandRunnerResult",
    "DockerCommandRunner",
    "LocalCommandRunner",
    "build_command_runner",
]
