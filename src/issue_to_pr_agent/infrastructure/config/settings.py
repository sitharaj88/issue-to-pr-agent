from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlparse

from ...domain.entities import ExecutionRuntime
from ...shared.exceptions import ConfigurationError


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _resolve_optional_path(value: str | None, *, base_dir: Path) -> Path | None:
    normalized = _normalize_optional(value)
    if normalized is None:
        return None
    return _resolve_path(normalized, base_dir=base_dir)


def _resolve_path(value: str, *, base_dir: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def _parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer.") from exc


def _parse_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number.") from exc


@dataclass(frozen=True)
class Settings:
    environment: str
    api_host: str
    api_port: int
    api_token: str | None
    auth_token_secret: str | None
    auth_token_issuer: str | None
    api_rate_limit_per_minute: int
    github_token: str | None
    github_api_base_url: str
    jira_base_url: str | None
    jira_token: str | None
    jira_project_mappings_path: Path | None
    jira_webhook_secret: str | None
    slack_webhook_url: str | None
    slack_signing_secret: str | None
    teams_webhook_url: str | None
    approval_policy_path: Path | None
    delivery_governance_policy_path: Path | None
    approval_ttl_hours: int
    openai_api_key: str | None
    openai_model: str
    openai_complex_model: str
    openai_base_url: str
    artifact_dir: Path
    artifact_base_url: str | None
    notification_dir: Path
    metrics_dir: Path
    telemetry_dir: Path
    audit_export_dir: Path
    sandbox_dir: Path
    database_path: Path
    database_url: str | None
    database_backend: str
    artifact_store_backend: str
    artifact_store_dir: Path
    artifact_store_base_url: str | None
    telemetry_sink_url: str | None
    log_level: str
    max_repo_files: int
    sandbox_max_file_bytes: int
    verification_runtime: ExecutionRuntime
    docker_binary: str
    docker_image: str
    docker_network: str
    docker_memory_mb: int
    docker_cpus: float
    webhook_secret: str | None
    webhook_actor: str
    webhook_team: str
    webhook_repo_roots_path: Path | None
    branch_prefix: str
    git_remote_name: str
    queue_backoff_seconds: int
    queue_max_attempts: int
    queue_lease_seconds: int
    queue_max_running_jobs_per_worker: int
    queue_max_running_jobs_per_tenant: int
    queue_candidate_scan_limit: int
    alert_stale_lease_threshold: int
    alert_failed_jobs_threshold: int
    alert_dedupe_seconds: int
    retention_notification_days: int
    retention_worker_heartbeat_days: int
    retention_alert_days: int
    retention_trace_days: int
    budget_max_units_per_job: int
    budget_max_pending_jobs: int
    budget_max_pending_jobs_per_tenant: int
    budget_cost_plan_heuristic: int
    budget_cost_plan_openai: int
    budget_cost_verify: int
    budget_cost_deliver: int
    router_planner_complexity_threshold: int
    router_patch_complexity_threshold: int
    user_agent: str = "issue-to-pr-agent/0.1.0"

    @classmethod
    def from_env(cls, *, cwd: Path | None = None) -> "Settings":
        base_dir = (cwd or Path.cwd()).resolve()
        artifact_dir_raw = os.getenv("ISSUE_TO_PR_ARTIFACT_DIR", ".issue-to-pr")
        artifact_dir = Path(artifact_dir_raw)
        if not artifact_dir.is_absolute():
            artifact_dir = (base_dir / artifact_dir).resolve()

        database_path_raw = os.getenv("ISSUE_TO_PR_DB_PATH")
        if database_path_raw:
            database_path = Path(database_path_raw)
            if not database_path.is_absolute():
                database_path = (base_dir / database_path).resolve()
        else:
            database_path = artifact_dir / "agent_runs.sqlite3"

        settings = cls(
            environment=os.getenv("APP_ENV", "local").strip().lower(),
            api_host=os.getenv("ISSUE_TO_PR_API_HOST", "127.0.0.1").strip(),
            api_port=_parse_int_env("ISSUE_TO_PR_API_PORT", 8080),
            api_token=_normalize_optional(os.getenv("ISSUE_TO_PR_API_TOKEN")),
            auth_token_secret=_normalize_optional(os.getenv("ISSUE_TO_PR_AUTH_TOKEN_SECRET")),
            auth_token_issuer=_normalize_optional(os.getenv("ISSUE_TO_PR_AUTH_TOKEN_ISSUER")),
            api_rate_limit_per_minute=_parse_int_env("ISSUE_TO_PR_API_RATE_LIMIT_PER_MINUTE", 120),
            github_token=_normalize_optional(os.getenv("GITHUB_TOKEN")),
            github_api_base_url=os.getenv("GITHUB_API_BASE_URL", "https://api.github.com").strip(),
            jira_base_url=_normalize_optional(os.getenv("ISSUE_TO_PR_JIRA_BASE_URL")),
            jira_token=_normalize_optional(os.getenv("ISSUE_TO_PR_JIRA_TOKEN")),
            jira_project_mappings_path=_resolve_optional_path(
                os.getenv("ISSUE_TO_PR_JIRA_PROJECT_MAPPINGS_PATH"),
                base_dir=base_dir,
            ),
            jira_webhook_secret=_normalize_optional(os.getenv("ISSUE_TO_PR_JIRA_WEBHOOK_SECRET")),
            slack_webhook_url=_normalize_optional(os.getenv("ISSUE_TO_PR_SLACK_WEBHOOK_URL")),
            slack_signing_secret=_normalize_optional(os.getenv("ISSUE_TO_PR_SLACK_SIGNING_SECRET")),
            teams_webhook_url=_normalize_optional(os.getenv("ISSUE_TO_PR_TEAMS_WEBHOOK_URL")),
            approval_policy_path=_resolve_optional_path(
                os.getenv("ISSUE_TO_PR_APPROVAL_POLICY_PATH"),
                base_dir=base_dir,
            ),
            delivery_governance_policy_path=_resolve_optional_path(
                os.getenv("ISSUE_TO_PR_DELIVERY_GOVERNANCE_POLICY_PATH"),
                base_dir=base_dir,
            ),
            approval_ttl_hours=_parse_int_env("ISSUE_TO_PR_APPROVAL_TTL_HOURS", 24),
            openai_api_key=_normalize_optional(os.getenv("OPENAI_API_KEY")),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip(),
            openai_complex_model=os.getenv("ISSUE_TO_PR_OPENAI_COMPLEX_MODEL", os.getenv("OPENAI_MODEL", "gpt-4.1-mini")).strip(),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip(),
            artifact_dir=artifact_dir,
            artifact_base_url=_normalize_optional(os.getenv("ISSUE_TO_PR_ARTIFACT_BASE_URL")),
            notification_dir=_resolve_path(
                os.getenv("ISSUE_TO_PR_NOTIFICATION_DIR", str(artifact_dir / "notifications")),
                base_dir=base_dir,
            ),
            metrics_dir=_resolve_path(
                os.getenv("ISSUE_TO_PR_METRICS_DIR", str(artifact_dir / "metrics")),
                base_dir=base_dir,
            ),
            telemetry_dir=_resolve_path(
                os.getenv("ISSUE_TO_PR_TELEMETRY_DIR", str(artifact_dir / "telemetry")),
                base_dir=base_dir,
            ),
            audit_export_dir=_resolve_path(
                os.getenv("ISSUE_TO_PR_AUDIT_EXPORT_DIR", str(artifact_dir / "audit-exports")),
                base_dir=base_dir,
            ),
            sandbox_dir=_resolve_path(
                os.getenv("ISSUE_TO_PR_SANDBOX_DIR", str(artifact_dir / "sandboxes")),
                base_dir=base_dir,
            ),
            database_path=database_path,
            database_url=_normalize_optional(os.getenv("ISSUE_TO_PR_DATABASE_URL")),
            database_backend=os.getenv("ISSUE_TO_PR_DATABASE_BACKEND", "sqlite").strip().lower(),
            artifact_store_backend=os.getenv("ISSUE_TO_PR_ARTIFACT_STORE_BACKEND", "filesystem").strip().lower(),
            artifact_store_dir=_resolve_path(
                os.getenv("ISSUE_TO_PR_ARTIFACT_STORE_DIR", str(artifact_dir / "artifact-store")),
                base_dir=base_dir,
            ),
            artifact_store_base_url=_normalize_optional(os.getenv("ISSUE_TO_PR_ARTIFACT_STORE_BASE_URL")),
            telemetry_sink_url=_normalize_optional(os.getenv("ISSUE_TO_PR_TELEMETRY_SINK_URL")),
            log_level=os.getenv("ISSUE_TO_PR_LOG_LEVEL", "INFO").strip().upper(),
            max_repo_files=_parse_int_env("ISSUE_TO_PR_MAX_REPO_FILES", 200),
            sandbox_max_file_bytes=_parse_int_env("ISSUE_TO_PR_SANDBOX_MAX_FILE_BYTES", 10 * 1024 * 1024),
            verification_runtime=ExecutionRuntime(
                os.getenv("ISSUE_TO_PR_VERIFICATION_RUNTIME", ExecutionRuntime.LOCAL.value).strip().lower()
            ),
            docker_binary=os.getenv("ISSUE_TO_PR_DOCKER_BINARY", "docker").strip(),
            docker_image=os.getenv("ISSUE_TO_PR_DOCKER_IMAGE", "python:3.11-slim").strip(),
            docker_network=os.getenv("ISSUE_TO_PR_DOCKER_NETWORK", "none").strip(),
            docker_memory_mb=_parse_int_env("ISSUE_TO_PR_DOCKER_MEMORY_MB", 1024),
            docker_cpus=_parse_float_env("ISSUE_TO_PR_DOCKER_CPUS", 1.0),
            webhook_secret=_normalize_optional(os.getenv("ISSUE_TO_PR_WEBHOOK_SECRET")),
            webhook_actor=os.getenv("ISSUE_TO_PR_WEBHOOK_ACTOR", "webhook-bot").strip(),
            webhook_team=os.getenv("ISSUE_TO_PR_WEBHOOK_TEAM", "automation").strip(),
            webhook_repo_roots_path=_resolve_optional_path(
                os.getenv("ISSUE_TO_PR_WEBHOOK_REPO_ROOTS_PATH"),
                base_dir=base_dir,
            ),
            branch_prefix=os.getenv("ISSUE_TO_PR_BRANCH_PREFIX", "agent/").strip(),
            git_remote_name=os.getenv("ISSUE_TO_PR_GIT_REMOTE", "origin").strip(),
            queue_backoff_seconds=_parse_int_env("ISSUE_TO_PR_QUEUE_BACKOFF_SECONDS", 30),
            queue_max_attempts=_parse_int_env("ISSUE_TO_PR_QUEUE_MAX_ATTEMPTS", 3),
            queue_lease_seconds=_parse_int_env("ISSUE_TO_PR_QUEUE_LEASE_SECONDS", 900),
            queue_max_running_jobs_per_worker=_parse_int_env(
                "ISSUE_TO_PR_QUEUE_MAX_RUNNING_JOBS_PER_WORKER",
                4,
            ),
            queue_max_running_jobs_per_tenant=_parse_int_env(
                "ISSUE_TO_PR_QUEUE_MAX_RUNNING_JOBS_PER_TENANT",
                2,
            ),
            queue_candidate_scan_limit=_parse_int_env("ISSUE_TO_PR_QUEUE_CANDIDATE_SCAN_LIMIT", 200),
            alert_stale_lease_threshold=_parse_int_env("ISSUE_TO_PR_ALERT_STALE_LEASE_THRESHOLD", 1),
            alert_failed_jobs_threshold=_parse_int_env("ISSUE_TO_PR_ALERT_FAILED_JOBS_THRESHOLD", 5),
            alert_dedupe_seconds=_parse_int_env("ISSUE_TO_PR_ALERT_DEDUPE_SECONDS", 3600),
            retention_notification_days=_parse_int_env("ISSUE_TO_PR_RETENTION_NOTIFICATION_DAYS", 30),
            retention_worker_heartbeat_days=_parse_int_env("ISSUE_TO_PR_RETENTION_WORKER_HEARTBEAT_DAYS", 7),
            retention_alert_days=_parse_int_env("ISSUE_TO_PR_RETENTION_ALERT_DAYS", 30),
            retention_trace_days=_parse_int_env("ISSUE_TO_PR_RETENTION_TRACE_DAYS", 14),
            budget_max_units_per_job=_parse_int_env("ISSUE_TO_PR_BUDGET_MAX_UNITS_PER_JOB", 20),
            budget_max_pending_jobs=_parse_int_env("ISSUE_TO_PR_BUDGET_MAX_PENDING_JOBS", 100),
            budget_max_pending_jobs_per_tenant=_parse_int_env(
                "ISSUE_TO_PR_BUDGET_MAX_PENDING_JOBS_PER_TENANT",
                25,
            ),
            budget_cost_plan_heuristic=_parse_int_env("ISSUE_TO_PR_BUDGET_COST_PLAN_HEURISTIC", 1),
            budget_cost_plan_openai=_parse_int_env("ISSUE_TO_PR_BUDGET_COST_PLAN_OPENAI", 5),
            budget_cost_verify=_parse_int_env("ISSUE_TO_PR_BUDGET_COST_VERIFY", 2),
            budget_cost_deliver=_parse_int_env("ISSUE_TO_PR_BUDGET_COST_DELIVER", 3),
            router_planner_complexity_threshold=_parse_int_env(
                "ISSUE_TO_PR_ROUTER_PLANNER_COMPLEXITY_THRESHOLD",
                14,
            ),
            router_patch_complexity_threshold=_parse_int_env(
                "ISSUE_TO_PR_ROUTER_PATCH_COMPLEXITY_THRESHOLD",
                18,
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.environment not in {"local", "staging", "production"}:
            raise ConfigurationError("APP_ENV must be one of: local, staging, production.")
        if not self.api_host:
            raise ConfigurationError("ISSUE_TO_PR_API_HOST must not be empty.")
        if self.api_port <= 0 or self.api_port > 65535:
            raise ConfigurationError("ISSUE_TO_PR_API_PORT must be between 1 and 65535.")
        if self.auth_token_secret is not None and len(self.auth_token_secret) < 16:
            raise ConfigurationError("ISSUE_TO_PR_AUTH_TOKEN_SECRET must be at least 16 characters.")
        if self.api_rate_limit_per_minute < 0:
            raise ConfigurationError("ISSUE_TO_PR_API_RATE_LIMIT_PER_MINUTE must be zero or greater.")
        if self.approval_ttl_hours <= 0:
            raise ConfigurationError("ISSUE_TO_PR_APPROVAL_TTL_HOURS must be greater than zero.")
        if not self.openai_model:
            raise ConfigurationError("OPENAI_MODEL must not be empty.")
        if not self.openai_complex_model:
            raise ConfigurationError("ISSUE_TO_PR_OPENAI_COMPLEX_MODEL must not be empty.")
        if self.max_repo_files <= 0:
            raise ConfigurationError("ISSUE_TO_PR_MAX_REPO_FILES must be greater than zero.")
        if self.sandbox_max_file_bytes <= 0:
            raise ConfigurationError("ISSUE_TO_PR_SANDBOX_MAX_FILE_BYTES must be greater than zero.")
        if not self.docker_binary:
            raise ConfigurationError("ISSUE_TO_PR_DOCKER_BINARY must not be empty.")
        if not self.docker_image:
            raise ConfigurationError("ISSUE_TO_PR_DOCKER_IMAGE must not be empty.")
        if not self.docker_network:
            raise ConfigurationError("ISSUE_TO_PR_DOCKER_NETWORK must not be empty.")
        if self.docker_memory_mb <= 0:
            raise ConfigurationError("ISSUE_TO_PR_DOCKER_MEMORY_MB must be greater than zero.")
        if self.docker_cpus <= 0:
            raise ConfigurationError("ISSUE_TO_PR_DOCKER_CPUS must be greater than zero.")
        if self.queue_backoff_seconds < 0:
            raise ConfigurationError("ISSUE_TO_PR_QUEUE_BACKOFF_SECONDS must be zero or greater.")
        if self.queue_max_attempts <= 0:
            raise ConfigurationError("ISSUE_TO_PR_QUEUE_MAX_ATTEMPTS must be greater than zero.")
        if self.queue_lease_seconds <= 0:
            raise ConfigurationError("ISSUE_TO_PR_QUEUE_LEASE_SECONDS must be greater than zero.")
        if self.queue_max_running_jobs_per_worker <= 0:
            raise ConfigurationError(
                "ISSUE_TO_PR_QUEUE_MAX_RUNNING_JOBS_PER_WORKER must be greater than zero."
            )
        if self.queue_max_running_jobs_per_tenant <= 0:
            raise ConfigurationError(
                "ISSUE_TO_PR_QUEUE_MAX_RUNNING_JOBS_PER_TENANT must be greater than zero."
            )
        if self.queue_candidate_scan_limit <= 0:
            raise ConfigurationError("ISSUE_TO_PR_QUEUE_CANDIDATE_SCAN_LIMIT must be greater than zero.")
        if self.alert_stale_lease_threshold <= 0:
            raise ConfigurationError("ISSUE_TO_PR_ALERT_STALE_LEASE_THRESHOLD must be greater than zero.")
        if self.alert_failed_jobs_threshold <= 0:
            raise ConfigurationError("ISSUE_TO_PR_ALERT_FAILED_JOBS_THRESHOLD must be greater than zero.")
        if self.alert_dedupe_seconds <= 0:
            raise ConfigurationError("ISSUE_TO_PR_ALERT_DEDUPE_SECONDS must be greater than zero.")
        if self.retention_notification_days <= 0:
            raise ConfigurationError("ISSUE_TO_PR_RETENTION_NOTIFICATION_DAYS must be greater than zero.")
        if self.retention_worker_heartbeat_days <= 0:
            raise ConfigurationError(
                "ISSUE_TO_PR_RETENTION_WORKER_HEARTBEAT_DAYS must be greater than zero."
            )
        if self.retention_alert_days <= 0:
            raise ConfigurationError("ISSUE_TO_PR_RETENTION_ALERT_DAYS must be greater than zero.")
        if self.retention_trace_days <= 0:
            raise ConfigurationError("ISSUE_TO_PR_RETENTION_TRACE_DAYS must be greater than zero.")
        if self.budget_max_units_per_job <= 0:
            raise ConfigurationError("ISSUE_TO_PR_BUDGET_MAX_UNITS_PER_JOB must be greater than zero.")
        if self.budget_max_pending_jobs <= 0:
            raise ConfigurationError("ISSUE_TO_PR_BUDGET_MAX_PENDING_JOBS must be greater than zero.")
        if self.budget_max_pending_jobs_per_tenant <= 0:
            raise ConfigurationError(
                "ISSUE_TO_PR_BUDGET_MAX_PENDING_JOBS_PER_TENANT must be greater than zero."
            )
        for field_name, value in (
            ("ISSUE_TO_PR_BUDGET_COST_PLAN_HEURISTIC", self.budget_cost_plan_heuristic),
            ("ISSUE_TO_PR_BUDGET_COST_PLAN_OPENAI", self.budget_cost_plan_openai),
            ("ISSUE_TO_PR_BUDGET_COST_VERIFY", self.budget_cost_verify),
            ("ISSUE_TO_PR_BUDGET_COST_DELIVER", self.budget_cost_deliver),
        ):
            if value <= 0:
                raise ConfigurationError(f"{field_name} must be greater than zero.")
        if self.router_planner_complexity_threshold <= 0:
            raise ConfigurationError(
                "ISSUE_TO_PR_ROUTER_PLANNER_COMPLEXITY_THRESHOLD must be greater than zero."
            )
        if self.router_patch_complexity_threshold <= 0:
            raise ConfigurationError(
                "ISSUE_TO_PR_ROUTER_PATCH_COMPLEXITY_THRESHOLD must be greater than zero."
            )
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("ISSUE_TO_PR_LOG_LEVEL must be a valid logging level.")
        if not self.branch_prefix:
            raise ConfigurationError("ISSUE_TO_PR_BRANCH_PREFIX must not be empty.")
        if not self.git_remote_name:
            raise ConfigurationError("ISSUE_TO_PR_GIT_REMOTE must not be empty.")
        github_parsed = urlparse(self.github_api_base_url)
        if github_parsed.scheme not in {"http", "https"} or not github_parsed.netloc:
            raise ConfigurationError("GITHUB_API_BASE_URL must be an absolute HTTP(S) URL.")
        if self.jira_base_url is not None:
            jira_parsed = urlparse(self.jira_base_url)
            if jira_parsed.scheme not in {"http", "https"} or not jira_parsed.netloc:
                raise ConfigurationError("ISSUE_TO_PR_JIRA_BASE_URL must be an absolute HTTP(S) URL.")
        if self.jira_project_mappings_path is not None and not self.jira_project_mappings_path.exists():
            raise ConfigurationError(
                "ISSUE_TO_PR_JIRA_PROJECT_MAPPINGS_PATH does not exist: "
                f"{self.jira_project_mappings_path}"
            )
        for field_name, value in (
            ("ISSUE_TO_PR_SLACK_WEBHOOK_URL", self.slack_webhook_url),
            ("ISSUE_TO_PR_TEAMS_WEBHOOK_URL", self.teams_webhook_url),
        ):
            if value is None:
                continue
            parsed_url = urlparse(value)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise ConfigurationError(f"{field_name} must be an absolute HTTP(S) URL.")
        if self.approval_policy_path is not None and not self.approval_policy_path.exists():
            raise ConfigurationError(
                f"ISSUE_TO_PR_APPROVAL_POLICY_PATH does not exist: {self.approval_policy_path}"
            )
        if self.delivery_governance_policy_path is not None and not self.delivery_governance_policy_path.exists():
            raise ConfigurationError(
                "ISSUE_TO_PR_DELIVERY_GOVERNANCE_POLICY_PATH does not exist: "
                f"{self.delivery_governance_policy_path}"
            )
        if self.webhook_repo_roots_path is not None and not self.webhook_repo_roots_path.exists():
            raise ConfigurationError(
                "ISSUE_TO_PR_WEBHOOK_REPO_ROOTS_PATH does not exist: "
                f"{self.webhook_repo_roots_path}"
            )
        parsed = urlparse(self.openai_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigurationError("OPENAI_BASE_URL must be an absolute HTTP(S) URL.")
        if self.artifact_base_url is not None:
            artifact_parsed = urlparse(self.artifact_base_url)
            if artifact_parsed.scheme not in {"http", "https"} or not artifact_parsed.netloc:
                raise ConfigurationError("ISSUE_TO_PR_ARTIFACT_BASE_URL must be an absolute HTTP(S) URL.")
        if self.notification_dir == self.database_path:
            raise ConfigurationError("ISSUE_TO_PR_NOTIFICATION_DIR must not point to the database file.")
        if self.metrics_dir == self.database_path:
            raise ConfigurationError("ISSUE_TO_PR_METRICS_DIR must not point to the database file.")
        if self.telemetry_dir == self.database_path:
            raise ConfigurationError("ISSUE_TO_PR_TELEMETRY_DIR must not point to the database file.")
        if self.audit_export_dir == self.database_path:
            raise ConfigurationError("ISSUE_TO_PR_AUDIT_EXPORT_DIR must not point to the database file.")
        if self.sandbox_dir == self.database_path:
            raise ConfigurationError("ISSUE_TO_PR_SANDBOX_DIR must not point to the database file.")
        if self.artifact_store_dir == self.database_path:
            raise ConfigurationError("ISSUE_TO_PR_ARTIFACT_STORE_DIR must not point to the database file.")
        if self.database_backend not in {"sqlite", "postgres"}:
            raise ConfigurationError("ISSUE_TO_PR_DATABASE_BACKEND must be either sqlite or postgres.")
        if self.database_backend == "postgres" and self.database_url is None:
            raise ConfigurationError("ISSUE_TO_PR_DATABASE_URL is required when using the postgres backend.")
        if self.database_url is not None:
            parsed_db = urlparse(self.database_url)
            if parsed_db.scheme not in {"sqlite", "postgres", "postgresql"}:
                raise ConfigurationError(
                    "ISSUE_TO_PR_DATABASE_URL must use sqlite://, postgres://, or postgresql://."
                )
        if self.artifact_store_backend not in {"filesystem", "shared"}:
            raise ConfigurationError(
                "ISSUE_TO_PR_ARTIFACT_STORE_BACKEND must be either filesystem or shared."
            )
        if self.artifact_store_base_url is not None:
            store_url = urlparse(self.artifact_store_base_url)
            if store_url.scheme not in {"http", "https"} or not store_url.netloc:
                raise ConfigurationError(
                    "ISSUE_TO_PR_ARTIFACT_STORE_BASE_URL must be an absolute HTTP(S) URL."
                )
        if self.telemetry_sink_url is not None:
            telemetry_url = urlparse(self.telemetry_sink_url)
            if telemetry_url.scheme not in {"http", "https"} or not telemetry_url.netloc:
                raise ConfigurationError("ISSUE_TO_PR_TELEMETRY_SINK_URL must be an absolute HTTP(S) URL.")
        if not self.webhook_actor:
            raise ConfigurationError("ISSUE_TO_PR_WEBHOOK_ACTOR must not be empty.")
        if not self.webhook_team:
            raise ConfigurationError("ISSUE_TO_PR_WEBHOOK_TEAM must not be empty.")

    def require_openai(self) -> None:
        if not self.openai_api_key:
            raise ConfigurationError("OPENAI_API_KEY is required when using the OpenAI planner.")

    def require_github_token(self) -> None:
        if not self.github_token:
            raise ConfigurationError("GITHUB_TOKEN is required for GitHub delivery operations.")
