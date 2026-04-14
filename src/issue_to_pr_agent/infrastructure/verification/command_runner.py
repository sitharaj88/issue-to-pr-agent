from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import time
from typing import Protocol

from ...infrastructure.config.settings import Settings


@dataclass(frozen=True)
class CommandRunnerResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


class CommandRunner(Protocol):
    def run(self, *, command: str, cwd: Path, timeout_seconds: int) -> CommandRunnerResult: ...


class LocalCommandRunner:
    def run(self, *, command: str, cwd: Path, timeout_seconds: int) -> CommandRunnerResult:
        start = time.monotonic()
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return CommandRunnerResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_ms=duration_ms,
        )


class DockerCommandRunner:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def run(self, *, command: str, cwd: Path, timeout_seconds: int) -> CommandRunnerResult:
        start = time.monotonic()
        completed = subprocess.run(
            self._docker_command(command=command, cwd=cwd),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return CommandRunnerResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_ms=duration_ms,
        )

    def _docker_command(self, *, command: str, cwd: Path) -> list[str]:
        workspace = cwd.resolve()
        return [
            self._settings.docker_binary,
            "run",
            "--rm",
            "--network",
            self._settings.docker_network,
            "--cpus",
            str(self._settings.docker_cpus),
            "--memory",
            f"{self._settings.docker_memory_mb}m",
            "-v",
            f"{workspace}:/workspace",
            "-w",
            "/workspace",
            self._settings.docker_image,
            "/bin/bash",
            "-lc",
            command,
        ]


LocalCommandRunnerResult = CommandRunnerResult
