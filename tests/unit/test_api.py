from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.application.services.authentication import issue_bearer_token
from issue_to_pr_agent.application.services.queue_budget import QueueBudgetManager
from issue_to_pr_agent.application.services.tenant_access import TenantAccessController
from issue_to_pr_agent.application.use_cases.manage_tenant import ManageTenantUseCase
from issue_to_pr_agent.domain.entities import (
    AlertRecord,
    AlertSeverity,
    AlertStatus,
    AuthSubjectType,
    ExecutionMode,
    ExecutionRuntime,
    IssueContext,
    NotificationEventType,
    NotificationRecord,
    NotificationStatus,
    PatchOperation,
    PatchExecutionMode,
    PatchExecutionRecord,
    PatchExecutionStatus,
    PatchOperationType,
    PatchProposal,
    PatcherProvider,
    PlannerProvider,
    PlatformPermission,
    RunRecord,
    RunStatus,
    TenantRole,
    TenantMembershipRecord,
    TraceEventRecord,
    VerificationRecord,
    VerificationStatus,
    VerificationStopReason,
)
from issue_to_pr_agent.infrastructure.config.settings import Settings
from issue_to_pr_agent.infrastructure.persistence.run_repository import RunRepository
from issue_to_pr_agent.interfaces.http.app import ControlPlaneApi


class FakeGitHubClient:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_issue(self, repo_full_name: str, issue_number: int) -> IssueContext:
        self.calls += 1
        return IssueContext(
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            title="Enable the module flag",
            body="The module flag should be enabled.",
            labels=["bug"],
            url=f"https://example.com/{repo_full_name}/issues/{issue_number}",
        )


class SimplePatcher:
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
        target = next(item for item in files if item.path == "flag_module.py")
        current_line = next(line for line in target.content.splitlines() if line.startswith("FLAG ="))
        return PatchProposal(
            proposal_id=f"{linked_run_id}-patch",
            linked_run_id=linked_run_id,
            summary="Enable the module flag",
            rationale="Set the flag to True.",
            operations=[
                PatchOperation(
                    type=PatchOperationType.REPLACE_TEXT,
                    path="flag_module.py",
                    find_text=current_line,
                    replace_text="FLAG = True",
                )
            ],
        )


class ApiTests(unittest.TestCase):
    def test_healthz_bypasses_auth_and_run_listing_requires_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api = _build_api(root, env={"ISSUE_TO_PR_API_TOKEN": "secret-token"})

            health = api.handle_request(method="GET", path="/healthz")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.body["status"], "ok")

            denied = api.handle_request(method="GET", path="/v1/runs")
            self.assertEqual(denied.status_code, 403)
            self.assertIn("X-Request-ID", denied.headers)

    def test_identity_me_returns_signed_principal_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = "0123456789abcdef0123456789abcdef"
            api = _build_api(
                root,
                env={
                    "ISSUE_TO_PR_AUTH_TOKEN_SECRET": secret,
                    "ISSUE_TO_PR_AUTH_TOKEN_ISSUER": "issue-to-pr",
                },
            )
            token = issue_bearer_token(
                secret=secret,
                issuer="issue-to-pr",
                subject="user-1",
                actor="alice",
                team="platform",
                groups=["platform", "reviewers"],
                scopes=["view_dashboard"],
            )

            response = api.handle_request(
                method="GET",
                path="/v1/identity/me",
                headers={"Authorization": f"Bearer {token}"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.body["authenticated"])
            self.assertEqual(response.body["principal"]["actor"], "alice")
            self.assertEqual(response.body["principal"]["team"], "platform")

    def test_signed_principal_can_request_approval_without_actor_in_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            secret = "0123456789abcdef0123456789abcdef"
            api = _build_api(
                root,
                env={
                    "ISSUE_TO_PR_AUTH_TOKEN_SECRET": secret,
                    "ISSUE_TO_PR_AUTH_TOKEN_ISSUER": "issue-to-pr",
                },
            )
            ManageTenantUseCase(api._repository, api._access_controller).register_tenant(  # type: ignore[attr-defined]
                tenant_id="tenant-1",
                name="Acme",
                repo_patterns=["acme/*"],
                admin_actor="alice",
                admin_team="platform",
                artifact_dir=artifact_dir,
            )
            _seed_approval_ready_run(api._repository, artifact_dir, root)  # type: ignore[attr-defined]
            token = issue_bearer_token(
                secret=secret,
                issuer="issue-to-pr",
                subject="user-1",
                actor="alice",
                team="platform",
                groups=["platform"],
                scopes=[PlatformPermission.REQUEST_APPROVAL.value],
            )

            response = api.handle_request(
                method="POST",
                path="/v1/approvals/request",
                headers={"Authorization": f"Bearer {token}"},
                body=json.dumps(
                    {
                        "run_id": "run-1",
                        "execution_id": "exec-1",
                        "verification_id": "verify-1",
                        "comment": "Ready for review",
                        "assigned_reviewers": ["bob"],
                    }
                ),
            )

            self.assertEqual(response.status_code, 201)
            approval = api.handle_request(
                method="GET",
                path=f"/v1/approvals/{response.body['approval_id']}",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(approval.status_code, 200)
            self.assertEqual(approval.body["requested_by"], "alice")
            self.assertEqual(approval.body["assigned_reviewers"], ["bob"])
            self.assertIsNotNone(approval.body["expires_at"])

    def test_openapi_document_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api = _build_api(root)

            response = api.handle_request(method="GET", path="/v1/openapi.json")

            self.assertEqual(response.status_code, 200)
            self.assertIn("/v1/plan", response.body["paths"])
            self.assertIn("/v1/webhooks/jira/issues", response.body["paths"])
            self.assertIn("/v1/webhooks/slack/approvals", response.body["paths"])
            self.assertEqual(response.body["x-control-plane"]["request_id_header"], "X-Request-ID")

    def test_ui_shell_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api = _build_api(root)

            response = api.handle_request(method="GET", path="/ui")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Content-Type"], "text/html; charset=utf-8")
            self.assertIn("Issue-to-PR Operator Console", response.body)
            self.assertIn("/ui/app.js", response.body)

    def test_plan_endpoint_creates_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "flag_module.py").write_text("FLAG = False\n", encoding="utf-8")
            api = _build_api(root)

            response = api.handle_request(
                method="POST",
                path="/v1/plan",
                body=json.dumps(
                    {
                        "repo": "acme/widgets",
                        "issue": 5,
                        "repo_root": str(root),
                        "provider": "heuristic",
                    }
                ),
            )

            self.assertEqual(response.status_code, 201)
            run_id = response.body["run_id"]
            stored = api._repository.get_run(run_id)  # type: ignore[attr-defined]
            self.assertIsNotNone(stored)

    def test_plan_endpoint_supports_idempotency_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "flag_module.py").write_text("FLAG = False\n", encoding="utf-8")
            github = FakeGitHubClient()
            api = _build_api(root, github_client=github)
            body = json.dumps(
                {
                    "repo": "acme/widgets",
                    "issue": 12,
                    "repo_root": str(root),
                    "provider": "heuristic",
                }
            )

            first = api.handle_request(
                method="POST",
                path="/v1/plan",
                headers={"Idempotency-Key": "plan-12"},
                body=body,
            )
            second = api.handle_request(
                method="POST",
                path="/v1/plan",
                headers={"Idempotency-Key": "plan-12"},
                body=body,
            )

            self.assertEqual(first.status_code, 201)
            self.assertEqual(second.status_code, 201)
            self.assertEqual(first.body["run_id"], second.body["run_id"])
            self.assertEqual(second.headers["X-Idempotent-Replay"], "true")
            self.assertEqual(github.calls, 1)

    def test_rate_limit_rejects_excess_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api = _build_api(root, env={"ISSUE_TO_PR_API_RATE_LIMIT_PER_MINUTE": "1"})

            first = api.handle_request(method="GET", path="/v1/runs", headers={"X-Api-Client": "test-client"})
            second = api.handle_request(method="GET", path="/v1/runs", headers={"X-Api-Client": "test-client"})

            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 429)
            self.assertEqual(second.body["error"], "API rate limit exceeded.")

    def test_prepare_sandbox_endpoint_creates_isolated_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            api = _build_api(root)

            response = api.handle_request(
                method="POST",
                path="/v1/sandboxes",
                body=json.dumps({"repo_root": str(root)}),
            )

            self.assertEqual(response.status_code, 201)
            sandbox_id = response.body["sandbox_id"]
            show = api.handle_request(method="GET", path=f"/v1/sandboxes/{sandbox_id}")
            self.assertEqual(show.status_code, 200)
            self.assertTrue(Path(show.body["workspace_root"]).joinpath("app.py").exists())

    def test_autofix_endpoint_supports_sandbox_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_flag_fixture(root)
            api = _build_api(root, patcher_factory=lambda settings, provider: SimplePatcher())

            plan_response = api.handle_request(
                method="POST",
                path="/v1/plan",
                body=json.dumps(
                    {
                        "repo": "acme/widgets",
                        "issue": 7,
                        "repo_root": str(root),
                        "provider": "heuristic",
                    }
                ),
            )
            run_id = plan_response.body["run_id"]

            response = api.handle_request(
                method="POST",
                path="/v1/autofix",
                body=json.dumps(
                    {
                        "run_id": run_id,
                        "repo_root": str(root),
                        "sandbox": True,
                        "max_attempts": 2,
                        "verify_max_attempts": 1,
                        "timeout_seconds": 30,
                    }
                ),
            )

            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.body["status"], "succeeded")
            self.assertEqual((root / "flag_module.py").read_text(encoding="utf-8"), "FLAG = False\n")
            sandbox_payload = api.handle_request(method="GET", path=f"/v1/sandboxes/{response.body['sandbox_id']}")
            self.assertEqual(sandbox_payload.status_code, 200)

    def test_autofix_endpoint_accepts_docker_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_flag_fixture(root)
            api = _build_api(root, patcher_factory=lambda settings, provider: SimplePatcher())

            plan_response = api.handle_request(
                method="POST",
                path="/v1/plan",
                body=json.dumps(
                    {
                        "repo": "acme/widgets",
                        "issue": 8,
                        "repo_root": str(root),
                        "provider": "heuristic",
                    }
                ),
            )
            run_id = plan_response.body["run_id"]
            fake_runner = mock.Mock()
            fake_runner.run.return_value = mock.Mock(exit_code=0, stdout="ok\n", stderr="", duration_ms=1)

            with mock.patch(
                "issue_to_pr_agent.interfaces.http.app.build_command_runner",
                return_value=fake_runner,
            ) as build_runner_mock:
                response = api.handle_request(
                    method="POST",
                    path="/v1/autofix",
                    body=json.dumps(
                        {
                            "run_id": run_id,
                            "repo_root": str(root),
                            "runtime": "docker",
                            "max_attempts": 1,
                            "verify_max_attempts": 1,
                            "timeout_seconds": 30,
                        }
                    ),
                )

            self.assertEqual(response.status_code, 201)
            self.assertEqual(build_runner_mock.call_args.args[1], ExecutionRuntime.DOCKER)
            self.assertTrue(fake_runner.run.called)

    def test_issue_webhook_enqueues_plan_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mapping_path = root / "repo-roots.json"
            mapping_path.write_text(json.dumps({"acme/widgets": str(root)}), encoding="utf-8")
            api = _build_api(
                root,
                env={
                    "ISSUE_TO_PR_WEBHOOK_SECRET": "topsecret",
                    "ISSUE_TO_PR_WEBHOOK_REPO_ROOTS_PATH": str(mapping_path),
                },
            )
            body = json.dumps(
                {
                    "action": "opened",
                    "repository": {"full_name": "acme/widgets"},
                    "issue": {"number": 11},
                }
            ).encode("utf-8")
            signature = "sha256=" + hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()

            response = api.handle_request(
                method="POST",
                path="/v1/webhooks/github/issues",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": signature,
                },
                body=body,
            )

            self.assertEqual(response.status_code, 202)
            job_id = response.body["job_id"]
            job = api.handle_request(method="GET", path=f"/v1/queue-jobs/{job_id}")
            self.assertEqual(job.status_code, 200)
            self.assertEqual(job.body["job_type"], "plan")

    def test_dashboard_and_notifications_endpoints_return_tenant_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            api = _build_api(root)
            ManageTenantUseCase(api._repository, api._access_controller).register_tenant(  # type: ignore[attr-defined]
                tenant_id="tenant-1",
                name="Acme",
                repo_patterns=["acme/*"],
                admin_actor="alice",
                admin_team="platform",
                artifact_dir=artifact_dir,
            )
            _seed_approval_ready_run(api._repository, artifact_dir, root)  # type: ignore[attr-defined]
            notification_path = artifact_dir / "notifications" / "note-1.json"
            notification_path.parent.mkdir(parents=True, exist_ok=True)
            notification_path.write_text("{}\n", encoding="utf-8")
            api._repository.save_notification(  # type: ignore[attr-defined]
                NotificationRecord(
                    notification_id="note-1",
                    created_at="2026-04-14T10:00:00+00:00",
                    tenant_id="tenant-1",
                    event_type=NotificationEventType.APPROVAL_REQUESTED,
                    status=NotificationStatus.EMITTED,
                    summary="Approval requested for acme/widgets.",
                    payload_path=notification_path,
                ),
                {
                    "notification_id": "note-1",
                    "tenant_id": "tenant-1",
                    "event_type": "approval_requested",
                    "status": "emitted",
                    "summary": "Approval requested for acme/widgets.",
                },
            )

            dashboard = api.handle_request(
                method="GET",
                path="/v1/dashboard",
                query_string="tenant_id=tenant-1&actor=alice&team=platform",
            )
            notifications = api.handle_request(
                method="GET",
                path="/v1/notifications",
                query_string="tenant_id=tenant-1&actor=alice&team=platform&limit=10",
            )

            self.assertEqual(dashboard.status_code, 200)
            self.assertEqual(dashboard.body["summary"]["tenant_name"], "Acme")
            self.assertEqual(dashboard.body["summary"]["run_counts"], {"succeeded": 1})
            self.assertEqual(notifications.status_code, 200)
            self.assertEqual(notifications.body["items"][0]["notification_id"], "note-1")

    def test_alerts_and_traces_endpoints_return_observability_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api = _build_api(root, env={"ISSUE_TO_PR_API_TOKEN": "secret-token"})
            artifact_dir = root / ".issue-to-pr"

            alert_path = artifact_dir / "telemetry" / "alerts" / "tenant-1" / "alert-1.json"
            alert_path.parent.mkdir(parents=True, exist_ok=True)
            alert_path.write_text("{}\n", encoding="utf-8")
            api._repository.save_alert(  # type: ignore[attr-defined]
                AlertRecord(
                    alert_id="alert-1",
                    created_at="2026-04-14T10:00:00+00:00",
                    tenant_id="tenant-1",
                    severity=AlertSeverity.ERROR,
                    source="queue_worker",
                    status=AlertStatus.OPEN,
                    summary="Queue job failed.",
                    payload_path=alert_path,
                ),
                {"alert_id": "alert-1"},
            )
            trace_path = artifact_dir / "telemetry" / "traces" / "trace-1" / "event-1.json"
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            trace_path.write_text("{}\n", encoding="utf-8")
            api._repository.save_trace_event(  # type: ignore[attr-defined]
                TraceEventRecord(
                    event_id="event-1",
                    trace_id="trace-1",
                    recorded_at="2026-04-14T10:05:00+00:00",
                    source="http_api",
                    span_name="GET /v1/runs",
                    status="completed",
                    payload_path=trace_path,
                    linked_run_id="run-1",
                    linked_job_id=None,
                ),
                {"event_id": "event-1"},
            )

            alerts = api.handle_request(
                method="GET",
                path="/v1/alerts",
                query_string="limit=10",
                headers={"Authorization": "Bearer secret-token"},
            )
            traces = api.handle_request(
                method="GET",
                path="/v1/traces",
                query_string="trace_id=trace-1",
                headers={"Authorization": "Bearer secret-token"},
            )

            self.assertEqual(alerts.status_code, 200)
            self.assertEqual(alerts.body["items"][0]["alert_id"], "alert-1")
            self.assertEqual(traces.status_code, 200)
            self.assertEqual(traces.body["items"][0]["trace_id"], "trace-1")

    def test_audit_export_and_retention_endpoints_execute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api = _build_api(root, env={"ISSUE_TO_PR_API_TOKEN": "secret-token"})
            _seed_approval_ready_run(api._repository, root / ".issue-to-pr", root)  # type: ignore[attr-defined]

            export = api.handle_request(
                method="POST",
                path="/v1/audits/exports",
                headers={"Authorization": "Bearer secret-token", "Content-Type": "application/json"},
                body=json.dumps({"run_id": "run-1"}),
            )
            retention = api.handle_request(
                method="POST",
                path="/v1/retention/enforce",
                headers={"Authorization": "Bearer secret-token", "Content-Type": "application/json"},
                body=json.dumps({"dry_run": True}),
            )

            self.assertEqual(export.status_code, 201)
            self.assertTrue(Path(export.body["archive_path"]).exists())
            self.assertEqual(retention.status_code, 200)
            self.assertTrue(retention.body["dry_run"])

    def test_queue_plan_endpoint_enqueues_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api = _build_api(root)

            response = api.handle_request(
                method="POST",
                path="/v1/queue/plan",
                body=json.dumps(
                    {
                        "repo": "acme/widgets",
                        "issue": 13,
                        "repo_root": str(root),
                        "actor": "alice",
                        "team": "platform",
                    }
                ),
            )

            self.assertEqual(response.status_code, 201)
            job = api.handle_request(method="GET", path=f"/v1/queue-jobs/{response.body['job_id']}")
            self.assertEqual(job.status_code, 200)
            self.assertEqual(job.body["job_type"], "plan")

    def test_identity_sync_endpoint_updates_tenant_memberships(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            secret = "0123456789abcdef0123456789abcdef"
            api = _build_api(
                root,
                env={
                    "ISSUE_TO_PR_AUTH_TOKEN_SECRET": secret,
                    "ISSUE_TO_PR_AUTH_TOKEN_ISSUER": "issue-to-pr",
                },
            )
            ManageTenantUseCase(api._repository, api._access_controller).register_tenant(  # type: ignore[attr-defined]
                tenant_id="tenant-1",
                name="Acme",
                repo_patterns=["acme/*"],
                admin_actor="alice",
                admin_team="platform",
                artifact_dir=artifact_dir,
            )
            token = issue_bearer_token(
                secret=secret,
                issuer="issue-to-pr",
                subject="sync-1",
                actor="scim-sync",
                subject_type=AuthSubjectType.SERVICE,
                scopes=[PlatformPermission.MANAGE_MEMBERSHIP.value],
                tenant_ids=["tenant-1"],
            )

            response = api.handle_request(
                method="POST",
                path="/v1/identity/sync",
                headers={"Authorization": f"Bearer {token}"},
                body=json.dumps(
                    {
                        "tenant_id": "tenant-1",
                        "replace_existing": True,
                        "memberships": [
                            {"actor": "olivia", "role": TenantRole.OPERATOR.value, "team": "delivery"},
                            {"actor": "riley", "role": TenantRole.REVIEWER.value, "team": "security"},
                        ],
                    }
                ),
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.body["created_count"], 2)
            self.assertEqual(response.body["removed_count"], 1)
            memberships = api._repository.list_tenant_memberships("tenant-1")  # type: ignore[attr-defined]
            self.assertEqual(sorted(item.actor for item in memberships), ["olivia", "riley"])


def _build_api(
    root: Path,
    *,
    env: dict[str, str] | None = None,
    patcher_factory=None,
    github_client: FakeGitHubClient | None = None,
) -> ControlPlaneApi:
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
        github_client=github_client or FakeGitHubClient(),
        patcher_factory=patcher_factory,
    )


def _seed_approval_ready_run(repository: RunRepository, artifact_dir: Path, root: Path) -> None:
    run_dir = artifact_dir / "runs" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "plan.md"
    pr_draft_path = run_dir / "pr.md"
    audit_path = run_dir / "run.json"
    report_path.write_text("# Plan\n", encoding="utf-8")
    pr_draft_path.write_text("Initial PR draft\n", encoding="utf-8")
    audit_path.write_text("{}\n", encoding="utf-8")
    repository.save_run(
        RunRecord(
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
        ),
        {
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
        },
    )
    execution_path = run_dir / "executions" / "exec-1.json"
    execution_path.parent.mkdir(parents=True, exist_ok=True)
    execution_path.write_text("{}\n", encoding="utf-8")
    repository.save_execution(
        PatchExecutionRecord(
            execution_id="exec-1",
            created_at="2026-04-13T10:05:00+00:00",
            proposal_id="proposal-1",
            linked_run_id="run-1",
            mode=PatchExecutionMode.APPLY,
            status=PatchExecutionStatus.SUCCEEDED,
            summary="Applied change",
            repo_root=root,
            receipt_path=execution_path,
        ),
        {
            "execution_id": "exec-1",
            "linked_run_id": "run-1",
            "receipt_path": str(execution_path),
            "receipts": [{"path": ".github/workflows/ci.yml", "changed": True}],
        },
    )
    verification_path = run_dir / "verification" / "verify-1.json"
    verification_path.parent.mkdir(parents=True, exist_ok=True)
    verification_path.write_text("{}\n", encoding="utf-8")
    repository.save_verification(
        VerificationRecord(
            verification_id="verify-1",
            created_at="2026-04-13T10:10:00+00:00",
            linked_run_id="run-1",
            linked_execution_id="exec-1",
            status=VerificationStatus.SUCCEEDED,
            stop_reason=VerificationStopReason.SUCCESS,
            summary="Verification succeeded",
            repo_root=root,
            receipt_path=verification_path,
        ),
        {
            "verification_id": "verify-1",
            "linked_run_id": "run-1",
            "linked_execution_id": "exec-1",
            "status": "succeeded",
            "stop_reason": "success",
            "receipt_path": str(verification_path),
            "attempts": [{"attempt_index": 1}],
            "skipped_commands": [],
        },
    )
    repository.save_tenant_membership(
        TenantMembershipRecord(
            tenant_id="tenant-1",
            actor="bob",
            role=TenantRole.REVIEWER,
            team="platform",
            created_at="2026-04-13T09:00:00+00:00",
            updated_at="2026-04-13T09:00:00+00:00",
        ),
        {"tenant_id": "tenant-1", "actor": "bob", "role": TenantRole.REVIEWER.value, "team": "platform"},
    )


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


if __name__ == "__main__":
    unittest.main()
