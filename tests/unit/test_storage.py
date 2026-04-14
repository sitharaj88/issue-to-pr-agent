from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.domain.entities import (
    AlertRecord,
    AlertSeverity,
    AlertStatus,
    ApprovalAction,
    ApprovalRecord,
    ApprovalRiskLevel,
    ApprovalStatus,
    AutofixAttemptRecord,
    AutofixAttemptStatus,
    AutofixRunRecord,
    AutofixStatus,
    NotificationEventType,
    NotificationRecord,
    NotificationStatus,
    QueueAttemptRecord,
    QueueAttemptStatus,
    QueueJobRecord,
    QueueJobStatus,
    QueueJobType,
    PatchProposalRecord,
    PatcherProvider,
    TenantMembershipRecord,
    TenantRecord,
    TenantRole,
    TenantStatus,
    TraceEventRecord,
    DeliveryRecord,
    DeliveryStatus,
    ExecutionMode,
    PatchExecutionMode,
    PatchExecutionRecord,
    PatchExecutionStatus,
    PlannerProvider,
    RunRecord,
    RunStatus,
    SandboxRecord,
    SandboxStatus,
    VerificationRecord,
    VerificationStatus,
    VerificationStopReason,
    WorkerHeartbeatRecord,
    WorkerStatus,
)
from issue_to_pr_agent.infrastructure.persistence.run_repository import RunRepository


class RunRepositoryTests(unittest.TestCase):
    def test_schema_migrations_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "runs.sqlite3"
            repository = RunRepository(db_path)
            migrations = repository.list_schema_migrations()
            self.assertGreaterEqual(len(migrations), 4)
            self.assertEqual(repository.current_schema_version(), migrations[-1].version)

    def test_save_and_load_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "runs.sqlite3"
            repository = RunRepository(db_path)
            record = RunRecord(
                run_id="abc123",
                created_at="2026-04-13T10:00:00+00:00",
                repo_full_name="acme/widgets",
                issue_number=42,
                planner_provider=PlannerProvider.HEURISTIC,
                execution_mode=ExecutionMode.PLAN_ONLY,
                status=RunStatus.SUCCEEDED,
                branch_name="agent/issue-42",
                summary="Investigate the issue and prepare a patch.",
                issue_url="https://example.com/issues/42",
                report_path=Path("/tmp/report.md"),
                pr_draft_path=Path("/tmp/pr.md"),
                audit_path=Path("/tmp/run.json"),
            )
            payload = {"run_id": "abc123", "status": "succeeded"}
            repository.save_run(record, payload)

            stored = repository.get_run("abc123")
            self.assertIsNotNone(stored)
            loaded_record, loaded_payload = stored or (None, None)
            self.assertEqual(loaded_record.repo_full_name, "acme/widgets")
            self.assertEqual(loaded_payload["run_id"], "abc123")

            runs = repository.list_runs(limit=5)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].run_id, "abc123")

    def test_save_and_load_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "runs.sqlite3"
            repository = RunRepository(db_path)
            record = PatchExecutionRecord(
                execution_id="exec123",
                created_at="2026-04-13T10:05:00+00:00",
                proposal_id="proposal-1",
                linked_run_id="abc123",
                mode=PatchExecutionMode.DRY_RUN,
                status=PatchExecutionStatus.SUCCEEDED,
                summary="Patch receipt",
                repo_root=Path("/tmp/repo"),
                receipt_path=Path("/tmp/receipt.json"),
            )
            payload = {"execution_id": "exec123", "status": "succeeded"}
            repository.save_execution(record, payload)

            stored = repository.get_execution("exec123")
            self.assertIsNotNone(stored)
            loaded_record, loaded_payload = stored or (None, None)
            self.assertEqual(loaded_record.proposal_id, "proposal-1")
            self.assertEqual(loaded_payload["execution_id"], "exec123")

            executions = repository.list_executions(limit=5)
            self.assertEqual(len(executions), 1)
            self.assertEqual(executions[0].execution_id, "exec123")

    def test_save_and_load_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "runs.sqlite3"
            repository = RunRepository(db_path)
            record = VerificationRecord(
                verification_id="verify123",
                created_at="2026-04-13T10:10:00+00:00",
                linked_run_id="abc123",
                linked_execution_id="exec123",
                status=VerificationStatus.SUCCEEDED,
                stop_reason=VerificationStopReason.SUCCESS,
                summary="Verification receipt",
                repo_root=Path("/tmp/repo"),
                receipt_path=Path("/tmp/verification.json"),
            )
            payload = {"verification_id": "verify123", "status": "succeeded"}
            repository.save_verification(record, payload)

            stored = repository.get_verification("verify123")
            self.assertIsNotNone(stored)
            loaded_record, loaded_payload = stored or (None, None)
            self.assertEqual(loaded_record.stop_reason, VerificationStopReason.SUCCESS)
            self.assertEqual(loaded_payload["verification_id"], "verify123")

            verifications = repository.list_verifications(limit=5)
            self.assertEqual(len(verifications), 1)
            self.assertEqual(verifications[0].verification_id, "verify123")

    def test_save_and_load_autofix_run_and_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "runs.sqlite3"
            repository = RunRepository(db_path)
            run_record = AutofixRunRecord(
                autofix_id="autofix123",
                created_at="2026-04-14T10:00:00+00:00",
                updated_at="2026-04-14T10:05:00+00:00",
                linked_run_id="run123",
                provider=PatcherProvider.OPENAI,
                status=AutofixStatus.SUCCEEDED,
                summary="Autofix succeeded after 2 attempts.",
                repo_root=Path("/tmp/repo"),
                max_attempts=3,
                attempt_count=2,
                latest_proposal_id="proposal-2",
                latest_execution_id="exec-2",
                latest_verification_id="verify-2",
                receipt_path=Path("/tmp/autofix.json"),
            )
            repository.save_autofix_run(
                run_record,
                {"autofix_id": "autofix123", "status": "succeeded", "attempt_count": 2},
            )
            attempt_record = AutofixAttemptRecord(
                attempt_id="attempt-1",
                autofix_id="autofix123",
                attempt_index=1,
                created_at="2026-04-14T10:01:00+00:00",
                status=AutofixAttemptStatus.FAILED,
                summary="Initial patch did not pass verification.",
                objective="Fix the flag module.",
                proposal_id="proposal-1",
                execution_id="exec-1",
                verification_id="verify-1",
                verification_stop_reason=VerificationStopReason.MAX_ATTEMPTS_REACHED,
                payload_path=Path("/tmp/autofix-attempt-1.json"),
            )
            repository.save_autofix_attempt(
                attempt_record,
                {"attempt_id": "attempt-1", "autofix_id": "autofix123", "status": "failed"},
            )

            stored_run = repository.get_autofix_run("autofix123")
            self.assertIsNotNone(stored_run)
            loaded_run, loaded_payload = stored_run or (None, None)
            self.assertEqual(loaded_run.status, AutofixStatus.SUCCEEDED)
            self.assertEqual(loaded_payload["attempt_count"], 2)

            autofix_runs = repository.list_autofix_runs(limit=5)
            self.assertEqual(len(autofix_runs), 1)
            self.assertEqual(autofix_runs[0].autofix_id, "autofix123")

            attempts = repository.list_autofix_attempts(autofix_id="autofix123", limit=5)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0].verification_stop_reason, VerificationStopReason.MAX_ATTEMPTS_REACHED)

    def test_save_and_load_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "runs.sqlite3"
            repository = RunRepository(db_path)
            record = SandboxRecord(
                sandbox_id="sandbox123",
                created_at="2026-04-14T11:00:00+00:00",
                updated_at="2026-04-14T11:05:00+00:00",
                linked_run_id="run123",
                linked_autofix_id="autofix123",
                status=SandboxStatus.USED,
                source_repo_root=Path("/tmp/repo"),
                workspace_root=Path("/tmp/repo/.issue-to-pr/sandboxes/sandbox123/workspace"),
                copied_file_count=4,
                skipped_entry_count=2,
                total_bytes=4096,
                summary="Sandbox used by autofix.",
                receipt_path=Path("/tmp/sandbox.json"),
            )
            repository.save_sandbox(
                record,
                {"sandbox_id": "sandbox123", "status": "used", "copied_file_count": 4, "skipped_entries": []},
            )

            stored = repository.get_sandbox("sandbox123")
            self.assertIsNotNone(stored)
            loaded_record, loaded_payload = stored or (None, None)
            self.assertEqual(loaded_record.status, SandboxStatus.USED)
            self.assertEqual(loaded_payload["copied_file_count"], 4)

            sandboxes = repository.list_sandboxes(limit=5)
            self.assertEqual(len(sandboxes), 1)
            self.assertEqual(sandboxes[0].sandbox_id, "sandbox123")

    def test_save_and_load_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "runs.sqlite3"
            repository = RunRepository(db_path)
            record = DeliveryRecord(
                delivery_id="delivery123",
                created_at="2026-04-13T10:15:00+00:00",
                linked_run_id="abc123",
                linked_execution_id="exec123",
                linked_verification_id="verify123",
                status=DeliveryStatus.SUCCEEDED,
                repo_full_name="acme/widgets",
                branch_name="agent/issue-42",
                base_branch="main",
                summary="Delivery receipt",
                receipt_path=Path("/tmp/delivery.json"),
            )
            payload = {"delivery_id": "delivery123", "status": "succeeded"}
            repository.save_delivery(record, payload)

            stored = repository.get_delivery("delivery123")
            self.assertIsNotNone(stored)
            loaded_record, loaded_payload = stored or (None, None)
            self.assertEqual(loaded_record.branch_name, "agent/issue-42")
            self.assertEqual(loaded_payload["delivery_id"], "delivery123")

            deliveries = repository.list_deliveries(limit=5)
            self.assertEqual(len(deliveries), 1)
            self.assertEqual(deliveries[0].delivery_id, "delivery123")

    def test_save_and_load_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "runs.sqlite3"
            repository = RunRepository(db_path)
            record = ApprovalRecord(
                approval_id="approval123",
                created_at="2026-04-13T10:20:00+00:00",
                updated_at="2026-04-13T10:20:00+00:00",
                action=ApprovalAction.DELIVERY,
                linked_run_id="abc123",
                linked_execution_id="exec123",
                linked_verification_id="verify123",
                repo_full_name="acme/widgets",
                status=ApprovalStatus.PENDING,
                risk_level=ApprovalRiskLevel.HIGH,
                requested_by="alice",
                requester_team="platform",
                required_approvals=1,
                approved_count=0,
                summary="High-risk approval is pending.",
                receipt_path=Path("/tmp/approval.json"),
            )
            payload = {"approval_id": "approval123", "status": "pending"}
            repository.save_approval(record, payload)

            stored = repository.get_approval("approval123")
            self.assertIsNotNone(stored)
            loaded_record, loaded_payload = stored or (None, None)
            self.assertEqual(loaded_record.requested_by, "alice")
            self.assertEqual(loaded_payload["approval_id"], "approval123")

            approvals = repository.list_approvals(limit=5, status=ApprovalStatus.PENDING)
            self.assertEqual(len(approvals), 1)
            self.assertEqual(approvals[0].approval_id, "approval123")

    def test_save_and_load_tenant_membership_and_notification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "runs.sqlite3"
            repository = RunRepository(db_path)
            tenant_record = TenantRecord(
                tenant_id="tenant-1",
                created_at="2026-04-13T10:25:00+00:00",
                updated_at="2026-04-13T10:25:00+00:00",
                name="Acme",
                status=TenantStatus.ACTIVE,
                summary="Acme tenant",
                config_path=Path("/tmp/tenant.json"),
            )
            repository.save_tenant(
                tenant_record,
                {"tenant_id": "tenant-1", "repo_patterns": ["acme/*"], "policy_overrides": {}},
            )
            membership_record = TenantMembershipRecord(
                tenant_id="tenant-1",
                actor="alice",
                role=TenantRole.ADMIN,
                team="platform",
                created_at="2026-04-13T10:25:00+00:00",
                updated_at="2026-04-13T10:25:00+00:00",
            )
            repository.save_tenant_membership(
                membership_record,
                {"tenant_id": "tenant-1", "actor": "alice", "role": "admin", "team": "platform"},
            )
            notification_record = NotificationRecord(
                notification_id="notice123",
                created_at="2026-04-13T10:30:00+00:00",
                tenant_id="tenant-1",
                event_type=NotificationEventType.APPROVAL_REQUESTED,
                status=NotificationStatus.EMITTED,
                summary="Approval queued",
                payload_path=Path("/tmp/notice.json"),
            )
            repository.save_notification(
                notification_record,
                {"notification_id": "notice123", "tenant_id": "tenant-1", "event_type": "approval_requested"},
            )

            stored_tenant = repository.get_tenant("tenant-1")
            self.assertIsNotNone(stored_tenant)
            tenant_loaded, tenant_payload = stored_tenant or (None, None)
            self.assertEqual(tenant_loaded.name, "Acme")
            self.assertEqual(tenant_payload["tenant_id"], "tenant-1")

            stored_membership = repository.get_tenant_membership("tenant-1", "alice")
            self.assertIsNotNone(stored_membership)
            membership_loaded, membership_payload = stored_membership or (None, None)
            self.assertEqual(membership_loaded.role, TenantRole.ADMIN)
            self.assertEqual(membership_payload["actor"], "alice")

            notifications = repository.list_notifications(tenant_id="tenant-1", limit=5)
            self.assertEqual(len(notifications), 1)
            self.assertEqual(notifications[0].notification_id, "notice123")

    def test_save_and_load_alert_and_trace_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "runs.sqlite3"
            repository = RunRepository(db_path)
            alert_record = AlertRecord(
                alert_id="alert123",
                created_at="2026-04-14T08:00:00+00:00",
                tenant_id="tenant-1",
                severity=AlertSeverity.ERROR,
                source="queue_worker",
                status=AlertStatus.OPEN,
                summary="Queue job failed.",
                payload_path=Path("/tmp/alert.json"),
            )
            repository.save_alert(
                alert_record,
                {"alert_id": "alert123", "tenant_id": "tenant-1", "severity": "error"},
            )
            trace_record = TraceEventRecord(
                event_id="traceevt123",
                trace_id="trace123",
                recorded_at="2026-04-14T08:05:00+00:00",
                source="http_api",
                span_name="GET /v1/runs",
                status="completed",
                payload_path=Path("/tmp/trace.json"),
                linked_run_id="run-1",
                linked_job_id="job-1",
            )
            repository.save_trace_event(
                trace_record,
                {"event_id": "traceevt123", "trace_id": "trace123", "status": "completed"},
            )

            alert = repository.get_alert("alert123")
            self.assertIsNotNone(alert)
            loaded_alert, alert_payload = alert or (None, None)
            self.assertEqual(loaded_alert.severity, AlertSeverity.ERROR)
            self.assertEqual(alert_payload["alert_id"], "alert123")

            alerts = repository.list_alerts(tenant_id="tenant-1", limit=5)
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].alert_id, "alert123")

            traces = repository.list_trace_events(trace_id="trace123", limit=5)
            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0].event_id, "traceevt123")

    def test_save_and_load_queue_job_attempt_and_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "runs.sqlite3"
            repository = RunRepository(db_path)
            job_record = QueueJobRecord(
                job_id="job123",
                created_at="2026-04-13T11:00:00+00:00",
                updated_at="2026-04-13T11:00:00+00:00",
                job_type=QueueJobType.PLAN,
                status=QueueJobStatus.QUEUED,
                repo_full_name="acme/widgets",
                issue_number=7,
                priority=1,
                requested_by="alice",
                tenant_id=None,
                worker_id=None,
                attempt_count=0,
                max_attempts=3,
                budget_units=6,
                budget_used=0,
                next_run_at="2026-04-13T11:00:00+00:00",
                summary="Queued plan",
                receipt_path=Path("/tmp/queue-job.json"),
            )
            repository.save_queue_job(job_record, {"job_id": "job123", "status": "queued"})

            stored_job = repository.get_queue_job("job123")
            self.assertIsNotNone(stored_job)
            loaded_job, loaded_job_payload = stored_job or (None, None)
            self.assertEqual(loaded_job.job_type, QueueJobType.PLAN)
            self.assertEqual(loaded_job_payload["job_id"], "job123")

            claimed = repository.claim_next_queue_job(
                worker_id="worker-1",
                now="2026-04-13T11:01:00+00:00",
            )
            self.assertIsNotNone(claimed)
            claimed_record, claimed_payload = claimed or (None, None)
            self.assertEqual(claimed_record.status, QueueJobStatus.RUNNING)
            self.assertEqual(claimed_record.attempt_count, 1)
            self.assertEqual(claimed_payload["worker_id"], "worker-1")

            attempt_record = QueueAttemptRecord(
                attempt_id="attempt123",
                job_id="job123",
                attempt_index=1,
                created_at="2026-04-13T11:01:00+00:00",
                finished_at="2026-04-13T11:01:05+00:00",
                worker_id="worker-1",
                status=QueueAttemptStatus.SUCCEEDED,
                summary="Attempt succeeded",
                payload_path=Path("/tmp/attempt.json"),
            )
            repository.save_queue_attempt(attempt_record, {"attempt_id": "attempt123", "job_id": "job123"})
            attempts = repository.list_queue_attempts("job123")
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0].attempt_id, "attempt123")

            heartbeat_record = WorkerHeartbeatRecord(
                worker_id="worker-1",
                recorded_at="2026-04-13T11:02:00+00:00",
                status=WorkerStatus.IDLE,
                current_job_id=None,
                summary="Worker idle",
                processed_jobs=1,
                succeeded_jobs=1,
                failed_jobs=0,
                cancelled_jobs=0,
                payload_path=Path("/tmp/heartbeat.json"),
            )
            repository.save_worker_heartbeat(
                heartbeat_record,
                {"worker_id": "worker-1", "recorded_at": "2026-04-13T11:02:00+00:00"},
            )
            heartbeats = repository.list_worker_heartbeats(worker_id="worker-1", limit=5)
            self.assertEqual(len(heartbeats), 1)
            self.assertEqual(heartbeats[0].worker_id, "worker-1")

    def test_save_and_load_patch_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "runs.sqlite3"
            repository = RunRepository(db_path)
            record = PatchProposalRecord(
                proposal_id="proposal123",
                created_at="2026-04-14T09:00:00+00:00",
                linked_run_id="run-1",
                provider=PatcherProvider.OPENAI,
                summary="Generated patch proposal",
                proposal_path=Path("/tmp/proposal.json"),
            )
            payload = {"proposal_id": "proposal123", "linked_run_id": "run-1"}
            repository.save_patch_proposal(record, payload)

            stored = repository.get_patch_proposal("proposal123")
            self.assertIsNotNone(stored)
            loaded_record, loaded_payload = stored or (None, None)
            self.assertEqual(loaded_record.provider, PatcherProvider.OPENAI)
            self.assertEqual(loaded_payload["proposal_id"], "proposal123")

            proposals = repository.list_patch_proposals(limit=5)
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0].proposal_id, "proposal123")
