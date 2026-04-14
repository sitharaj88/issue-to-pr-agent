from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.application.use_cases.manage_sandbox import ManageSandboxUseCase
from issue_to_pr_agent.application.use_cases.execute_patch_proposal import ExecutePatchProposalUseCase
from issue_to_pr_agent.application.use_cases.run_autofix import RunAutofixUseCase
from issue_to_pr_agent.application.use_cases.run_sandboxed_autofix import RunSandboxedAutofixUseCase
from issue_to_pr_agent.application.use_cases.run_sandboxed_patch_execution import (
    RunSandboxedPatchExecutionUseCase,
)
from issue_to_pr_agent.domain.entities import (
    ExecutionMode,
    PatchExecutionMode,
    PatchOperation,
    PatchOperationType,
    PatchProposal,
    PatcherProvider,
    PlannerProvider,
    RunRecord,
    RunStatus,
    SandboxStatus,
)
from issue_to_pr_agent.domain.policies.safety import SafetyPolicy
from issue_to_pr_agent.infrastructure.persistence.run_repository import RunRepository
from issue_to_pr_agent.infrastructure.sandbox import LocalSandboxManager


class SinglePatchPatcher:
    provider = PatcherProvider.OPENAI

    def generate(
        self,
        *,
        linked_run_id: str,
        issue,
        plan,
        planning_context,
        repo_root: Path,
        files,
        allowed_existing_paths: list[str],
        suggested_new_file_directories: list[str],
        objective: str | None = None,
    ) -> PatchProposal:
        file_context = next(item for item in files if item.path == "flag_module.py")
        current_line = _flag_line(file_context.content)
        return PatchProposal(
            proposal_id=f"{linked_run_id}-sandboxed",
            linked_run_id=linked_run_id,
            summary="Enable the module flag",
            rationale="Update the sandbox copy only.",
            operations=[
                PatchOperation(
                    type=PatchOperationType.REPLACE_TEXT,
                    path="flag_module.py",
                    find_text=current_line,
                    replace_text="FLAG = True",
                )
            ],
        )


class SandboxWorkflowTests(unittest.TestCase):
    def test_prepare_sandbox_copies_workspace_and_skips_internal_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("x=1\n", encoding="utf-8")
            artifact_dir.mkdir()
            (artifact_dir / "ignored.txt").write_text("internal\n", encoding="utf-8")
            (root / "big.bin").write_text("0123456789", encoding="utf-8")

            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            use_case = ManageSandboxUseCase(
                repository,
                LocalSandboxManager(max_file_bytes=5),
            )
            result = use_case.prepare(
                repo_root=root,
                sandbox_dir=artifact_dir / "sandboxes",
                artifact_dir=artifact_dir,
                summary="Sandbox copy test.",
            )

            self.assertTrue((result.receipt.workspace_root / "src" / "app.py").exists())
            self.assertFalse((result.receipt.workspace_root / ".issue-to-pr").exists())
            self.assertFalse((result.receipt.workspace_root / "big.bin").exists())
            self.assertGreaterEqual(result.receipt.skipped_entry_count, 2)

            stored = repository.get_sandbox(result.sandbox_id)
            self.assertIsNotNone(stored)

    def test_prepare_sandbox_clones_clean_git_repo_and_preserves_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            remote = root / "remote.git"
            artifact_dir = source / ".issue-to-pr"
            self._init_repo(source, remote)

            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            use_case = ManageSandboxUseCase(
                repository,
                LocalSandboxManager(max_file_bytes=1024 * 1024),
            )
            result = use_case.prepare(
                repo_root=source,
                sandbox_dir=artifact_dir / "sandboxes",
                artifact_dir=artifact_dir,
                summary="Git clone sandbox test.",
            )

            self.assertTrue((result.receipt.workspace_root / ".git").exists())
            self.assertEqual(result.receipt.materialization_strategy, "git_clone")
            self.assertEqual(
                self._git(result.receipt.workspace_root, "remote", "get-url", "origin"),
                str(remote),
            )
            self.assertEqual(
                self._git(result.receipt.workspace_root, "config", "user.email"),
                "agent@example.com",
            )

    def test_sandboxed_autofix_preserves_source_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            _write_flag_fixture(root)
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            run_id = _create_planning_run(repository, artifact_dir, run_id="run-sandbox")

            sandbox_use_case = ManageSandboxUseCase(
                repository,
                LocalSandboxManager(max_file_bytes=1024 * 1024),
            )
            autofix_use_case = RunAutofixUseCase(
                repository,
                SinglePatchPatcher(),
                SafetyPolicy(branch_prefix="agent/"),
            )

            result = RunSandboxedAutofixUseCase(sandbox_use_case, autofix_use_case).run(
                run_id=run_id,
                source_repo_root=root,
                artifact_dir=artifact_dir,
                sandbox_dir=artifact_dir / "sandboxes",
                max_attempts=2,
                verify_max_attempts=1,
                timeout_seconds=30,
            )

            self.assertEqual(result.autofix.status.value, "succeeded")
            self.assertEqual(result.sandbox.receipt.status, SandboxStatus.USED)
            self.assertEqual((root / "flag_module.py").read_text(encoding="utf-8"), "FLAG = False\n")
            self.assertIn(
                "FLAG = True",
                (result.sandbox.receipt.workspace_root / "flag_module.py").read_text(encoding="utf-8"),
            )
            stored = repository.get_sandbox(result.sandbox.sandbox_id)
            self.assertIsNotNone(stored)
            sandbox_record, _ = stored or (None, None)
            self.assertEqual(sandbox_record.linked_autofix_id, result.autofix.autofix_id)

    def test_sandboxed_patch_execution_preserves_source_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            source = root / "service.py"
            source.write_text("value = 'old'\n", encoding="utf-8")
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            sandbox_use_case = ManageSandboxUseCase(
                repository,
                LocalSandboxManager(max_file_bytes=1024 * 1024),
            )
            executor = ExecutePatchProposalUseCase(repository)
            proposal = PatchProposal(
                proposal_id="sandboxed-proposal",
                summary="Update service value in isolation",
                operations=[
                    PatchOperation(
                        type=PatchOperationType.REPLACE_TEXT,
                        path="service.py",
                        find_text="'old'",
                        replace_text="'new'",
                    )
                ],
            )

            result = RunSandboxedPatchExecutionUseCase(sandbox_use_case, executor).run(
                proposal=proposal,
                source_repo_root=root,
                artifact_dir=artifact_dir,
                sandbox_dir=artifact_dir / "sandboxes",
                mode=PatchExecutionMode.APPLY,
            )

            self.assertEqual(source.read_text(encoding="utf-8"), "value = 'old'\n")
            self.assertEqual(result.sandbox.receipt.status, SandboxStatus.USED)
            self.assertEqual(
                (result.sandbox.receipt.workspace_root / "service.py").read_text(encoding="utf-8"),
                "value = 'new'\n",
            )
            sandbox_payload = repository.get_sandbox(result.sandbox.sandbox_id)
            self.assertIsNotNone(sandbox_payload)
            _, payload = sandbox_payload or (None, None)
            self.assertEqual(payload["linked_execution_id"], result.execution.execution_id)

    def _init_repo(self, root: Path, remote: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self._git(root, "init")
        self._git(root, "config", "user.email", "agent@example.com")
        self._git(root, "config", "user.name", "Issue Agent")
        (root / "app.py").write_text("print('before')\n", encoding="utf-8")
        self._git(root, "add", "app.py")
        self._git(root, "commit", "-m", "Initial commit")
        self._git(root, "branch", "-M", "main")
        self._git(root.parent, "init", "--bare", str(remote))
        self._git(root, "remote", "add", "origin", str(remote))
        self._git(root, "push", "-u", "origin", "main")

    def _git(self, cwd: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            check=True,
            text=True,
        )
        return completed.stdout.strip()


def _write_flag_fixture(root: Path) -> None:
    (root / "flag_module.py").write_text("FLAG = False\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_flag_module.py").write_text(
        "from pathlib import Path\n"
        "import unittest\n\n"
        "class FlagModuleTests(unittest.TestCase):\n"
        "    def test_flag_is_true(self):\n"
        "        namespace = {}\n"
        "        exec(Path('flag_module.py').read_text(encoding='utf-8'), namespace)\n"
        "        self.assertIs(namespace['FLAG'], True)\n",
        encoding="utf-8",
    )


def _create_planning_run(repository: RunRepository, artifact_dir: Path, *, run_id: str) -> str:
    run_record = RunRecord(
        run_id=run_id,
        created_at="2026-04-14T12:00:00+00:00",
        repo_full_name="acme/widgets",
        issue_number=31,
        planner_provider=PlannerProvider.HEURISTIC,
        execution_mode=ExecutionMode.PLAN_ONLY,
        status=RunStatus.SUCCEEDED,
        branch_name="agent/issue-31",
        summary="Turn the module flag on in isolation.",
        issue_url="https://example.com/issues/31",
        report_path=artifact_dir / "runs" / run_id / "plan.md",
        pr_draft_path=artifact_dir / "runs" / run_id / "pr.md",
        audit_path=artifact_dir / "runs" / run_id / "run.json",
    )
    run_record.audit_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "issue": {
            "repo_full_name": "acme/widgets",
            "issue_number": 31,
            "title": "Enable flag in sandbox",
            "body": "The flag should be enabled in an isolated workspace.",
            "labels": ["bug"],
            "url": "https://example.com/issues/31",
        },
        "plan": {
            "summary": "Turn the module flag on in isolation.",
            "files_to_inspect": ["flag_module.py"],
            "tests": ["python3 -m unittest discover -s tests -v"],
            "branch_name": "agent/issue-31",
            "pr_title": "Enable sandbox flag",
            "pr_body": "Enable the module flag inside the sandbox workspace.",
        },
        "planning_context": {
            "summary": "The issue affects a single module-level flag.",
            "issue_keywords": ["flag", "sandbox"],
            "repository_profile": {
                "primary_language": "python",
                "detected_languages": ["python"],
                "detected_frameworks": [],
                "build_systems": [],
                "test_commands": ["python3 -m unittest discover -s tests -v"],
            },
            "ranked_files": [
                {
                    "path": "flag_module.py",
                    "score": 10,
                    "reasons": ["Matches the issue keyword and failing behavior."],
                    "preview": "FLAG = False",
                }
            ],
            "suggested_test_commands": ["python3 -m unittest discover -s tests -v"],
        },
    }
    repository.save_run(run_record, payload)
    return run_id


def _flag_line(content: str) -> str:
    for line in content.splitlines():
        if line.startswith("FLAG ="):
            return line
    raise AssertionError("FLAG line not found in file context.")


if __name__ == "__main__":
    unittest.main()
