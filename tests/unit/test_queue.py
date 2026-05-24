from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.application.services.queue_budget import QueueBudgetManager
from issue_to_pr_agent.application.services.tenant_access import TenantAccessController
from issue_to_pr_agent.application.use_cases.manage_queue import ManageQueueUseCase
from issue_to_pr_agent.application.use_cases.process_queue import ProcessQueueUseCase
from issue_to_pr_agent.domain.entities import IssueContext, QueueJobRecord, QueueJobStatus, QueueJobType
from issue_to_pr_agent.infrastructure.config.settings import Settings
from issue_to_pr_agent.infrastructure.persistence.run_repository import RunRepository
from issue_to_pr_agent.observability.metrics import QueueMetricsReporter


class FakeGitHubClient:
    def fetch_issue(self, repo_full_name: str, issue_number: int) -> IssueContext:
        return IssueContext(
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            title="Handle missing config",
            body="The agent should fail with a clear error when config is missing.",
            labels=["bug"],
            url=f"https://example.com/{repo_full_name}/issues/{issue_number}",
        )


class FailingGitHubClient:
    def fetch_issue(self, repo_full_name: str, issue_number: int) -> IssueContext:
        raise RuntimeError(f"Unable to fetch issue {repo_full_name}#{issue_number}")


class QueueWorkflowTests(unittest.TestCase):
    def test_enqueue_plan_and_process_worker_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("demo\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"ISSUE_TO_PR_QUEUE_BACKOFF_SECONDS": "0"}, clear=True):
                settings = Settings.from_env(cwd=root)

            repository = RunRepository(settings.database_path)
            access_controller = TenantAccessController(repository)
            budget_manager = QueueBudgetManager(settings, repository)
            metrics_reporter = QueueMetricsReporter(repository)
            manage = ManageQueueUseCase(repository, settings, access_controller, budget_manager)

            queued = manage.enqueue_plan(
                repo_full_name="acme/widgets",
                issue_number=7,
                repo_root=root,
                provider="heuristic",
                actor="alice",
                team="platform",
            )

            processor = ProcessQueueUseCase(
                repository,
                settings,
                access_controller,
                budget_manager,
                metrics_reporter,
                github_client=FakeGitHubClient(),
            )
            result = processor.process(worker_id="worker-1", max_jobs=1)

            self.assertEqual(result.processed_jobs, 1)
            self.assertEqual(result.succeeded_jobs, 1)
            job = repository.get_queue_job(queued.job_id)
            self.assertIsNotNone(job)
            job_record, job_payload = job or (None, None)
            self.assertEqual(job_record.status, QueueJobStatus.SUCCEEDED)
            self.assertEqual(job_record.budget_used, settings.budget_cost_plan_heuristic)
            self.assertTrue(job_payload["linked_run_id"])
            self.assertEqual(len(repository.list_queue_attempts(queued.job_id)), 1)
            self.assertTrue(result.heartbeat_path.exists())
            self.assertTrue(result.metrics_json_path.exists())
            self.assertTrue(result.metrics_prom_path.exists())

    def test_cancel_and_resume_queue_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("demo\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"ISSUE_TO_PR_QUEUE_BACKOFF_SECONDS": "0"}, clear=True):
                settings = Settings.from_env(cwd=root)

            repository = RunRepository(settings.database_path)
            access_controller = TenantAccessController(repository)
            budget_manager = QueueBudgetManager(settings, repository)
            manage = ManageQueueUseCase(repository, settings, access_controller, budget_manager)

            queued = manage.enqueue_plan(
                repo_full_name="acme/widgets",
                issue_number=8,
                repo_root=root,
                provider="heuristic",
                actor="alice",
                team="platform",
            )
            cancelled = manage.cancel_job(job_id=queued.job_id, actor="alice", team="platform")
            self.assertEqual(cancelled.status, QueueJobStatus.CANCELLED)

            resumed = manage.resume_job(
                job_id=queued.job_id,
                actor="alice",
                team="platform",
                reset_attempts=True,
            )
            self.assertEqual(resumed.status, QueueJobStatus.QUEUED)

    def test_worker_retries_failed_job_and_marks_terminal_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("demo\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"ISSUE_TO_PR_QUEUE_BACKOFF_SECONDS": "0"}, clear=True):
                settings = Settings.from_env(cwd=root)

            repository = RunRepository(settings.database_path)
            access_controller = TenantAccessController(repository)
            budget_manager = QueueBudgetManager(settings, repository)
            metrics_reporter = QueueMetricsReporter(repository)
            manage = ManageQueueUseCase(repository, settings, access_controller, budget_manager)

            queued = manage.enqueue_plan(
                repo_full_name="acme/widgets",
                issue_number=9,
                repo_root=root,
                provider="heuristic",
                actor="alice",
                team="platform",
                max_attempts=2,
                budget_units=2,
            )
            processor = ProcessQueueUseCase(
                repository,
                settings,
                access_controller,
                budget_manager,
                metrics_reporter,
                github_client=FailingGitHubClient(),
            )

            first = processor.process(worker_id="worker-1", max_jobs=1)
            self.assertEqual(first.failed_jobs, 1)
            first_job = repository.get_queue_job(queued.job_id)
            self.assertIsNotNone(first_job)
            self.assertEqual(first_job[0].status, QueueJobStatus.QUEUED)

            second = processor.process(worker_id="worker-1", max_jobs=1)
            self.assertEqual(second.failed_jobs, 1)
            second_job = repository.get_queue_job(queued.job_id)
            self.assertIsNotNone(second_job)
            self.assertEqual(second_job[0].status, QueueJobStatus.FAILED)
            self.assertEqual(second_job[0].attempt_count, 2)
            self.assertEqual(second_job[0].budget_used, 2)
            self.assertEqual(len(repository.list_queue_attempts(queued.job_id)), 2)

    def test_claim_next_queue_job_respects_worker_tags_and_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("demo\n", encoding="utf-8")
            settings = Settings.from_env(cwd=root)
            repository = RunRepository(settings.database_path)
            access_controller = TenantAccessController(repository)
            budget_manager = QueueBudgetManager(settings, repository)
            manage = ManageQueueUseCase(repository, settings, access_controller, budget_manager)

            tagged = manage.enqueue_plan(
                repo_full_name="acme/widgets",
                issue_number=10,
                repo_root=root,
                provider="heuristic",
                actor="alice",
                team="platform",
                required_worker_tags=["docker", "linux"],
                concurrency_key="repo:acme/widgets",
            )
            blocked = repository.claim_next_queue_job(
                worker_id="worker-1",
                now="2099-01-01T00:00:00+00:00",
                worker_tags=["linux"],
            )
            self.assertIsNone(blocked)

            claimed = repository.claim_next_queue_job(
                worker_id="worker-1",
                now="2099-01-01T00:00:00+00:00",
                worker_tags=["linux", "docker"],
            )
            self.assertIsNotNone(claimed)
            claimed_record, claimed_payload = claimed or (None, None)
            self.assertEqual(claimed_record.job_id, tagged.job_id)
            self.assertEqual(claimed_record.status, QueueJobStatus.RUNNING)
            self.assertEqual(claimed_payload["concurrency_key"], "repo:acme/widgets")

            verify_record = QueueJobRecord(
                job_id="verify-1",
                created_at="2026-04-14T12:00:00+00:00",
                updated_at="2026-04-14T12:00:00+00:00",
                job_type=QueueJobType.VERIFY,
                status=QueueJobStatus.QUEUED,
                repo_full_name="acme/widgets",
                issue_number=None,
                priority=0,
                requested_by="alice",
                tenant_id=None,
                worker_id=None,
                attempt_count=0,
                max_attempts=3,
                budget_units=1,
                budget_used=0,
                next_run_at="2026-04-14T12:00:00+00:00",
                summary="Queued verification",
                receipt_path=settings.artifact_dir / "queue" / "verify-1.json",
                linked_run_id="run-1",
                linked_execution_id="exec-1",
                linked_verification_id=None,
                concurrency_key="repo:acme/widgets",
                required_worker_tags=[],
                lease_token=None,
                lease_expires_at=None,
                rehydration_count=0,
                cancel_requested=False,
                error_message=None,
            )
            repository.save_queue_job(
                verify_record,
                {"job_id": "verify-1", "status": "queued", "concurrency_key": "repo:acme/widgets"},
            )
            second = repository.claim_next_queue_job(
                worker_id="worker-2",
                now="2099-01-01T00:00:00+00:00",
                worker_tags=["docker"],
            )
            self.assertIsNone(second)

            stored = repository.get_queue_job("verify-1")
            self.assertIsNotNone(stored)
            self.assertEqual(stored[0].status, QueueJobStatus.QUEUED)

    def test_requeue_expired_running_job_rehydrates_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings.from_env(cwd=root)
            repository = RunRepository(settings.database_path)
            receipt_path = settings.artifact_dir / "queue" / "job-1.json"
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            running = QueueJobRecord(
                job_id="job-1",
                created_at="2026-04-14T09:00:00+00:00",
                updated_at="2026-04-14T09:01:00+00:00",
                job_type=QueueJobType.PLAN,
                status=QueueJobStatus.RUNNING,
                repo_full_name="acme/widgets",
                issue_number=11,
                priority=0,
                requested_by="alice",
                tenant_id="acme",
                worker_id="worker-a",
                attempt_count=1,
                max_attempts=3,
                budget_units=1,
                budget_used=0,
                next_run_at="2026-04-14T09:00:00+00:00",
                summary="Running plan job",
                receipt_path=receipt_path,
                linked_run_id=None,
                linked_execution_id=None,
                linked_verification_id=None,
                concurrency_key="repo:acme/widgets",
                required_worker_tags=["docker"],
                lease_token="lease-1",
                lease_expires_at="2026-04-14T09:05:00+00:00",
                rehydration_count=0,
                cancel_requested=False,
                error_message=None,
            )
            repository.save_queue_job(
                running,
                {
                    "job_id": "job-1",
                    "status": "running",
                    "worker_id": "worker-a",
                    "lease_token": "lease-1",
                    "lease_expires_at": "2026-04-14T09:05:00+00:00",
                },
            )

            reclaimed = repository.requeue_expired_queue_jobs(now="2026-04-14T09:06:00+00:00")
            self.assertEqual(reclaimed, 1)

            stored = repository.get_queue_job("job-1")
            self.assertIsNotNone(stored)
            record, payload = stored or (None, None)
            self.assertEqual(record.status, QueueJobStatus.QUEUED)
            self.assertIsNone(record.worker_id)
            self.assertIsNone(record.lease_token)
            self.assertIsNone(record.lease_expires_at)
            self.assertEqual(record.rehydration_count, 1)
            self.assertEqual(payload["rehydration_count"], 1)
            self.assertEqual(payload["resume_state"]["last_worker_id"], "worker-a")
            self.assertEqual(payload["resume_state"]["last_lease_token"], "lease-1")

    def test_claim_next_queue_job_prefers_tenant_with_less_running_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings.from_env(cwd=root)
            repository = RunRepository(settings.database_path)
            receipt_dir = settings.artifact_dir / "queue"
            receipt_dir.mkdir(parents=True, exist_ok=True)

            running = QueueJobRecord(
                job_id="running-a",
                created_at="2026-04-14T09:00:00+00:00",
                updated_at="2026-04-14T09:00:00+00:00",
                job_type=QueueJobType.PLAN,
                status=QueueJobStatus.RUNNING,
                repo_full_name="acme/widgets",
                issue_number=12,
                priority=0,
                requested_by="alice",
                tenant_id="tenant-a",
                worker_id="worker-a",
                attempt_count=1,
                max_attempts=3,
                budget_units=1,
                budget_used=0,
                next_run_at="2026-04-14T09:00:00+00:00",
                summary="Already running",
                receipt_path=receipt_dir / "running-a.json",
                linked_run_id=None,
                linked_execution_id=None,
                linked_verification_id=None,
                concurrency_key=None,
                required_worker_tags=[],
                lease_token="lease-running",
                lease_expires_at="2026-04-14T11:00:00+00:00",
                rehydration_count=0,
                cancel_requested=False,
                error_message=None,
            )
            tenant_a_queued = QueueJobRecord(
                job_id="queued-a",
                created_at="2026-04-14T09:01:00+00:00",
                updated_at="2026-04-14T09:01:00+00:00",
                job_type=QueueJobType.VERIFY,
                status=QueueJobStatus.QUEUED,
                repo_full_name="acme/widgets",
                issue_number=None,
                priority=5,
                requested_by="alice",
                tenant_id="tenant-a",
                worker_id=None,
                attempt_count=0,
                max_attempts=3,
                budget_units=1,
                budget_used=0,
                next_run_at="2026-04-14T09:01:00+00:00",
                summary="Queued A",
                receipt_path=receipt_dir / "queued-a.json",
                linked_run_id="run-a",
                linked_execution_id=None,
                linked_verification_id=None,
                concurrency_key=None,
                required_worker_tags=[],
                lease_token=None,
                lease_expires_at=None,
                rehydration_count=0,
                cancel_requested=False,
                error_message=None,
            )
            tenant_b_queued = QueueJobRecord(
                job_id="queued-b",
                created_at="2026-04-14T09:02:00+00:00",
                updated_at="2026-04-14T09:02:00+00:00",
                job_type=QueueJobType.VERIFY,
                status=QueueJobStatus.QUEUED,
                repo_full_name="beta/widgets",
                issue_number=None,
                priority=5,
                requested_by="bob",
                tenant_id="tenant-b",
                worker_id=None,
                attempt_count=0,
                max_attempts=3,
                budget_units=1,
                budget_used=0,
                next_run_at="2026-04-14T09:02:00+00:00",
                summary="Queued B",
                receipt_path=receipt_dir / "queued-b.json",
                linked_run_id="run-b",
                linked_execution_id=None,
                linked_verification_id=None,
                concurrency_key=None,
                required_worker_tags=[],
                lease_token=None,
                lease_expires_at=None,
                rehydration_count=0,
                cancel_requested=False,
                error_message=None,
            )
            repository.save_queue_job(running, {"job_id": "running-a", "status": "running"})
            repository.save_queue_job(tenant_a_queued, {"job_id": "queued-a", "status": "queued"})
            repository.save_queue_job(tenant_b_queued, {"job_id": "queued-b", "status": "queued"})

            claimed = repository.claim_next_queue_job(
                worker_id="worker-z",
                now="2026-04-14T09:03:00+00:00",
                max_running_jobs_per_tenant=5,
            )
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed[0].job_id, "queued-b")


if __name__ == "__main__":
    unittest.main()
