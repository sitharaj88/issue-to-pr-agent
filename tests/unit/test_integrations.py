from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.application.services.approval_policy import ApprovalPolicyEvaluator
from issue_to_pr_agent.application.services.queue_budget import QueueBudgetManager
from issue_to_pr_agent.application.services.tenant_access import TenantAccessController
from issue_to_pr_agent.application.use_cases.manage_approval import RequestApprovalUseCase
from issue_to_pr_agent.application.use_cases.manage_queue import ManageQueueUseCase
from issue_to_pr_agent.application.use_cases.manage_tenant import ManageTenantUseCase
from issue_to_pr_agent.application.use_cases.process_queue import ProcessQueueUseCase
from issue_to_pr_agent.domain.entities import (
    ApprovalStatus,
    DeliveryStatus,
    ExecutionMode,
    NotificationEventType,
    PatchExecutionMode,
    PatchExecutionRecord,
    PatchExecutionStatus,
    PlannerProvider,
    QueueJobStatus,
    RunRecord,
    RunStatus,
    TenantRole,
    VerificationRecord,
    VerificationStatus,
    VerificationStopReason,
)
from issue_to_pr_agent.infrastructure.config.settings import Settings
from issue_to_pr_agent.infrastructure.notifications import FileNotificationOutbox
from issue_to_pr_agent.infrastructure.persistence.run_repository import RunRepository
from issue_to_pr_agent.interfaces.http.app import ControlPlaneApi
from issue_to_pr_agent.observability.metrics import QueueMetricsReporter


class ExplodingGitHubClient:
    def fetch_issue(self, repo_full_name: str, issue_number: int):  # pragma: no cover - should never be called
        raise AssertionError(f"GitHub issue fetch should not be used for external issue {repo_full_name}#{issue_number}")


class RecordingSlackClient:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def send_event(self, *, event_type: str, summary: str, payload: dict[str, object]) -> None:
        self.events.append((event_type, summary, payload))


class RecordingTeamsClient:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def send_event(self, *, event_type: str, summary: str, payload: dict[str, object]) -> None:
        self.events.append((event_type, summary, payload))


class RecordingJiraClient:
    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []

    def add_comment(self, issue_key: str, body: str) -> None:
        self.comments.append((issue_key, body))


class EnterpriseIntegrationTests(unittest.TestCase):
    def test_external_plan_job_uses_provided_issue_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("demo\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"ISSUE_TO_PR_ARTIFACT_DIR": str(root / ".issue-to-pr")}, clear=True):
                settings = Settings.from_env(cwd=root)

            repository = RunRepository(settings.database_path)
            access_controller = TenantAccessController(repository)
            budget_manager = QueueBudgetManager(settings, repository)
            metrics_reporter = QueueMetricsReporter(repository)

            queued = ManageQueueUseCase(repository, settings, access_controller, budget_manager).enqueue_external_plan(
                repo_full_name="acme/widgets",
                external_key="ENG-123",
                external_title="Enable the platform flag",
                external_body="The Jira ticket should drive the same planning flow.",
                external_labels=["platform", "bug"],
                external_url="https://jira.example.com/browse/ENG-123",
                source_system="jira",
                repo_root=root,
                provider="heuristic",
                actor="webhook-bot",
                team="automation",
            )

            result = ProcessQueueUseCase(
                repository,
                settings,
                access_controller,
                budget_manager,
                metrics_reporter,
                github_client=ExplodingGitHubClient(),
            ).process(worker_id="worker-1", max_jobs=1)

            self.assertEqual(result.succeeded_jobs, 1)
            job = repository.get_queue_job(queued.job_id)
            self.assertIsNotNone(job)
            job_record, _ = job or (None, None)
            self.assertEqual(job_record.status, QueueJobStatus.SUCCEEDED)
            self.assertTrue(job_record.linked_run_id)
            run = repository.get_run(job_record.linked_run_id or "")
            self.assertIsNotNone(run)
            _, run_payload = run or (None, None)
            self.assertEqual(run_payload["external_ticket"]["system"], "jira")
            self.assertEqual(run_payload["external_ticket"]["key"], "ENG-123")
            self.assertEqual(run_payload["issue"]["title"], "Enable the platform flag")

    def test_jira_webhook_enqueues_external_plan_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "service.py").write_text("FLAG = False\n", encoding="utf-8")
            mappings_path = root / "jira-projects.json"
            mappings_path.write_text(
                json.dumps(
                    {
                        "ENG": {
                            "repo": "acme/widgets",
                            "repo_root": str(root),
                            "provider": "heuristic",
                            "team": "automation",
                        }
                    }
                ),
                encoding="utf-8",
            )
            api = _build_api(
                root,
                env={
                    "ISSUE_TO_PR_JIRA_PROJECT_MAPPINGS_PATH": str(mappings_path),
                    "ISSUE_TO_PR_JIRA_WEBHOOK_SECRET": "jira-secret",
                },
            )

            response = api.handle_request(
                method="POST",
                path="/v1/webhooks/jira/issues",
                headers={
                    "X-Issue-To-Pr-Jira-Secret": "jira-secret",
                    "X-Atlassian-Webhook-Identifier": "jira-delivery-1",
                },
                body=json.dumps(
                    {
                        "issue": {
                            "key": "ENG-123",
                            "fields": {
                                "summary": "Fix the service flag",
                                "description": {
                                    "type": "doc",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [{"type": "text", "text": "Turn the service flag on."}],
                                        }
                                    ],
                                },
                                "labels": ["bug", "platform"],
                                "project": {"key": "ENG"},
                            },
                        }
                    }
                ),
            )

            self.assertEqual(response.status_code, 202)
            job = api._repository.get_queue_job(response.body["job_id"])  # type: ignore[attr-defined]
            self.assertIsNotNone(job)
            _, payload = job or (None, None)
            self.assertEqual(payload["repo_full_name"], "acme/widgets")
            self.assertEqual(payload["parameters"]["external_issue"]["key"], "ENG-123")
            self.assertEqual(payload["parameters"]["external_issue"]["labels"], ["bug", "platform"])

    def test_slack_approval_webhook_reviews_pending_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = "slack-signing-secret"
            api = _build_api(
                root,
                env={
                    "ISSUE_TO_PR_SLACK_SIGNING_SECRET": secret,
                },
            )
            artifact_dir = api._settings.artifact_dir  # type: ignore[attr-defined]
            manage_tenant = ManageTenantUseCase(api._repository, api._access_controller)  # type: ignore[attr-defined]
            manage_tenant.register_tenant(
                tenant_id="tenant-1",
                name="Acme",
                repo_patterns=["acme/*"],
                admin_actor="alice",
                admin_team="platform",
                artifact_dir=artifact_dir,
            )
            manage_tenant.add_membership(
                tenant_id="tenant-1",
                actor="alice",
                member_actor="bob",
                role=TenantRole.REVIEWER,
                team="security",
            )
            _seed_approval_ready_run(api._repository, artifact_dir, root, external_ticket={"system": "jira", "key": "ENG-123"})  # type: ignore[attr-defined]
            approval = RequestApprovalUseCase(
                api._repository,  # type: ignore[attr-defined]
                ApprovalPolicyEvaluator(
                    policy_overrides={
                        "default": {
                            "required_approvals_by_risk": {
                                "low": 1,
                                "medium": 1,
                                "high": 1,
                                "critical": 1,
                            }
                        }
                    }
                ),
            ).request_delivery_approval(
                run_id="run-1",
                execution_id="exec-1",
                verification_id="verify-1",
                actor="alice",
                team="platform",
            )
            self.assertEqual(approval.receipt.status, ApprovalStatus.PENDING)

            interaction_payload = {
                "user": {"username": "bob"},
                "actions": [
                    {
                        "action_id": "approve",
                        "value": json.dumps({"approval_id": approval.approval_id, "team": "security"}),
                    }
                ],
            }
            form_body = urlencode({"payload": json.dumps(interaction_payload)}).encode("utf-8")
            timestamp = "1713090000"
            signature = "v0=" + hmac.new(
                secret.encode("utf-8"),
                f"v0:{timestamp}:".encode("utf-8") + form_body,
                hashlib.sha256,
            ).hexdigest()

            response = api.handle_request(
                method="POST",
                path="/v1/webhooks/slack/approvals",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Slack-Request-Timestamp": timestamp,
                    "X-Slack-Signature": signature,
                },
                body=form_body,
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.body["status"], ApprovalStatus.APPROVED.value)
            stored = api._repository.get_approval(approval.approval_id)  # type: ignore[attr-defined]
            self.assertIsNotNone(stored)
            record, _ = stored or (None, None)
            self.assertEqual(record.status, ApprovalStatus.APPROVED)
            notifications = api._repository.list_notifications(tenant_id="tenant-1", limit=10)  # type: ignore[attr-defined]
            self.assertTrue(any(item.event_type == NotificationEventType.APPROVAL_REVIEWED for item in notifications))

    def test_notification_outbox_fanout_dispatches_to_slack_teams_and_jira(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(
                "os.environ",
                {
                    "ISSUE_TO_PR_ARTIFACT_DIR": str(root / ".issue-to-pr"),
                    "ISSUE_TO_PR_SLACK_WEBHOOK_URL": "https://slack.example.com/hook",
                    "ISSUE_TO_PR_TEAMS_WEBHOOK_URL": "https://teams.example.com/hook",
                    "ISSUE_TO_PR_JIRA_BASE_URL": "https://jira.example.com",
                    "ISSUE_TO_PR_JIRA_TOKEN": "jira-token",
                },
                clear=True,
            ):
                settings = Settings.from_env(cwd=root)

            repository = RunRepository(settings.database_path)
            _seed_run(
                repository,
                settings.artifact_dir,
                root,
                run_id="run-1",
                external_ticket={"system": "jira", "key": "ENG-123", "url": "https://jira.example.com/browse/ENG-123"},
            )
            slack = RecordingSlackClient()
            teams = RecordingTeamsClient()
            jira = RecordingJiraClient()
            outbox = FileNotificationOutbox(
                repository,
                settings=settings,
                slack_client=slack,
                teams_client=teams,
                jira_client=jira,
            )

            record = outbox.emit(
                tenant_id="tenant-1",
                event_type=NotificationEventType.APPROVAL_REQUESTED,
                summary="Approval requested for external ticket.",
                payload={"run_id": "run-1", "status": "pending"},
                output_dir=settings.notification_dir,
            )

            self.assertEqual(len(slack.events), 1)
            self.assertEqual(len(teams.events), 1)
            self.assertEqual(jira.comments, [("ENG-123", mock.ANY)])
            stored = repository.list_notifications(tenant_id="tenant-1", limit=10)
            self.assertEqual(len(stored), 1)
            payload = json.loads(record.payload_path.read_text(encoding="utf-8"))
            self.assertEqual([item["destination"] for item in payload["dispatch_results"]], ["slack", "teams", "jira"])
            self.assertTrue(all(item["status"] == "sent" for item in payload["dispatch_results"]))


def _build_api(root: Path, *, env: dict[str, str] | None = None) -> ControlPlaneApi:
    base_env = {"ISSUE_TO_PR_ARTIFACT_DIR": str(root / ".issue-to-pr")}
    if env:
        base_env.update(env)
    with mock.patch.dict("os.environ", base_env, clear=True):
        settings = Settings.from_env(cwd=root)
    repository = RunRepository(settings.database_path)
    access_controller = TenantAccessController(repository)
    budget_manager = QueueBudgetManager(settings, repository)
    return ControlPlaneApi(
        settings=settings,
        repository=repository,
        access_controller=access_controller,
        budget_manager=budget_manager,
        github_client=ExplodingGitHubClient(),
    )


def _seed_approval_ready_run(
    repository: RunRepository,
    artifact_dir: Path,
    root: Path,
    *,
    external_ticket: dict[str, object] | None = None,
) -> None:
    _seed_run(repository, artifact_dir, root, run_id="run-1", external_ticket=external_ticket)
    run_dir = artifact_dir / "runs" / "run-1"
    execution_path = run_dir / "executions" / "exec-1.json"
    execution_path.parent.mkdir(parents=True, exist_ok=True)
    execution_payload = {
        "execution_id": "exec-1",
        "linked_run_id": "run-1",
        "mode": PatchExecutionMode.APPLY.value,
        "status": PatchExecutionStatus.SUCCEEDED.value,
        "summary": "Applied patch",
        "operations": [{"path": "service.py"}],
    }
    execution_path.write_text(json.dumps(execution_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    repository.save_execution(
        PatchExecutionRecord(
            execution_id="exec-1",
            created_at="2026-04-13T10:05:00+00:00",
            proposal_id="proposal-1",
            linked_run_id="run-1",
            mode=PatchExecutionMode.APPLY,
            status=PatchExecutionStatus.SUCCEEDED,
            summary="Applied patch",
            repo_root=root,
            receipt_path=execution_path,
        ),
        execution_payload,
    )

    verification_path = run_dir / "verifications" / "verify-1.json"
    verification_path.parent.mkdir(parents=True, exist_ok=True)
    verification_payload = {
        "verification_id": "verify-1",
        "linked_run_id": "run-1",
        "linked_execution_id": "exec-1",
        "status": VerificationStatus.SUCCEEDED.value,
        "stop_reason": VerificationStopReason.SUCCESS.value,
        "summary": "Verification passed",
        "attempts": [{"command": "pytest", "status": "passed"}],
        "skipped_commands": [],
    }
    verification_path.write_text(json.dumps(verification_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    repository.save_verification(
        VerificationRecord(
            verification_id="verify-1",
            created_at="2026-04-13T10:10:00+00:00",
            linked_run_id="run-1",
            linked_execution_id="exec-1",
            status=VerificationStatus.SUCCEEDED,
            stop_reason=VerificationStopReason.SUCCESS,
            summary="Verification passed",
            repo_root=root,
            receipt_path=verification_path,
        ),
        verification_payload,
    )


def _seed_run(
    repository: RunRepository,
    artifact_dir: Path,
    root: Path,
    *,
    run_id: str,
    external_ticket: dict[str, object] | None = None,
) -> None:
    run_dir = artifact_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "plan.md"
    pr_draft_path = run_dir / "pr.md"
    audit_path = run_dir / "run.json"
    report_path.write_text("# Plan\n", encoding="utf-8")
    pr_draft_path.write_text("Initial PR draft\n", encoding="utf-8")
    run_payload = {
        "issue": {
            "repo_full_name": "acme/widgets",
            "issue_number": 1,
            "title": "Update the repository",
            "labels": [],
            "url": "https://example.com/acme/widgets/issues/1",
        },
        "repo_snapshot": {"root": str(root), "is_dirty": False},
        "command_assessments": [],
        "plan": {"summary": "Change set", "branch_name": "agent/issue-1"},
        "artifacts": {
            "report_path": str(report_path),
            "pr_draft_path": str(pr_draft_path),
            "audit_path": str(audit_path),
        },
        "external_ticket": external_ticket,
    }
    audit_path.write_text(json.dumps(run_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    repository.save_run(
        RunRecord(
            run_id=run_id,
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
        ),
        run_payload,
    )


if __name__ == "__main__":
    unittest.main()
