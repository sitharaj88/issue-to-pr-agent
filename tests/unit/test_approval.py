from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.application.services.approval_policy import ApprovalPolicyEvaluator
from issue_to_pr_agent.application.use_cases.manage_approval import RequestApprovalUseCase, ReviewApprovalUseCase
from issue_to_pr_agent.domain.entities import (
    ApprovalDecision,
    ApprovalRiskLevel,
    ApprovalStatus,
    ExecutionMode,
    PatchExecutionMode,
    PatchExecutionRecord,
    PatchExecutionStatus,
    PlannerProvider,
    RunRecord,
    RunStatus,
    VerificationRecord,
    VerificationStatus,
    VerificationStopReason,
)
from issue_to_pr_agent.infrastructure.persistence.run_repository import RunRepository
from issue_to_pr_agent.shared.exceptions import PolicyError


class ApprovalWorkflowTests(unittest.TestCase):
    def test_request_approval_marks_high_risk_delivery_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            self._seed_run(repository, artifact_dir, root, labels=[])
            self._seed_execution(repository, artifact_dir, root, changed_path=".github/workflows/ci.yml")
            self._seed_verification(repository, artifact_dir, root)

            result = RequestApprovalUseCase(
                repository,
                ApprovalPolicyEvaluator(),
            ).request_delivery_approval(
                run_id="run-1",
                execution_id="exec-1",
                verification_id="verify-1",
                actor="alice",
                team="platform",
                comment="Requesting delivery approval",
            )

            self.assertEqual(result.receipt.status, ApprovalStatus.PENDING)
            self.assertEqual(result.receipt.risk_level, ApprovalRiskLevel.HIGH)
            self.assertEqual(result.receipt.required_approvals, 1)
            self.assertIn(".github/workflows/ci.yml", " ".join(result.receipt.reasons))

    def test_review_approval_blocks_self_approval_and_accepts_distinct_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            self._seed_run(repository, artifact_dir, root, labels=[])
            self._seed_execution(repository, artifact_dir, root, changed_path=".github/workflows/ci.yml")
            self._seed_verification(repository, artifact_dir, root)

            requested = RequestApprovalUseCase(
                repository,
                ApprovalPolicyEvaluator(),
            ).request_delivery_approval(
                run_id="run-1",
                execution_id="exec-1",
                verification_id="verify-1",
                actor="alice",
                team="platform",
            )

            reviewer = ReviewApprovalUseCase(repository, ApprovalPolicyEvaluator())
            with self.assertRaises(PolicyError):
                reviewer.decide(
                    approval_id=requested.approval_id,
                    actor="alice",
                    team="platform",
                    decision=ApprovalDecision.APPROVE,
                )

            approved = reviewer.decide(
                approval_id=requested.approval_id,
                actor="bob",
                team="platform",
                decision=ApprovalDecision.APPROVE,
                comment="Approved for delivery",
            )

            self.assertEqual(approved.receipt.status, ApprovalStatus.APPROVED)
            self.assertEqual(approved.receipt.approved_count, 1)
            self.assertEqual(len(approved.receipt.decisions), 1)
            self.assertEqual(approved.receipt.decisions[0].actor, "bob")

    def test_request_approval_rejects_blocked_issue_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            self._seed_run(repository, artifact_dir, root, labels=["do-not-merge"])
            self._seed_execution(repository, artifact_dir, root, changed_path="app.py")
            self._seed_verification(repository, artifact_dir, root)

            result = RequestApprovalUseCase(
                repository,
                ApprovalPolicyEvaluator(),
            ).request_delivery_approval(
                run_id="run-1",
                execution_id="exec-1",
                verification_id="verify-1",
                actor="alice",
                team="platform",
            )

            self.assertEqual(result.receipt.status, ApprovalStatus.REJECTED)
            self.assertIn("do-not-merge", " ".join(result.receipt.blocked_reasons))

    def test_review_approval_rejects_expired_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            self._seed_run(repository, artifact_dir, root, labels=[])
            self._seed_execution(repository, artifact_dir, root, changed_path=".github/workflows/ci.yml")
            self._seed_verification(repository, artifact_dir, root)

            requested = RequestApprovalUseCase(
                repository,
                ApprovalPolicyEvaluator(),
            ).request_delivery_approval(
                run_id="run-1",
                execution_id="exec-1",
                verification_id="verify-1",
                actor="alice",
                team="platform",
                expires_in_hours=1,
            )
            record, payload = repository.get_approval(requested.approval_id) or (None, None)
            self.assertIsNotNone(record)
            payload["expires_at"] = (
                datetime.now(timezone.utc) - timedelta(minutes=5)
            ).isoformat()
            repository.save_approval(record, payload)

            with self.assertRaisesRegex(Exception, "expired"):
                ReviewApprovalUseCase(repository, ApprovalPolicyEvaluator()).decide(
                    approval_id=requested.approval_id,
                    actor="bob",
                    team="platform",
                    decision=ApprovalDecision.APPROVE,
                )

    def test_review_approval_enforces_assigned_reviewer_actor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            self._seed_run(repository, artifact_dir, root, labels=[])
            self._seed_execution(repository, artifact_dir, root, changed_path=".github/workflows/ci.yml")
            self._seed_verification(repository, artifact_dir, root)

            requested = RequestApprovalUseCase(
                repository,
                ApprovalPolicyEvaluator(),
            ).request_delivery_approval(
                run_id="run-1",
                execution_id="exec-1",
                verification_id="verify-1",
                actor="alice",
                team="platform",
                assigned_reviewers=["bob"],
            )

            with self.assertRaises(PolicyError):
                ReviewApprovalUseCase(repository, ApprovalPolicyEvaluator()).decide(
                    approval_id=requested.approval_id,
                    actor="carol",
                    team="platform",
                    decision=ApprovalDecision.APPROVE,
                )

    def _seed_run(
        self,
        repository: RunRepository,
        artifact_dir: Path,
        root: Path,
        *,
        labels: list[str],
    ) -> None:
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
            summary="Change set",
            issue_url="https://example.com/acme/widgets/issues/1",
            report_path=report_path,
            pr_draft_path=pr_draft_path,
            audit_path=audit_path,
        )
        payload = {
            "issue": {
                "repo_full_name": "acme/widgets",
                "issue_number": 1,
                "title": "Update the repository",
                "labels": labels,
                "url": "https://example.com/acme/widgets/issues/1",
            },
            "repo_snapshot": {
                "root": str(root),
                "is_dirty": False,
            },
            "command_assessments": [],
            "plan": {
                "summary": "Change set",
                "branch_name": "agent/issue-1",
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
        changed_path: str,
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
            summary="Applied change",
            repo_root=root,
            receipt_path=receipt_path,
        )
        payload = {
            "execution_id": "exec-1",
            "linked_run_id": "run-1",
            "receipt_path": str(receipt_path),
            "receipts": [{"path": changed_path, "changed": True}],
        }
        repository.save_execution(record, payload)

    def _seed_verification(self, repository: RunRepository, artifact_dir: Path, root: Path) -> None:
        verification_dir = artifact_dir / "runs" / "run-1" / "verification"
        verification_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = verification_dir / "verify-1.json"
        receipt_path.write_text("{}\n", encoding="utf-8")
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
            "attempts": [{"attempt_index": 1}],
            "skipped_commands": [],
        }
        repository.save_verification(record, payload)
