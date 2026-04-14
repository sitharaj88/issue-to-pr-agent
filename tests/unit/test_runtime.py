from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.infrastructure.config.settings import Settings
from issue_to_pr_agent.infrastructure.verification import DockerCommandRunner, LocalCommandRunner, build_command_runner


class RuntimeTests(unittest.TestCase):
    def test_build_command_runner_uses_docker_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict(
                "os.environ",
                {
                    "ISSUE_TO_PR_ARTIFACT_DIR": str(cwd / ".issue-to-pr"),
                    "ISSUE_TO_PR_VERIFICATION_RUNTIME": "docker",
                },
                clear=True,
            ):
                settings = Settings.from_env(cwd=cwd)

            runner = build_command_runner(settings)
            self.assertIsInstance(runner, DockerCommandRunner)

    def test_docker_command_runner_invokes_expected_container_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict(
                "os.environ",
                {
                    "ISSUE_TO_PR_ARTIFACT_DIR": str(cwd / ".issue-to-pr"),
                    "ISSUE_TO_PR_DOCKER_BINARY": "docker-test",
                    "ISSUE_TO_PR_DOCKER_IMAGE": "python:3.12-slim",
                    "ISSUE_TO_PR_DOCKER_NETWORK": "none",
                    "ISSUE_TO_PR_DOCKER_MEMORY_MB": "512",
                    "ISSUE_TO_PR_DOCKER_CPUS": "1.5",
                },
                clear=True,
            ):
                settings = Settings.from_env(cwd=cwd)

            runner = DockerCommandRunner(settings)
            completed = mock.Mock(returncode=0, stdout="ok\n", stderr="")
            with mock.patch("subprocess.run", return_value=completed) as run_mock:
                result = runner.run(
                    command="python3 -m unittest discover -s tests -v",
                    cwd=cwd,
                    timeout_seconds=30,
                )

            self.assertEqual(result.exit_code, 0)
            args = run_mock.call_args.args[0]
            self.assertEqual(args[0], "docker-test")
            self.assertIn("run", args)
            self.assertIn("--network", args)
            self.assertIn("none", args)
            self.assertIn("--cpus", args)
            self.assertIn("1.5", args)
            self.assertIn("--memory", args)
            self.assertIn("512m", args)
            self.assertIn("python:3.12-slim", args)
            self.assertEqual(args[-3:], ["/bin/bash", "-lc", "python3 -m unittest discover -s tests -v"])

    def test_build_command_runner_uses_local_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict("os.environ", {"ISSUE_TO_PR_ARTIFACT_DIR": str(cwd / ".issue-to-pr")}, clear=True):
                settings = Settings.from_env(cwd=cwd)

            runner = build_command_runner(settings)
            self.assertIsInstance(runner, LocalCommandRunner)


if __name__ == "__main__":
    unittest.main()
