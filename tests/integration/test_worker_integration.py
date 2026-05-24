from __future__ import annotations

from datetime import datetime, timezone
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
from issue_to_pr_agent.domain.entities import QueueJobStatus
from issue_to_pr_agent.infrastructure.config.settings import Settings
from issue_to_pr_agent.infrastructure.persistence.run_repository import RunRepository
from issue_to_pr_agent.observability.metrics import QueueMetricsReporter


class FakeGitHubClient:
    def fetch_issue(self, repo_full_name: str, issue_number: int):
        from issue_to_pr_agent.domain.entities import IssueContext
        return IssueContext(
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            title="Integrate graceful shutdown",
            body="Verify graceful shutdown.",
            labels=[],
            url=f"https://example.com/{repo_full_name}/issues/{issue_number}",
        )


class TestWorkerIntegration(unittest.TestCase):
    def test_graceful_shutdown_blocks_processing(self) -> None:
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

            # Enqueue a plan job
            queued = manage.enqueue_plan(
                repo_full_name="acme/widgets",
                issue_number=10,
                repo_root=root,
                provider="heuristic",
                actor="alice",
                team="platform",
            )

            # Initialize worker with shutdown_requested always returning True
            processor = ProcessQueueUseCase(
                repository,
                settings,
                access_controller,
                budget_manager,
                metrics_reporter,
                github_client=FakeGitHubClient(),
                shutdown_requested=lambda: True,
            )

            result = processor.process(worker_id="worker-1", max_jobs=5)

            # Succeeded jobs should be 0 because it stopped immediately
            self.assertEqual(result.processed_jobs, 0)
            self.assertEqual(result.succeeded_jobs, 0)
            
            # The job status in the database should still be QUEUED
            job = repository.get_queue_job(queued.job_id)
            self.assertIsNotNone(job)
            self.assertEqual(job[0].status, QueueJobStatus.QUEUED)
