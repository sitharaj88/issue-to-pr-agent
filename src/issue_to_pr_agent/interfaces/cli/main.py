from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from ...agents.planner.heuristic import HeuristicPlanner
from ...application.services.approval_policy import ApprovalPolicyEvaluator
from ...application.services.audit_export import RunAuditExporter
from ...application.services.delivery_governance import DeliveryGovernancePolicyEvaluator
from ...application.services.proposal_template import ProposalTemplateBuilder
from ...application.services.queue_budget import QueueBudgetManager
from ...application.services.retention import RetentionEnforcer
from ...application.services.tenant_access import TenantAccessController
from ...application.use_cases.dashboard import DashboardUseCase
from ...application.use_cases.deliver_run import DeliverRunUseCase
from ...application.use_cases.execute_patch_proposal import ExecutePatchProposalUseCase
from ...application.use_cases.generate_patch_proposal import GeneratePatchProposalUseCase
from ...application.use_cases.manage_approval import RequestApprovalUseCase, ReviewApprovalUseCase
from ...application.use_cases.manage_queue import ManageQueueUseCase
from ...application.use_cases.manage_release import ManageReleaseUseCase
from ...application.use_cases.manage_sandbox import ManageSandboxUseCase
from ...application.use_cases.manage_tenant import ManageTenantUseCase
from ...application.use_cases.plan_issue_to_pr import IssueToPRAgent
from ...application.use_cases.process_queue import ProcessQueueUseCase
from ...application.use_cases.run_autofix import RunAutofixUseCase
from ...application.use_cases.run_smoke_test import RunSmokeTestUseCase
from ...application.use_cases.run_sandboxed_autofix import RunSandboxedAutofixUseCase
from ...application.use_cases.run_sandboxed_patch_execution import (
    RunSandboxedPatchExecutionUseCase,
    SandboxedPatchExecutionFailedError,
)
from ...application.use_cases.sync_identity import SyncIdentityUseCase
from ...application.use_cases.verify_run import VerifyRunUseCase
from ...domain.entities import (
    AlertStatus,
    ApprovalDecision,
    ApprovalStatus,
    AutofixStatus,
    DeliveryStatus,
    ExecutionRuntime,
    IdentitySyncMembership,
    NotificationEventType,
    PatchExecutionMode,
    PatchProposal,
    PlatformPermission,
    QueueJobStatus,
    QueueJobType,
    TenantRole,
    TenantStatus,
)
from ...domain.policies.safety import SafetyPolicy
from ...domain.policies.workspace import WorkspaceGuardrails
from ...infrastructure.config.settings import Settings
from ...infrastructure.notifications import FileNotificationOutbox
from ...infrastructure.persistence.run_repository import RunRepository
from ...infrastructure.sandbox import LocalSandboxManager
from ...infrastructure.verification import build_command_runner
from ...infrastructure.workspace.mutator import LocalWorkspaceMutator
from ...integrations.github.client import GitHubClient
from ...integrations.openai.patcher import OpenAIPatcher
from ...integrations.openai.planner import OpenAIPlanner
from ...integrations.telemetry import TelemetrySinkClient
from ...observability.alerts import AlertManager
from ...observability.logging.config import configure_logging
from ...observability.metrics import QueueMetricsReporter
from ...observability.tracing import TraceRecorder


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operate the issue-to-PR agent workflows.")
    subparsers = parser.add_subparsers(dest="command")

    plan_parser = subparsers.add_parser("plan", help="Generate a plan and PR draft from a GitHub issue.")
    plan_parser.add_argument("--repo", required=True, help="Repository in owner/name form.")
    plan_parser.add_argument("--issue", required=True, type=int, help="Issue number.")
    plan_parser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the local checkout or workspace to inspect.",
    )
    plan_parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory where run artifacts will be written.",
    )
    plan_parser.add_argument(
        "--provider",
        choices=("heuristic", "openai"),
        default="heuristic",
        help="Planning backend.",
    )
    plan_parser.add_argument(
        "--objective",
        default=None,
        help="Optional implementation objective to bias the plan.",
    )
    plan_parser.add_argument(
        "--create-branch",
        action="store_true",
        help="Create the suggested local git branch after planning.",
    )

    runs_parser = subparsers.add_parser("runs", help="List recent planning runs.")
    runs_parser.add_argument("--limit", type=int, default=20, help="Maximum number of runs to list.")

    show_run_parser = subparsers.add_parser("show-run", help="Show the stored JSON payload for a run.")
    show_run_parser.add_argument("--run-id", required=True, help="Run identifier to inspect.")

    draft_patch_parser = subparsers.add_parser(
        "draft-patch",
        help="Create a patch proposal template from a planning run.",
    )
    draft_patch_parser.add_argument("--run-id", required=True, help="Planning run identifier.")
    draft_patch_parser.add_argument(
        "--output-file",
        default=None,
        help="Optional path to write the proposal template.",
    )

    generate_patch_parser = subparsers.add_parser(
        "generate-patch",
        help="Generate an autonomous patch proposal from a planning run.",
    )
    generate_patch_parser.add_argument("--run-id", required=True, help="Planning run identifier.")
    generate_patch_parser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the repository root used to load current file contents.",
    )
    generate_patch_parser.add_argument(
        "--provider",
        choices=("openai",),
        default="openai",
        help="Patch generation backend.",
    )
    generate_patch_parser.add_argument(
        "--objective",
        default=None,
        help="Optional implementation objective to bias the patch generation.",
    )

    autofix_parser = subparsers.add_parser(
        "autofix",
        help="Run the bounded autonomous patch-apply-verify repair loop.",
    )
    autofix_parser.add_argument("--run-id", required=True, help="Planning run identifier.")
    autofix_parser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the repository root used for patch generation and verification.",
    )
    autofix_parser.add_argument(
        "--provider",
        choices=("openai",),
        default="openai",
        help="Patch generation backend.",
    )
    autofix_parser.add_argument(
        "--runtime",
        choices=("local", "docker"),
        default=None,
        help="Verification runtime. Defaults to ISSUE_TO_PR_VERIFICATION_RUNTIME.",
    )
    autofix_parser.add_argument("--max-attempts", type=int, default=3, help="Maximum autofix attempts.")
    autofix_parser.add_argument(
        "--verify-max-attempts",
        type=int,
        default=3,
        help="Maximum verification commands to try per autofix attempt.",
    )
    autofix_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=120,
        help="Timeout for each verification command attempt.",
    )
    autofix_parser.add_argument(
        "--objective",
        default=None,
        help="Optional implementation objective for the first autofix attempt.",
    )
    autofix_parser.add_argument(
        "--sandbox",
        action="store_true",
        help="Run autofix inside an isolated copied sandbox workspace.",
    )

    autofix_runs_parser = subparsers.add_parser("autofix-runs", help="List recent autofix runs.")
    autofix_runs_parser.add_argument("--limit", type=int, default=20, help="Maximum number of autofix runs to list.")

    show_autofix_run_parser = subparsers.add_parser(
        "show-autofix-run",
        help="Show the stored JSON payload for an autofix run.",
    )
    show_autofix_run_parser.add_argument("--autofix-id", required=True, help="Autofix identifier to inspect.")

    autofix_attempts_parser = subparsers.add_parser(
        "autofix-attempts",
        help="List attempts for an autofix run.",
    )
    autofix_attempts_parser.add_argument("--autofix-id", required=True, help="Autofix identifier.")
    autofix_attempts_parser.add_argument("--limit", type=int, default=50, help="Maximum number of attempts to list.")

    prepare_sandbox_parser = subparsers.add_parser(
        "prepare-sandbox",
        help="Create an isolated sandbox copy of a repository root.",
    )
    prepare_sandbox_parser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the repository root to copy into the sandbox.",
    )
    prepare_sandbox_parser.add_argument("--run-id", default=None, help="Optional planning run identifier to link.")

    sandboxes_parser = subparsers.add_parser("sandboxes", help="List recent sandbox sessions.")
    sandboxes_parser.add_argument("--limit", type=int, default=20, help="Maximum number of sandboxes to list.")

    show_sandbox_parser = subparsers.add_parser(
        "show-sandbox",
        help="Show the stored JSON payload for a sandbox session.",
    )
    show_sandbox_parser.add_argument("--sandbox-id", required=True, help="Sandbox identifier to inspect.")

    cleanup_sandbox_parser = subparsers.add_parser(
        "cleanup-sandbox",
        help="Mark a sandbox as cleaned up and remove its workspace directory.",
    )
    cleanup_sandbox_parser.add_argument("--sandbox-id", required=True, help="Sandbox identifier to clean up.")

    patch_proposals_parser = subparsers.add_parser(
        "patch-proposals",
        help="List generated patch proposals.",
    )
    patch_proposals_parser.add_argument("--limit", type=int, default=20, help="Maximum number of proposals to list.")

    show_patch_proposal_parser = subparsers.add_parser(
        "show-patch-proposal",
        help="Show the stored JSON payload for a generated patch proposal.",
    )
    show_patch_proposal_parser.add_argument("--proposal-id", required=True, help="Patch proposal identifier.")

    execute_patch_parser = subparsers.add_parser(
        "execute-patch",
        help="Execute a patch proposal in dry-run or apply mode.",
    )
    execute_patch_parser.add_argument("--proposal-file", required=True, help="Path to a patch proposal JSON file.")
    execute_patch_parser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the repository root where the proposal should be executed.",
    )
    execute_patch_parser.add_argument(
        "--sandbox",
        action="store_true",
        help="Prepare an isolated sandbox workspace and execute the proposal there.",
    )
    execute_patch_parser.add_argument(
        "--sandbox-id",
        default=None,
        help="Reuse an existing sandbox workspace instead of --repo-root.",
    )
    execute_patch_parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory where execution receipts will be written.",
    )
    execute_patch_parser.add_argument(
        "--mode",
        choices=("dry_run", "apply"),
        default="dry_run",
        help="Execution mode. Defaults to dry_run.",
    )

    executions_parser = subparsers.add_parser("executions", help="List recent patch executions.")
    executions_parser.add_argument("--limit", type=int, default=20, help="Maximum number of executions to list.")

    show_execution_parser = subparsers.add_parser(
        "show-execution",
        help="Show the stored JSON payload for an execution receipt.",
    )
    show_execution_parser.add_argument("--execution-id", required=True, help="Execution identifier to inspect.")

    verify_parser = subparsers.add_parser(
        "verify",
        help="Run verification commands from a planning run or execution context.",
    )
    verify_target_group = verify_parser.add_mutually_exclusive_group(required=True)
    verify_target_group.add_argument("--run-id", help="Planning run identifier to verify.")
    verify_target_group.add_argument("--execution-id", help="Execution identifier linked to a planning run.")
    verify_parser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the repository root where verification should run.",
    )
    verify_parser.add_argument(
        "--sandbox-id",
        default=None,
        help="Optional sandbox identifier whose workspace should be verified.",
    )
    verify_parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory where verification receipts will be written when no run is linked.",
    )
    verify_parser.add_argument("--max-attempts", type=int, default=3, help="Maximum verification attempts.")
    verify_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=120,
        help="Timeout for each verification command attempt.",
    )
    verify_parser.add_argument(
        "--runtime",
        choices=("local", "docker"),
        default=None,
        help="Verification runtime. Defaults to ISSUE_TO_PR_VERIFICATION_RUNTIME.",
    )

    verifications_parser = subparsers.add_parser("verifications", help="List recent verification runs.")
    verifications_parser.add_argument("--limit", type=int, default=20, help="Maximum number of verifications to list.")

    show_verification_parser = subparsers.add_parser(
        "show-verification",
        help="Show the stored JSON payload for a verification receipt.",
    )
    show_verification_parser.add_argument("--verification-id", required=True, help="Verification identifier to inspect.")

    request_approval_parser = subparsers.add_parser(
        "request-approval",
        help="Create a delivery approval request from a verified execution.",
    )
    request_approval_parser.add_argument("--run-id", required=True, help="Planning run identifier.")
    request_approval_parser.add_argument("--execution-id", required=True, help="Execution identifier to link.")
    request_approval_parser.add_argument("--verification-id", required=True, help="Verification identifier to link.")
    request_approval_parser.add_argument("--actor", required=True, help="Requester identifier.")
    request_approval_parser.add_argument("--team", required=True, help="Requester team.")
    request_approval_parser.add_argument("--comment", default="", help="Optional request comment.")
    request_approval_parser.add_argument(
        "--expires-in-hours",
        type=int,
        default=None,
        help="Optional approval expiry in hours. Defaults to ISSUE_TO_PR_APPROVAL_TTL_HOURS.",
    )
    request_approval_parser.add_argument(
        "--assigned-reviewer",
        action="append",
        default=[],
        help="Optional reviewer actor assignment. Can be provided multiple times.",
    )
    request_approval_parser.add_argument(
        "--assigned-reviewer-team",
        action="append",
        default=[],
        help="Optional reviewer team assignment. Can be provided multiple times.",
    )

    review_approval_parser = subparsers.add_parser(
        "review-approval",
        help="Approve or reject an approval request.",
    )
    review_approval_parser.add_argument("--approval-id", required=True, help="Approval identifier to review.")
    review_approval_parser.add_argument("--actor", required=True, help="Reviewer identifier.")
    review_approval_parser.add_argument("--team", required=True, help="Reviewer team.")
    review_approval_parser.add_argument(
        "--decision",
        required=True,
        choices=("approve", "reject"),
        help="Reviewer decision.",
    )
    review_approval_parser.add_argument("--comment", default="", help="Optional reviewer comment.")

    approvals_parser = subparsers.add_parser("approvals", help="List recent approval requests.")
    approvals_parser.add_argument("--limit", type=int, default=20, help="Maximum number of approvals to list.")
    approvals_parser.add_argument(
        "--status",
        choices=("pending", "approved", "rejected"),
        default=None,
        help="Optional approval status filter.",
    )

    show_approval_parser = subparsers.add_parser(
        "show-approval",
        help="Show the stored JSON payload for an approval receipt.",
    )
    show_approval_parser.add_argument("--approval-id", required=True, help="Approval identifier to inspect.")

    register_tenant_parser = subparsers.add_parser(
        "register-tenant",
        help="Create a tenant and bootstrap its first admin membership.",
    )
    register_tenant_parser.add_argument("--tenant-id", required=True, help="Tenant identifier.")
    register_tenant_parser.add_argument("--name", required=True, help="Tenant display name.")
    register_tenant_parser.add_argument(
        "--repo-pattern",
        action="append",
        required=True,
        help="Repository pattern assigned to the tenant. Can be provided multiple times.",
    )
    register_tenant_parser.add_argument("--admin-actor", required=True, help="Bootstrap admin actor.")
    register_tenant_parser.add_argument("--admin-team", required=True, help="Bootstrap admin team.")
    register_tenant_parser.add_argument(
        "--policy-file",
        default=None,
        help="Optional JSON file containing tenant approval policy overrides.",
    )

    tenants_parser = subparsers.add_parser("tenants", help="List registered tenants.")
    tenants_parser.add_argument("--limit", type=int, default=100, help="Maximum number of tenants to list.")

    set_tenant_policy_parser = subparsers.add_parser(
        "set-tenant-policy",
        help="Update tenant-specific approval policy overrides.",
    )
    set_tenant_policy_parser.add_argument("--tenant-id", required=True, help="Tenant identifier.")
    set_tenant_policy_parser.add_argument("--actor", required=True, help="Admin actor performing the update.")
    set_tenant_policy_parser.add_argument("--policy-file", required=True, help="JSON file with policy overrides.")

    set_tenant_status_parser = subparsers.add_parser(
        "set-tenant-status",
        help="Activate or suspend a tenant.",
    )
    set_tenant_status_parser.add_argument("--tenant-id", required=True, help="Tenant identifier.")
    set_tenant_status_parser.add_argument("--actor", required=True, help="Admin actor performing the update.")
    set_tenant_status_parser.add_argument(
        "--status",
        required=True,
        choices=("active", "suspended"),
        help="New tenant status.",
    )

    add_member_parser = subparsers.add_parser(
        "add-member",
        help="Add or update a tenant member.",
    )
    add_member_parser.add_argument("--tenant-id", required=True, help="Tenant identifier.")
    add_member_parser.add_argument("--actor", required=True, help="Admin actor performing the update.")
    add_member_parser.add_argument("--member-actor", required=True, help="Actor to add or update.")
    add_member_parser.add_argument(
        "--role",
        required=True,
        choices=("admin", "operator", "reviewer", "observer"),
        help="Tenant role for the member.",
    )
    add_member_parser.add_argument("--team", required=True, help="Team name for the member.")

    sync_memberships_parser = subparsers.add_parser(
        "sync-memberships",
        help="Synchronize tenant memberships from a JSON file.",
    )
    sync_memberships_parser.add_argument("--tenant-id", required=True, help="Tenant identifier.")
    sync_memberships_parser.add_argument("--actor", required=True, help="Admin actor performing the sync.")
    sync_memberships_parser.add_argument(
        "--team",
        default=None,
        help="Optional actor team for membership permission checks.",
    )
    sync_memberships_parser.add_argument(
        "--memberships-file",
        required=True,
        help="JSON file with a memberships array of actor, role, and team entries.",
    )
    sync_memberships_parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="Remove existing memberships not present in the sync payload.",
    )

    members_parser = subparsers.add_parser("members", help="List tenant members.")
    members_parser.add_argument("--tenant-id", required=True, help="Tenant identifier.")
    members_parser.add_argument("--actor", required=True, help="Admin actor requesting the member list.")

    dashboard_parser = subparsers.add_parser("dashboard", help="Show a tenant dashboard summary.")
    dashboard_parser.add_argument("--tenant-id", required=True, help="Tenant identifier.")
    dashboard_parser.add_argument("--actor", required=True, help="Actor requesting the dashboard.")

    notifications_parser = subparsers.add_parser("notifications", help="List tenant notification outbox events.")
    notifications_parser.add_argument("--tenant-id", required=True, help="Tenant identifier.")
    notifications_parser.add_argument("--actor", required=True, help="Actor requesting notifications.")
    notifications_parser.add_argument("--limit", type=int, default=20, help="Maximum number of notifications to list.")

    alerts_parser = subparsers.add_parser("alerts", help="List emitted observability alerts.")
    alerts_parser.add_argument("--tenant-id", default=None, help="Optional tenant identifier filter.")
    alerts_parser.add_argument("--limit", type=int, default=20, help="Maximum number of alerts to list.")
    alerts_parser.add_argument(
        "--status",
        choices=("open", "acknowledged"),
        default=None,
        help="Optional alert status filter.",
    )

    traces_parser = subparsers.add_parser("traces", help="List recorded trace events.")
    traces_parser.add_argument("--trace-id", default=None, help="Optional trace identifier filter.")
    traces_parser.add_argument("--run-id", default=None, help="Optional linked run identifier filter.")
    traces_parser.add_argument("--job-id", default=None, help="Optional linked job identifier filter.")
    traces_parser.add_argument("--limit", type=int, default=50, help="Maximum number of trace events to list.")

    export_audit_parser = subparsers.add_parser("export-audit", help="Export an audit bundle for a planning run.")
    export_audit_parser.add_argument("--run-id", required=True, help="Planning run identifier.")
    export_audit_parser.add_argument("--output-dir", default=None, help="Optional audit export output directory.")

    retention_parser = subparsers.add_parser("enforce-retention", help="Evaluate or enforce retention rules.")
    retention_parser.add_argument("--dry-run", action="store_true", help="Only report what would be deleted.")

    schema_status_parser = subparsers.add_parser("schema-status", help="Show applied schema migrations.")
    schema_status_parser.add_argument(
        "--json",
        action="store_true",
        help="Render the schema status as JSON.",
    )

    backup_state_parser = subparsers.add_parser("backup-state", help="Create a database and artifact backup bundle.")
    backup_state_parser.add_argument("--output-dir", default=None, help="Optional backup output directory.")
    backup_state_parser.add_argument(
        "--skip-artifacts",
        action="store_true",
        help="Back up only the database file and manifest.",
    )

    restore_state_parser = subparsers.add_parser("restore-state", help="Restore a backup manifest into a target directory.")
    restore_state_parser.add_argument("--manifest-path", required=True, help="Backup manifest to restore from.")
    restore_state_parser.add_argument("--target-dir", required=True, help="Target directory for restored state.")

    release_manifest_parser = subparsers.add_parser("release-manifest", help="Generate a deployment release manifest.")
    release_manifest_parser.add_argument("--output-file", default=None, help="Optional release-manifest output path.")

    smoke_test_parser = subparsers.add_parser("smoke-test", help="Run the local end-to-end smoke workflow.")
    smoke_test_parser.add_argument("--output-dir", default=None, help="Optional smoke artifact output directory.")

    enqueue_plan_parser = subparsers.add_parser(
        "enqueue-plan",
        help="Queue a planning job for worker execution.",
    )
    enqueue_plan_parser.add_argument("--repo", required=True, help="Repository in owner/name form.")
    enqueue_plan_parser.add_argument("--issue", required=True, type=int, help="Issue number.")
    enqueue_plan_parser.add_argument("--repo-root", default=".", help="Path to the local repository root.")
    enqueue_plan_parser.add_argument("--actor", required=True, help="Actor requesting the queued plan.")
    enqueue_plan_parser.add_argument("--team", required=True, help="Actor team.")
    enqueue_plan_parser.add_argument(
        "--provider",
        choices=("heuristic", "openai"),
        default="heuristic",
        help="Planning backend.",
    )
    enqueue_plan_parser.add_argument("--objective", default=None, help="Optional implementation objective.")
    enqueue_plan_parser.add_argument("--create-branch", action="store_true", help="Create the planned branch.")
    enqueue_plan_parser.add_argument("--priority", type=int, default=0, help="Queue priority.")
    enqueue_plan_parser.add_argument("--max-attempts", type=int, default=None, help="Maximum queue attempts.")
    enqueue_plan_parser.add_argument("--budget-units", type=int, default=None, help="Optional queue budget.")
    enqueue_plan_parser.add_argument("--output-dir", default=None, help="Optional artifact output directory.")
    enqueue_plan_parser.add_argument(
        "--worker-tag",
        action="append",
        default=None,
        help="Optional required worker tag. Can be provided multiple times.",
    )
    enqueue_plan_parser.add_argument(
        "--concurrency-key",
        default=None,
        help="Optional concurrency key used to serialize similar jobs across workers.",
    )

    enqueue_verify_parser = subparsers.add_parser(
        "enqueue-verify",
        help="Queue a verification job for worker execution.",
    )
    enqueue_verify_target = enqueue_verify_parser.add_mutually_exclusive_group(required=True)
    enqueue_verify_target.add_argument("--run-id", help="Planning run identifier.")
    enqueue_verify_target.add_argument("--execution-id", help="Execution identifier linked to a planning run.")
    enqueue_verify_parser.add_argument("--repo-root", default=".", help="Path to the repository root.")
    enqueue_verify_parser.add_argument("--actor", required=True, help="Actor requesting the queued verification.")
    enqueue_verify_parser.add_argument("--team", required=True, help="Actor team.")
    enqueue_verify_parser.add_argument("--priority", type=int, default=0, help="Queue priority.")
    enqueue_verify_parser.add_argument("--max-attempts", type=int, default=None, help="Maximum queue attempts.")
    enqueue_verify_parser.add_argument("--budget-units", type=int, default=None, help="Optional queue budget.")
    enqueue_verify_parser.add_argument(
        "--verify-max-attempts",
        type=int,
        default=3,
        help="Maximum verification command attempts inside the verification job.",
    )
    enqueue_verify_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=120,
        help="Timeout for each verification command attempt.",
    )
    enqueue_verify_parser.add_argument("--output-dir", default=None, help="Optional artifact output directory.")
    enqueue_verify_parser.add_argument(
        "--worker-tag",
        action="append",
        default=None,
        help="Optional required worker tag. Can be provided multiple times.",
    )
    enqueue_verify_parser.add_argument(
        "--concurrency-key",
        default=None,
        help="Optional concurrency key used to serialize similar jobs across workers.",
    )

    enqueue_deliver_parser = subparsers.add_parser(
        "enqueue-deliver",
        help="Queue a delivery job for worker execution.",
    )
    enqueue_deliver_parser.add_argument("--run-id", required=True, help="Planning run identifier.")
    enqueue_deliver_parser.add_argument("--execution-id", required=True, help="Execution identifier to deliver.")
    enqueue_deliver_parser.add_argument("--verification-id", required=True, help="Verification identifier to deliver.")
    enqueue_deliver_parser.add_argument("--approval-id", default=None, help="Optional approval identifier.")
    enqueue_deliver_parser.add_argument("--repo-root", default=".", help="Path to the repository root.")
    enqueue_deliver_parser.add_argument("--actor", required=True, help="Actor requesting the queued delivery.")
    enqueue_deliver_parser.add_argument("--team", required=True, help="Actor team.")
    enqueue_deliver_parser.add_argument("--priority", type=int, default=0, help="Queue priority.")
    enqueue_deliver_parser.add_argument("--max-attempts", type=int, default=None, help="Maximum queue attempts.")
    enqueue_deliver_parser.add_argument("--budget-units", type=int, default=None, help="Optional queue budget.")
    enqueue_deliver_parser.add_argument("--base-branch", default=None, help="Optional GitHub base branch override.")
    enqueue_deliver_parser.add_argument(
        "--rollout-stage",
        default=None,
        help="Optional rollout stage metadata such as dev, staging, or production.",
    )
    enqueue_deliver_parser.add_argument("--commit-message", default=None, help="Optional git commit message.")
    enqueue_deliver_parser.add_argument("--pr-title", default=None, help="Optional pull request title.")
    enqueue_deliver_parser.add_argument("--skip-pr-comment", action="store_true", help="Skip the PR comment.")
    enqueue_deliver_parser.add_argument(
        "--worker-tag",
        action="append",
        default=None,
        help="Optional required worker tag. Can be provided multiple times.",
    )
    enqueue_deliver_parser.add_argument(
        "--concurrency-key",
        default=None,
        help="Optional concurrency key used to serialize similar jobs across workers.",
    )

    queue_jobs_parser = subparsers.add_parser("queue-jobs", help="List queued worker jobs.")
    queue_jobs_parser.add_argument("--limit", type=int, default=20, help="Maximum number of jobs to list.")
    queue_jobs_parser.add_argument(
        "--status",
        choices=("queued", "running", "succeeded", "failed", "cancelled"),
        default=None,
        help="Optional queue status filter.",
    )
    queue_jobs_parser.add_argument(
        "--job-type",
        choices=("plan", "verify", "deliver"),
        default=None,
        help="Optional queue job type filter.",
    )

    show_job_parser = subparsers.add_parser("show-job", help="Show a queued job payload.")
    show_job_parser.add_argument("--job-id", required=True, help="Queue job identifier.")

    cancel_job_parser = subparsers.add_parser("cancel-job", help="Cancel a queued or running job.")
    cancel_job_parser.add_argument("--job-id", required=True, help="Queue job identifier.")
    cancel_job_parser.add_argument("--actor", required=True, help="Actor requesting cancellation.")
    cancel_job_parser.add_argument("--team", required=True, help="Actor team.")

    resume_job_parser = subparsers.add_parser("resume-job", help="Resume a failed or cancelled job.")
    resume_job_parser.add_argument("--job-id", required=True, help="Queue job identifier.")
    resume_job_parser.add_argument("--actor", required=True, help="Actor requesting resume.")
    resume_job_parser.add_argument("--team", required=True, help="Actor team.")
    resume_job_parser.add_argument(
        "--reset-attempts",
        action="store_true",
        help="Reset queue attempts and budget usage before resuming.",
    )

    queue_attempts_parser = subparsers.add_parser("queue-attempts", help="List attempts for a queued job.")
    queue_attempts_parser.add_argument("--job-id", required=True, help="Queue job identifier.")

    worker_run_parser = subparsers.add_parser("worker-run", help="Process queued jobs as a worker.")
    worker_run_parser.add_argument("--worker-id", required=True, help="Worker identifier.")
    worker_run_parser.add_argument("--max-jobs", type=int, default=1, help="Maximum jobs to process.")
    worker_run_parser.add_argument(
        "--job-type",
        action="append",
        choices=("plan", "verify", "deliver"),
        default=None,
        help="Optional job type filter. Can be provided multiple times.",
    )
    worker_run_parser.add_argument(
        "--worker-tag",
        action="append",
        default=None,
        help="Advertised worker tag. Can be provided multiple times.",
    )

    worker_heartbeats_parser = subparsers.add_parser("worker-heartbeats", help="List worker heartbeats.")
    worker_heartbeats_parser.add_argument("--worker-id", default=None, help="Optional worker identifier filter.")
    worker_heartbeats_parser.add_argument("--limit", type=int, default=20, help="Maximum heartbeats to list.")

    metrics_parser = subparsers.add_parser("metrics", help="Write and show queue metrics snapshots.")
    metrics_parser.add_argument("--output-dir", default=None, help="Optional metrics output directory.")

    deliver_parser = subparsers.add_parser(
        "deliver",
        help="Commit, push, and publish a draft pull request from a verified execution.",
    )
    deliver_parser.add_argument("--run-id", required=True, help="Planning run identifier.")
    deliver_parser.add_argument("--execution-id", required=True, help="Execution identifier to deliver.")
    deliver_parser.add_argument("--verification-id", required=True, help="Verification identifier to deliver.")
    deliver_parser.add_argument(
        "--approval-id",
        default=None,
        help="Optional approved approval request identifier. Required when the approval policy gates delivery.",
    )
    deliver_parser.add_argument(
        "--actor",
        default=None,
        help="Optional actor performing the delivery. Required when the repository is assigned to a tenant.",
    )
    deliver_parser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the repository root that contains the verified workspace changes.",
    )
    deliver_parser.add_argument(
        "--sandbox-id",
        default=None,
        help="Optional sandbox identifier whose workspace should be delivered.",
    )
    deliver_parser.add_argument(
        "--base-branch",
        default=None,
        help="Optional GitHub base branch override. Defaults to the repository default branch.",
    )
    deliver_parser.add_argument(
        "--rollout-stage",
        default=None,
        help="Optional rollout stage metadata such as dev, staging, or production.",
    )
    deliver_parser.add_argument(
        "--commit-message",
        default=None,
        help="Optional git commit message override.",
    )
    deliver_parser.add_argument(
        "--pr-title",
        default=None,
        help="Optional pull request title override.",
    )
    deliver_parser.add_argument(
        "--skip-pr-comment",
        action="store_true",
        help="Skip the delivery summary comment on the created pull request.",
    )

    deliveries_parser = subparsers.add_parser("deliveries", help="List recent GitHub delivery attempts.")
    deliveries_parser.add_argument("--limit", type=int, default=20, help="Maximum number of deliveries to list.")

    show_delivery_parser = subparsers.add_parser(
        "show-delivery",
        help="Show the stored JSON payload for a delivery receipt.",
    )
    show_delivery_parser.add_argument("--delivery-id", required=True, help="Delivery identifier to inspect.")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    known_commands = {
        "plan",
        "runs",
        "show-run",
        "draft-patch",
        "generate-patch",
        "autofix",
        "autofix-runs",
        "show-autofix-run",
        "autofix-attempts",
        "prepare-sandbox",
        "sandboxes",
        "show-sandbox",
        "cleanup-sandbox",
        "patch-proposals",
        "show-patch-proposal",
        "execute-patch",
        "executions",
        "show-execution",
        "verify",
        "verifications",
        "show-verification",
        "request-approval",
        "review-approval",
        "approvals",
        "show-approval",
        "register-tenant",
        "tenants",
        "set-tenant-policy",
        "set-tenant-status",
        "add-member",
        "sync-memberships",
        "members",
        "dashboard",
        "notifications",
        "alerts",
        "traces",
        "export-audit",
        "enforce-retention",
        "schema-status",
        "backup-state",
        "restore-state",
        "release-manifest",
        "smoke-test",
        "enqueue-plan",
        "enqueue-verify",
        "enqueue-deliver",
        "queue-jobs",
        "show-job",
        "cancel-job",
        "resume-job",
        "queue-attempts",
        "worker-run",
        "worker-heartbeats",
        "metrics",
        "deliver",
        "deliveries",
        "show-delivery",
        "-h",
        "--help",
    }
    if argv and argv[0] not in known_commands:
        argv = ["plan", *argv]

    parser = build_parser()
    args = parser.parse_args(argv)

    settings = Settings.from_env(cwd=Path.cwd())
    configure_logging(settings.log_level)
    repository = RunRepository(settings.database_path)
    access_controller = TenantAccessController(repository)
    notification_outbox = FileNotificationOutbox(repository, settings=settings)
    budget_manager = QueueBudgetManager(settings, repository)
    metrics_reporter = QueueMetricsReporter(repository)
    telemetry_client = TelemetrySinkClient(settings)
    trace_recorder = TraceRecorder(repository, sink_client=telemetry_client)
    alert_manager = AlertManager(repository, settings, sink_client=telemetry_client)

    try:
        if args.command == "runs":
            return _list_runs(repository, limit=args.limit)
        if args.command == "show-run":
            return _show_run(repository, run_id=args.run_id)
        if args.command == "draft-patch":
            return _draft_patch(repository, artifact_dir=settings.artifact_dir, run_id=args.run_id, output_file=args.output_file)
        if args.command == "generate-patch":
            settings.require_openai()
            return _generate_patch(
                repository,
                settings=settings,
                run_id=args.run_id,
                repo_root=args.repo_root,
                provider=args.provider,
                objective=args.objective,
            )
        if args.command == "autofix":
            settings.require_openai()
            return _autofix(
                repository,
                settings=settings,
                run_id=args.run_id,
                repo_root=args.repo_root,
                provider=args.provider,
                max_attempts=args.max_attempts,
                verify_max_attempts=args.verify_max_attempts,
                timeout_seconds=args.timeout_seconds,
                objective=args.objective,
                sandbox=args.sandbox,
                runtime=args.runtime,
            )
        if args.command == "autofix-runs":
            return _list_autofix_runs(repository, limit=args.limit)
        if args.command == "show-autofix-run":
            return _show_autofix_run(repository, autofix_id=args.autofix_id)
        if args.command == "autofix-attempts":
            return _list_autofix_attempts(repository, autofix_id=args.autofix_id, limit=args.limit)
        if args.command == "prepare-sandbox":
            return _prepare_sandbox(
                repository,
                settings=settings,
                repo_root=args.repo_root,
                run_id=args.run_id,
            )
        if args.command == "sandboxes":
            return _list_sandboxes(repository, limit=args.limit)
        if args.command == "show-sandbox":
            return _show_sandbox(repository, sandbox_id=args.sandbox_id)
        if args.command == "cleanup-sandbox":
            return _cleanup_sandbox(repository, settings=settings, sandbox_id=args.sandbox_id)
        if args.command == "patch-proposals":
            return _list_patch_proposals(repository, limit=args.limit)
        if args.command == "show-patch-proposal":
            return _show_patch_proposal(repository, proposal_id=args.proposal_id)
        if args.command == "executions":
            return _list_executions(repository, limit=args.limit)
        if args.command == "show-execution":
            return _show_execution(repository, execution_id=args.execution_id)
        if args.command == "verifications":
            return _list_verifications(repository, limit=args.limit)
        if args.command == "show-verification":
            return _show_verification(repository, verification_id=args.verification_id)
        if args.command == "approvals":
            status = ApprovalStatus(args.status) if args.status else None
            return _list_approvals(repository, limit=args.limit, status=status)
        if args.command == "show-approval":
            return _show_approval(repository, approval_id=args.approval_id)
        if args.command == "tenants":
            return _list_tenants(repository, limit=args.limit)
        if args.command == "members":
            return _list_members(
                repository,
                access_controller=access_controller,
                tenant_id=args.tenant_id,
                actor=args.actor,
            )
        if args.command == "dashboard":
            return _dashboard(
                repository,
                access_controller=access_controller,
                tenant_id=args.tenant_id,
                actor=args.actor,
            )
        if args.command == "notifications":
            return _list_notifications(
                repository,
                access_controller=access_controller,
                tenant_id=args.tenant_id,
                actor=args.actor,
                limit=args.limit,
            )
        if args.command == "alerts":
            status = AlertStatus(args.status) if args.status else None
            return _list_alerts(repository, tenant_id=args.tenant_id, limit=args.limit, status=status)
        if args.command == "traces":
            return _list_traces(
                repository,
                trace_id=args.trace_id,
                run_id=args.run_id,
                job_id=args.job_id,
                limit=args.limit,
            )
        if args.command == "export-audit":
            return _export_audit(
                repository,
                run_id=args.run_id,
                output_dir=Path(args.output_dir).resolve() if args.output_dir else settings.audit_export_dir,
            )
        if args.command == "enforce-retention":
            return _enforce_retention(repository, settings=settings, dry_run=args.dry_run)
        if args.command == "schema-status":
            return _schema_status(ManageReleaseUseCase(repository, settings), as_json=args.json)
        if args.command == "backup-state":
            return _backup_state(
                ManageReleaseUseCase(repository, settings),
                output_dir=Path(args.output_dir).resolve() if args.output_dir else settings.artifact_dir / "backups",
                include_artifacts=not args.skip_artifacts,
            )
        if args.command == "restore-state":
            return _restore_state(
                ManageReleaseUseCase(repository, settings),
                manifest_path=Path(args.manifest_path).resolve(),
                target_dir=Path(args.target_dir).resolve(),
            )
        if args.command == "release-manifest":
            return _release_manifest(
                ManageReleaseUseCase(repository, settings),
                output_file=Path(args.output_file).resolve()
                if args.output_file
                else settings.artifact_dir / "release-manifest.json",
            )
        if args.command == "smoke-test":
            return _smoke_test(
                RunSmokeTestUseCase(repository, settings),
                output_dir=Path(args.output_dir).resolve() if args.output_dir else settings.artifact_dir,
            )
        if args.command == "queue-jobs":
            status = QueueJobStatus(args.status) if args.status else None
            job_type = QueueJobType(args.job_type) if args.job_type else None
            return _list_queue_jobs(repository, limit=args.limit, status=status, job_type=job_type)
        if args.command == "show-job":
            return _show_queue_job(repository, job_id=args.job_id)
        if args.command == "queue-attempts":
            return _list_queue_attempts(repository, job_id=args.job_id)
        if args.command == "worker-heartbeats":
            return _list_worker_heartbeats(repository, worker_id=args.worker_id, limit=args.limit)
        if args.command == "metrics":
            return _metrics(metrics_reporter, output_dir=Path(args.output_dir).resolve() if args.output_dir else settings.metrics_dir)
        if args.command == "deliveries":
            return _list_deliveries(repository, limit=args.limit)
        if args.command == "show-delivery":
            return _show_delivery(repository, delivery_id=args.delivery_id)
        if args.command == "execute-patch":
            return _execute_patch(
                repository,
                settings=settings,
                artifact_dir=settings.artifact_dir,
                proposal_file=args.proposal_file,
                repo_root=args.repo_root,
                sandbox=args.sandbox,
                sandbox_id=args.sandbox_id,
                output_dir=args.output_dir,
                mode=args.mode,
            )
        if args.command == "verify":
            return _verify(
                repository,
                settings=settings,
                safety_policy=SafetyPolicy(branch_prefix=settings.branch_prefix),
                artifact_dir=settings.artifact_dir,
                run_id=args.run_id,
                execution_id=args.execution_id,
                repo_root=args.repo_root,
                sandbox_id=args.sandbox_id,
                output_dir=args.output_dir,
                max_attempts=args.max_attempts,
                timeout_seconds=args.timeout_seconds,
                runtime=args.runtime,
            )
        if args.command == "request-approval":
            return _request_approval(
                repository,
                settings=settings,
                access_controller=access_controller,
                notification_outbox=notification_outbox,
                run_id=args.run_id,
                execution_id=args.execution_id,
                verification_id=args.verification_id,
                actor=args.actor,
                team=args.team,
                comment=args.comment,
                expires_in_hours=args.expires_in_hours,
                assigned_reviewers=args.assigned_reviewer,
                assigned_reviewer_teams=args.assigned_reviewer_team,
            )
        if args.command == "review-approval":
            return _review_approval(
                repository,
                settings=settings,
                access_controller=access_controller,
                notification_outbox=notification_outbox,
                approval_id=args.approval_id,
                actor=args.actor,
                team=args.team,
                decision=args.decision,
                comment=args.comment,
            )
        if args.command == "register-tenant":
            return _register_tenant(
                repository,
                access_controller=access_controller,
                artifact_dir=settings.artifact_dir,
                tenant_id=args.tenant_id,
                name=args.name,
                repo_patterns=args.repo_pattern,
                admin_actor=args.admin_actor,
                admin_team=args.admin_team,
                policy_file=args.policy_file,
            )
        if args.command == "set-tenant-policy":
            return _set_tenant_policy(
                repository,
                access_controller=access_controller,
                tenant_id=args.tenant_id,
                actor=args.actor,
                policy_file=args.policy_file,
            )
        if args.command == "set-tenant-status":
            return _set_tenant_status(
                repository,
                access_controller=access_controller,
                tenant_id=args.tenant_id,
                actor=args.actor,
                status=args.status,
            )
        if args.command == "add-member":
            return _add_member(
                repository,
                access_controller=access_controller,
                tenant_id=args.tenant_id,
                actor=args.actor,
                member_actor=args.member_actor,
                role=args.role,
                team=args.team,
            )
        if args.command == "sync-memberships":
            return _sync_memberships(
                repository,
                access_controller=access_controller,
                tenant_id=args.tenant_id,
                actor=args.actor,
                team=args.team,
                memberships_file=args.memberships_file,
                replace_existing=args.replace_existing,
            )
        if args.command == "enqueue-plan":
            return _enqueue_plan(
                repository,
                settings=settings,
                access_controller=access_controller,
                budget_manager=budget_manager,
                repo_full_name=args.repo,
                issue_number=args.issue,
                repo_root=args.repo_root,
                actor=args.actor,
                team=args.team,
                provider=args.provider,
                objective=args.objective,
                create_branch=args.create_branch,
                priority=args.priority,
                max_attempts=args.max_attempts,
                budget_units=args.budget_units,
                output_dir=args.output_dir,
                worker_tags=args.worker_tag,
                concurrency_key=args.concurrency_key,
            )
        if args.command == "enqueue-verify":
            return _enqueue_verify(
                repository,
                settings=settings,
                access_controller=access_controller,
                budget_manager=budget_manager,
                run_id=args.run_id,
                execution_id=args.execution_id,
                repo_root=args.repo_root,
                actor=args.actor,
                team=args.team,
                priority=args.priority,
                max_attempts=args.max_attempts,
                budget_units=args.budget_units,
                verify_max_attempts=args.verify_max_attempts,
                timeout_seconds=args.timeout_seconds,
                output_dir=args.output_dir,
                worker_tags=args.worker_tag,
                concurrency_key=args.concurrency_key,
            )
        if args.command == "enqueue-deliver":
            return _enqueue_deliver(
                repository,
                settings=settings,
                access_controller=access_controller,
                budget_manager=budget_manager,
                run_id=args.run_id,
                execution_id=args.execution_id,
                verification_id=args.verification_id,
                approval_id=args.approval_id,
                repo_root=args.repo_root,
                actor=args.actor,
                team=args.team,
                priority=args.priority,
                max_attempts=args.max_attempts,
                budget_units=args.budget_units,
                base_branch=args.base_branch,
                rollout_stage=args.rollout_stage,
                commit_message=args.commit_message,
                pr_title=args.pr_title,
                publish_pr_comment=not args.skip_pr_comment,
                worker_tags=args.worker_tag,
                concurrency_key=args.concurrency_key,
            )
        if args.command == "cancel-job":
            return _cancel_queue_job(
                repository,
                settings=settings,
                access_controller=access_controller,
                budget_manager=budget_manager,
                job_id=args.job_id,
                actor=args.actor,
                team=args.team,
            )
        if args.command == "resume-job":
            return _resume_queue_job(
                repository,
                settings=settings,
                access_controller=access_controller,
                budget_manager=budget_manager,
                job_id=args.job_id,
                actor=args.actor,
                team=args.team,
                reset_attempts=args.reset_attempts,
            )
        if args.command == "worker-run":
            return _worker_run(
                repository,
                settings=settings,
                access_controller=access_controller,
                budget_manager=budget_manager,
                notification_outbox=notification_outbox,
                metrics_reporter=metrics_reporter,
                trace_recorder=trace_recorder,
                alert_manager=alert_manager,
                worker_id=args.worker_id,
                max_jobs=args.max_jobs,
                job_types=args.job_type,
                worker_tags=args.worker_tag,
            )
        if args.command == "deliver":
            settings.require_github_token()
            return _deliver(
                repository,
                github=GitHubClient(settings),
                safety_policy=SafetyPolicy(branch_prefix=settings.branch_prefix),
                settings=settings,
                access_controller=access_controller,
                notification_outbox=notification_outbox,
                artifact_dir=settings.artifact_dir,
                artifact_base_url=settings.artifact_base_url,
                remote_name=settings.git_remote_name,
                run_id=args.run_id,
                execution_id=args.execution_id,
                verification_id=args.verification_id,
                approval_id=args.approval_id,
                actor=args.actor,
                repo_root=args.repo_root,
                sandbox_id=args.sandbox_id,
                base_branch=args.base_branch,
                rollout_stage=args.rollout_stage,
                commit_message=args.commit_message,
                pr_title=args.pr_title,
                publish_pr_comment=not args.skip_pr_comment,
            )
        if args.command != "plan":
            parser.print_help()
            return 1

        output_dir = Path(args.output_dir).resolve() if args.output_dir else settings.artifact_dir
        github = GitHubClient(settings)
        planner = HeuristicPlanner() if args.provider == "heuristic" else OpenAIPlanner(settings)
        agent = IssueToPRAgent(
            github,
            planner,
            repository,
            SafetyPolicy(branch_prefix=settings.branch_prefix),
            max_repo_files=settings.max_repo_files,
        )
        result = agent.run(
            repo_full_name=args.repo,
            issue_number=args.issue,
            repo_root=Path(args.repo_root).resolve(),
            output_dir=output_dir,
            objective=args.objective,
            create_branch=args.create_branch,
        )
    except Exception as exc:
        parser.exit(status=1, message=f"error: {exc}\n")

    blocked = sum(1 for item in result.command_assessments if item.decision.value == "block")
    review = sum(1 for item in result.command_assessments if item.decision.value == "review")
    allowed = sum(1 for item in result.command_assessments if item.decision.value == "allow")
    print(f"Issue: {result.issue.repo_full_name}#{result.issue.issue_number}")
    print(f"Run ID: {result.run_id}")
    print(f"Plan summary: {result.plan.summary}")
    print(f"Run directory: {result.run_directory}")
    print(f"Report written to: {result.report_path}")
    print(f"PR draft written to: {result.pr_draft_path}")
    print(f"Audit payload written to: {result.audit_path}")
    print(f"Command review: allow={allowed} review={review} block={blocked}")
    if args.create_branch:
        print(f"Created branch: {result.plan.branch_name}")
    return 0


def _list_runs(repository: RunRepository, *, limit: int) -> int:
    runs = repository.list_runs(limit=limit)
    if not runs:
        print("No runs found.")
        return 0
    for run in runs:
        print(
            " | ".join(
                [
                    run.created_at,
                    run.status.value,
                    run.run_id,
                    f"{run.repo_full_name}#{run.issue_number}",
                    run.planner_provider.value,
                    run.execution_mode.value,
                    run.summary,
                ]
            )
        )
    return 0


def _show_run(repository: RunRepository, *, run_id: str) -> int:
    run = repository.get_run(run_id)
    if run is None:
        print(f"Run not found: {run_id}")
        return 1
    _, payload = run
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _draft_patch(
    repository: RunRepository,
    *,
    artifact_dir: Path,
    run_id: str,
    output_file: str | None,
) -> int:
    run = repository.get_run(run_id)
    if run is None:
        print(f"Run not found: {run_id}")
        return 1
    record, payload = run
    template = ProposalTemplateBuilder().build(run_id=run_id, payload=payload)
    destination = Path(output_file).resolve() if output_file else record.audit_path.parent / "patch-proposal.template.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(template, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Patch proposal template written to: {destination}")
    return 0


def _generate_patch(
    repository: RunRepository,
    *,
    settings: Settings,
    run_id: str,
    repo_root: str,
    provider: str,
    objective: str | None,
) -> int:
    if provider != "openai":
        raise ValueError(f"Unsupported patch provider: {provider}")
    generator = GeneratePatchProposalUseCase(
        repository,
        OpenAIPatcher(settings),
    )
    result = generator.generate(
        run_id=run_id,
        repo_root=Path(repo_root).resolve(),
        objective=objective,
    )
    print(f"Proposal ID: {result.proposal_id}")
    print(f"Summary: {result.proposal.summary}")
    print(f"Operations: {len(result.proposal.operations)}")
    print(f"Proposal written to: {result.proposal_path}")
    return 0


def _autofix(
    repository: RunRepository,
    *,
    settings: Settings,
    run_id: str,
    repo_root: str,
    provider: str,
    max_attempts: int,
    verify_max_attempts: int,
    timeout_seconds: int,
    objective: str | None,
    sandbox: bool,
    runtime: str | None,
) -> int:
    if provider != "openai":
        raise ValueError(f"Unsupported patch provider: {provider}")
    verification_runtime = ExecutionRuntime(runtime) if runtime else settings.verification_runtime
    patcher = OpenAIPatcher(settings)
    autofix_use_case = RunAutofixUseCase(
        repository,
        patcher,
        SafetyPolicy(branch_prefix=settings.branch_prefix),
        verifier=VerifyRunUseCase(
            repository,
            SafetyPolicy(branch_prefix=settings.branch_prefix),
            command_runner=build_command_runner(settings, verification_runtime),
        ),
    )
    if sandbox:
        sandbox_use_case = ManageSandboxUseCase(
            repository,
            LocalSandboxManager(max_file_bytes=settings.sandbox_max_file_bytes),
        )
        result = RunSandboxedAutofixUseCase(sandbox_use_case, autofix_use_case).run(
            run_id=run_id,
            source_repo_root=Path(repo_root).resolve(),
            artifact_dir=settings.artifact_dir,
            sandbox_dir=settings.sandbox_dir,
            max_attempts=max_attempts,
            verify_max_attempts=verify_max_attempts,
            timeout_seconds=timeout_seconds,
            objective=objective,
        )
        print(f"Sandbox ID: {result.sandbox.sandbox_id}")
        print(f"Sandbox workspace: {result.sandbox.receipt.workspace_root}")
        print(f"Sandbox strategy: {result.sandbox.receipt.materialization_strategy}")
        autofix_result = result.autofix
    else:
        autofix_result = autofix_use_case.run(
            run_id=run_id,
            repo_root=Path(repo_root).resolve(),
            artifact_dir=settings.artifact_dir,
            max_attempts=max_attempts,
            verify_max_attempts=verify_max_attempts,
            timeout_seconds=timeout_seconds,
            objective=objective,
        )
    print(f"Autofix ID: {autofix_result.autofix_id}")
    print(f"Status: {autofix_result.status.value}")
    print(f"Attempts: {len(autofix_result.receipt.attempts)}")
    print(f"Receipt written to: {autofix_result.receipt_path}")
    if autofix_result.receipt.latest_verification_id:
        print(f"Latest verification ID: {autofix_result.receipt.latest_verification_id}")
    return 0 if autofix_result.status == AutofixStatus.SUCCEEDED else 1


def _list_autofix_runs(repository: RunRepository, *, limit: int) -> int:
    runs = repository.list_autofix_runs(limit=limit)
    if not runs:
        print("No autofix runs found.")
        return 0
    for run in runs:
        print(
            " | ".join(
                [
                    run.updated_at,
                    run.status.value,
                    run.autofix_id,
                    run.linked_run_id,
                    f"attempts={run.attempt_count}/{run.max_attempts}",
                    run.summary,
                ]
            )
        )
    return 0


def _show_autofix_run(repository: RunRepository, *, autofix_id: str) -> int:
    run = repository.get_autofix_run(autofix_id)
    if run is None:
        print(f"Autofix run not found: {autofix_id}")
        return 1
    _, payload = run
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _list_autofix_attempts(repository: RunRepository, *, autofix_id: str, limit: int) -> int:
    attempts = repository.list_autofix_attempts(autofix_id=autofix_id, limit=limit)
    if not attempts:
        print("No autofix attempts found.")
        return 0
    for attempt in attempts:
        print(
            " | ".join(
                [
                    attempt.created_at,
                    attempt.status.value,
                    attempt.attempt_id,
                    f"attempt={attempt.attempt_index}",
                    attempt.summary,
                ]
            )
        )
    return 0


def _prepare_sandbox(
    repository: RunRepository,
    *,
    settings: Settings,
    repo_root: str,
    run_id: str | None,
) -> int:
    result = ManageSandboxUseCase(
        repository,
        LocalSandboxManager(max_file_bytes=settings.sandbox_max_file_bytes),
    ).prepare(
        repo_root=Path(repo_root).resolve(),
        sandbox_dir=settings.sandbox_dir,
        artifact_dir=settings.artifact_dir,
        linked_run_id=run_id,
        summary="Sandbox prepared by operator request.",
    )
    print(f"Sandbox ID: {result.sandbox_id}")
    print(f"Workspace: {result.receipt.workspace_root}")
    print(f"Strategy: {result.receipt.materialization_strategy}")
    if result.receipt.source_branch:
        print(f"Source branch: {result.receipt.source_branch}")
    if result.receipt.source_head_sha:
        print(f"Source HEAD: {result.receipt.source_head_sha}")
    print(f"Copied files: {result.receipt.copied_file_count}")
    print(f"Receipt written to: {result.receipt_path}")
    return 0


def _list_sandboxes(repository: RunRepository, *, limit: int) -> int:
    sandboxes = repository.list_sandboxes(limit=limit)
    if not sandboxes:
        print("No sandboxes found.")
        return 0
    for sandbox in sandboxes:
        print(
            " | ".join(
                [
                    sandbox.updated_at,
                    sandbox.status.value,
                    sandbox.sandbox_id,
                    sandbox.linked_run_id or "-",
                    sandbox.summary,
                ]
            )
        )
    return 0


def _show_sandbox(repository: RunRepository, *, sandbox_id: str) -> int:
    sandbox = repository.get_sandbox(sandbox_id)
    if sandbox is None:
        print(f"Sandbox not found: {sandbox_id}")
        return 1
    _, payload = sandbox
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _cleanup_sandbox(repository: RunRepository, *, settings: Settings, sandbox_id: str) -> int:
    result = ManageSandboxUseCase(
        repository,
        LocalSandboxManager(max_file_bytes=settings.sandbox_max_file_bytes),
    ).cleanup(
        sandbox_id=sandbox_id,
        remove_workspace=True,
    )
    print(f"Sandbox ID: {result.sandbox_id}")
    print(f"Status: {result.receipt.status.value}")
    print(f"Receipt written to: {result.receipt_path}")
    return 0


def _resolve_repo_root(repository: RunRepository, *, repo_root: str, sandbox_id: str | None) -> Path:
    if sandbox_id is None:
        return Path(repo_root).resolve()
    sandbox = repository.get_sandbox(sandbox_id)
    if sandbox is None:
        raise ValueError(f"Sandbox not found: {sandbox_id}")
    record, _ = sandbox
    return record.workspace_root.resolve()


def _list_patch_proposals(repository: RunRepository, *, limit: int) -> int:
    proposals = repository.list_patch_proposals(limit=limit)
    if not proposals:
        print("No patch proposals found.")
        return 0
    for proposal in proposals:
        print(
            " | ".join(
                [
                    proposal.created_at,
                    proposal.provider.value,
                    proposal.proposal_id,
                    proposal.linked_run_id,
                    proposal.summary,
                ]
            )
        )
    return 0


def _show_patch_proposal(repository: RunRepository, *, proposal_id: str) -> int:
    proposal = repository.get_patch_proposal(proposal_id)
    if proposal is None:
        print(f"Patch proposal not found: {proposal_id}")
        return 1
    _, payload = proposal
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _execute_patch(
    repository: RunRepository,
    *,
    settings: Settings,
    artifact_dir: Path,
    proposal_file: str,
    repo_root: str,
    sandbox: bool,
    sandbox_id: str | None,
    output_dir: str | None,
    mode: str,
) -> int:
    proposal_path = Path(proposal_file).resolve()
    data = json.loads(proposal_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Patch proposal file must contain a JSON object.")
    proposal = PatchProposal.from_dict(data)
    output_base = Path(output_dir).resolve() if output_dir else artifact_dir
    executor = ExecutePatchProposalUseCase(
        repository,
        guardrails=WorkspaceGuardrails(),
        mutator=LocalWorkspaceMutator(),
    )
    failed = False
    sandbox_result = None
    try:
        if sandbox:
            sandbox_use_case = ManageSandboxUseCase(
                repository,
                LocalSandboxManager(max_file_bytes=settings.sandbox_max_file_bytes),
            )
            result = RunSandboxedPatchExecutionUseCase(sandbox_use_case, executor).run(
                proposal=proposal,
                source_repo_root=Path(repo_root).resolve(),
                artifact_dir=output_base,
                sandbox_dir=settings.sandbox_dir,
                mode=PatchExecutionMode(mode),
            )
            sandbox_result = result.sandbox
            execution_result = result.execution
        else:
            execution_result = executor.execute(
                proposal=proposal,
                repo_root=_resolve_repo_root(repository, repo_root=repo_root, sandbox_id=sandbox_id),
                artifact_dir=output_base,
                mode=PatchExecutionMode(mode),
            )
    except SandboxedPatchExecutionFailedError as exc:
        failed = True
        sandbox_result = exc.result.sandbox
        execution_result = exc.result.execution
    except PatchExecutionFailedError as exc:
        failed = True
        execution_result = exc.result
    if sandbox_result is not None:
        print(f"Sandbox ID: {sandbox_result.sandbox_id}")
        print(f"Sandbox workspace: {sandbox_result.receipt.workspace_root}")
    changed_files = sum(1 for item in execution_result.receipt.receipts if item.changed)
    print(f"Execution ID: {execution_result.execution_id}")
    print(f"Mode: {execution_result.mode.value}")
    print(f"Receipt written to: {execution_result.receipt_path}")
    print(f"Changed files: {changed_files}")
    if execution_result.receipt.error_message:
        print(f"Error: {execution_result.receipt.error_message}")
    return 1 if failed else 0


def _list_executions(repository: RunRepository, *, limit: int) -> int:
    executions = repository.list_executions(limit=limit)
    if not executions:
        print("No executions found.")
        return 0
    for execution in executions:
        print(
            " | ".join(
                [
                    execution.created_at,
                    execution.status.value,
                    execution.execution_id,
                    execution.proposal_id,
                    execution.mode.value,
                    execution.summary,
                ]
            )
        )
    return 0


def _show_execution(repository: RunRepository, *, execution_id: str) -> int:
    execution = repository.get_execution(execution_id)
    if execution is None:
        print(f"Execution not found: {execution_id}")
        return 1
    _, payload = execution
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _verify(
    repository: RunRepository,
    *,
    settings: Settings,
    safety_policy: SafetyPolicy,
    artifact_dir: Path,
    run_id: str | None,
    execution_id: str | None,
    repo_root: str,
    sandbox_id: str | None,
    output_dir: str | None,
    max_attempts: int,
    timeout_seconds: int,
    runtime: str | None,
) -> int:
    output_base = Path(output_dir).resolve() if output_dir else artifact_dir
    verification_runtime = ExecutionRuntime(runtime) if runtime else settings.verification_runtime
    verifier = VerifyRunUseCase(
        repository,
        safety_policy,
        command_runner=build_command_runner(settings, verification_runtime),
    )
    result = verifier.verify(
        repo_root=_resolve_repo_root(repository, repo_root=repo_root, sandbox_id=sandbox_id),
        artifact_dir=output_base,
        run_id=run_id,
        execution_id=execution_id,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
    )
    print(f"Verification ID: {result.verification_id}")
    print(f"Status: {result.receipt.status.value}")
    print(f"Stop reason: {result.receipt.stop_reason.value}")
    print(f"Receipt written to: {result.receipt_path}")
    print(f"Attempts: {len(result.receipt.attempts)}")
    return 0


def _list_verifications(repository: RunRepository, *, limit: int) -> int:
    verifications = repository.list_verifications(limit=limit)
    if not verifications:
        print("No verifications found.")
        return 0
    for verification in verifications:
        print(
            " | ".join(
                [
                    verification.created_at,
                    verification.status.value,
                    verification.verification_id,
                    verification.stop_reason.value,
                    verification.summary,
                ]
            )
        )
    return 0


def _show_verification(repository: RunRepository, *, verification_id: str) -> int:
    verification = repository.get_verification(verification_id)
    if verification is None:
        print(f"Verification not found: {verification_id}")
        return 1
    _, payload = verification
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _request_approval(
    repository: RunRepository,
    *,
    settings: Settings,
    access_controller: TenantAccessController,
    notification_outbox: FileNotificationOutbox,
    run_id: str,
    execution_id: str,
    verification_id: str,
    actor: str,
    team: str,
    comment: str,
    expires_in_hours: int | None,
    assigned_reviewers: list[str],
    assigned_reviewer_teams: list[str],
) -> int:
    run = repository.get_run(run_id)
    if run is None:
        print(f"Run not found: {run_id}")
        return 1
    run_record, _ = run
    tenant_context = access_controller.require_repo_permission(
        repo_full_name=run_record.repo_full_name,
        actor=actor,
        permission=PlatformPermission.REQUEST_APPROVAL,
        team=team,
    )
    approval_policy = _approval_policy_for_context(settings=settings, tenant_context=tenant_context)
    requester = RequestApprovalUseCase(repository, approval_policy)
    result = requester.request_delivery_approval(
        run_id=run_id,
        execution_id=execution_id,
        verification_id=verification_id,
        actor=actor,
        team=team,
        comment=comment,
        expires_in_hours=expires_in_hours or settings.approval_ttl_hours,
        assigned_reviewers=assigned_reviewers,
        assigned_reviewer_teams=assigned_reviewer_teams,
    )
    _emit_platform_notification(
        notification_outbox=notification_outbox,
        settings=settings,
        tenant_context=tenant_context,
        event_type=NotificationEventType.APPROVAL_REQUESTED,
        summary=f"Approval {result.approval_id} is {result.receipt.status.value} for {run_record.repo_full_name}.",
        payload={
            "approval_id": result.approval_id,
            "repo_full_name": run_record.repo_full_name,
            "status": result.receipt.status.value,
            "risk_level": result.receipt.risk_level.value,
            "required_approvals": result.receipt.required_approvals,
        },
    )
    print(f"Approval ID: {result.approval_id}")
    print(f"Status: {result.receipt.status.value}")
    print(f"Risk level: {result.receipt.risk_level.value}")
    print(f"Required approvals: {result.receipt.required_approvals}")
    if result.receipt.expires_at:
        print(f"Expires at: {result.receipt.expires_at}")
    print(f"Receipt written to: {result.receipt_path}")
    return 0 if result.receipt.status != ApprovalStatus.REJECTED else 1


def _review_approval(
    repository: RunRepository,
    *,
    settings: Settings,
    access_controller: TenantAccessController,
    notification_outbox: FileNotificationOutbox,
    approval_id: str,
    actor: str,
    team: str,
    decision: str,
    comment: str,
) -> int:
    approval = repository.get_approval(approval_id)
    if approval is None:
        print(f"Approval not found: {approval_id}")
        return 1
    approval_record, _ = approval
    tenant_context = access_controller.require_repo_permission(
        repo_full_name=approval_record.repo_full_name,
        actor=actor,
        permission=PlatformPermission.REVIEW_APPROVAL,
        team=team,
    )
    approval_policy = _approval_policy_for_context(settings=settings, tenant_context=tenant_context)
    reviewer = ReviewApprovalUseCase(repository, approval_policy)
    result = reviewer.decide(
        approval_id=approval_id,
        actor=actor,
        team=team,
        decision=ApprovalDecision(decision),
        comment=comment,
    )
    _emit_platform_notification(
        notification_outbox=notification_outbox,
        settings=settings,
        tenant_context=tenant_context,
        event_type=NotificationEventType.APPROVAL_REVIEWED,
        summary=(
            f"Approval {approval_id} is now {result.receipt.status.value} "
            f"after a {decision} decision."
        ),
        payload={
            "approval_id": approval_id,
            "repo_full_name": approval_record.repo_full_name,
            "status": result.receipt.status.value,
            "approved_count": result.receipt.approved_count,
            "required_approvals": result.receipt.required_approvals,
        },
    )
    print(f"Approval ID: {result.approval_id}")
    print(f"Status: {result.receipt.status.value}")
    print(f"Approved count: {result.receipt.approved_count}/{result.receipt.required_approvals}")
    print(f"Receipt written to: {result.receipt_path}")
    return 0 if result.receipt.status != ApprovalStatus.REJECTED else 1


def _list_approvals(
    repository: RunRepository,
    *,
    limit: int,
    status: ApprovalStatus | None,
) -> int:
    approvals = repository.list_approvals(limit=limit, status=status)
    if not approvals:
        print("No approvals found.")
        return 0
    for approval in approvals:
        print(
            " | ".join(
                [
                    approval.updated_at,
                    approval.status.value,
                    approval.approval_id,
                    approval.repo_full_name,
                    approval.risk_level.value,
                    f"{approval.approved_count}/{approval.required_approvals}",
                    approval.summary,
                ]
            )
        )
    return 0


def _show_approval(repository: RunRepository, *, approval_id: str) -> int:
    approval = repository.get_approval(approval_id)
    if approval is None:
        print(f"Approval not found: {approval_id}")
        return 1
    _, payload = approval
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _register_tenant(
    repository: RunRepository,
    *,
    access_controller: TenantAccessController,
    artifact_dir: Path,
    tenant_id: str,
    name: str,
    repo_patterns: list[str],
    admin_actor: str,
    admin_team: str,
    policy_file: str | None,
) -> int:
    result = ManageTenantUseCase(repository, access_controller).register_tenant(
        tenant_id=tenant_id,
        name=name,
        repo_patterns=repo_patterns,
        admin_actor=admin_actor,
        admin_team=admin_team,
        artifact_dir=artifact_dir,
        policy_overrides=_load_optional_json_file(policy_file),
    )
    print(f"Tenant ID: {result.tenant_id}")
    print(f"Config written to: {result.config_path}")
    return 0


def _list_tenants(repository: RunRepository, *, limit: int) -> int:
    tenants = repository.list_tenants(limit=limit)
    if not tenants:
        print("No tenants found.")
        return 0
    for tenant in tenants:
        print(
            " | ".join(
                [
                    tenant.updated_at,
                    tenant.status.value,
                    tenant.tenant_id,
                    tenant.name,
                    tenant.summary,
                ]
            )
        )
    return 0


def _set_tenant_policy(
    repository: RunRepository,
    *,
    access_controller: TenantAccessController,
    tenant_id: str,
    actor: str,
    policy_file: str,
) -> int:
    result = ManageTenantUseCase(repository, access_controller).set_policy_overrides(
        tenant_id=tenant_id,
        actor=actor,
        policy_overrides=_load_json_file(policy_file),
    )
    print(f"Tenant ID: {result.tenant_id}")
    print(f"Config updated at: {result.config_path}")
    return 0


def _set_tenant_status(
    repository: RunRepository,
    *,
    access_controller: TenantAccessController,
    tenant_id: str,
    actor: str,
    status: str,
) -> int:
    result = ManageTenantUseCase(repository, access_controller).set_status(
        tenant_id=tenant_id,
        actor=actor,
        status=TenantStatus(status),
    )
    print(f"Tenant ID: {result.tenant_id}")
    print(f"Config updated at: {result.config_path}")
    return 0


def _add_member(
    repository: RunRepository,
    *,
    access_controller: TenantAccessController,
    tenant_id: str,
    actor: str,
    member_actor: str,
    role: str,
    team: str,
) -> int:
    membership = ManageTenantUseCase(repository, access_controller).add_membership(
        tenant_id=tenant_id,
        actor=actor,
        member_actor=member_actor,
        role=TenantRole(role),
        team=team,
    )
    print(f"Tenant ID: {membership.tenant_id}")
    print(f"Member: {membership.actor}")
    print(f"Role: {membership.role.value}")
    print(f"Team: {membership.team}")
    return 0


def _sync_memberships(
    repository: RunRepository,
    *,
    access_controller: TenantAccessController,
    tenant_id: str,
    actor: str,
    team: str | None,
    memberships_file: str,
    replace_existing: bool,
) -> int:
    payload = json.loads(Path(memberships_file).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        print("Membership sync file must contain a JSON object.")
        return 1
    memberships_payload = payload.get("memberships")
    if not isinstance(memberships_payload, list) or not memberships_payload:
        print("Membership sync file must include a non-empty memberships array.")
        return 1
    if any(not isinstance(item, dict) for item in memberships_payload):
        print("Each membership entry must be a JSON object.")
        return 1
    memberships = [
        IdentitySyncMembership(
            actor=_required_string(item, "actor"),
            role=TenantRole(_required_string(item, "role")),
            team=_required_string(item, "team"),
        )
        for item in memberships_payload
    ]
    result = SyncIdentityUseCase(repository, access_controller).sync_tenant_memberships(
        tenant_id=tenant_id,
        memberships=memberships,
        replace_existing=replace_existing,
        actor=actor,
        team=team,
    )
    print(f"Tenant ID: {result.receipt.tenant_id}")
    print(f"Synced by: {result.receipt.synced_by}")
    print(f"Created: {result.receipt.created_count}")
    print(f"Updated: {result.receipt.updated_count}")
    print(f"Removed: {result.receipt.removed_count}")
    print(f"Membership count: {result.receipt.membership_count}")
    return 0


def _list_members(
    repository: RunRepository,
    *,
    access_controller: TenantAccessController,
    tenant_id: str,
    actor: str,
) -> int:
    access_controller.require_tenant_permission(
        tenant_id=tenant_id,
        actor=actor,
        permission=PlatformPermission.MANAGE_MEMBERSHIP,
    )
    members = repository.list_tenant_memberships(tenant_id)
    if not members:
        print("No members found.")
        return 0
    for member in members:
        print(
            " | ".join(
                [
                    member.updated_at,
                    member.actor,
                    member.role.value,
                    member.team,
                ]
            )
        )
    return 0


def _dashboard(
    repository: RunRepository,
    *,
    access_controller: TenantAccessController,
    tenant_id: str,
    actor: str,
) -> int:
    result = DashboardUseCase(repository, access_controller).build(tenant_id=tenant_id, actor=actor)
    summary = result.summary
    print(f"Tenant: {summary.tenant_id} | {summary.tenant_name}")
    print(f"Generated at: {summary.generated_at}")
    print(f"Run counts: {json.dumps(summary.run_counts, sort_keys=True)}")
    print(f"Approval counts: {json.dumps(summary.approval_counts, sort_keys=True)}")
    print(f"Delivery counts: {json.dumps(summary.delivery_counts, sort_keys=True)}")
    print(f"Notification counts: {json.dumps(summary.notification_counts, sort_keys=True)}")
    print("Pending approvals:")
    if not summary.pending_approvals:
        print("  none")
    else:
        for item in summary.pending_approvals:
            print(f"  {item.created_at} | {item.record_id} | {item.status} | {item.summary}")
    print("Recent deliveries:")
    if not summary.recent_deliveries:
        print("  none")
    else:
        for item in summary.recent_deliveries:
            print(f"  {item.created_at} | {item.record_id} | {item.status} | {item.summary}")
    print("Recent notifications:")
    if not summary.recent_notifications:
        print("  none")
    else:
        for item in summary.recent_notifications:
            print(f"  {item.created_at} | {item.record_id} | {item.status} | {item.summary}")
    return 0


def _list_notifications(
    repository: RunRepository,
    *,
    access_controller: TenantAccessController,
    tenant_id: str,
    actor: str,
    limit: int,
) -> int:
    access_controller.require_tenant_permission(
        tenant_id=tenant_id,
        actor=actor,
        permission=PlatformPermission.VIEW_NOTIFICATIONS,
    )
    notifications = repository.list_notifications(tenant_id=tenant_id, limit=limit)
    if not notifications:
        print("No notifications found.")
        return 0
    for item in notifications:
        print(
            " | ".join(
                [
                    item.created_at,
                    item.event_type.value,
                    item.notification_id,
                    item.summary,
                ]
            )
        )
    return 0


def _list_alerts(
    repository: RunRepository,
    *,
    tenant_id: str | None,
    limit: int,
    status: AlertStatus | None,
) -> int:
    alerts = repository.list_alerts(tenant_id=tenant_id, limit=limit, status=status)
    if not alerts:
        print("No alerts found.")
        return 0
    for item in alerts:
        print(
            " | ".join(
                [
                    item.created_at,
                    item.severity.value,
                    item.source,
                    item.alert_id,
                    item.tenant_id or "-",
                    item.summary,
                ]
            )
        )
    return 0


def _list_traces(
    repository: RunRepository,
    *,
    trace_id: str | None,
    run_id: str | None,
    job_id: str | None,
    limit: int,
) -> int:
    traces = repository.list_trace_events(
        trace_id=trace_id,
        linked_run_id=run_id,
        linked_job_id=job_id,
        limit=limit,
    )
    if not traces:
        print("No trace events found.")
        return 0
    for item in traces:
        print(
            " | ".join(
                [
                    item.recorded_at,
                    item.trace_id,
                    item.source,
                    item.span_name,
                    item.status,
                    item.linked_run_id or "-",
                    item.linked_job_id or "-",
                ]
            )
        )
    return 0


def _export_audit(repository: RunRepository, *, run_id: str, output_dir: Path) -> int:
    result = RunAuditExporter(repository).export_run(run_id=run_id, output_dir=output_dir)
    print(f"Export ID: {result.export_id}")
    print(f"Bundle written to: {result.bundle_path}")
    print(f"Manifest written to: {result.manifest_path}")
    print(f"Archive written to: {result.archive_path}")
    return 0


def _enforce_retention(repository: RunRepository, *, settings: Settings, dry_run: bool) -> int:
    result = RetentionEnforcer(repository, settings).enforce(dry_run=dry_run)
    print(f"Dry run: {result.dry_run}")
    print(f"Notifications matched: {result.notification_count}")
    print(f"Worker heartbeats matched: {result.worker_heartbeat_count}")
    print(f"Alerts matched: {result.alert_count}")
    print(f"Trace events matched: {result.trace_count}")
    if result.deleted_paths:
        print("Deleted paths:")
        for item in result.deleted_paths:
            print(f"  {item}")
    return 0


def _schema_status(manager: ManageReleaseUseCase, *, as_json: bool) -> int:
    result = manager.schema_status()
    if as_json:
        print(
            json.dumps(
                {
                    "current_version": result.current_version,
                    "migrations": result.migrations,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"Current schema version: {result.current_version}")
    for item in result.migrations:
        print(f"{item['version']} | {item['name']} | {item['applied_at']}")
    return 0


def _backup_state(
    manager: ManageReleaseUseCase,
    *,
    output_dir: Path,
    include_artifacts: bool,
) -> int:
    result = manager.backup_state(output_dir=output_dir, include_artifacts=include_artifacts)
    print(f"Backup directory: {result.backup_dir}")
    print(f"Manifest written to: {result.manifest_path}")
    print(f"Archive written to: {result.archive_path}")
    return 0


def _restore_state(
    manager: ManageReleaseUseCase,
    *,
    manifest_path: Path,
    target_dir: Path,
) -> int:
    result = manager.restore_state(manifest_path=manifest_path, target_dir=target_dir)
    print(f"Restored state into: {result.target_dir}")
    print(f"Database restored to: {result.restored_database_path}")
    print(f"Artifacts restored to: {result.restored_artifact_dir}")
    return 0


def _release_manifest(manager: ManageReleaseUseCase, *, output_file: Path) -> int:
    result = manager.build_release_manifest(output_path=output_file)
    print(f"Release manifest written to: {result.manifest_path}")
    print(json.dumps(result.manifest, indent=2, sort_keys=True))
    return 0


def _smoke_test(use_case: RunSmokeTestUseCase, *, output_dir: Path) -> int:
    result = use_case.run(output_dir=output_dir)
    print(f"Smoke receipt written to: {result.receipt_path}")
    print(json.dumps(result.payload, indent=2, sort_keys=True))
    return 0 if result.payload.get("verification_status") == "succeeded" else 1


def _enqueue_plan(
    repository: RunRepository,
    *,
    settings: Settings,
    access_controller: TenantAccessController,
    budget_manager: QueueBudgetManager,
    repo_full_name: str,
    issue_number: int,
    repo_root: str,
    actor: str,
    team: str,
    provider: str,
    objective: str | None,
    create_branch: bool,
    priority: int,
    max_attempts: int | None,
    budget_units: int | None,
    output_dir: str | None,
    worker_tags: list[str] | None,
    concurrency_key: str | None,
) -> int:
    manager = ManageQueueUseCase(repository, settings, access_controller, budget_manager)
    result = manager.enqueue_plan(
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        repo_root=Path(repo_root).resolve(),
        provider=provider,
        actor=actor,
        team=team,
        objective=objective,
        create_branch=create_branch,
        priority=priority,
        max_attempts=max_attempts,
        budget_units=budget_units,
        output_dir=Path(output_dir).resolve() if output_dir else None,
        required_worker_tags=worker_tags,
        concurrency_key=concurrency_key,
    )
    print(f"Job ID: {result.job_id}")
    print(f"Status: {result.status.value}")
    print(f"Receipt written to: {result.receipt_path}")
    return 0


def _enqueue_verify(
    repository: RunRepository,
    *,
    settings: Settings,
    access_controller: TenantAccessController,
    budget_manager: QueueBudgetManager,
    run_id: str | None,
    execution_id: str | None,
    repo_root: str,
    actor: str,
    team: str,
    priority: int,
    max_attempts: int | None,
    budget_units: int | None,
    verify_max_attempts: int,
    timeout_seconds: int,
    output_dir: str | None,
    worker_tags: list[str] | None,
    concurrency_key: str | None,
) -> int:
    manager = ManageQueueUseCase(repository, settings, access_controller, budget_manager)
    result = manager.enqueue_verify(
        run_id=run_id,
        execution_id=execution_id,
        repo_root=Path(repo_root).resolve(),
        actor=actor,
        team=team,
        priority=priority,
        max_attempts=max_attempts,
        budget_units=budget_units,
        verify_max_attempts=verify_max_attempts,
        timeout_seconds=timeout_seconds,
        output_dir=Path(output_dir).resolve() if output_dir else None,
        required_worker_tags=worker_tags,
        concurrency_key=concurrency_key,
    )
    print(f"Job ID: {result.job_id}")
    print(f"Status: {result.status.value}")
    print(f"Receipt written to: {result.receipt_path}")
    return 0


def _enqueue_deliver(
    repository: RunRepository,
    *,
    settings: Settings,
    access_controller: TenantAccessController,
    budget_manager: QueueBudgetManager,
    run_id: str,
    execution_id: str,
    verification_id: str,
    approval_id: str | None,
    repo_root: str,
    actor: str,
    team: str,
    priority: int,
    max_attempts: int | None,
    budget_units: int | None,
    base_branch: str | None,
    rollout_stage: str | None,
    commit_message: str | None,
    pr_title: str | None,
    publish_pr_comment: bool,
    worker_tags: list[str] | None,
    concurrency_key: str | None,
) -> int:
    manager = ManageQueueUseCase(repository, settings, access_controller, budget_manager)
    result = manager.enqueue_deliver(
        run_id=run_id,
        execution_id=execution_id,
        verification_id=verification_id,
        approval_id=approval_id,
        repo_root=Path(repo_root).resolve(),
        actor=actor,
        team=team,
        priority=priority,
        max_attempts=max_attempts,
        budget_units=budget_units,
        base_branch=base_branch,
        rollout_stage=rollout_stage,
        commit_message=commit_message,
        pr_title=pr_title,
        publish_pr_comment=publish_pr_comment,
        required_worker_tags=worker_tags,
        concurrency_key=concurrency_key,
    )
    print(f"Job ID: {result.job_id}")
    print(f"Status: {result.status.value}")
    print(f"Receipt written to: {result.receipt_path}")
    return 0


def _list_queue_jobs(
    repository: RunRepository,
    *,
    limit: int,
    status: QueueJobStatus | None,
    job_type: QueueJobType | None,
) -> int:
    jobs = repository.list_queue_jobs(limit=limit, status=status, job_type=job_type)
    if not jobs:
        print("No queue jobs found.")
        return 0
    for job in jobs:
        print(
            " | ".join(
                [
                    job.updated_at,
                    job.status.value,
                    job.job_id,
                    job.job_type.value,
                    job.repo_full_name,
                    f"attempts={job.attempt_count}/{job.max_attempts}",
                    f"budget={job.budget_used}/{job.budget_units}",
                    f"lease={job.lease_expires_at or '-'}",
                    f"tags={','.join(job.required_worker_tags) or '-'}",
                    job.summary,
                ]
            )
        )
    return 0


def _show_queue_job(repository: RunRepository, *, job_id: str) -> int:
    job = repository.get_queue_job(job_id)
    if job is None:
        print(f"Queue job not found: {job_id}")
        return 1
    _, payload = job
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _cancel_queue_job(
    repository: RunRepository,
    *,
    settings: Settings,
    access_controller: TenantAccessController,
    budget_manager: QueueBudgetManager,
    job_id: str,
    actor: str,
    team: str,
) -> int:
    manager = ManageQueueUseCase(repository, settings, access_controller, budget_manager)
    result = manager.cancel_job(job_id=job_id, actor=actor, team=team)
    print(f"Job ID: {result.job_id}")
    print(f"Status: {result.status.value}")
    print(f"Receipt written to: {result.receipt_path}")
    return 0


def _resume_queue_job(
    repository: RunRepository,
    *,
    settings: Settings,
    access_controller: TenantAccessController,
    budget_manager: QueueBudgetManager,
    job_id: str,
    actor: str,
    team: str,
    reset_attempts: bool,
) -> int:
    manager = ManageQueueUseCase(repository, settings, access_controller, budget_manager)
    result = manager.resume_job(
        job_id=job_id,
        actor=actor,
        team=team,
        reset_attempts=reset_attempts,
    )
    print(f"Job ID: {result.job_id}")
    print(f"Status: {result.status.value}")
    print(f"Receipt written to: {result.receipt_path}")
    return 0


def _list_queue_attempts(repository: RunRepository, *, job_id: str) -> int:
    attempts = repository.list_queue_attempts(job_id)
    if not attempts:
        print("No queue attempts found.")
        return 0
    for attempt in attempts:
        print(
            " | ".join(
                [
                    attempt.created_at,
                    attempt.status.value,
                    attempt.attempt_id,
                    f"attempt={attempt.attempt_index}",
                    attempt.worker_id,
                    attempt.summary,
                ]
            )
        )
    return 0


def _worker_run(
    repository: RunRepository,
    *,
    settings: Settings,
    access_controller: TenantAccessController,
    budget_manager: QueueBudgetManager,
    notification_outbox: FileNotificationOutbox,
    metrics_reporter: QueueMetricsReporter,
    trace_recorder: TraceRecorder,
    alert_manager: AlertManager,
    worker_id: str,
    max_jobs: int,
    job_types: list[str] | None,
    worker_tags: list[str] | None,
) -> int:
    processor = ProcessQueueUseCase(
        repository,
        settings,
        access_controller,
        budget_manager,
        metrics_reporter,
        notification_outbox=notification_outbox,
        trace_recorder=trace_recorder,
        alert_manager=alert_manager,
    )
    result = processor.process(
        worker_id=worker_id,
        max_jobs=max_jobs,
        allowed_types=[QueueJobType(item) for item in job_types] if job_types else None,
        worker_tags=worker_tags,
    )
    print(f"Worker ID: {result.worker_id}")
    print(f"Processed jobs: {result.processed_jobs}")
    print(f"Succeeded jobs: {result.succeeded_jobs}")
    print(f"Failed jobs: {result.failed_jobs}")
    print(f"Cancelled jobs: {result.cancelled_jobs}")
    print(f"Heartbeat written to: {result.heartbeat_path}")
    print(f"Metrics JSON written to: {result.metrics_json_path}")
    print(f"Metrics Prometheus written to: {result.metrics_prom_path}")
    return 0


def _list_worker_heartbeats(
    repository: RunRepository,
    *,
    worker_id: str | None,
    limit: int,
) -> int:
    heartbeats = repository.list_worker_heartbeats(worker_id=worker_id, limit=limit)
    if not heartbeats:
        print("No worker heartbeats found.")
        return 0
    for heartbeat in heartbeats:
        print(
            " | ".join(
                [
                    heartbeat.recorded_at,
                    heartbeat.status.value,
                    heartbeat.worker_id,
                    heartbeat.current_job_id or "-",
                    ",".join(heartbeat.advertised_worker_tags) or "-",
                    heartbeat.summary,
                ]
            )
        )
    return 0


def _metrics(metrics_reporter: QueueMetricsReporter, *, output_dir: Path) -> int:
    json_path, prom_path = metrics_reporter.write_snapshot(output_dir)
    print(f"Metrics JSON written to: {json_path}")
    print(f"Metrics Prometheus written to: {prom_path}")
    print(prom_path.read_text(encoding="utf-8").strip())
    return 0


def _deliver(
    repository: RunRepository,
    *,
    github: GitHubClient,
    safety_policy: SafetyPolicy,
    settings: Settings,
    access_controller: TenantAccessController,
    notification_outbox: FileNotificationOutbox,
    artifact_dir: Path,
    artifact_base_url: str | None,
    remote_name: str,
    run_id: str,
    execution_id: str,
    verification_id: str,
    approval_id: str | None,
    actor: str | None,
    repo_root: str,
    sandbox_id: str | None,
    base_branch: str | None,
    rollout_stage: str | None,
    commit_message: str | None,
    pr_title: str | None,
    publish_pr_comment: bool,
) -> int:
    run = repository.get_run(run_id)
    if run is None:
        print(f"Run not found: {run_id}")
        return 1
    run_record, _ = run
    tenant_context = access_controller.require_repo_permission(
        repo_full_name=run_record.repo_full_name,
        actor=actor,
        permission=PlatformPermission.DELIVER,
    )
    approval_policy = _approval_policy_for_context(
        settings=settings,
        tenant_context=tenant_context,
    )
    delivery_governance_policy = _delivery_governance_for_context(
        settings=settings,
        tenant_context=tenant_context,
    )
    deliverer = DeliverRunUseCase(
        repository,
        github,
        safety_policy,
        approval_policy=approval_policy,
        delivery_governance_policy=delivery_governance_policy,
    )
    result = deliverer.deliver(
        run_id=run_id,
        execution_id=execution_id,
        verification_id=verification_id,
        approval_id=approval_id,
        repo_root=_resolve_repo_root(repository, repo_root=repo_root, sandbox_id=sandbox_id),
        artifact_dir=artifact_dir,
        artifact_base_url=artifact_base_url,
        artifact_store_backend=settings.artifact_store_backend,
        artifact_store_dir=settings.artifact_store_dir,
        artifact_store_base_url=settings.artifact_store_base_url,
        remote_name=remote_name,
        base_branch=base_branch,
        rollout_stage=rollout_stage,
        commit_message=commit_message,
        pr_title=pr_title,
        publish_pr_comment=publish_pr_comment,
    )
    _emit_platform_notification(
        notification_outbox=notification_outbox,
        settings=settings,
        tenant_context=tenant_context,
        event_type=NotificationEventType.DELIVERY_SUCCEEDED
        if result.receipt.status == DeliveryStatus.SUCCEEDED
        else NotificationEventType.DELIVERY_BLOCKED,
        summary=(
            f"Delivery {result.delivery_id} is {result.receipt.status.value} "
            f"for {result.receipt.repo_full_name}."
        ),
        payload={
            "delivery_id": result.delivery_id,
            "repo_full_name": result.receipt.repo_full_name,
            "status": result.receipt.status.value,
            "approval_id": result.receipt.linked_approval_id,
            "error_message": result.receipt.error_message,
        },
    )
    print(f"Delivery ID: {result.delivery_id}")
    print(f"Status: {result.receipt.status.value}")
    print(f"Receipt written to: {result.receipt_path}")
    if result.receipt.commit_sha:
        print(f"Commit SHA: {result.receipt.commit_sha}")
    if result.receipt.pr is not None:
        print(f"Draft PR: {result.receipt.pr.html_url}")
    if result.receipt.pr_comment is not None:
        print(f"PR comment: {result.receipt.pr_comment.html_url}")
    if result.receipt.error_message:
        print(f"Error: {result.receipt.error_message}")
    return 0 if result.receipt.status == DeliveryStatus.SUCCEEDED else 1


def _list_deliveries(repository: RunRepository, *, limit: int) -> int:
    deliveries = repository.list_deliveries(limit=limit)
    if not deliveries:
        print("No deliveries found.")
        return 0
    for delivery in deliveries:
        print(
            " | ".join(
                [
                    delivery.created_at,
                    delivery.status.value,
                    delivery.delivery_id,
                    delivery.repo_full_name,
                    delivery.branch_name,
                    delivery.summary,
                ]
            )
        )
    return 0


def _show_delivery(repository: RunRepository, *, delivery_id: str) -> int:
    delivery = repository.get_delivery(delivery_id)
    if delivery is None:
        print(f"Delivery not found: {delivery_id}")
        return 1
    _, payload = delivery
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _approval_policy_for_context(
    *,
    settings: Settings,
    tenant_context: tuple[object, dict[str, object]] | None,
) -> ApprovalPolicyEvaluator:
    overrides = None
    if tenant_context is not None:
        _, tenant_payload = tenant_context
        policy_overrides = tenant_payload.get("policy_overrides")
        if isinstance(policy_overrides, dict):
            overrides = policy_overrides
    return ApprovalPolicyEvaluator(settings.approval_policy_path, policy_overrides=overrides)


def _delivery_governance_for_context(
    *,
    settings: Settings,
    tenant_context: tuple[object, dict[str, object]] | None,
) -> DeliveryGovernancePolicyEvaluator:
    overrides = None
    if tenant_context is not None:
        _, tenant_payload = tenant_context
        policy_overrides = tenant_payload.get("policy_overrides")
        if isinstance(policy_overrides, dict):
            overrides = policy_overrides
    return DeliveryGovernancePolicyEvaluator(
        settings.delivery_governance_policy_path,
        policy_overrides=overrides,
    )


def _emit_platform_notification(
    *,
    notification_outbox: FileNotificationOutbox,
    settings: Settings,
    tenant_context: tuple[object, dict[str, object]] | None,
    event_type: NotificationEventType,
    summary: str,
    payload: dict[str, object],
) -> None:
    if tenant_context is None:
        return
    tenant_record, _ = tenant_context
    notification_outbox.emit(
        tenant_id=tenant_record.tenant_id,
        event_type=event_type,
        summary=summary,
        payload=payload,
        output_dir=settings.notification_dir,
    )


def _load_json_file(path: str) -> dict[str, object]:
    data = json.loads(Path(path).resolve().read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return data


def _load_optional_json_file(path: str | None) -> dict[str, object] | None:
    if path is None:
        return None
    return _load_json_file(path)


if __name__ == "__main__":
    raise SystemExit(main())
