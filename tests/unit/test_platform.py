from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.application.services.tenant_access import TenantAccessController
from issue_to_pr_agent.application.use_cases.dashboard import DashboardUseCase
from issue_to_pr_agent.application.use_cases.manage_tenant import ManageTenantUseCase
from issue_to_pr_agent.application.use_cases.sync_identity import SyncIdentityUseCase
from issue_to_pr_agent.domain.entities import (
    ApprovalAction,
    ApprovalRecord,
    ApprovalRiskLevel,
    ApprovalStatus,
    AuthSubjectType,
    AuthenticatedPrincipal,
    DeliveryRecord,
    DeliveryStatus,
    ExecutionMode,
    IdentitySyncMembership,
    NotificationEventType,
    PlannerProvider,
    PlatformPermission,
    RunRecord,
    RunStatus,
    TenantStatus,
    TenantRole,
)
from issue_to_pr_agent.infrastructure.notifications import FileNotificationOutbox
from issue_to_pr_agent.infrastructure.persistence.run_repository import RunRepository
from issue_to_pr_agent.shared.exceptions import PolicyError


class PlatformUseCaseTests(unittest.TestCase):
    def test_register_tenant_bootstraps_admin_and_writes_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            access_controller = TenantAccessController(repository)

            result = ManageTenantUseCase(repository, access_controller).register_tenant(
                tenant_id="tenant-1",
                name="Acme",
                repo_patterns=["acme/*"],
                admin_actor="alice",
                admin_team="platform",
                artifact_dir=artifact_dir,
            )

            self.assertTrue(result.config_path.exists())
            config = json.loads(result.config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["tenant_id"], "tenant-1")
            self.assertEqual(config["repo_patterns"], ["acme/*"])
            self.assertEqual(config["policy_overrides"], {})

            stored = repository.get_tenant("tenant-1")
            self.assertIsNotNone(stored)
            tenant_record, tenant_payload = stored or (None, None)
            self.assertEqual(tenant_record.name, "Acme")
            self.assertEqual(tenant_payload["repo_patterns"], ["acme/*"])
            self.assertEqual(
                access_controller.get_membership_role(tenant_id="tenant-1", actor="alice"),
                TenantRole.ADMIN,
            )

    def test_manage_tenant_updates_policy_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            access_controller = TenantAccessController(repository)
            use_case = ManageTenantUseCase(repository, access_controller)
            use_case.register_tenant(
                tenant_id="tenant-1",
                name="Acme",
                repo_patterns=["acme/*"],
                admin_actor="alice",
                admin_team="platform",
                artifact_dir=artifact_dir,
            )

            use_case.set_policy_overrides(
                tenant_id="tenant-1",
                actor="alice",
                policy_overrides={
                    "default": {
                        "required_approvals_by_risk": {"high": 2},
                    }
                },
            )
            stored = repository.get_tenant("tenant-1")
            self.assertIsNotNone(stored)
            _, tenant_payload = stored or (None, None)
            self.assertEqual(
                tenant_payload["policy_overrides"]["default"]["required_approvals_by_risk"]["high"],
                2,
            )

            use_case.set_status(
                tenant_id="tenant-1",
                actor="alice",
                status=TenantStatus.SUSPENDED,
            )
            suspended = repository.get_tenant("tenant-1")
            self.assertIsNotNone(suspended)
            tenant_record, _ = suspended or (None, None)
            self.assertEqual(tenant_record.status, TenantStatus.SUSPENDED)
            with self.assertRaises(PolicyError):
                access_controller.require_tenant_permission(
                    tenant_id="tenant-1",
                    actor="alice",
                    permission=PlatformPermission.VIEW_DASHBOARD,
                )

    def test_access_controller_enforces_repo_permissions_and_team(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            access_controller = TenantAccessController(repository)
            use_case = ManageTenantUseCase(repository, access_controller)
            use_case.register_tenant(
                tenant_id="tenant-1",
                name="Acme",
                repo_patterns=["acme/*"],
                admin_actor="alice",
                admin_team="platform",
                artifact_dir=artifact_dir,
            )
            use_case.add_membership(
                tenant_id="tenant-1",
                actor="alice",
                member_actor="olivia",
                role=TenantRole.OPERATOR,
                team="delivery",
            )

            context = access_controller.require_repo_permission(
                repo_full_name="acme/widgets",
                actor="olivia",
                permission=PlatformPermission.OPERATE_QUEUE,
                team="delivery",
            )
            self.assertIsNotNone(context)
            self.assertEqual(context[0].tenant_id, "tenant-1")

            context = access_controller.require_repo_permission(
                repo_full_name="acme/widgets",
                actor="olivia",
                permission=PlatformPermission.REQUEST_APPROVAL,
                team="delivery",
            )
            self.assertIsNotNone(context)
            self.assertEqual(context[0].tenant_id, "tenant-1")

            with self.assertRaises(PolicyError):
                access_controller.require_repo_permission(
                    repo_full_name="acme/widgets",
                    actor="olivia",
                    permission=PlatformPermission.REVIEW_APPROVAL,
                    team="delivery",
                )

            with self.assertRaises(PolicyError):
                access_controller.require_repo_permission(
                    repo_full_name="acme/widgets",
                    actor="olivia",
                    permission=PlatformPermission.DELIVER,
                    team="security",
                )

            self.assertIsNone(
                access_controller.require_repo_permission(
                    repo_full_name="other/repo",
                    actor=None,
                    permission=PlatformPermission.DELIVER,
                )
            )

    def test_service_principal_can_sync_memberships_for_scoped_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            access_controller = TenantAccessController(repository)
            ManageTenantUseCase(repository, access_controller).register_tenant(
                tenant_id="tenant-1",
                name="Acme",
                repo_patterns=["acme/*"],
                admin_actor="alice",
                admin_team="platform",
                artifact_dir=artifact_dir,
            )
            principal = AuthenticatedPrincipal(
                subject="scim-sync",
                actor="scim-sync",
                subject_type=AuthSubjectType.SERVICE,
                scopes=[PlatformPermission.MANAGE_MEMBERSHIP.value],
                tenant_ids=["tenant-1"],
            )

            result = SyncIdentityUseCase(repository, access_controller).sync_tenant_memberships(
                tenant_id="tenant-1",
                memberships=[
                    IdentitySyncMembership(actor="olivia", role=TenantRole.OPERATOR, team="delivery"),
                    IdentitySyncMembership(actor="riley", role=TenantRole.REVIEWER, team="security"),
                ],
                replace_existing=False,
                principal=principal,
            )

            self.assertEqual(result.receipt.created_count, 2)
            self.assertEqual(result.receipt.updated_count, 0)
            self.assertEqual(result.receipt.removed_count, 0)
            membership = repository.get_tenant_membership("tenant-1", "riley")
            self.assertIsNotNone(membership)
            self.assertEqual((membership or (None, None))[0].role, TenantRole.REVIEWER)

    def test_dashboard_summarizes_tenant_scoped_records_and_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_dir = root / ".issue-to-pr"
            repository = RunRepository(artifact_dir / "agent_runs.sqlite3")
            access_controller = TenantAccessController(repository)
            ManageTenantUseCase(repository, access_controller).register_tenant(
                tenant_id="tenant-1",
                name="Acme",
                repo_patterns=["acme/*"],
                admin_actor="alice",
                admin_team="platform",
                artifact_dir=artifact_dir,
            )

            self._seed_run(
                repository,
                run_id="run-1",
                repo_full_name="acme/widgets",
                issue_number=1,
                status=RunStatus.SUCCEEDED,
                artifact_dir=artifact_dir,
            )
            self._seed_run(
                repository,
                run_id="run-2",
                repo_full_name="other/service",
                issue_number=2,
                status=RunStatus.FAILED,
                artifact_dir=artifact_dir,
            )
            self._seed_approval(
                repository,
                approval_id="approval-1",
                repo_full_name="acme/widgets",
                status=ApprovalStatus.PENDING,
                artifact_dir=artifact_dir,
            )
            self._seed_approval(
                repository,
                approval_id="approval-2",
                repo_full_name="other/service",
                status=ApprovalStatus.APPROVED,
                artifact_dir=artifact_dir,
            )
            self._seed_delivery(
                repository,
                delivery_id="delivery-1",
                repo_full_name="acme/widgets",
                status=DeliveryStatus.SUCCEEDED,
                artifact_dir=artifact_dir,
            )
            self._seed_delivery(
                repository,
                delivery_id="delivery-2",
                repo_full_name="other/service",
                status=DeliveryStatus.FAILED,
                artifact_dir=artifact_dir,
            )
            notification = FileNotificationOutbox(repository).emit(
                tenant_id="tenant-1",
                event_type=NotificationEventType.DELIVERY_SUCCEEDED,
                summary="Delivery completed for acme/widgets.",
                payload={"delivery_id": "delivery-1"},
                output_dir=artifact_dir / "notifications",
            )

            result = DashboardUseCase(repository, access_controller).build(
                tenant_id="tenant-1",
                actor="alice",
            )

            self.assertEqual(result.summary.run_counts, {"succeeded": 1})
            self.assertEqual(result.summary.approval_counts, {"pending": 1})
            self.assertEqual(result.summary.delivery_counts, {"succeeded": 1})
            self.assertEqual(result.summary.notification_counts, {"delivery_succeeded": 1})
            self.assertEqual(len(result.summary.pending_approvals), 1)
            self.assertEqual(result.summary.pending_approvals[0].record_id, "approval-1")
            self.assertEqual(len(result.summary.recent_deliveries), 1)
            self.assertEqual(result.summary.recent_deliveries[0].record_id, "delivery-1")
            self.assertEqual(len(result.summary.recent_notifications), 1)
            self.assertEqual(result.summary.recent_notifications[0].record_id, notification.notification_id)
            self.assertTrue(notification.payload_path.exists())

    def _seed_run(
        self,
        repository: RunRepository,
        *,
        run_id: str,
        repo_full_name: str,
        issue_number: int,
        status: RunStatus,
        artifact_dir: Path,
    ) -> None:
        run_dir = artifact_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / "plan.md"
        pr_draft_path = run_dir / "pr.md"
        audit_path = run_dir / "run.json"
        report_path.write_text("# Plan\n", encoding="utf-8")
        pr_draft_path.write_text("Draft PR\n", encoding="utf-8")
        audit_path.write_text("{}\n", encoding="utf-8")
        repository.save_run(
            RunRecord(
                run_id=run_id,
                created_at="2026-04-13T10:00:00+00:00",
                repo_full_name=repo_full_name,
                issue_number=issue_number,
                planner_provider=PlannerProvider.HEURISTIC,
                execution_mode=ExecutionMode.PLAN_ONLY,
                status=status,
                branch_name=f"agent/{run_id}",
                summary=f"Run {run_id}",
                issue_url=f"https://example.com/{repo_full_name}/issues/{issue_number}",
                report_path=report_path,
                pr_draft_path=pr_draft_path,
                audit_path=audit_path,
            ),
            {
                "run_id": run_id,
                "issue": {
                    "repo_full_name": repo_full_name,
                    "issue_number": issue_number,
                    "labels": [],
                },
            },
        )

    def _seed_approval(
        self,
        repository: RunRepository,
        *,
        approval_id: str,
        repo_full_name: str,
        status: ApprovalStatus,
        artifact_dir: Path,
    ) -> None:
        receipt_path = artifact_dir / "approvals" / f"{approval_id}.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text("{}\n", encoding="utf-8")
        repository.save_approval(
            ApprovalRecord(
                approval_id=approval_id,
                created_at="2026-04-13T10:05:00+00:00",
                updated_at="2026-04-13T10:06:00+00:00",
                action=ApprovalAction.DELIVERY,
                linked_run_id="run-1",
                linked_execution_id="exec-1",
                linked_verification_id="verify-1",
                repo_full_name=repo_full_name,
                status=status,
                risk_level=ApprovalRiskLevel.HIGH,
                requested_by="alice",
                requester_team="platform",
                required_approvals=1,
                approved_count=0 if status == ApprovalStatus.PENDING else 1,
                summary=f"Approval {approval_id}",
                receipt_path=receipt_path,
            ),
            {"approval_id": approval_id, "status": status.value},
        )

    def _seed_delivery(
        self,
        repository: RunRepository,
        *,
        delivery_id: str,
        repo_full_name: str,
        status: DeliveryStatus,
        artifact_dir: Path,
    ) -> None:
        receipt_path = artifact_dir / "deliveries" / f"{delivery_id}.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text("{}\n", encoding="utf-8")
        repository.save_delivery(
            DeliveryRecord(
                delivery_id=delivery_id,
                created_at="2026-04-13T10:10:00+00:00",
                linked_run_id="run-1",
                linked_execution_id="exec-1",
                linked_verification_id="verify-1",
                status=status,
                repo_full_name=repo_full_name,
                branch_name="agent/issue-1",
                base_branch="main",
                summary=f"Delivery {delivery_id}",
                receipt_path=receipt_path,
            ),
            {"delivery_id": delivery_id, "status": status.value},
        )


if __name__ == "__main__":
    unittest.main()
