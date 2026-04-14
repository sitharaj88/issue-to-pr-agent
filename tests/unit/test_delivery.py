from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.application.use_cases.deliver_run import DeliverRunUseCase
from issue_to_pr_agent.application.use_cases.manage_approval import RequestApprovalUseCase, ReviewApprovalUseCase
from issue_to_pr_agent.application.services.approval_policy import ApprovalPolicyEvaluator
from issue_to_pr_agent.application.services.delivery_governance import DeliveryGovernancePolicyEvaluator
from issue_to_pr_agent.application.services.delivery_summary import DeliverySummaryBuilder
from issue_to_pr_agent.domain.entities import (
    ApprovalDecision,
    DeliveryStatus,
    ExecutionMode,
    IssueCommentSummary,
    PatchProposalRecord,
    PatchExecutionMode,
    PatchExecutionRecord,
    PatchExecutionStatus,
    PatcherProvider,
    PlannerProvider,
    PullRequestSummary,
    RunRecord,
    RunStatus,
    VerificationRecord,
    VerificationStatus,
    VerificationStopReason,
)
from issue_to_pr_agent.domain.policies.safety import SafetyPolicy
from issue_to_pr_agent.infrastructure.persistence.run_repository import RunRepository


class FakeGitHubClient:
    def __init__(self, *, branch_protected: bool = True) -> None:
        self.created_prs: list[dict[str, object]] = []
        self.comments: list[dict[str, object]] = []
        self.branch_protected = branch_protected

    def fetch_repository(self, repo_full_name: str):
        return type(
            "RepositoryInfo",
            (),
            {
                "repo_full_name": repo_full_name,
                "default_branch": "main",
                "html_url": f"https://example.com/{repo_full_name}",
            },
        )()

    def fetch_branch_protection(self, repo_full_name: str, branch_name: str) -> bool:
        return self.branch_protected

    def create_pull_request(
        self,
        repo_full_name: str,
        *,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
        draft: bool = True,
    ) -> PullRequestSummary:
        self.created_prs.append(
            {
                "repo_full_name": repo_full_name,
                "title": title,
                "body": body,
                "head_branch": head_branch,
                "base_branch": base_branch,
                "draft": draft,
            }
        )
        return PullRequestSummary(
            number=17,
            url=f"https://api.example.com/repos/{repo_full_name}/pulls/17",
            html_url=f"https://example.com/{repo_full_name}/pull/17",
            title=title,
        )

    def add_issue_comment(self, repo_full_name: str, issue_number: int, *, body: str) -> IssueCommentSummary:
        self.comments.append(
            {
                "repo_full_name": repo_full_name,
                "issue_number": issue_number,
                "body": body,
            }
        )
        return IssueCommentSummary(
            comment_id=19,
            url=f"https://api.example.com/repos/{repo_full_name}/issues/{issue_number}/comments/19",
            html_url=f"https://example.com/{repo_full_name}/pull/{issue_number}#issuecomment-19",
        )


class DeliveryUseCaseTests(unittest.TestCase):
    def test_delivery_summary_can_publish_artifacts_to_shared_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            run_dir = artifact_dir / "runs" / "run-1"
            store_dir = root / "shared-artifacts"
            run_dir.mkdir(parents=True, exist_ok=True)
            report_path = run_dir / "plan.md"
            report_path.write_text("plan\n", encoding="utf-8")

            builder = DeliverySummaryBuilder(
                artifact_dir=artifact_dir,
                artifact_store_backend="shared",
                artifact_store_dir=store_dir,
                artifact_store_base_url="https://artifacts.example.com/shared/",
            )

            artifacts = builder.build_artifact_references(
                run_payload={"artifacts": {"report_path": str(report_path)}},
                execution_payload={},
                verification_payload={},
            )

            self.assertEqual(len(artifacts), 1)
            self.assertEqual(
                Path(artifacts[0].path).resolve(),
                (store_dir / "runs" / "run-1" / "plan.md").resolve(),
            )
            self.assertTrue((store_dir / "runs" / "run-1" / "plan.md").exists())
            self.assertEqual(
                artifacts[0].url,
                "https://artifacts.example.com/shared/runs/run-1/plan.md",
            )

    def test_deliver_run_commits_pushes_and_records_draft_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            remote = Path(tmp) / "remote.git"
            root.mkdir()
            self._init_repo(root, remote)

            artifact_dir = root / ".issue-to-pr"
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            self._seed_run(repository, artifact_dir, root)
            self._seed_execution(repository, artifact_dir, root)
            self._seed_verification(repository, artifact_dir, root)

            (root / "app.py").write_text("print('after')\n", encoding="utf-8")

            github = FakeGitHubClient()
            result = DeliverRunUseCase(
                repository,
                github,
                SafetyPolicy(branch_prefix="agent/"),
            ).deliver(
                run_id="run-1",
                execution_id="exec-1",
                verification_id="verify-1",
                approval_id=None,
                repo_root=root,
                artifact_dir=artifact_dir,
                artifact_base_url="https://artifacts.example.com/issue-to-pr/",
                remote_name="origin",
                rollout_stage="staging",
            )

            self.assertEqual(result.receipt.status, DeliveryStatus.SUCCEEDED)
            self.assertIsNotNone(result.receipt.commit_sha)
            self.assertIsNotNone(result.receipt.pr)
            self.assertIsNotNone(result.receipt.pr_comment)
            self.assertTrue(result.receipt_path.exists())
            self.assertEqual(self._git_output(root, "rev-parse", "--abbrev-ref", "HEAD"), "agent/issue-1")
            self.assertIn("refs/heads/agent/issue-1", self._git_output(remote, "show-ref", "--heads", git_dir=True))
            self.assertEqual(len(github.created_prs), 1)
            self.assertEqual(github.created_prs[0]["base_branch"], "main")
            self.assertEqual(github.created_prs[0]["head_branch"], "agent/issue-1")
            self.assertIn("Delivery Artifacts", github.created_prs[0]["body"])
            self.assertIn("Rollout stage: `staging`", github.created_prs[0]["body"])
            self.assertEqual(len(github.comments), 1)

            stored = repository.get_delivery(result.delivery_id)
            self.assertIsNotNone(stored)
            _, payload = stored or (None, None)
            self.assertEqual(payload["status"], "succeeded")
            self.assertEqual(payload["pr"]["number"], 17)
            self.assertEqual(payload["rollout_stage"], "staging")
            self.assertTrue(payload["branch_protection_required"])
            self.assertTrue(payload["branch_protection_verified"])
            self.assertIsNotNone(payload["rollback_base_sha"])
            self.assertTrue(payload["artifacts"][0]["url"].startswith("https://artifacts.example.com/issue-to-pr/"))

    def test_deliver_run_fails_on_unexpected_workspace_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            remote = Path(tmp) / "remote.git"
            root.mkdir()
            self._init_repo(root, remote)

            artifact_dir = root / ".issue-to-pr"
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            self._seed_run(repository, artifact_dir, root)
            self._seed_execution(repository, artifact_dir, root)
            self._seed_verification(repository, artifact_dir, root)

            (root / "app.py").write_text("print('after')\n", encoding="utf-8")
            (root / "notes.txt").write_text("unexpected\n", encoding="utf-8")

            github = FakeGitHubClient()
            result = DeliverRunUseCase(
                repository,
                github,
                SafetyPolicy(branch_prefix="agent/"),
            ).deliver(
                run_id="run-1",
                execution_id="exec-1",
                verification_id="verify-1",
                approval_id=None,
                repo_root=root,
                artifact_dir=artifact_dir,
                artifact_base_url=None,
                remote_name="origin",
            )

            self.assertEqual(result.receipt.status, DeliveryStatus.FAILED)
            self.assertIn("notes.txt", result.receipt.error_message or "")
            self.assertEqual(len(github.created_prs), 0)
            self.assertEqual(self._git_output(root, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_deliver_run_requires_approved_request_for_high_risk_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            remote = Path(tmp) / "remote.git"
            root.mkdir()
            self._init_repo(root, remote)

            artifact_dir = root / ".issue-to-pr"
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            self._seed_run(repository, artifact_dir, root)
            self._seed_execution(repository, artifact_dir, root, changed_path=".github/workflows/ci.yml")
            self._seed_verification(repository, artifact_dir, root)

            workflow_dir = root / ".github" / "workflows"
            workflow_dir.mkdir(parents=True, exist_ok=True)
            (workflow_dir / "ci.yml").write_text("name: CI\n", encoding="utf-8")

            github = FakeGitHubClient()
            deliverer = DeliverRunUseCase(
                repository,
                github,
                SafetyPolicy(branch_prefix="agent/"),
                approval_policy=ApprovalPolicyEvaluator(),
            )
            blocked = deliverer.deliver(
                run_id="run-1",
                execution_id="exec-1",
                verification_id="verify-1",
                approval_id=None,
                repo_root=root,
                artifact_dir=artifact_dir,
                artifact_base_url=None,
                remote_name="origin",
            )
            self.assertEqual(blocked.receipt.status, DeliveryStatus.FAILED)
            self.assertIn("approved approval request", blocked.receipt.error_message or "")

            approval = RequestApprovalUseCase(repository, ApprovalPolicyEvaluator()).request_delivery_approval(
                run_id="run-1",
                execution_id="exec-1",
                verification_id="verify-1",
                actor="alice",
                team="platform",
            )
            ReviewApprovalUseCase(repository, ApprovalPolicyEvaluator()).decide(
                approval_id=approval.approval_id,
                actor="bob",
                team="platform",
                decision=ApprovalDecision.APPROVE,
            )

            delivered = deliverer.deliver(
                run_id="run-1",
                execution_id="exec-1",
                verification_id="verify-1",
                approval_id=approval.approval_id,
                repo_root=root,
                artifact_dir=artifact_dir,
                artifact_base_url=None,
                remote_name="origin",
            )
            self.assertEqual(delivered.receipt.status, DeliveryStatus.FAILED)
            self.assertIn("rollout stage", delivered.receipt.error_message or "")

            delivered = deliverer.deliver(
                run_id="run-1",
                execution_id="exec-1",
                verification_id="verify-1",
                approval_id=approval.approval_id,
                repo_root=root,
                artifact_dir=artifact_dir,
                artifact_base_url=None,
                remote_name="origin",
                rollout_stage="staging",
            )
            self.assertEqual(delivered.receipt.status, DeliveryStatus.SUCCEEDED)
            self.assertEqual(delivered.receipt.linked_approval_id, approval.approval_id)
            self.assertEqual(len(github.created_prs), 1)

    def test_deliver_run_blocks_unprotected_base_branch_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            remote = Path(tmp) / "remote.git"
            root.mkdir()
            self._init_repo(root, remote)

            artifact_dir = root / ".issue-to-pr"
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            self._seed_run(repository, artifact_dir, root)
            self._seed_execution(repository, artifact_dir, root)
            self._seed_verification(repository, artifact_dir, root)

            (root / "app.py").write_text("print('after')\n", encoding="utf-8")

            result = DeliverRunUseCase(
                repository,
                FakeGitHubClient(branch_protected=False),
                SafetyPolicy(branch_prefix="agent/"),
                delivery_governance_policy=DeliveryGovernancePolicyEvaluator(),
            ).deliver(
                run_id="run-1",
                execution_id="exec-1",
                verification_id="verify-1",
                approval_id=None,
                repo_root=root,
                artifact_dir=artifact_dir,
                artifact_base_url=None,
                remote_name="origin",
                rollout_stage="staging",
            )

            self.assertEqual(result.receipt.status, DeliveryStatus.FAILED)
            self.assertIn("not protected", result.receipt.error_message or "")

    def test_deliver_run_blocks_disallowed_patch_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            remote = Path(tmp) / "remote.git"
            root.mkdir()
            self._init_repo(root, remote)

            artifact_dir = root / ".issue-to-pr"
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            self._seed_run(repository, artifact_dir, root)
            self._seed_execution(repository, artifact_dir, root)
            self._seed_verification(repository, artifact_dir, root)
            self._seed_patch_proposal(
                repository,
                artifact_dir,
                provider=PatcherProvider.OPENAI,
                model="gpt-4.1-mini",
            )

            (root / "app.py").write_text("print('after')\n", encoding="utf-8")

            result = DeliverRunUseCase(
                repository,
                FakeGitHubClient(),
                SafetyPolicy(branch_prefix="agent/"),
                delivery_governance_policy=DeliveryGovernancePolicyEvaluator(
                    policy_overrides={
                        "default": {
                            "allowed_patch_providers": ["openai"],
                            "allowed_patch_models": ["gpt-5.4"],
                        }
                    }
                ),
            ).deliver(
                run_id="run-1",
                execution_id="exec-1",
                verification_id="verify-1",
                approval_id=None,
                repo_root=root,
                artifact_dir=artifact_dir,
                artifact_base_url=None,
                remote_name="origin",
                rollout_stage="staging",
            )

            self.assertEqual(result.receipt.status, DeliveryStatus.FAILED)
            self.assertIn("Patch model 'gpt-4.1-mini'", result.receipt.error_message or "")

    def test_deliver_run_rejects_expired_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            remote = Path(tmp) / "remote.git"
            root.mkdir()
            self._init_repo(root, remote)

            artifact_dir = root / ".issue-to-pr"
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            self._seed_run(repository, artifact_dir, root)
            self._seed_execution(repository, artifact_dir, root, changed_path=".github/workflows/ci.yml")
            self._seed_verification(repository, artifact_dir, root)

            workflow_dir = root / ".github" / "workflows"
            workflow_dir.mkdir(parents=True, exist_ok=True)
            (workflow_dir / "ci.yml").write_text("name: CI\n", encoding="utf-8")

            approval = RequestApprovalUseCase(repository, ApprovalPolicyEvaluator()).request_delivery_approval(
                run_id="run-1",
                execution_id="exec-1",
                verification_id="verify-1",
                actor="alice",
                team="platform",
                expires_in_hours=1,
            )
            ReviewApprovalUseCase(repository, ApprovalPolicyEvaluator()).decide(
                approval_id=approval.approval_id,
                actor="bob",
                team="platform",
                decision=ApprovalDecision.APPROVE,
            )
            record, payload = repository.get_approval(approval.approval_id) or (None, None)
            self.assertIsNotNone(record)
            payload["expires_at"] = (
                datetime.now(timezone.utc) - timedelta(minutes=10)
            ).isoformat()
            repository.save_approval(record, payload)

            result = DeliverRunUseCase(
                repository,
                FakeGitHubClient(),
                SafetyPolicy(branch_prefix="agent/"),
                approval_policy=ApprovalPolicyEvaluator(),
            ).deliver(
                run_id="run-1",
                execution_id="exec-1",
                verification_id="verify-1",
                approval_id=approval.approval_id,
                repo_root=root,
                artifact_dir=artifact_dir,
                artifact_base_url=None,
                remote_name="origin",
            )

            self.assertEqual(result.receipt.status, DeliveryStatus.FAILED)
            self.assertIn("expired", result.receipt.error_message or "")

    def _seed_run(self, repository: RunRepository, artifact_dir: Path, root: Path) -> None:
        run_dir = artifact_dir / "runs" / "run-1"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / "plan.md"
        pr_draft_path = run_dir / "pr.md"
        audit_path = run_dir / "run.json"
        report_path.write_text("# Plan\n", encoding="utf-8")
        pr_draft_path.write_text("Initial PR draft\n", encoding="utf-8")
        audit_path.write_text("{}\n", encoding="utf-8")
        record = RunRecord(
            run_id="run-1",
            created_at="2026-04-13T10:00:00+00:00",
            repo_full_name="acme/widgets",
            issue_number=1,
            planner_provider=PlannerProvider.HEURISTIC,
            execution_mode=ExecutionMode.PLAN_ONLY,
            status=RunStatus.SUCCEEDED,
            branch_name="agent/issue-1",
            summary="Update app.py for issue #1",
            issue_url="https://example.com/acme/widgets/issues/1",
            report_path=report_path,
            pr_draft_path=pr_draft_path,
            audit_path=audit_path,
        )
        payload = {
            "issue": {
                "repo_full_name": "acme/widgets",
                "issue_number": 1,
                "title": "Update the app output",
                "url": "https://example.com/acme/widgets/issues/1",
            },
            "repo_snapshot": {
                "root": str(root),
                "is_dirty": False,
            },
            "plan": {
                "summary": "Update app.py for issue #1",
                "branch_name": "agent/issue-1",
                "pr_title": "Update app output",
                "pr_body": "Implements the requested output change.",
            },
            "artifacts": {
                "report_path": str(report_path),
                "pr_draft_path": str(pr_draft_path),
                "audit_path": str(audit_path),
            },
        }
        repository.save_run(record, payload)

    def _seed_execution(
        self,
        repository: RunRepository,
        artifact_dir: Path,
        root: Path,
        *,
        changed_path: str = "app.py",
    ) -> None:
        receipt_path = artifact_dir / "runs" / "run-1" / "executions" / "exec-1.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text("{}\n", encoding="utf-8")
        record = PatchExecutionRecord(
            execution_id="exec-1",
            created_at="2026-04-13T10:05:00+00:00",
            proposal_id="proposal-1",
            linked_run_id="run-1",
            mode=PatchExecutionMode.APPLY,
            status=PatchExecutionStatus.SUCCEEDED,
            summary="Applied app.py change",
            repo_root=root,
            receipt_path=receipt_path,
        )
        payload = {
            "execution_id": "exec-1",
            "linked_run_id": "run-1",
            "receipt_path": str(receipt_path),
            "receipts": [
                {
                    "path": changed_path,
                    "changed": True,
                }
            ],
        }
        repository.save_execution(record, payload)

    def _seed_verification(self, repository: RunRepository, artifact_dir: Path, root: Path) -> None:
        verification_dir = artifact_dir / "runs" / "run-1" / "verification"
        verification_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = verification_dir / "verify-1.json"
        stdout_path = verification_dir / "attempt-1.stdout.log"
        stderr_path = verification_dir / "attempt-1.stderr.log"
        receipt_path.write_text("{}\n", encoding="utf-8")
        stdout_path.write_text("ok\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        record = VerificationRecord(
            verification_id="verify-1",
            created_at="2026-04-13T10:10:00+00:00",
            linked_run_id="run-1",
            linked_execution_id="exec-1",
            status=VerificationStatus.SUCCEEDED,
            stop_reason=VerificationStopReason.SUCCESS,
            summary="Verification succeeded",
            repo_root=root,
            receipt_path=receipt_path,
        )
        payload = {
            "verification_id": "verify-1",
            "linked_run_id": "run-1",
            "linked_execution_id": "exec-1",
            "status": "succeeded",
            "stop_reason": "success",
            "receipt_path": str(receipt_path),
            "attempts": [
                {
                    "attempt_index": 1,
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                }
            ],
        }
        repository.save_verification(record, payload)

    def _seed_patch_proposal(
        self,
        repository: RunRepository,
        artifact_dir: Path,
        *,
        provider: PatcherProvider,
        model: str | None,
    ) -> None:
        proposal_path = artifact_dir / "runs" / "run-1" / "patch-proposals" / "proposal-1.json"
        proposal_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "proposal_id": "proposal-1",
            "linked_run_id": "run-1",
            "provider": provider.value,
            "model": model,
            "summary": "Generated patch proposal",
            "operations": [
                {
                    "type": "replace_text",
                    "path": "app.py",
                    "find_text": "print('before')",
                    "replace_text": "print('after')",
                }
            ],
        }
        proposal_path.write_text("{}\n", encoding="utf-8")
        repository.save_patch_proposal(
            PatchProposalRecord(
                proposal_id="proposal-1",
                created_at="2026-04-13T10:04:00+00:00",
                linked_run_id="run-1",
                provider=provider,
                summary="Generated patch proposal",
                proposal_path=proposal_path,
            ),
            payload,
        )

    def _init_repo(self, root: Path, remote: Path) -> None:
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

    def _git_output(self, cwd: Path, *args: str, git_dir: bool = False) -> str:
        command = ["git"]
        if git_dir:
            command.extend(["--git-dir", str(cwd)])
        command.extend(args)
        completed = subprocess.run(
            command,
            cwd=None if git_dir else cwd,
            capture_output=True,
            check=True,
            text=True,
        )
        return completed.stdout.strip()
