from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
import hashlib
import hmac
import json
from json import JSONDecodeError
from pathlib import Path
import time
from typing import Callable
from urllib.parse import parse_qs
from uuid import uuid4

from ...agents.patcher.base import PatcherClient
from ...agents.planner.base import PlannerClient
from ...agents.planner.heuristic import HeuristicPlanner
from ...application.services.approval_policy import ApprovalPolicyEvaluator
from ...application.services.authentication import authenticate_bearer_token
from ...application.services.audit_export import RunAuditExporter
from ...application.services.delivery_governance import DeliveryGovernancePolicyEvaluator
from ...application.services.queue_budget import QueueBudgetManager
from ...application.services.retention import RetentionEnforcer
from ...application.services.tenant_access import TenantAccessController
from ...application.use_cases.dashboard import DashboardUseCase
from ...application.use_cases.deliver_run import DeliverRunUseCase
from ...application.use_cases.execute_patch_proposal import (
    ExecutePatchProposalUseCase,
    PatchExecutionFailedError,
)
from ...application.use_cases.generate_patch_proposal import GeneratePatchProposalUseCase
from ...application.use_cases.manage_approval import RequestApprovalUseCase, ReviewApprovalUseCase
from ...application.use_cases.manage_queue import ManageQueueUseCase
from ...application.use_cases.manage_sandbox import ManageSandboxUseCase
from ...application.use_cases.plan_issue_to_pr import IssueToPRAgent
from ...application.use_cases.run_autofix import RunAutofixUseCase
from ...application.use_cases.run_sandboxed_autofix import RunSandboxedAutofixUseCase
from ...application.use_cases.run_sandboxed_patch_execution import (
    RunSandboxedPatchExecutionUseCase,
    SandboxedPatchExecutionFailedError,
)
from ...application.use_cases.sync_identity import SyncIdentityUseCase
from ...application.use_cases.verify_run import VerifyRunUseCase
from ...domain.entities import (
    AlertRecord,
    ApprovalDecision,
    ApprovalRecord,
    ApprovalStatus,
    AuthenticatedPrincipal,
    AutofixAttemptRecord,
    AutofixRunRecord,
    DashboardSummary,
    DeliveryRecord,
    ExecutionRuntime,
    IdentitySyncMembership,
    NotificationEventType,
    NotificationRecord,
    PatchExecutionMode,
    PatchExecutionRecord,
    PatchProposal,
    PatchProposalRecord,
    PlatformPermission,
    QueueAttemptRecord,
    QueueJobRecord,
    QueueJobStatus,
    QueueJobType,
    RunRecord,
    SandboxRecord,
    TenantRole,
    TraceEventRecord,
    VerificationRecord,
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
from ...integrations.jira.client import JiraClient
from ...integrations.openai.patcher import OpenAIPatcher
from ...integrations.openai.planner import OpenAIPlanner
from ...integrations.slack.client import SlackWebhookClient
from ...integrations.telemetry import TelemetrySinkClient
from ...observability.alerts import AlertManager
from ...observability.tracing import TraceRecorder
from .ui import render_operator_console, render_script, render_stylesheet
from ...shared.exceptions import ConfigurationError, PolicyError


PlannerFactory = Callable[[Settings, str], PlannerClient]
PatcherFactory = Callable[[Settings, str], PatcherClient]


@dataclass(frozen=True)
class JsonResponse:
    status_code: int
    body: object
    headers: dict[str, str] = field(default_factory=dict)


class ApiError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class RateLimitResult:
    limit: int
    remaining: int
    reset_epoch_seconds: int


class InMemoryRateLimiter:
    def __init__(self, limit_per_minute: int) -> None:
        self._limit_per_minute = limit_per_minute
        self._requests: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> RateLimitResult | None:
        if self._limit_per_minute <= 0:
            return None
        now = time.time()
        window_start = now - 60
        bucket = self._requests[key]
        while bucket and bucket[0] <= window_start:
            bucket.popleft()
        if len(bucket) >= self._limit_per_minute:
            reset_epoch_seconds = int(bucket[0] + 60)
            raise ApiError(429, "API rate limit exceeded.")
        bucket.append(now)
        return RateLimitResult(
            limit=self._limit_per_minute,
            remaining=max(0, self._limit_per_minute - len(bucket)),
            reset_epoch_seconds=int(now + 60),
        )


class FileIdempotencyStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def replay(
        self,
        *,
        method: str,
        path: str,
        key: str,
        request_hash: str,
    ) -> JsonResponse | None:
        payload = self._load(method=method, path=path, key=key)
        if payload is None:
            return None
        if payload.get("request_hash") != request_hash:
            raise ApiError(409, "Idempotency key has already been used with a different request payload.")
        body = payload.get("body")
        if not isinstance(body, (dict, list, str, int, float, bool)) and body is not None:
            raise ApiError(500, "Stored idempotency payload is invalid.")
        headers = payload.get("headers")
        if not isinstance(headers, dict):
            headers = {}
        return JsonResponse(
            status_code=int(payload.get("status_code", 200)),
            body=body,
            headers={str(key): str(value) for key, value in headers.items()},
        )

    def save(
        self,
        *,
        method: str,
        path: str,
        key: str,
        request_hash: str,
        response: JsonResponse,
    ) -> None:
        payload = {
            "method": method,
            "path": path,
            "key": key,
            "request_hash": request_hash,
            "status_code": response.status_code,
            "body": response.body,
            "headers": response.headers,
        }
        target = self._path(method=method, path=path, key=key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _load(self, *, method: str, path: str, key: str) -> dict[str, object] | None:
        target = self._path(method=method, path=path, key=key)
        if not target.exists():
            return None
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ApiError(500, "Stored idempotency payload is not a JSON object.")
        return payload

    def _path(self, *, method: str, path: str, key: str) -> Path:
        digest = hashlib.sha256(f"{method}\0{path}\0{key}".encode("utf-8")).hexdigest()
        return self._root / f"{digest}.json"


class ControlPlaneApi:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: RunRepository,
        access_controller: TenantAccessController,
        budget_manager: QueueBudgetManager,
        github_client: GitHubClient | None = None,
        planner_factory: PlannerFactory | None = None,
        patcher_factory: PatcherFactory | None = None,
        rate_limiter: InMemoryRateLimiter | None = None,
        idempotency_store: FileIdempotencyStore | None = None,
        notification_outbox: FileNotificationOutbox | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._access_controller = access_controller
        self._budget_manager = budget_manager
        self._github_client = github_client or GitHubClient(settings)
        self._jira_client = JiraClient(settings)
        self._slack_client = SlackWebhookClient(settings)
        self._planner_factory = planner_factory or _default_planner_factory
        self._patcher_factory = patcher_factory or _default_patcher_factory
        self._rate_limiter = rate_limiter or InMemoryRateLimiter(settings.api_rate_limit_per_minute)
        self._idempotency_store = idempotency_store or FileIdempotencyStore(
            settings.artifact_dir / "api" / "idempotency"
        )
        self._notification_outbox = notification_outbox or FileNotificationOutbox(repository, settings=settings)
        self._telemetry_client = TelemetrySinkClient(settings)
        self._trace_recorder = TraceRecorder(repository, sink_client=self._telemetry_client)
        self._alert_manager = AlertManager(repository, settings, sink_client=self._telemetry_client)

    def handle_request(
        self,
        *,
        method: str,
        path: str,
        query_string: str = "",
        headers: dict[str, str] | None = None,
        body: bytes | str | None = None,
    ) -> JsonResponse:
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        body_bytes = _body_bytes(body)
        query = parse_qs(query_string, keep_blank_values=False)
        request_id = normalized_headers.get("x-request-id") or uuid4().hex
        rate_limit_headers: dict[str, str] = {}
        self._trace_recorder.record(
            trace_id=request_id,
            source="http_api",
            span_name=f"{method} {path}",
            status="received",
            payload={"method": method, "path": path, "query_string": query_string},
            output_dir=self._settings.telemetry_dir / "traces",
        )
        try:
            principal = self._authenticate(path=path, headers=normalized_headers)
            rate_limit_headers = self._rate_limit(path=path, headers=normalized_headers)
            replay = self._idempotent_replay(
                method=method,
                path=path,
                headers=normalized_headers,
                body=body_bytes,
            )
            if replay is not None:
                finalized = self._finalize_response(
                    replay,
                    request_id=request_id,
                    extra_headers={**rate_limit_headers, "X-Idempotent-Replay": "true"},
                )
                self._trace_recorder.record(
                    trace_id=request_id,
                    source="http_api",
                    span_name=f"{method} {path}",
                    status="replayed",
                    payload={"method": method, "path": path, "status_code": finalized.status_code},
                    output_dir=self._settings.telemetry_dir / "traces",
                )
                return finalized
            response = self._dispatch(
                method=method,
                path=path,
                query=query,
                headers=normalized_headers,
                body=body_bytes,
                principal=principal,
            )
            self._persist_idempotency(
                method=method,
                path=path,
                headers=normalized_headers,
                body=body_bytes,
                response=response,
            )
            finalized = self._finalize_response(
                response,
                request_id=request_id,
                extra_headers=rate_limit_headers,
            )
            self._trace_recorder.record(
                trace_id=request_id,
                source="http_api",
                span_name=f"{method} {path}",
                status="completed",
                payload={"method": method, "path": path, "status_code": finalized.status_code},
                output_dir=self._settings.telemetry_dir / "traces",
            )
            return finalized
        except ApiError as exc:
            response = self._json(exc.status_code, {"error": str(exc)})
        except PolicyError as exc:
            response = self._json(403, {"error": str(exc)})
        except (ValueError, FileNotFoundError, NotADirectoryError, ConfigurationError, JSONDecodeError) as exc:
            response = self._json(400, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive path
            response = self._json(500, {"error": str(exc)})
        finalized = self._finalize_response(
            response,
            request_id=request_id,
            extra_headers=rate_limit_headers,
        )
        self._trace_recorder.record(
            trace_id=request_id,
            source="http_api",
            span_name=f"{method} {path}",
            status="failed" if finalized.status_code >= 400 else "completed",
            payload={"method": method, "path": path, "status_code": finalized.status_code},
            output_dir=self._settings.telemetry_dir / "traces",
        )
        return finalized

    def _dispatch(
        self,
        *,
        method: str,
        path: str,
        query: dict[str, list[str]],
        headers: dict[str, str],
        body: bytes,
        principal: AuthenticatedPrincipal | None,
    ) -> JsonResponse:
        if method == "GET" and path in {"/ui", "/ui/"}:
            return self._html(render_operator_console())
        if method == "GET" and path == "/ui/styles.css":
            return self._text(render_stylesheet(), content_type="text/css; charset=utf-8")
        if method == "GET" and path == "/ui/app.js":
            return self._text(render_script(), content_type="application/javascript; charset=utf-8")
        if method == "GET" and path == "/healthz":
            return self._json(200, {"status": "ok", "environment": self._settings.environment})
        if method == "GET" and path == "/v1/openapi.json":
            return self._json(200, _openapi_schema())
        if method == "GET" and path == "/v1/identity/me":
            return self._identity_me(principal)
        if method == "GET" and path == "/v1/dashboard":
            return self._dashboard(query, principal=principal)
        if method == "GET" and path == "/v1/notifications":
            return self._notifications(query, principal=principal)
        if method == "GET" and path == "/v1/alerts":
            return self._alerts(query, principal=principal)
        if method == "GET" and path == "/v1/traces":
            return self._traces(query)

        if method == "GET" and path == "/v1/runs":
            return self._list_response(query, fetch=lambda limit: self._repository.list_runs(limit=limit), serializer=_run_record_to_dict)
        if method == "GET" and path.startswith("/v1/runs/"):
            return self._get_payload(self._repository.get_run(_tail(path, "/v1/runs/")), "run")

        if method == "GET" and path == "/v1/patch-proposals":
            return self._list_response(
                query,
                fetch=lambda limit: self._repository.list_patch_proposals(limit=limit),
                serializer=_patch_proposal_record_to_dict,
            )
        if method == "GET" and path.startswith("/v1/patch-proposals/"):
            return self._get_payload(
                self._repository.get_patch_proposal(_tail(path, "/v1/patch-proposals/")),
                "patch proposal",
            )

        if method == "GET" and path == "/v1/executions":
            return self._list_response(
                query,
                fetch=lambda limit: self._repository.list_executions(limit=limit),
                serializer=_execution_record_to_dict,
            )
        if method == "GET" and path.startswith("/v1/executions/"):
            return self._get_payload(self._repository.get_execution(_tail(path, "/v1/executions/")), "execution")

        if method == "GET" and path == "/v1/verifications":
            return self._list_response(
                query,
                fetch=lambda limit: self._repository.list_verifications(limit=limit),
                serializer=_verification_record_to_dict,
            )
        if method == "GET" and path.startswith("/v1/verifications/"):
            return self._get_payload(
                self._repository.get_verification(_tail(path, "/v1/verifications/")),
                "verification",
            )

        if method == "GET" and path == "/v1/autofix-runs":
            return self._list_response(
                query,
                fetch=lambda limit: self._repository.list_autofix_runs(limit=limit),
                serializer=_autofix_record_to_dict,
            )
        if method == "GET" and path.startswith("/v1/autofix-runs/"):
            return self._get_payload(
                self._repository.get_autofix_run(_tail(path, "/v1/autofix-runs/")),
                "autofix run",
            )
        if method == "GET" and path.startswith("/v1/autofix-attempts/"):
            autofix_id = _tail(path, "/v1/autofix-attempts/")
            return self._list_response(
                query,
                fetch=lambda limit: self._repository.list_autofix_attempts(autofix_id=autofix_id, limit=limit),
                serializer=_autofix_attempt_record_to_dict,
                default_limit=50,
            )

        if method == "GET" and path == "/v1/sandboxes":
            return self._list_response(
                query,
                fetch=lambda limit: self._repository.list_sandboxes(limit=limit),
                serializer=_sandbox_record_to_dict,
            )
        if method == "GET" and path.startswith("/v1/sandboxes/"):
            return self._get_payload(self._repository.get_sandbox(_tail(path, "/v1/sandboxes/")), "sandbox")

        if method == "GET" and path == "/v1/approvals":
            status = _approval_status_param(query)
            return self._list_response(
                query,
                fetch=lambda limit: self._repository.list_approvals(limit=limit, status=status),
                serializer=_approval_record_to_dict,
            )
        if method == "GET" and path.startswith("/v1/approvals/"):
            return self._get_payload(self._repository.get_approval(_tail(path, "/v1/approvals/")), "approval")

        if method == "GET" and path == "/v1/deliveries":
            return self._list_response(
                query,
                fetch=lambda limit: self._repository.list_deliveries(limit=limit),
                serializer=_delivery_record_to_dict,
            )
        if method == "GET" and path.startswith("/v1/deliveries/"):
            return self._get_payload(self._repository.get_delivery(_tail(path, "/v1/deliveries/")), "delivery")

        if method == "GET" and path == "/v1/queue-jobs":
            status = _queue_status_param(query)
            job_type = _queue_type_param(query)
            return self._list_response(
                query,
                fetch=lambda limit: self._repository.list_queue_jobs(limit=limit, status=status, job_type=job_type),
                serializer=_queue_job_record_to_dict,
            )
        if method == "GET" and path.startswith("/v1/queue-jobs/"):
            return self._get_payload(self._repository.get_queue_job(_tail(path, "/v1/queue-jobs/")), "queue job")
        if method == "GET" and path.startswith("/v1/queue-attempts/"):
            job_id = _tail(path, "/v1/queue-attempts/")
            return self._list_response(
                query,
                fetch=lambda _limit: self._repository.list_queue_attempts(job_id),
                serializer=_queue_attempt_record_to_dict,
                default_limit=50,
            )

        if method == "POST" and path == "/v1/plan":
            return self._plan(_json_object(body))
        if method == "POST" and path == "/v1/patch-proposals/generate":
            return self._generate_patch(_json_object(body))
        if method == "POST" and path == "/v1/patch-executions":
            return self._execute_patch(_json_object(body))
        if method == "POST" and path == "/v1/verify":
            return self._verify(_json_object(body))
        if method == "POST" and path == "/v1/autofix":
            return self._autofix(_json_object(body))
        if method == "POST" and path == "/v1/sandboxes":
            return self._prepare_sandbox(_json_object(body))
        if method == "POST" and path.startswith("/v1/sandboxes/") and path.endswith("/cleanup"):
            return self._cleanup_sandbox(_tail(path, "/v1/sandboxes/").removesuffix("/cleanup"))
        if method == "POST" and path == "/v1/approvals/request":
            return self._request_approval(_json_object(body), principal=principal)
        if method == "POST" and path == "/v1/approvals/review":
            return self._review_approval(_json_object(body), principal=principal)
        if method == "POST" and path == "/v1/deliver":
            return self._deliver(_json_object(body), principal=principal)
        if method == "POST" and path == "/v1/identity/sync":
            return self._sync_identity(_json_object(body), principal=principal)
        if method == "POST" and path == "/v1/audits/exports":
            return self._export_audit(_json_object(body))
        if method == "POST" and path == "/v1/retention/enforce":
            return self._enforce_retention(_json_object(body))
        if method == "POST" and path == "/v1/queue/plan":
            return self._enqueue_plan(_json_object(body), principal=principal)
        if method == "POST" and path == "/v1/queue/verify":
            return self._enqueue_verify(_json_object(body), principal=principal)
        if method == "POST" and path == "/v1/queue/deliver":
            return self._enqueue_deliver(_json_object(body), principal=principal)
        if method == "POST" and path.startswith("/v1/queue-jobs/") and path.endswith("/cancel"):
            return self._cancel_job(
                _tail(path, "/v1/queue-jobs/").removesuffix("/cancel"),
                _json_object(body),
                principal=principal,
            )
        if method == "POST" and path.startswith("/v1/queue-jobs/") and path.endswith("/resume"):
            return self._resume_job(
                _tail(path, "/v1/queue-jobs/").removesuffix("/resume"),
                _json_object(body),
                principal=principal,
            )
        if method == "POST" and path == "/v1/webhooks/jira/issues":
            return self._ingest_jira_issue_webhook(headers=headers, body=body)
        if method == "POST" and path == "/v1/webhooks/slack/approvals":
            return self._ingest_slack_approval_webhook(headers=headers, body=body)
        if method == "POST" and path == "/v1/webhooks/github/issues":
            return self._ingest_github_issue_webhook(headers=headers, body=body)
        raise ApiError(404, f"Route not found: {method} {path}")

    def _plan(self, payload: dict[str, object]) -> JsonResponse:
        provider = _string_value(payload.get("provider")) or "heuristic"
        planner = self._planner_factory(self._settings, provider)
        agent = IssueToPRAgent(
            self._github_client,
            planner,
            self._repository,
            SafetyPolicy(branch_prefix=self._settings.branch_prefix),
            max_repo_files=self._settings.max_repo_files,
        )
        result = agent.run(
            repo_full_name=_required_string(payload, "repo"),
            issue_number=_required_int(payload, "issue"),
            repo_root=Path(_required_string(payload, "repo_root")).resolve(),
            output_dir=Path(_string_value(payload.get("output_dir")) or str(self._settings.artifact_dir)).resolve(),
            objective=_optional_string(payload.get("objective")),
            create_branch=_bool_value(payload.get("create_branch"), default=False),
        )
        return self._json(
            201,
            {
                "run_id": result.run_id,
                "summary": result.plan.summary,
                "report_path": str(result.report_path),
                "pr_draft_path": str(result.pr_draft_path),
                "audit_path": str(result.audit_path),
            },
        )

    def _generate_patch(self, payload: dict[str, object]) -> JsonResponse:
        provider = _string_value(payload.get("provider")) or "openai"
        patcher = self._patcher_factory(self._settings, provider)
        result = GeneratePatchProposalUseCase(self._repository, patcher).generate(
            run_id=_required_string(payload, "run_id"),
            repo_root=self._resolve_repo_root(
                repo_root=_optional_string(payload.get("repo_root")),
                sandbox_id=_optional_string(payload.get("sandbox_id")),
            ),
            objective=_optional_string(payload.get("objective")),
        )
        return self._json(
            201,
            {
                "proposal_id": result.proposal_id,
                "summary": result.proposal.summary,
                "operation_count": len(result.proposal.operations),
                "proposal_path": str(result.proposal_path),
            },
        )

    def _execute_patch(self, payload: dict[str, object]) -> JsonResponse:
        sandbox = _bool_value(payload.get("sandbox"), default=False)
        sandbox_id = _optional_string(payload.get("sandbox_id"))
        if sandbox and sandbox_id:
            raise ValueError("Provide either sandbox=true or sandbox_id, not both.")
        proposal = self._load_proposal(payload)
        executor = ExecutePatchProposalUseCase(
            self._repository,
            guardrails=WorkspaceGuardrails(),
            mutator=LocalWorkspaceMutator(),
        )
        mode = PatchExecutionMode(_string_value(payload.get("mode")) or PatchExecutionMode.DRY_RUN.value)
        artifact_dir = Path(_string_value(payload.get("output_dir")) or str(self._settings.artifact_dir)).resolve()
        if sandbox:
            sandbox_result = RunSandboxedPatchExecutionUseCase(
                ManageSandboxUseCase(
                    self._repository,
                    LocalSandboxManager(max_file_bytes=self._settings.sandbox_max_file_bytes),
                ),
                executor,
            ).run(
                proposal=proposal,
                source_repo_root=Path(_required_string(payload, "repo_root")).resolve(),
                artifact_dir=artifact_dir,
                sandbox_dir=self._settings.sandbox_dir,
                mode=mode,
            )
            response_body = {
                "sandbox_id": sandbox_result.sandbox.sandbox_id,
                "workspace_root": str(sandbox_result.sandbox.receipt.workspace_root),
                "execution_id": sandbox_result.execution.execution_id,
                "status": sandbox_result.execution.receipt.status.value,
                "receipt_path": str(sandbox_result.execution.receipt_path),
            }
            return self._json(201, response_body)
        try:
            result = executor.execute(
                proposal=proposal,
                repo_root=self._resolve_repo_root(
                    repo_root=_optional_string(payload.get("repo_root")),
                    sandbox_id=sandbox_id,
                ),
                artifact_dir=artifact_dir,
                mode=mode,
            )
        except PatchExecutionFailedError as exc:
            return self._json(
                201,
                {
                    "execution_id": exc.result.execution_id,
                    "status": exc.result.receipt.status.value,
                    "receipt_path": str(exc.result.receipt_path),
                    "error_message": exc.result.receipt.error_message,
                },
            )
        return self._json(
            201,
            {
                "execution_id": result.execution_id,
                "status": result.receipt.status.value,
                "receipt_path": str(result.receipt_path),
            },
        )

    def _verify(self, payload: dict[str, object]) -> JsonResponse:
        verification_runtime = ExecutionRuntime(
            _string_value(payload.get("runtime")) or self._settings.verification_runtime.value
        )
        result = VerifyRunUseCase(
            self._repository,
            SafetyPolicy(branch_prefix=self._settings.branch_prefix),
            command_runner=build_command_runner(self._settings, verification_runtime),
        ).verify(
            repo_root=self._resolve_repo_root(
                repo_root=_optional_string(payload.get("repo_root")),
                sandbox_id=_optional_string(payload.get("sandbox_id")),
            ),
            artifact_dir=Path(_string_value(payload.get("output_dir")) or str(self._settings.artifact_dir)).resolve(),
            run_id=_optional_string(payload.get("run_id")),
            execution_id=_optional_string(payload.get("execution_id")),
            max_attempts=_required_int(payload, "max_attempts", default=3),
            timeout_seconds=_required_int(payload, "timeout_seconds", default=120),
        )
        return self._json(
            201,
            {
                "verification_id": result.verification_id,
                "status": result.receipt.status.value,
                "stop_reason": result.receipt.stop_reason.value,
                "receipt_path": str(result.receipt_path),
            },
        )

    def _autofix(self, payload: dict[str, object]) -> JsonResponse:
        provider = _string_value(payload.get("provider")) or "openai"
        patcher = self._patcher_factory(self._settings, provider)
        sandbox = _bool_value(payload.get("sandbox"), default=False)
        verification_runtime = ExecutionRuntime(
            _string_value(payload.get("runtime")) or self._settings.verification_runtime.value
        )
        autofix_use_case = RunAutofixUseCase(
            self._repository,
            patcher,
            SafetyPolicy(branch_prefix=self._settings.branch_prefix),
            verifier=VerifyRunUseCase(
                self._repository,
                SafetyPolicy(branch_prefix=self._settings.branch_prefix),
                command_runner=build_command_runner(self._settings, verification_runtime),
            ),
        )
        if sandbox:
            result = RunSandboxedAutofixUseCase(
                ManageSandboxUseCase(
                    self._repository,
                    LocalSandboxManager(max_file_bytes=self._settings.sandbox_max_file_bytes),
                ),
                autofix_use_case,
            ).run(
                run_id=_required_string(payload, "run_id"),
                source_repo_root=Path(_required_string(payload, "repo_root")).resolve(),
                artifact_dir=self._settings.artifact_dir,
                sandbox_dir=self._settings.sandbox_dir,
                max_attempts=_required_int(payload, "max_attempts", default=3),
                verify_max_attempts=_required_int(payload, "verify_max_attempts", default=3),
                timeout_seconds=_required_int(payload, "timeout_seconds", default=120),
                objective=_optional_string(payload.get("objective")),
            )
            return self._json(
                201,
                {
                    "sandbox_id": result.sandbox.sandbox_id,
                    "sandbox_workspace": str(result.sandbox.receipt.workspace_root),
                    "sandbox_strategy": result.sandbox.receipt.materialization_strategy,
                    "autofix_id": result.autofix.autofix_id,
                    "status": result.autofix.status.value,
                    "attempt_count": len(result.autofix.receipt.attempts),
                    "receipt_path": str(result.autofix.receipt_path),
                },
            )
        result = autofix_use_case.run(
            run_id=_required_string(payload, "run_id"),
            repo_root=self._resolve_repo_root(
                repo_root=_optional_string(payload.get("repo_root")),
                sandbox_id=_optional_string(payload.get("sandbox_id")),
            ),
            artifact_dir=self._settings.artifact_dir,
            max_attempts=_required_int(payload, "max_attempts", default=3),
            verify_max_attempts=_required_int(payload, "verify_max_attempts", default=3),
            timeout_seconds=_required_int(payload, "timeout_seconds", default=120),
            objective=_optional_string(payload.get("objective")),
        )
        return self._json(
            201,
            {
                "autofix_id": result.autofix_id,
                "status": result.status.value,
                "attempt_count": len(result.receipt.attempts),
                "receipt_path": str(result.receipt_path),
            },
        )

    def _prepare_sandbox(self, payload: dict[str, object]) -> JsonResponse:
        result = ManageSandboxUseCase(
            self._repository,
            LocalSandboxManager(max_file_bytes=self._settings.sandbox_max_file_bytes),
        ).prepare(
            repo_root=Path(_required_string(payload, "repo_root")).resolve(),
            sandbox_dir=self._settings.sandbox_dir,
            artifact_dir=self._settings.artifact_dir,
            linked_run_id=_optional_string(payload.get("run_id")),
            summary=_optional_string(payload.get("summary")) or "Sandbox prepared by API request.",
        )
        return self._json(
            201,
            {
                "sandbox_id": result.sandbox_id,
                "workspace_root": str(result.receipt.workspace_root),
                "materialization_strategy": result.receipt.materialization_strategy,
                "source_branch": result.receipt.source_branch,
                "source_head_sha": result.receipt.source_head_sha,
                "copied_file_count": result.receipt.copied_file_count,
                "receipt_path": str(result.receipt_path),
            },
        )

    def _cleanup_sandbox(self, sandbox_id: str) -> JsonResponse:
        result = ManageSandboxUseCase(
            self._repository,
            LocalSandboxManager(max_file_bytes=self._settings.sandbox_max_file_bytes),
        ).cleanup(
            sandbox_id=sandbox_id,
            remove_workspace=True,
        )
        return self._json(
            200,
            {
                "sandbox_id": result.sandbox_id,
                "status": result.receipt.status.value,
                "receipt_path": str(result.receipt_path),
            },
        )

    def _identity_me(self, principal: AuthenticatedPrincipal | None) -> JsonResponse:
        if principal is None:
            return self._json(200, {"authenticated": False, "principal": None})
        return self._json(200, {"authenticated": True, "principal": _principal_to_dict(principal)})

    def _dashboard(
        self,
        query: dict[str, list[str]],
        *,
        principal: AuthenticatedPrincipal | None,
    ) -> JsonResponse:
        tenant_id = _required_query_string(query, "tenant_id")
        actor = _query_string(query, "actor")
        team = _query_string(query, "team")
        summary = DashboardUseCase(self._repository, self._access_controller).build(
            tenant_id=tenant_id,
            actor=actor,
            team=team,
            principal=principal,
        )
        return self._json(200, {"summary": _dashboard_summary_to_dict(summary.summary)})

    def _notifications(
        self,
        query: dict[str, list[str]],
        *,
        principal: AuthenticatedPrincipal | None,
    ) -> JsonResponse:
        tenant_id = _required_query_string(query, "tenant_id")
        team = _query_string(query, "team")
        if principal is not None:
            self._access_controller.require_tenant_permission_for_principal(
                tenant_id=tenant_id,
                principal=principal,
                permission=PlatformPermission.VIEW_NOTIFICATIONS,
                team=team,
            )
        else:
            self._access_controller.require_tenant_permission(
                tenant_id=tenant_id,
                actor=_required_query_string(query, "actor"),
                permission=PlatformPermission.VIEW_NOTIFICATIONS,
                team=team,
            )
        limit = _int_param(query, "limit", 20, min_value=1)
        items = self._repository.list_notifications(tenant_id=tenant_id, limit=limit)
        return self._json(
            200,
            {
                "items": [_notification_record_to_dict(item) for item in items],
                "pagination": {
                    "limit": limit,
                    "offset": 0,
                    "next_offset": None,
                    "count": len(items),
                },
            },
        )

    def _alerts(
        self,
        query: dict[str, list[str]],
        *,
        principal: AuthenticatedPrincipal | None,
    ) -> JsonResponse:
        tenant_id = _query_string(query, "tenant_id")
        team = _query_string(query, "team")
        if tenant_id:
            if principal is not None:
                self._access_controller.require_tenant_permission_for_principal(
                    tenant_id=tenant_id,
                    principal=principal,
                    permission=PlatformPermission.VIEW_NOTIFICATIONS,
                    team=team,
                )
            else:
                self._access_controller.require_tenant_permission(
                    tenant_id=tenant_id,
                    actor=_required_query_string(query, "actor"),
                    permission=PlatformPermission.VIEW_NOTIFICATIONS,
                    team=team,
                )
        limit = _int_param(query, "limit", 20, min_value=1)
        items = self._repository.list_alerts(tenant_id=tenant_id or None, limit=limit)
        return self._json(
            200,
            {
                "items": [_alert_record_to_dict(item) for item in items],
                "pagination": {
                    "limit": limit,
                    "offset": 0,
                    "next_offset": None,
                    "count": len(items),
                },
            },
        )

    def _traces(self, query: dict[str, list[str]]) -> JsonResponse:
        limit = _int_param(query, "limit", 50, min_value=1)
        items = self._repository.list_trace_events(
            trace_id=_query_string(query, "trace_id") or None,
            linked_run_id=_query_string(query, "run_id") or None,
            linked_job_id=_query_string(query, "job_id") or None,
            limit=limit,
        )
        return self._json(
            200,
            {
                "items": [_trace_event_record_to_dict(item) for item in items],
                "pagination": {
                    "limit": limit,
                    "offset": 0,
                    "next_offset": None,
                    "count": len(items),
                },
            },
        )

    def _request_approval(
        self,
        payload: dict[str, object],
        *,
        principal: AuthenticatedPrincipal | None,
    ) -> JsonResponse:
        run = self._repository.get_run(_required_string(payload, "run_id"))
        if run is None:
            raise ValueError(f"Run not found: {_required_string(payload, 'run_id')}")
        run_record, _ = run
        actor, team, tenant_context = self._principal_request_context(
            repo_full_name=run_record.repo_full_name,
            payload=payload,
            principal=principal,
            permission=PlatformPermission.REQUEST_APPROVAL,
            require_team=True,
        )
        result = RequestApprovalUseCase(
            self._repository,
            _approval_policy_for_context(settings=self._settings, tenant_context=tenant_context),
        ).request_delivery_approval(
            run_id=run_record.run_id,
            execution_id=_required_string(payload, "execution_id"),
            verification_id=_required_string(payload, "verification_id"),
            actor=actor,
            team=team,
            comment=_string_value(payload.get("comment")),
            expires_in_hours=_optional_int(payload.get("expires_in_hours")) or self._settings.approval_ttl_hours,
            assigned_reviewers=_string_list(payload.get("assigned_reviewers")),
            assigned_reviewer_teams=_string_list(payload.get("assigned_reviewer_teams")),
        )
        self._emit_platform_notification(
            tenant_context=tenant_context,
            event_type=NotificationEventType.APPROVAL_REQUESTED,
            summary=f"Approval {result.approval_id} is {result.receipt.status.value} for {run_record.repo_full_name}.",
            payload={
                "approval_id": result.approval_id,
                "run_id": run_record.run_id,
                "repo_full_name": run_record.repo_full_name,
                "status": result.receipt.status.value,
                "risk_level": result.receipt.risk_level.value,
                "required_approvals": result.receipt.required_approvals,
            },
        )
        return self._json(
            201,
            {
                "approval_id": result.approval_id,
                "status": result.receipt.status.value,
                "risk_level": result.receipt.risk_level.value,
                "required_approvals": result.receipt.required_approvals,
                "expires_at": result.receipt.expires_at,
                "receipt_path": str(result.receipt_path),
            },
        )

    def _review_approval(
        self,
        payload: dict[str, object],
        *,
        principal: AuthenticatedPrincipal | None,
    ) -> JsonResponse:
        approval_id = _required_string(payload, "approval_id")
        approval = self._repository.get_approval(approval_id)
        if approval is None:
            raise ValueError(f"Approval not found: {approval_id}")
        approval_record, _ = approval
        actor, team, tenant_context = self._principal_request_context(
            repo_full_name=approval_record.repo_full_name,
            payload=payload,
            principal=principal,
            permission=PlatformPermission.REVIEW_APPROVAL,
            require_team=True,
        )
        result = ReviewApprovalUseCase(
            self._repository,
            _approval_policy_for_context(settings=self._settings, tenant_context=tenant_context),
        ).decide(
            approval_id=approval_id,
            actor=actor,
            team=team,
            decision=ApprovalDecision(_required_string(payload, "decision")),
            comment=_string_value(payload.get("comment")),
        )
        self._emit_platform_notification(
            tenant_context=tenant_context,
            event_type=NotificationEventType.APPROVAL_REVIEWED,
            summary=f"Approval {approval_id} is now {result.receipt.status.value}.",
            payload={
                "approval_id": approval_id,
                "run_id": approval_record.linked_run_id,
                "repo_full_name": approval_record.repo_full_name,
                "status": result.receipt.status.value,
                "approved_count": result.receipt.approved_count,
                "required_approvals": result.receipt.required_approvals,
            },
        )
        return self._json(
            200,
            {
                "approval_id": result.approval_id,
                "status": result.receipt.status.value,
                "approved_count": result.receipt.approved_count,
                "required_approvals": result.receipt.required_approvals,
                "receipt_path": str(result.receipt_path),
            },
        )

    def _deliver(
        self,
        payload: dict[str, object],
        *,
        principal: AuthenticatedPrincipal | None,
    ) -> JsonResponse:
        run_id = _required_string(payload, "run_id")
        run = self._repository.get_run(run_id)
        if run is None:
            raise ValueError(f"Run not found: {run_id}")
        run_record, _ = run
        _, _, tenant_context = self._principal_request_context(
            repo_full_name=run_record.repo_full_name,
            payload=payload,
            principal=principal,
            permission=PlatformPermission.DELIVER,
            require_team=False,
        )
        result = DeliverRunUseCase(
            self._repository,
            self._github_client,
            SafetyPolicy(branch_prefix=self._settings.branch_prefix),
            approval_policy=_approval_policy_for_context(settings=self._settings, tenant_context=tenant_context),
            delivery_governance_policy=_delivery_governance_for_context(
                settings=self._settings,
                tenant_context=tenant_context,
            ),
        ).deliver(
            run_id=run_id,
            execution_id=_required_string(payload, "execution_id"),
            verification_id=_required_string(payload, "verification_id"),
            approval_id=_optional_string(payload.get("approval_id")),
            repo_root=self._resolve_repo_root(
                repo_root=_optional_string(payload.get("repo_root")),
                sandbox_id=_optional_string(payload.get("sandbox_id")),
            ),
            artifact_dir=self._settings.artifact_dir,
            artifact_base_url=self._settings.artifact_base_url,
            artifact_store_backend=self._settings.artifact_store_backend,
            artifact_store_dir=self._settings.artifact_store_dir,
            artifact_store_base_url=self._settings.artifact_store_base_url,
            remote_name=self._settings.git_remote_name,
            base_branch=_optional_string(payload.get("base_branch")),
            rollout_stage=_optional_string(payload.get("rollout_stage")),
            commit_message=_optional_string(payload.get("commit_message")),
            pr_title=_optional_string(payload.get("pr_title")),
            publish_pr_comment=_bool_value(payload.get("publish_pr_comment"), default=True),
        )
        self._emit_platform_notification(
            tenant_context=tenant_context,
            event_type=(
                NotificationEventType.DELIVERY_SUCCEEDED
                if result.receipt.status == DeliveryStatus.SUCCEEDED
                else NotificationEventType.DELIVERY_BLOCKED
            ),
            summary=f"Delivery {result.delivery_id} is {result.receipt.status.value} for {result.receipt.repo_full_name}.",
            payload={
                "delivery_id": result.delivery_id,
                "run_id": result.receipt.linked_run_id,
                "approval_id": result.receipt.linked_approval_id,
                "repo_full_name": result.receipt.repo_full_name,
                "status": result.receipt.status.value,
                "error_message": result.receipt.error_message,
            },
        )
        return self._json(
            201,
            {
                "delivery_id": result.delivery_id,
                "status": result.receipt.status.value,
                "commit_sha": result.receipt.commit_sha,
                "rollout_stage": result.receipt.rollout_stage,
                "receipt_path": str(result.receipt_path),
                "error_message": result.receipt.error_message,
            },
        )

    def _sync_identity(
        self,
        payload: dict[str, object],
        *,
        principal: AuthenticatedPrincipal | None,
    ) -> JsonResponse:
        if principal is None:
            raise PolicyError("Signed principal authentication is required for identity sync.")
        tenant_id = _required_string(payload, "tenant_id")
        memberships_payload = payload.get("memberships")
        if not isinstance(memberships_payload, list) or not memberships_payload:
            raise ValueError("memberships must be a non-empty list.")
        if any(not isinstance(item, dict) for item in memberships_payload):
            raise ValueError("Each membership entry must be a JSON object.")
        memberships = [
            IdentitySyncMembership(
                actor=_required_string(item, "actor"),
                role=TenantRole(_required_string(item, "role")),
                team=_required_string(item, "team"),
            )
            for item in memberships_payload
        ]
        result = SyncIdentityUseCase(self._repository, self._access_controller).sync_tenant_memberships(
            tenant_id=tenant_id,
            memberships=memberships,
            replace_existing=_bool_value(payload.get("replace_existing"), default=False),
            team=_optional_string(payload.get("team")),
            principal=principal,
        )
        return self._json(
            200,
            {
                "tenant_id": result.receipt.tenant_id,
                "synced_at": result.receipt.synced_at,
                "synced_by": result.receipt.synced_by,
                "replace_existing": result.receipt.replace_existing,
                "created_count": result.receipt.created_count,
                "updated_count": result.receipt.updated_count,
                "removed_count": result.receipt.removed_count,
                "membership_count": result.receipt.membership_count,
            },
        )

    def _export_audit(self, payload: dict[str, object]) -> JsonResponse:
        result = RunAuditExporter(self._repository).export_run(
            run_id=_required_string(payload, "run_id"),
            output_dir=Path(
                _string_value(payload.get("output_dir")) or str(self._settings.audit_export_dir)
            ).resolve(),
        )
        return self._json(
            201,
            {
                "export_id": result.export_id,
                "run_id": result.run_id,
                "bundle_path": str(result.bundle_path),
                "manifest_path": str(result.manifest_path),
                "archive_path": str(result.archive_path),
            },
        )

    def _enforce_retention(self, payload: dict[str, object]) -> JsonResponse:
        result = RetentionEnforcer(self._repository, self._settings).enforce(
            dry_run=_bool_value(payload.get("dry_run"), default=True)
        )
        return self._json(
            200,
            {
                "dry_run": result.dry_run,
                "notification_count": result.notification_count,
                "worker_heartbeat_count": result.worker_heartbeat_count,
                "alert_count": result.alert_count,
                "trace_count": result.trace_count,
                "deleted_paths": result.deleted_paths,
            },
        )

    def _enqueue_plan(
        self,
        payload: dict[str, object],
        *,
        principal: AuthenticatedPrincipal | None,
    ) -> JsonResponse:
        actor, team = self._actor_and_team_from_request(payload=payload, principal=principal, require_team=True)
        job = ManageQueueUseCase(
            self._repository,
            self._settings,
            self._access_controller,
            self._budget_manager,
        ).enqueue_plan(
            repo_full_name=_required_string(payload, "repo"),
            issue_number=_required_int(payload, "issue"),
            repo_root=Path(_required_string(payload, "repo_root")).resolve(),
            provider=_string_value(payload.get("provider")) or "heuristic",
            actor=actor,
            team=team,
            objective=_optional_string(payload.get("objective")),
            create_branch=_bool_value(payload.get("create_branch"), default=False),
            priority=_required_int(payload, "priority", default=0),
            max_attempts=_optional_int(payload.get("max_attempts")),
            budget_units=_optional_int(payload.get("budget_units")),
            output_dir=_optional_path(payload.get("output_dir")),
            required_worker_tags=_string_list(payload.get("required_worker_tags")),
            concurrency_key=_optional_string(payload.get("concurrency_key")),
        )
        return self._json(
            201,
            {
                "job_id": job.job_id,
                "status": job.status.value,
                "receipt_path": str(job.receipt_path),
            },
        )

    def _enqueue_verify(
        self,
        payload: dict[str, object],
        *,
        principal: AuthenticatedPrincipal | None,
    ) -> JsonResponse:
        actor, team = self._actor_and_team_from_request(payload=payload, principal=principal, require_team=True)
        job = ManageQueueUseCase(
            self._repository,
            self._settings,
            self._access_controller,
            self._budget_manager,
        ).enqueue_verify(
            run_id=_optional_string(payload.get("run_id")),
            execution_id=_optional_string(payload.get("execution_id")),
            repo_root=self._resolve_repo_root(
                repo_root=_optional_string(payload.get("repo_root")),
                sandbox_id=_optional_string(payload.get("sandbox_id")),
            ),
            actor=actor,
            team=team,
            priority=_required_int(payload, "priority", default=0),
            max_attempts=_optional_int(payload.get("max_attempts")),
            budget_units=_optional_int(payload.get("budget_units")),
            verify_max_attempts=_required_int(payload, "verify_max_attempts", default=3),
            timeout_seconds=_required_int(payload, "timeout_seconds", default=120),
            output_dir=_optional_path(payload.get("output_dir")),
            required_worker_tags=_string_list(payload.get("required_worker_tags")),
            concurrency_key=_optional_string(payload.get("concurrency_key")),
        )
        return self._json(
            201,
            {
                "job_id": job.job_id,
                "status": job.status.value,
                "receipt_path": str(job.receipt_path),
            },
        )

    def _enqueue_deliver(
        self,
        payload: dict[str, object],
        *,
        principal: AuthenticatedPrincipal | None,
    ) -> JsonResponse:
        actor, team = self._actor_and_team_from_request(payload=payload, principal=principal, require_team=True)
        job = ManageQueueUseCase(
            self._repository,
            self._settings,
            self._access_controller,
            self._budget_manager,
        ).enqueue_deliver(
            run_id=_required_string(payload, "run_id"),
            execution_id=_required_string(payload, "execution_id"),
            verification_id=_required_string(payload, "verification_id"),
            approval_id=_optional_string(payload.get("approval_id")),
            actor=actor,
            team=team,
            repo_root=self._resolve_repo_root(
                repo_root=_optional_string(payload.get("repo_root")),
                sandbox_id=_optional_string(payload.get("sandbox_id")),
            ),
            priority=_required_int(payload, "priority", default=0),
            max_attempts=_optional_int(payload.get("max_attempts")),
            budget_units=_optional_int(payload.get("budget_units")),
            base_branch=_optional_string(payload.get("base_branch")),
            rollout_stage=_optional_string(payload.get("rollout_stage")),
            commit_message=_optional_string(payload.get("commit_message")),
            pr_title=_optional_string(payload.get("pr_title")),
            publish_pr_comment=_bool_value(payload.get("publish_pr_comment"), default=True),
            required_worker_tags=_string_list(payload.get("required_worker_tags")),
            concurrency_key=_optional_string(payload.get("concurrency_key")),
        )
        return self._json(
            201,
            {
                "job_id": job.job_id,
                "status": job.status.value,
                "receipt_path": str(job.receipt_path),
            },
        )

    def _cancel_job(
        self,
        job_id: str,
        payload: dict[str, object],
        *,
        principal: AuthenticatedPrincipal | None,
    ) -> JsonResponse:
        actor, team = self._actor_and_team_from_request(payload=payload, principal=principal, require_team=True)
        result = ManageQueueUseCase(
            self._repository,
            self._settings,
            self._access_controller,
            self._budget_manager,
        ).cancel_job(
            job_id=job_id,
            actor=actor,
            team=team,
        )
        return self._json(200, {"job_id": result.job_id, "status": result.status.value, "receipt_path": str(result.receipt_path)})

    def _resume_job(
        self,
        job_id: str,
        payload: dict[str, object],
        *,
        principal: AuthenticatedPrincipal | None,
    ) -> JsonResponse:
        actor, team = self._actor_and_team_from_request(payload=payload, principal=principal, require_team=True)
        result = ManageQueueUseCase(
            self._repository,
            self._settings,
            self._access_controller,
            self._budget_manager,
        ).resume_job(
            job_id=job_id,
            actor=actor,
            team=team,
            reset_attempts=_bool_value(payload.get("reset_attempts"), default=False),
        )
        return self._json(200, {"job_id": result.job_id, "status": result.status.value, "receipt_path": str(result.receipt_path)})

    def _ingest_jira_issue_webhook(self, *, headers: dict[str, str], body: bytes) -> JsonResponse:
        self._verify_jira_secret(headers=headers)
        payload = _json_object(body)
        issue_payload = _dict_value(payload.get("issue"))
        fields = _dict_value(issue_payload.get("fields"))
        issue_key = _required_string(issue_payload, "key")
        project_key = _required_string(_dict_value(fields.get("project")), "key")
        mapping = self._resolve_jira_project_mapping(project_key)
        repo_root = _resolve_mapping_path(mapping, "repo_root", base_dir=self._settings.jira_project_mappings_path)
        job = ManageQueueUseCase(
            self._repository,
            self._settings,
            self._access_controller,
            self._budget_manager,
        ).enqueue_external_plan(
            repo_full_name=_required_mapping_string(mapping, "repo", fallback_key="repo_full_name"),
            external_key=issue_key,
            external_title=_string_value(fields.get("summary")) or issue_key,
            external_body=_jira_description_text(fields.get("description")),
            external_labels=_string_list(fields.get("labels")),
            external_url=self._jira_client.build_issue_url(issue_key),
            source_system="jira",
            repo_root=repo_root,
            provider=_string_value(mapping.get("provider")) or "heuristic",
            actor=_string_value(mapping.get("actor")) or self._settings.webhook_actor,
            team=_string_value(mapping.get("team")) or self._settings.webhook_team,
            objective=_optional_string(mapping.get("objective")),
            create_branch=_bool_value(mapping.get("create_branch"), default=False),
            required_worker_tags=_string_list(mapping.get("required_worker_tags")),
            concurrency_key=_optional_string(mapping.get("concurrency_key")),
        )
        return self._json(
            202,
            {
                "queued": True,
                "job_id": job.job_id,
                "status": job.status.value,
                "receipt_path": str(job.receipt_path),
            },
        )

    def _ingest_slack_approval_webhook(self, *, headers: dict[str, str], body: bytes) -> JsonResponse:
        self._slack_client.verify_signature(
            timestamp=headers.get("x-slack-request-timestamp"),
            signature=headers.get("x-slack-signature"),
            body=body,
        )
        payload = _slack_payload(headers=headers, body=body)
        review_payload = _slack_approval_review_payload(payload)
        response = self._review_approval(review_payload, principal=None)
        return self._json(
            200,
            {
                "ok": True,
                "approval_id": response.body["approval_id"],
                "status": response.body["status"],
                "approved_count": response.body["approved_count"],
                "required_approvals": response.body["required_approvals"],
            },
        )

    def _ingest_github_issue_webhook(self, *, headers: dict[str, str], body: bytes) -> JsonResponse:
        event = headers.get("x-github-event", "")
        if event != "issues":
            raise ApiError(400, f"Unsupported GitHub event: {event or 'missing'}")
        self._verify_github_signature(headers=headers, body=body)
        payload = _json_object(body)
        action = _string_value(payload.get("action"))
        if action not in {"opened", "reopened", "labeled"}:
            return self._json(202, {"ignored": True, "reason": f"Unsupported issue action: {action}"})
        repository_payload = _dict_value(payload.get("repository"))
        issue_payload = _dict_value(payload.get("issue"))
        repo_full_name = _required_string(repository_payload, "full_name")
        issue_number = _required_int(issue_payload, "number")
        repo_root = self._resolve_webhook_repo_root(repo_full_name)
        job = ManageQueueUseCase(
            self._repository,
            self._settings,
            self._access_controller,
            self._budget_manager,
        ).enqueue_plan(
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            repo_root=repo_root,
            provider="heuristic",
            actor=self._settings.webhook_actor,
            team=self._settings.webhook_team,
            objective=None,
            create_branch=False,
        )
        return self._json(
            202,
            {
                "queued": True,
                "job_id": job.job_id,
                "status": job.status.value,
                "receipt_path": str(job.receipt_path),
            },
        )

    def _emit_platform_notification(
        self,
        *,
        tenant_context: tuple[object, dict[str, object]] | None,
        event_type: NotificationEventType,
        summary: str,
        payload: dict[str, object],
    ) -> None:
        if tenant_context is None:
            return
        tenant_record, _ = tenant_context
        self._notification_outbox.emit(
            tenant_id=tenant_record.tenant_id,
            event_type=event_type,
            summary=summary,
            payload=payload,
            output_dir=self._settings.notification_dir,
        )

    def _resolve_webhook_repo_root(self, repo_full_name: str) -> Path:
        if self._settings.webhook_repo_roots_path is None:
            raise ValueError(
                "ISSUE_TO_PR_WEBHOOK_REPO_ROOTS_PATH is required for GitHub webhook plan ingestion."
            )
        payload = json.loads(self._settings.webhook_repo_roots_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Webhook repo root mapping file must contain a JSON object.")
        raw_path = payload.get(repo_full_name)
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError(f"No local repo root mapping is configured for {repo_full_name}.")
        path = Path(raw_path)
        if not path.is_absolute():
            path = (self._settings.webhook_repo_roots_path.parent / path).resolve()
        return path

    def _resolve_jira_project_mapping(self, project_key: str) -> dict[str, object]:
        if self._settings.jira_project_mappings_path is None:
            raise ValueError(
                "ISSUE_TO_PR_JIRA_PROJECT_MAPPINGS_PATH is required for Jira webhook plan ingestion."
            )
        payload = json.loads(self._settings.jira_project_mappings_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Jira project mapping file must contain a JSON object.")
        mapping = payload.get(project_key)
        if not isinstance(mapping, dict):
            raise ValueError(f"No Jira project mapping is configured for {project_key}.")
        return mapping

    def _resolve_repo_root(self, *, repo_root: str | None, sandbox_id: str | None) -> Path:
        if sandbox_id is not None:
            sandbox = self._repository.get_sandbox(sandbox_id)
            if sandbox is None:
                raise ValueError(f"Sandbox not found: {sandbox_id}")
            record, _ = sandbox
            return record.workspace_root.resolve()
        if repo_root is None:
            raise ValueError("repo_root is required when sandbox_id is not provided.")
        return Path(repo_root).resolve()

    def _load_proposal(self, payload: dict[str, object]) -> PatchProposal:
        proposal_payload = payload.get("proposal")
        if isinstance(proposal_payload, dict):
            return PatchProposal.from_dict(proposal_payload)
        proposal_id = _optional_string(payload.get("proposal_id"))
        if proposal_id is None:
            raise ValueError("Provide either proposal or proposal_id.")
        stored = self._repository.get_patch_proposal(proposal_id)
        if stored is None:
            raise ValueError(f"Patch proposal not found: {proposal_id}")
        _, stored_payload = stored
        return PatchProposal.from_dict(stored_payload)

    def _verify_github_signature(self, *, headers: dict[str, str], body: bytes) -> None:
        secret = self._settings.webhook_secret
        if not secret:
            return
        signature = headers.get("x-hub-signature-256")
        if not signature:
            raise PolicyError("Missing GitHub webhook signature.")
        expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise PolicyError("GitHub webhook signature is invalid.")

    def _verify_jira_secret(self, *, headers: dict[str, str]) -> None:
        secret = self._settings.jira_webhook_secret
        if not secret:
            return
        provided = (
            headers.get("x-issue-to-pr-jira-secret")
            or headers.get("x-jira-webhook-secret")
            or _bearer_token(headers.get("authorization", ""))
        )
        if not provided:
            raise PolicyError("Missing Jira webhook secret.")
        if not hmac.compare_digest(provided, secret):
            raise PolicyError("Jira webhook secret is invalid.")

    def _authenticate(
        self,
        *,
        path: str,
        headers: dict[str, str],
    ) -> AuthenticatedPrincipal | None:
        if path == "/healthz" or path.startswith("/v1/webhooks/") or path.startswith("/ui"):
            return None
        auth = headers.get("authorization", "")
        principal = self._authenticate_signed_token(auth)
        if principal is not None:
            return principal
        if self._settings.api_token is None and self._settings.auth_token_secret is None:
            return None
        if self._settings.api_token is not None and auth == f"Bearer {self._settings.api_token}":
            return None
        raise PolicyError("API token is missing or invalid.")

    def _authenticate_signed_token(self, auth_header: str) -> AuthenticatedPrincipal | None:
        if self._settings.auth_token_secret is None:
            return None
        if not auth_header.startswith("Bearer "):
            return None
        token = auth_header.removeprefix("Bearer ").strip()
        if not token or (self._settings.api_token is not None and token == self._settings.api_token):
            return None
        return authenticate_bearer_token(
            token,
            secret=self._settings.auth_token_secret,
            expected_issuer=self._settings.auth_token_issuer,
        )

    def _principal_request_context(
        self,
        *,
        repo_full_name: str,
        payload: dict[str, object],
        principal: AuthenticatedPrincipal | None,
        permission: PlatformPermission,
        require_team: bool,
    ) -> tuple[str, str, tuple[object, dict[str, object]] | None]:
        actor, team = self._actor_and_team_from_request(payload=payload, principal=principal, require_team=require_team)
        if principal is not None:
            tenant_context = self._access_controller.require_repo_permission_for_principal(
                repo_full_name=repo_full_name,
                principal=principal,
                permission=permission,
                team=team,
            )
        else:
            tenant_context = self._access_controller.require_repo_permission(
                repo_full_name=repo_full_name,
                actor=actor,
                permission=permission,
                team=team,
            )
        return actor, team, tenant_context

    def _actor_and_team_from_request(
        self,
        *,
        payload: dict[str, object],
        principal: AuthenticatedPrincipal | None,
        require_team: bool,
    ) -> tuple[str, str]:
        actor = _optional_string(payload.get("actor"))
        team = _optional_string(payload.get("team"))
        if principal is None:
            resolved_actor = _required_string(payload, "actor")
            resolved_team = _required_string(payload, "team") if require_team else (team or "")
            return resolved_actor, resolved_team
        if actor is not None and actor != principal.actor:
            raise PolicyError("Payload actor does not match the authenticated principal.")
        principal_teams = [item for item in ([principal.team] + principal.groups) if item]
        if team is not None and team not in principal_teams:
            raise PolicyError("Payload team does not match the authenticated principal.")
        resolved_team = team or principal.team or (principal.groups[0] if principal.groups else "")
        if require_team and not resolved_team:
            raise PolicyError("Authenticated principal does not include a team.")
        return principal.actor, resolved_team

    def _rate_limit(self, *, path: str, headers: dict[str, str]) -> dict[str, str]:
        if path == "/healthz" or path.startswith("/ui"):
            return {}
        result = self._rate_limiter.check(_rate_limit_identity(headers))
        if result is None:
            return {}
        return {
            "X-RateLimit-Limit": str(result.limit),
            "X-RateLimit-Remaining": str(result.remaining),
            "X-RateLimit-Reset": str(result.reset_epoch_seconds),
        }

    def _idempotent_replay(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> JsonResponse | None:
        key = _idempotency_key(method=method, path=path, headers=headers)
        if key is None:
            return None
        return self._idempotency_store.replay(
            method=method,
            path=path,
            key=key,
            request_hash=_request_hash(method=method, path=path, body=body),
        )

    def _persist_idempotency(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        response: JsonResponse,
    ) -> None:
        key = _idempotency_key(method=method, path=path, headers=headers)
        if key is None or response.status_code >= 500:
            return
        self._idempotency_store.save(
            method=method,
            path=path,
            key=key,
            request_hash=_request_hash(method=method, path=path, body=body),
            response=response,
        )

    def _get_payload(self, stored: tuple[object, dict[str, object]] | None, label: str) -> JsonResponse:
        if stored is None:
            raise ApiError(404, f"{label.capitalize()} not found.")
        _, payload = stored
        return self._json(200, payload)

    def _list_response(
        self,
        query: dict[str, list[str]],
        *,
        fetch: Callable[[int], list[object]],
        serializer: Callable[[object], dict[str, object]],
        default_limit: int = 20,
    ) -> JsonResponse:
        limit = _int_param(query, "limit", default_limit, min_value=1)
        offset = _int_param(query, "offset", 0, min_value=0)
        items = fetch(limit + offset + 1)
        page_items = items[offset : offset + limit]
        next_offset = offset + limit if len(items) > offset + limit else None
        return self._json(
            200,
            {
                "items": [serializer(item) for item in page_items],
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "next_offset": next_offset,
                    "count": len(page_items),
                },
            },
        )

    def _json(self, status_code: int, body: object, *, headers: dict[str, str] | None = None) -> JsonResponse:
        response_headers = {"Content-Type": "application/json"}
        if headers:
            response_headers.update(headers)
        return JsonResponse(status_code=status_code, body=body, headers=response_headers)

    def _html(self, body: str) -> JsonResponse:
        return self._text(body, content_type="text/html; charset=utf-8")

    def _text(self, body: str, *, content_type: str) -> JsonResponse:
        return JsonResponse(status_code=200, body=body, headers={"Content-Type": content_type})

    def _finalize_response(
        self,
        response: JsonResponse,
        *,
        request_id: str,
        extra_headers: dict[str, str],
    ) -> JsonResponse:
        headers = dict(response.headers)
        headers["X-Request-ID"] = request_id
        headers.update(extra_headers)
        return JsonResponse(status_code=response.status_code, body=response.body, headers=headers)


def _default_planner_factory(settings: Settings, provider: str) -> PlannerClient:
    if provider == "heuristic":
        return HeuristicPlanner()
    if provider == "openai":
        settings.require_openai()
        return OpenAIPlanner(settings)
    raise ValueError(f"Unsupported planner provider: {provider}")


def _default_patcher_factory(settings: Settings, provider: str) -> PatcherClient:
    if provider != "openai":
        raise ValueError(f"Unsupported patch provider: {provider}")
    settings.require_openai()
    return OpenAIPatcher(settings)


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


def _rate_limit_identity(headers: dict[str, str]) -> str:
    for key in ("x-api-client", "x-forwarded-for", "authorization"):
        value = headers.get(key)
        if isinstance(value, str) and value.strip():
            return f"{key}:{value.strip()}"
    return "anonymous"


def _idempotency_key(*, method: str, path: str, headers: dict[str, str]) -> str | None:
    if method != "POST":
        return None
    if path.startswith("/v1/webhooks/"):
        for header_name in (
            "x-github-delivery",
            "x-atlassian-webhook-identifier",
            "x-slack-request-timestamp",
            "idempotency-key",
        ):
            value = _optional_string(headers.get(header_name))
            if value is not None:
                return value
        return None
    return _optional_string(headers.get("idempotency-key"))


def _request_hash(*, method: str, path: str, body: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(method.encode("utf-8"))
    digest.update(b"\0")
    digest.update(path.encode("utf-8"))
    digest.update(b"\0")
    digest.update(body)
    return digest.hexdigest()


def _openapi_schema() -> dict[str, object]:
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Issue-to-PR Control Plane API",
            "version": "1.0.0",
            "description": "HTTP control plane for planning, execution, verification, approval, delivery, and queue workflows.",
        },
        "paths": {
            "/healthz": {"get": {"summary": "Health check"}},
            "/ui": {"get": {"summary": "Operator and reviewer console"}},
            "/v1/openapi.json": {"get": {"summary": "OpenAPI document"}},
            "/v1/identity/me": {"get": {"summary": "Inspect the authenticated principal"}},
            "/v1/identity/sync": {"post": {"summary": "Synchronize tenant memberships from an identity provider"}},
            "/v1/dashboard": {"get": {"summary": "Load a tenant dashboard summary"}},
            "/v1/notifications": {"get": {"summary": "List tenant notification events"}},
            "/v1/alerts": {"get": {"summary": "List emitted alerts"}},
            "/v1/traces": {"get": {"summary": "List recorded trace events"}},
            "/v1/runs": {"get": {"summary": "List planning runs"}},
            "/v1/plan": {"post": {"summary": "Create a planning run"}},
            "/v1/patch-proposals": {"get": {"summary": "List patch proposals"}},
            "/v1/patch-proposals/generate": {"post": {"summary": "Generate a patch proposal"}},
            "/v1/patch-executions": {"post": {"summary": "Execute a patch proposal"}},
            "/v1/executions": {"get": {"summary": "List patch executions"}},
            "/v1/verifications": {"get": {"summary": "List verifications"}},
            "/v1/verify": {"post": {"summary": "Run verification"}},
            "/v1/autofix-runs": {"get": {"summary": "List autofix runs"}},
            "/v1/autofix": {"post": {"summary": "Run bounded autofix"}},
            "/v1/sandboxes": {"get": {"summary": "List sandboxes"}, "post": {"summary": "Prepare a sandbox"}},
            "/v1/approvals": {"get": {"summary": "List approval requests"}},
            "/v1/approvals/request": {"post": {"summary": "Request delivery approval"}},
            "/v1/approvals/review": {"post": {"summary": "Review an approval request"}},
            "/v1/deliveries": {"get": {"summary": "List deliveries"}},
            "/v1/deliver": {"post": {"summary": "Deliver a verified execution"}},
            "/v1/audits/exports": {"post": {"summary": "Export an audit bundle for a run"}},
            "/v1/retention/enforce": {"post": {"summary": "Evaluate or enforce retention rules"}},
            "/v1/queue-jobs": {"get": {"summary": "List queued jobs"}},
            "/v1/queue/plan": {"post": {"summary": "Enqueue a plan job"}},
            "/v1/queue/verify": {"post": {"summary": "Enqueue a verification job"}},
            "/v1/queue/deliver": {"post": {"summary": "Enqueue a delivery job"}},
            "/v1/webhooks/github/issues": {"post": {"summary": "GitHub issues webhook"}},
            "/v1/webhooks/jira/issues": {"post": {"summary": "Jira issues webhook"}},
            "/v1/webhooks/slack/approvals": {"post": {"summary": "Slack approval action webhook"}},
        },
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                }
            }
        },
        "x-control-plane": {
            "request_id_header": "X-Request-ID",
            "idempotency_key_header": "Idempotency-Key",
        },
    }


def _body_bytes(body: bytes | str | None) -> bytes:
    if body is None:
        return b""
    if isinstance(body, bytes):
        return body
    return body.encode("utf-8")


def _json_object(body: bytes) -> dict[str, object]:
    if not body:
        return {}
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON request body must be an object.")
    return payload


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _required_mapping_string(
    mapping: dict[str, object],
    key: str,
    *,
    fallback_key: str | None = None,
) -> str:
    value = _string_value(mapping.get(key))
    if value:
        return value
    if fallback_key is not None:
        fallback_value = _string_value(mapping.get(fallback_key))
        if fallback_value:
            return fallback_value
    raise ValueError(f"Jira project mapping is missing required field: {key}")


def _resolve_mapping_path(
    mapping: dict[str, object],
    key: str,
    *,
    base_dir: Path | None,
) -> Path:
    raw_path = _required_mapping_string(mapping, key)
    path = Path(raw_path)
    if not path.is_absolute():
        if base_dir is None:
            raise ValueError(f"Relative mapping path requires a base directory: {raw_path}")
        path = (base_dir.parent / path).resolve()
    return path


def _jira_description_text(value: object) -> str:
    if isinstance(value, str):
        return value
    lines: list[str] = []
    _collect_jira_text(value, lines)
    normalized = "\n".join(line for line in (item.strip() for item in lines) if line)
    return normalized


def _collect_jira_text(value: object, lines: list[str]) -> None:
    if isinstance(value, str):
        lines.append(value)
        return
    if isinstance(value, list):
        for item in value:
            _collect_jira_text(item, lines)
        return
    if not isinstance(value, dict):
        return
    text = value.get("text")
    if isinstance(text, str) and text.strip():
        lines.append(text)
    content = value.get("content")
    if isinstance(content, list):
        for item in content:
            _collect_jira_text(item, lines)


def _slack_payload(*, headers: dict[str, str], body: bytes) -> dict[str, object]:
    content_type = headers.get("content-type", "")
    if content_type.startswith("application/x-www-form-urlencoded"):
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=False)
        payload_values = parsed.get("payload")
        if not payload_values:
            raise ValueError("Slack form-encoded payload must include a payload field.")
        raw_payload = payload_values[0]
        payload = json.loads(raw_payload)
        if not isinstance(payload, dict):
            raise ValueError("Slack payload must decode to a JSON object.")
        return payload
    return _json_object(body)


def _slack_approval_review_payload(payload: dict[str, object]) -> dict[str, object]:
    actions = payload.get("actions")
    action = actions[0] if isinstance(actions, list) and actions and isinstance(actions[0], dict) else {}
    metadata = {}
    raw_value = _string_value(_dict_value(action).get("value"))
    if raw_value:
        try:
            parsed_value = json.loads(raw_value)
        except JSONDecodeError:
            parsed_value = None
        if isinstance(parsed_value, dict):
            metadata = parsed_value
    decision = (
        _string_value(_dict_value(action).get("action_id"))
        or _string_value(_dict_value(metadata).get("decision"))
        or raw_value
        or _string_value(payload.get("decision"))
    ).lower()
    if decision not in {ApprovalDecision.APPROVE.value, ApprovalDecision.REJECT.value}:
        raise ValueError("Slack approval payload must include an approve or reject decision.")
    actor = (
        _string_value(_dict_value(payload.get("user")).get("username"))
        or _string_value(_dict_value(payload.get("user")).get("name"))
        or _string_value(_dict_value(metadata).get("actor"))
        or _string_value(payload.get("actor"))
    )
    if not actor:
        raise ValueError("Slack approval payload must include a reviewer actor.")
    team = (
        _string_value(_dict_value(metadata).get("team"))
        or _string_value(payload.get("team"))
        or _string_value(payload.get("team_name"))
        or _string_value(_dict_value(payload.get("team")).get("domain"))
        or _string_value(_dict_value(payload.get("team")).get("name"))
    )
    if not team:
        raise ValueError("Slack approval payload must include a reviewer team.")
    approval_id = (
        _string_value(_dict_value(metadata).get("approval_id"))
        or _string_value(payload.get("approval_id"))
        or _string_value(payload.get("callback_id"))
    )
    if not approval_id:
        raise ValueError("Slack approval payload must include an approval identifier.")
    return {
        "approval_id": approval_id,
        "actor": actor,
        "team": team,
        "decision": decision,
        "comment": _string_value(_dict_value(metadata).get("comment")) or _string_value(payload.get("comment")),
    }


def _principal_to_dict(principal: AuthenticatedPrincipal) -> dict[str, object]:
    return {
        "subject": principal.subject,
        "actor": principal.actor,
        "subject_type": principal.subject_type.value,
        "issuer": principal.issuer,
        "team": principal.team,
        "groups": principal.groups,
        "scopes": principal.scopes,
        "tenant_ids": principal.tenant_ids,
        "issued_at": principal.issued_at,
        "expires_at": principal.expires_at,
    }


def _string_value(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _bearer_token(value: str) -> str:
    if not isinstance(value, str):
        return ""
    if value.startswith("Bearer "):
        return value.removeprefix("Bearer ").strip()
    return ""


def _optional_string(value: object) -> str | None:
    result = _string_value(value)
    return result or None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _required_string(payload: dict[str, object], key: str) -> str:
    value = _string_value(payload.get(key))
    if not value:
        raise ValueError(f"Missing required string field: {key}")
    return value


def _required_int(payload: dict[str, object], key: str, *, default: int | None = None) -> int:
    value = payload.get(key)
    if value is None:
        if default is not None:
            return default
        raise ValueError(f"Missing required integer field: {key}")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Field must be an integer: {key}")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Optional integer field must be an integer when provided.")
    return value


def _bool_value(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError("Boolean field must be true or false.")
    return value


def _optional_path(value: object) -> Path | None:
    raw = _optional_string(value)
    return Path(raw).resolve() if raw is not None else None


def _int_param(
    query: dict[str, list[str]],
    key: str,
    default: int,
    *,
    min_value: int,
) -> int:
    raw = query.get(key, [str(default)])[0]
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"Query parameter must be an integer: {key}") from exc
    if value < min_value:
        comparator = "greater than zero" if min_value == 1 else f"greater than or equal to {min_value}"
        raise ValueError(f"Query parameter must be {comparator}: {key}")
    return value


def _queue_status_param(query: dict[str, list[str]]) -> QueueJobStatus | None:
    raw = query.get("status", [None])[0]
    return QueueJobStatus(raw) if raw is not None else None


def _queue_type_param(query: dict[str, list[str]]) -> QueueJobType | None:
    raw = query.get("job_type", [None])[0]
    return QueueJobType(raw) if raw is not None else None


def _approval_status_param(query: dict[str, list[str]]) -> ApprovalStatus | None:
    raw = query.get("status", [None])[0]
    return ApprovalStatus(raw) if raw is not None else None


def _query_string(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    return _optional_string(values[0])


def _required_query_string(query: dict[str, list[str]], key: str) -> str:
    value = _query_string(query, key)
    if value is None:
        raise ValueError(f"Query parameter '{key}' is required.")
    return value


def _tail(path: str, prefix: str) -> str:
    tail = path.removeprefix(prefix)
    return tail.strip("/")


def _run_record_to_dict(record: object) -> dict[str, object]:
    assert isinstance(record, RunRecord)
    return {
        "run_id": record.run_id,
        "created_at": record.created_at,
        "repo_full_name": record.repo_full_name,
        "issue_number": record.issue_number,
        "status": record.status.value,
        "planner_provider": record.planner_provider.value,
        "summary": record.summary,
    }


def _patch_proposal_record_to_dict(record: object) -> dict[str, object]:
    assert isinstance(record, PatchProposalRecord)
    return {
        "proposal_id": record.proposal_id,
        "created_at": record.created_at,
        "linked_run_id": record.linked_run_id,
        "provider": record.provider.value,
        "summary": record.summary,
    }


def _execution_record_to_dict(record: object) -> dict[str, object]:
    assert isinstance(record, PatchExecutionRecord)
    return {
        "execution_id": record.execution_id,
        "created_at": record.created_at,
        "proposal_id": record.proposal_id,
        "linked_run_id": record.linked_run_id,
        "mode": record.mode.value,
        "status": record.status.value,
        "summary": record.summary,
    }


def _verification_record_to_dict(record: object) -> dict[str, object]:
    assert isinstance(record, VerificationRecord)
    return {
        "verification_id": record.verification_id,
        "created_at": record.created_at,
        "linked_run_id": record.linked_run_id,
        "linked_execution_id": record.linked_execution_id,
        "status": record.status.value,
        "stop_reason": record.stop_reason.value,
        "summary": record.summary,
    }


def _autofix_record_to_dict(record: object) -> dict[str, object]:
    assert isinstance(record, AutofixRunRecord)
    return {
        "autofix_id": record.autofix_id,
        "linked_run_id": record.linked_run_id,
        "status": record.status.value,
        "attempt_count": record.attempt_count,
        "max_attempts": record.max_attempts,
        "summary": record.summary,
        "updated_at": record.updated_at,
    }


def _autofix_attempt_record_to_dict(record: object) -> dict[str, object]:
    assert isinstance(record, AutofixAttemptRecord)
    return {
        "attempt_id": record.attempt_id,
        "attempt_index": record.attempt_index,
        "status": record.status.value,
        "summary": record.summary,
        "proposal_id": record.proposal_id,
        "execution_id": record.execution_id,
        "verification_id": record.verification_id,
        "verification_stop_reason": None
        if record.verification_stop_reason is None
        else record.verification_stop_reason.value,
        "created_at": record.created_at,
    }


def _sandbox_record_to_dict(record: object) -> dict[str, object]:
    assert isinstance(record, SandboxRecord)
    return {
        "sandbox_id": record.sandbox_id,
        "linked_run_id": record.linked_run_id,
        "linked_autofix_id": record.linked_autofix_id,
        "status": record.status.value,
        "workspace_root": str(record.workspace_root),
        "copied_file_count": record.copied_file_count,
        "skipped_entry_count": record.skipped_entry_count,
        "updated_at": record.updated_at,
        "summary": record.summary,
    }


def _approval_record_to_dict(record: object) -> dict[str, object]:
    assert isinstance(record, ApprovalRecord)
    return {
        "approval_id": record.approval_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "repo_full_name": record.repo_full_name,
        "status": record.status.value,
        "risk_level": record.risk_level.value,
        "required_approvals": record.required_approvals,
        "approved_count": record.approved_count,
        "summary": record.summary,
    }


def _delivery_record_to_dict(record: object) -> dict[str, object]:
    assert isinstance(record, DeliveryRecord)
    return {
        "delivery_id": record.delivery_id,
        "created_at": record.created_at,
        "linked_run_id": record.linked_run_id,
        "linked_execution_id": record.linked_execution_id,
        "linked_verification_id": record.linked_verification_id,
        "status": record.status.value,
        "repo_full_name": record.repo_full_name,
        "branch_name": record.branch_name,
        "base_branch": record.base_branch,
        "summary": record.summary,
    }


def _queue_job_record_to_dict(record: object) -> dict[str, object]:
    assert isinstance(record, QueueJobRecord)
    return {
        "job_id": record.job_id,
        "job_type": record.job_type.value,
        "status": record.status.value,
        "repo_full_name": record.repo_full_name,
        "issue_number": record.issue_number,
        "priority": record.priority,
        "attempt_count": record.attempt_count,
        "max_attempts": record.max_attempts,
        "summary": record.summary,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "concurrency_key": record.concurrency_key,
        "required_worker_tags": record.required_worker_tags,
        "lease_token": record.lease_token,
        "lease_expires_at": record.lease_expires_at,
        "rehydration_count": record.rehydration_count,
    }


def _queue_attempt_record_to_dict(record: object) -> dict[str, object]:
    assert isinstance(record, QueueAttemptRecord)
    return {
        "attempt_id": record.attempt_id,
        "job_id": record.job_id,
        "attempt_index": record.attempt_index,
        "created_at": record.created_at,
        "finished_at": record.finished_at,
        "worker_id": record.worker_id,
        "status": record.status.value,
        "summary": record.summary,
        "error_message": record.error_message,
    }


def _notification_record_to_dict(record: object) -> dict[str, object]:
    assert isinstance(record, NotificationRecord)
    return {
        "notification_id": record.notification_id,
        "created_at": record.created_at,
        "tenant_id": record.tenant_id,
        "event_type": record.event_type.value,
        "status": record.status.value,
        "summary": record.summary,
    }


def _alert_record_to_dict(record: object) -> dict[str, object]:
    assert isinstance(record, AlertRecord)
    return {
        "alert_id": record.alert_id,
        "created_at": record.created_at,
        "tenant_id": record.tenant_id,
        "severity": record.severity.value,
        "source": record.source,
        "status": record.status.value,
        "summary": record.summary,
    }


def _trace_event_record_to_dict(record: object) -> dict[str, object]:
    assert isinstance(record, TraceEventRecord)
    return {
        "event_id": record.event_id,
        "trace_id": record.trace_id,
        "recorded_at": record.recorded_at,
        "source": record.source,
        "span_name": record.span_name,
        "status": record.status,
        "linked_run_id": record.linked_run_id,
        "linked_job_id": record.linked_job_id,
    }


def _dashboard_summary_to_dict(summary: DashboardSummary) -> dict[str, object]:
    return {
        "tenant_id": summary.tenant_id,
        "tenant_name": summary.tenant_name,
        "generated_at": summary.generated_at,
        "run_counts": summary.run_counts,
        "approval_counts": summary.approval_counts,
        "delivery_counts": summary.delivery_counts,
        "notification_counts": summary.notification_counts,
        "pending_approvals": [
            {
                "record_type": item.record_type,
                "record_id": item.record_id,
                "created_at": item.created_at,
                "status": item.status,
                "summary": item.summary,
            }
            for item in summary.pending_approvals
        ],
        "recent_deliveries": [
            {
                "record_type": item.record_type,
                "record_id": item.record_id,
                "created_at": item.created_at,
                "status": item.status,
                "summary": item.summary,
            }
            for item in summary.recent_deliveries
        ],
        "recent_notifications": [
            {
                "record_type": item.record_type,
                "record_id": item.record_id,
                "created_at": item.created_at,
                "status": item.status,
                "summary": item.summary,
            }
            for item in summary.recent_notifications
        ],
    }
