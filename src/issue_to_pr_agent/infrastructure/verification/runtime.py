from __future__ import annotations

from ...domain.entities import ExecutionRuntime
from ..config.settings import Settings
from .command_runner import CommandRunner, DockerCommandRunner, LocalCommandRunner


def build_command_runner(settings: Settings, runtime: ExecutionRuntime | None = None) -> CommandRunner:
    resolved = runtime or settings.verification_runtime
    if resolved == ExecutionRuntime.DOCKER:
        return DockerCommandRunner(settings)
    return LocalCommandRunner()
