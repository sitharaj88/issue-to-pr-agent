from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class PlannerProvider(str, Enum):
    HEURISTIC = "heuristic"
    OPENAI = "openai"


class ExecutionMode(str, Enum):
    PLAN_ONLY = "plan_only"
    PREPARE_BRANCH = "prepare_branch"


class RunStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CommandDecision(str, Enum):
    ALLOW = "allow"
    REVIEW = "review"
    BLOCK = "block"


class PatchOperationType(str, Enum):
    WRITE_FILE = "write_file"
    REPLACE_TEXT = "replace_text"
    APPEND_TEXT = "append_text"


class PatcherProvider(str, Enum):
    OPENAI = "openai"


class PatchExecutionMode(str, Enum):
    DRY_RUN = "dry_run"
    APPLY = "apply"


class PatchExecutionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AutofixStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AutofixAttemptStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SandboxStatus(str, Enum):
    PREPARED = "prepared"
    USED = "used"
    CLEANED_UP = "cleaned_up"
    FAILED = "failed"


class ExecutionRuntime(str, Enum):
    LOCAL = "local"
    DOCKER = "docker"


class VerificationStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class VerificationAttemptStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class VerificationStopReason(str, Enum):
    SUCCESS = "success"
    NO_CANDIDATE_COMMANDS = "no_candidate_commands"
    NO_ALLOWED_COMMANDS = "no_allowed_commands"
    CANDIDATE_COMMANDS_EXHAUSTED = "candidate_commands_exhausted"
    MAX_ATTEMPTS_REACHED = "max_attempts_reached"
    EXECUTION_ERROR = "execution_error"


class DeliveryStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ApprovalAction(str, Enum):
    DELIVERY = "delivery"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class ApprovalRiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TenantStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class TenantRole(str, Enum):
    ADMIN = "admin"
    OPERATOR = "operator"
    REVIEWER = "reviewer"
    OBSERVER = "observer"


class PlatformPermission(str, Enum):
    MANAGE_TENANT = "manage_tenant"
    MANAGE_POLICY = "manage_policy"
    MANAGE_MEMBERSHIP = "manage_membership"
    OPERATE_QUEUE = "operate_queue"
    VIEW_QUEUE = "view_queue"
    REQUEST_APPROVAL = "request_approval"
    REVIEW_APPROVAL = "review_approval"
    DELIVER = "deliver"
    VIEW_DASHBOARD = "view_dashboard"
    VIEW_NOTIFICATIONS = "view_notifications"


class AuthSubjectType(str, Enum):
    USER = "user"
    SERVICE = "service"


class NotificationEventType(str, Enum):
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_REVIEWED = "approval_reviewed"
    DELIVERY_SUCCEEDED = "delivery_succeeded"
    DELIVERY_BLOCKED = "delivery_blocked"


class NotificationStatus(str, Enum):
    EMITTED = "emitted"


class AlertSeverity(str, Enum):
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AlertStatus(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"


class QueueJobType(str, Enum):
    PLAN = "plan"
    VERIFY = "verify"
    DELIVER = "deliver"


class QueueJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class QueueAttemptStatus(str, Enum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkerStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"


@dataclass(frozen=True)
class IssueContext:
    repo_full_name: str
    issue_number: int
    title: str
    body: str
    labels: list[str]
    url: str


@dataclass(frozen=True)
class RepoSnapshot:
    root: Path
    is_git_repo: bool
    branch: str | None
    status_short: str
    tracked_files: list[str]
    is_dirty: bool


@dataclass(frozen=True)
class AgentPlan:
    summary: str
    assumptions: list[str] = field(default_factory=list)
    files_to_inspect: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    branch_name: str = "agent/issue-work"
    pr_title: str = ""
    pr_body: str = ""
    risks: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "AgentPlan":
        return cls(
            summary=_as_string(data.get("summary"), "No summary provided."),
            assumptions=_as_string_list(data.get("assumptions")),
            files_to_inspect=_as_string_list(data.get("files_to_inspect")),
            commands=_as_string_list(data.get("commands")),
            tests=_as_string_list(data.get("tests")),
            branch_name=_as_string(data.get("branch_name"), "agent/issue-work"),
            pr_title=_as_string(data.get("pr_title"), ""),
            pr_body=_as_string(data.get("pr_body"), ""),
            risks=_as_string_list(data.get("risks")),
        )


@dataclass(frozen=True)
class RepositoryProfile:
    primary_language: str
    detected_languages: list[str] = field(default_factory=list)
    detected_frameworks: list[str] = field(default_factory=list)
    build_systems: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class IndexedSymbol:
    name: str
    kind: str
    path: str
    line: int
    signature: str = ""


@dataclass(frozen=True)
class RepositoryIndex:
    files_indexed: int = 0
    symbol_count: int = 0
    top_symbols: list[IndexedSymbol] = field(default_factory=list)
    complexity_score: int = 0
    index_version: str = "v1"


@dataclass(frozen=True)
class EvaluationScore:
    score: int
    summary: str
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RankedFile:
    path: str
    score: int
    reasons: list[str] = field(default_factory=list)
    preview: str = ""


@dataclass(frozen=True)
class PlanningContext:
    summary: str
    issue_keywords: list[str] = field(default_factory=list)
    repository_profile: RepositoryProfile = field(
        default_factory=lambda: RepositoryProfile(primary_language="unknown")
    )
    repository_index: RepositoryIndex = field(default_factory=RepositoryIndex)
    evaluation: EvaluationScore = field(
        default_factory=lambda: EvaluationScore(score=0, summary="Planning context not evaluated.")
    )
    ranked_files: list[RankedFile] = field(default_factory=list)
    suggested_test_commands: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CommandAssessment:
    command: str
    decision: CommandDecision
    reason: str


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    created_at: str
    repo_full_name: str
    issue_number: int
    planner_provider: PlannerProvider
    execution_mode: ExecutionMode
    status: RunStatus
    branch_name: str
    summary: str
    issue_url: str
    report_path: Path
    pr_draft_path: Path
    audit_path: Path
    error_message: str | None = None


@dataclass(frozen=True)
class PatchOperation:
    type: PatchOperationType
    path: str
    content: str = ""
    find_text: str = ""
    replace_text: str = ""
    allow_overwrite: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "PatchOperation":
        type_value = _as_string(data.get("type"), "")
        if not type_value:
            raise ValueError("Patch operation must include a type.")
        return cls(
            type=PatchOperationType(type_value),
            path=_as_string(data.get("path"), ""),
            content=_as_string(data.get("content"), ""),
            find_text=_as_string(data.get("find_text"), ""),
            replace_text=_as_string(data.get("replace_text"), ""),
            allow_overwrite=bool(data.get("allow_overwrite", False)),
        )


@dataclass(frozen=True)
class PatchProposal:
    proposal_id: str
    summary: str
    linked_run_id: str | None = None
    rationale: str = ""
    operations: list[PatchOperation] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "PatchProposal":
        operations_raw = data.get("operations")
        if not isinstance(operations_raw, list):
            raise ValueError("Patch proposal must include an operations list.")
        return cls(
            proposal_id=_as_string(data.get("proposal_id"), "proposal"),
            summary=_as_string(data.get("summary"), "Patch proposal"),
            linked_run_id=_as_optional_string(data.get("linked_run_id")),
            rationale=_as_string(data.get("rationale"), ""),
            operations=[PatchOperation.from_dict(item) for item in operations_raw if isinstance(item, dict)],
        )


@dataclass(frozen=True)
class PatchFileContext:
    path: str
    exists: bool
    content: str
    preview: str = ""


@dataclass(frozen=True)
class PatchProposalRecord:
    proposal_id: str
    created_at: str
    linked_run_id: str
    provider: PatcherProvider
    summary: str
    proposal_path: Path
    error_message: str | None = None


@dataclass(frozen=True)
class FileMutationReceipt:
    operation_index: int
    operation_type: PatchOperationType
    path: str
    changed: bool
    before_sha256: str | None
    after_sha256: str
    before_bytes: int
    after_bytes: int
    detail: str


@dataclass(frozen=True)
class PatchExecutionReceipt:
    execution_id: str
    proposal_id: str
    linked_run_id: str | None
    mode: PatchExecutionMode
    status: PatchExecutionStatus
    repo_root: Path
    summary: str
    receipts: list[FileMutationReceipt] = field(default_factory=list)
    blocked_paths: list[str] = field(default_factory=list)
    error_message: str | None = None


@dataclass(frozen=True)
class PatchExecutionRecord:
    execution_id: str
    created_at: str
    proposal_id: str
    linked_run_id: str | None
    mode: PatchExecutionMode
    status: PatchExecutionStatus
    summary: str
    repo_root: Path
    receipt_path: Path
    error_message: str | None = None


@dataclass(frozen=True)
class TestCommandCandidate:
    command: str
    source: str


@dataclass(frozen=True)
class VerificationAttempt:
    attempt_index: int
    command: str
    source: str
    status: VerificationAttemptStatus
    exit_code: int | None
    duration_ms: int
    stdout_path: Path | None
    stderr_path: Path | None
    note: str


@dataclass(frozen=True)
class VerificationReceipt:
    verification_id: str
    linked_run_id: str | None
    linked_execution_id: str | None
    status: VerificationStatus
    stop_reason: VerificationStopReason
    repo_root: Path
    summary: str
    attempts: list[VerificationAttempt] = field(default_factory=list)
    skipped_commands: list[CommandAssessment] = field(default_factory=list)
    error_message: str | None = None


@dataclass(frozen=True)
class VerificationRecord:
    verification_id: str
    created_at: str
    linked_run_id: str | None
    linked_execution_id: str | None
    status: VerificationStatus
    stop_reason: VerificationStopReason
    summary: str
    repo_root: Path
    receipt_path: Path
    error_message: str | None = None


@dataclass(frozen=True)
class AutofixAttemptReceipt:
    attempt_id: str
    created_at: str
    attempt_index: int
    status: AutofixAttemptStatus
    summary: str
    objective: str
    proposal_id: str | None = None
    execution_id: str | None = None
    verification_id: str | None = None
    verification_stop_reason: VerificationStopReason | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class AutofixReceipt:
    autofix_id: str
    linked_run_id: str
    provider: PatcherProvider
    status: AutofixStatus
    repo_root: Path
    max_attempts: int
    objective: str
    attempts: list[AutofixAttemptReceipt] = field(default_factory=list)
    latest_proposal_id: str | None = None
    latest_execution_id: str | None = None
    latest_verification_id: str | None = None
    summary: str = ""
    error_message: str | None = None


@dataclass(frozen=True)
class AutofixRunRecord:
    autofix_id: str
    created_at: str
    updated_at: str
    linked_run_id: str
    provider: PatcherProvider
    status: AutofixStatus
    summary: str
    repo_root: Path
    max_attempts: int
    attempt_count: int
    latest_proposal_id: str | None
    latest_execution_id: str | None
    latest_verification_id: str | None
    receipt_path: Path
    error_message: str | None = None


@dataclass(frozen=True)
class AutofixAttemptRecord:
    attempt_id: str
    autofix_id: str
    attempt_index: int
    created_at: str
    status: AutofixAttemptStatus
    summary: str
    objective: str
    proposal_id: str | None
    execution_id: str | None
    verification_id: str | None
    verification_stop_reason: VerificationStopReason | None
    payload_path: Path
    error_message: str | None = None


@dataclass(frozen=True)
class SandboxReceipt:
    sandbox_id: str
    linked_run_id: str | None
    linked_autofix_id: str | None
    linked_execution_id: str | None
    linked_delivery_id: str | None
    status: SandboxStatus
    source_repo_root: Path
    workspace_root: Path
    copied_file_count: int
    skipped_entry_count: int
    total_bytes: int
    materialization_strategy: str = "copy"
    source_branch: str | None = None
    source_head_sha: str | None = None
    skipped_entries: list[str] = field(default_factory=list)
    summary: str = ""
    error_message: str | None = None


@dataclass(frozen=True)
class SandboxRecord:
    sandbox_id: str
    created_at: str
    updated_at: str
    linked_run_id: str | None
    linked_autofix_id: str | None
    status: SandboxStatus
    source_repo_root: Path
    workspace_root: Path
    copied_file_count: int
    skipped_entry_count: int
    total_bytes: int
    summary: str
    receipt_path: Path
    error_message: str | None = None


@dataclass(frozen=True)
class ArtifactReference:
    label: str
    path: str
    url: str | None = None


@dataclass(frozen=True)
class GitHubRepositoryInfo:
    repo_full_name: str
    default_branch: str
    html_url: str


@dataclass(frozen=True)
class PullRequestSummary:
    number: int
    url: str
    html_url: str
    title: str


@dataclass(frozen=True)
class IssueCommentSummary:
    comment_id: int
    url: str
    html_url: str


@dataclass(frozen=True)
class DeliveryReceipt:
    delivery_id: str
    linked_run_id: str
    linked_execution_id: str
    linked_verification_id: str
    linked_approval_id: str | None
    status: DeliveryStatus
    repo_root: Path
    repo_full_name: str
    branch_name: str
    base_branch: str
    commit_sha: str | None
    commit_message: str
    pr: PullRequestSummary | None
    pr_comment: IssueCommentSummary | None
    rollout_stage: str | None = None
    rollback_base_sha: str | None = None
    branch_protection_required: bool = False
    branch_protection_verified: bool = False
    governance_reasons: list[str] = field(default_factory=list)
    governance_blocked_reasons: list[str] = field(default_factory=list)
    governance_policy_snapshot: dict[str, object] = field(default_factory=dict)
    artifacts: list[ArtifactReference] = field(default_factory=list)
    summary: str = ""
    error_message: str | None = None


@dataclass(frozen=True)
class DeliveryRecord:
    delivery_id: str
    created_at: str
    linked_run_id: str
    linked_execution_id: str
    linked_verification_id: str
    status: DeliveryStatus
    repo_full_name: str
    branch_name: str
    base_branch: str
    summary: str
    receipt_path: Path
    error_message: str | None = None


@dataclass(frozen=True)
class ApprovalReviewerDecision:
    actor: str
    team: str
    decision: ApprovalDecision
    comment: str
    decided_at: str


@dataclass(frozen=True)
class ApprovalEvaluation:
    action: ApprovalAction
    risk_level: ApprovalRiskLevel
    approval_required: bool
    required_approvals: int
    required_reviewer_teams: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    summary: str = ""
    policy_snapshot: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryGovernanceEvaluation:
    rollout_stage: str | None
    branch_protection_required: bool
    branch_protection_verified: bool
    reasons: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    summary: str = ""
    policy_snapshot: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SchemaMigrationRecord:
    version: int
    name: str
    applied_at: str


@dataclass(frozen=True)
class ApprovalReceipt:
    approval_id: str
    action: ApprovalAction
    linked_run_id: str
    linked_execution_id: str
    linked_verification_id: str
    repo_full_name: str
    status: ApprovalStatus
    risk_level: ApprovalRiskLevel
    requested_by: str
    requester_team: str
    request_comment: str
    required_approvals: int
    approved_count: int
    expires_at: str | None = None
    required_reviewer_teams: list[str] = field(default_factory=list)
    assigned_reviewers: list[str] = field(default_factory=list)
    assigned_reviewer_teams: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    decisions: list[ApprovalReviewerDecision] = field(default_factory=list)
    summary: str = ""
    error_message: str | None = None


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    created_at: str
    updated_at: str
    action: ApprovalAction
    linked_run_id: str
    linked_execution_id: str
    linked_verification_id: str
    repo_full_name: str
    status: ApprovalStatus
    risk_level: ApprovalRiskLevel
    requested_by: str
    requester_team: str
    required_approvals: int
    approved_count: int
    summary: str
    receipt_path: Path
    expires_at: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class TenantRecord:
    tenant_id: str
    created_at: str
    updated_at: str
    name: str
    status: TenantStatus
    summary: str
    config_path: Path


@dataclass(frozen=True)
class TenantMembershipRecord:
    tenant_id: str
    actor: str
    role: TenantRole
    team: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    subject: str
    actor: str
    subject_type: AuthSubjectType
    issuer: str | None = None
    team: str | None = None
    groups: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    tenant_ids: list[str] = field(default_factory=list)
    issued_at: str | None = None
    expires_at: str | None = None


@dataclass(frozen=True)
class IdentitySyncMembership:
    actor: str
    role: TenantRole
    team: str


@dataclass(frozen=True)
class IdentitySyncReceipt:
    tenant_id: str
    synced_at: str
    synced_by: str
    replace_existing: bool
    created_count: int
    updated_count: int
    removed_count: int
    membership_count: int


@dataclass(frozen=True)
class NotificationRecord:
    notification_id: str
    created_at: str
    tenant_id: str
    event_type: NotificationEventType
    status: NotificationStatus
    summary: str
    payload_path: Path


@dataclass(frozen=True)
class DashboardRecordRef:
    record_type: str
    record_id: str
    created_at: str
    status: str
    summary: str


@dataclass(frozen=True)
class DashboardSummary:
    tenant_id: str
    tenant_name: str
    generated_at: str
    run_counts: dict[str, int] = field(default_factory=dict)
    approval_counts: dict[str, int] = field(default_factory=dict)
    delivery_counts: dict[str, int] = field(default_factory=dict)
    notification_counts: dict[str, int] = field(default_factory=dict)
    pending_approvals: list[DashboardRecordRef] = field(default_factory=list)
    recent_deliveries: list[DashboardRecordRef] = field(default_factory=list)
    recent_notifications: list[DashboardRecordRef] = field(default_factory=list)


@dataclass(frozen=True)
class AlertRecord:
    alert_id: str
    created_at: str
    tenant_id: str | None
    severity: AlertSeverity
    source: str
    status: AlertStatus
    summary: str
    payload_path: Path


@dataclass(frozen=True)
class TraceEventRecord:
    event_id: str
    trace_id: str
    recorded_at: str
    source: str
    span_name: str
    status: str
    payload_path: Path
    linked_run_id: str | None = None
    linked_job_id: str | None = None


@dataclass(frozen=True)
class QueueJobRecord:
    job_id: str
    created_at: str
    updated_at: str
    job_type: QueueJobType
    status: QueueJobStatus
    repo_full_name: str
    issue_number: int | None
    priority: int
    requested_by: str | None
    tenant_id: str | None
    worker_id: str | None
    attempt_count: int
    max_attempts: int
    budget_units: int
    budget_used: int
    next_run_at: str
    summary: str
    receipt_path: Path
    linked_run_id: str | None = None
    linked_execution_id: str | None = None
    linked_verification_id: str | None = None
    concurrency_key: str | None = None
    required_worker_tags: list[str] = field(default_factory=list)
    lease_token: str | None = None
    lease_expires_at: str | None = None
    rehydration_count: int = 0
    cancel_requested: bool = False
    error_message: str | None = None


@dataclass(frozen=True)
class QueueAttemptRecord:
    attempt_id: str
    job_id: str
    attempt_index: int
    created_at: str
    finished_at: str | None
    worker_id: str
    status: QueueAttemptStatus
    summary: str
    payload_path: Path
    error_message: str | None = None


@dataclass(frozen=True)
class WorkerHeartbeatRecord:
    worker_id: str
    recorded_at: str
    status: WorkerStatus
    current_job_id: str | None
    summary: str
    processed_jobs: int
    succeeded_jobs: int
    failed_jobs: int
    cancelled_jobs: int
    payload_path: Path
    advertised_worker_tags: list[str] = field(default_factory=list)
    active_lease_token: str | None = None
    queue_capacity: int = 0


@dataclass(frozen=True)
class QueueMetricsSnapshot:
    generated_at: str
    queue_counts: dict[str, int] = field(default_factory=dict)
    type_counts: dict[str, int] = field(default_factory=dict)
    tenant_counts: dict[str, int] = field(default_factory=dict)
    budget_reserved: int = 0
    budget_used: int = 0
    active_workers: int = 0
    worker_status_counts: dict[str, int] = field(default_factory=dict)
    leased_jobs: int = 0
    stale_leases: int = 0
    running_by_tenant: dict[str, int] = field(default_factory=dict)


def _as_string(value: object, default: str) -> str:
    return value if isinstance(value, str) and value.strip() else default


def _as_optional_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _as_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]
