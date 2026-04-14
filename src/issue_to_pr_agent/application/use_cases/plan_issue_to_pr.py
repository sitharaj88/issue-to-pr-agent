from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from uuid import uuid4

from ...agents.context_builder.repository import RepositoryContextBuilder
from ...agents.planner.base import PlannerClient
from ...application.services.evaluation import PlanningEvaluator
from ...application.services.plan_validation import PlanValidator
from ...domain.entities import (
    AgentPlan,
    CommandAssessment,
    ExecutionMode,
    IssueContext,
    PlanningContext,
    RepoSnapshot,
    RunRecord,
    RunStatus,
)
from ...domain.policies.safety import SafetyPolicy
from ...infrastructure.persistence.run_repository import RunRepository
from ...infrastructure.scm.local_repo import LocalRepoInspector
from ...integrations.github.client import GitHubClient


@dataclass(frozen=True)
class AgentRunResult:
    run_id: str
    issue: IssueContext
    repo_snapshot: RepoSnapshot
    planning_context: PlanningContext
    plan: AgentPlan
    command_assessments: list[CommandAssessment]
    run_directory: Path
    report_path: Path
    pr_draft_path: Path
    audit_path: Path


class IssueToPRAgent:
    def __init__(
        self,
        github: GitHubClient,
        planner: PlannerClient,
        run_repository: RunRepository,
        safety_policy: SafetyPolicy,
        *,
        max_repo_files: int = 200,
        context_builder: RepositoryContextBuilder | None = None,
        plan_validator: PlanValidator | None = None,
        planning_evaluator: PlanningEvaluator | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._github = github
        self._planner = planner
        self._run_repository = run_repository
        self._safety_policy = safety_policy
        self._max_repo_files = max_repo_files
        self._context_builder = context_builder or RepositoryContextBuilder()
        self._plan_validator = plan_validator or PlanValidator()
        self._planning_evaluator = planning_evaluator or PlanningEvaluator()
        self._logger = logger or logging.getLogger(__name__)

    def run(
        self,
        *,
        repo_full_name: str,
        issue_number: int,
        repo_root: Path,
        output_dir: Path,
        objective: str | None = None,
        create_branch: bool = False,
    ) -> AgentRunResult:
        issue = self._github.fetch_issue(repo_full_name, issue_number)
        return self.run_with_issue(
            issue=issue,
            repo_root=repo_root,
            output_dir=output_dir,
            objective=objective,
            create_branch=create_branch,
        )

    def run_with_issue(
        self,
        *,
        issue: IssueContext,
        repo_root: Path,
        output_dir: Path,
        objective: str | None = None,
        create_branch: bool = False,
        external_ticket: dict[str, object] | None = None,
    ) -> AgentRunResult:
        run_id = uuid4().hex[:12]
        created_at = datetime.now(timezone.utc).isoformat()
        if not repo_root.exists():
            raise FileNotFoundError(f"Repository root does not exist: {repo_root}")
        if not repo_root.is_dir():
            raise NotADirectoryError(f"Repository root is not a directory: {repo_root}")

        run_directory = output_dir / "runs" / run_id
        run_directory.mkdir(parents=True, exist_ok=True)
        report_path = run_directory / "plan.md"
        pr_draft_path = run_directory / "pr.md"
        audit_path = run_directory / "run.json"
        execution_mode = ExecutionMode.PREPARE_BRANCH if create_branch else ExecutionMode.PLAN_ONLY

        snapshot: RepoSnapshot | None = None
        planning_context: PlanningContext | None = None
        plan: AgentPlan | None = None
        assessments: list[CommandAssessment] = []

        try:
            self._logger.info(
                "Starting issue planning run",
                extra={
                    "run_id": run_id,
                    "repo_full_name": issue.repo_full_name,
                    "issue_number": issue.issue_number,
                    "provider": self._planner.provider.value,
                },
            )
            inspector = LocalRepoInspector(repo_root)
            snapshot = inspector.snapshot(max_files=self._max_repo_files)
            planning_context = self._context_builder.build(issue, snapshot, objective)
            plan = self._planner.plan(issue, snapshot, planning_context, objective)
            plan = self._plan_validator.normalize(plan, snapshot, planning_context)
            planning_context = replace(
                planning_context,
                evaluation=self._planning_evaluator.evaluate(
                    planning_context=planning_context,
                    plan=plan,
                ),
            )
            assessments = self._safety_policy.assess_commands(plan.commands + plan.tests)

            if create_branch:
                self._safety_policy.ensure_branch_name(plan.branch_name)
                inspector.create_branch(plan.branch_name)

            report_path.write_text(
                _render_report(
                    issue,
                    snapshot,
                    planning_context,
                    plan,
                    assessments,
                    run_id,
                    execution_mode,
                    external_ticket=external_ticket,
                ),
                encoding="utf-8",
            )
            pr_draft_path.write_text(plan.pr_body.strip() + "\n", encoding="utf-8")
            payload = _build_audit_payload(
                run_id=run_id,
                created_at=created_at,
                issue=issue,
                snapshot=snapshot,
                planning_context=planning_context,
                plan=plan,
                assessments=assessments,
                execution_mode=execution_mode,
                planner_provider=self._planner.provider.value,
                planner_model=getattr(self._planner, "model_name", None),
                status=RunStatus.SUCCEEDED.value,
                report_path=report_path,
                pr_draft_path=pr_draft_path,
                audit_path=audit_path,
                error_message=None,
                external_ticket=external_ticket,
            )
            audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            record = RunRecord(
                run_id=run_id,
                created_at=created_at,
                repo_full_name=issue.repo_full_name,
                issue_number=issue.issue_number,
                planner_provider=self._planner.provider,
                execution_mode=execution_mode,
                status=RunStatus.SUCCEEDED,
                branch_name=plan.branch_name,
                summary=plan.summary,
                issue_url=issue.url,
                report_path=report_path,
                pr_draft_path=pr_draft_path,
                audit_path=audit_path,
            )
            self._run_repository.save_run(record, payload)
            return AgentRunResult(
                run_id=run_id,
                issue=issue,
                repo_snapshot=snapshot,
                planning_context=planning_context,
                plan=plan,
                command_assessments=assessments,
                run_directory=run_directory,
                report_path=report_path,
                pr_draft_path=pr_draft_path,
                audit_path=audit_path,
            )
        except Exception as exc:
            payload = {
                "run_id": run_id,
                "created_at": created_at,
                "repo_full_name": issue.repo_full_name,
                "issue_number": issue.issue_number,
                "planner_provider": self._planner.provider.value,
                "planner_model": getattr(self._planner, "model_name", None),
                "execution_mode": execution_mode.value,
                "status": RunStatus.FAILED.value,
                "error_message": str(exc),
                "issue_url": issue.url,
                "external_ticket": external_ticket,
                "report_path": str(report_path),
                "pr_draft_path": str(pr_draft_path),
                "audit_path": str(audit_path),
            }
            audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            record = RunRecord(
                run_id=run_id,
                created_at=created_at,
                repo_full_name=issue.repo_full_name,
                issue_number=issue.issue_number,
                planner_provider=self._planner.provider,
                execution_mode=execution_mode,
                status=RunStatus.FAILED,
                branch_name=plan.branch_name if plan else "",
                summary=plan.summary if plan else (objective or "Run failed before plan generation."),
                issue_url=issue.url,
                report_path=report_path,
                pr_draft_path=pr_draft_path,
                audit_path=audit_path,
                error_message=str(exc),
            )
            self._run_repository.save_run(record, payload)
            self._logger.exception(
                "Issue planning run failed",
                extra={
                    "run_id": run_id,
                    "repo_full_name": issue.repo_full_name,
                    "issue_number": issue.issue_number,
                    "provider": self._planner.provider.value,
                },
            )
            raise


def _render_report(
    issue: IssueContext,
    snapshot: RepoSnapshot,
    planning_context: PlanningContext,
    plan: AgentPlan,
    assessments: list[CommandAssessment],
    run_id: str,
    execution_mode: ExecutionMode,
    *,
    external_ticket: dict[str, object] | None = None,
) -> str:
    tracked_files = "\n".join(f"- `{path}`" for path in snapshot.tracked_files) or "- None"
    assumptions = "\n".join(f"- {item}" for item in plan.assumptions) or "- None"
    inspect_files = "\n".join(f"- `{path}`" for path in plan.files_to_inspect) or "- None"
    commands = "\n".join(f"- `{item}`" for item in plan.commands) or "- None"
    tests = "\n".join(f"- `{item}`" for item in plan.tests) or "- None"
    command_assessments = (
        "\n".join(
            f"- `{item.command}` -> `{item.decision.value}`: {item.reason}" for item in assessments
        )
        or "- None"
    )
    risks = "\n".join(f"- {item}" for item in plan.risks) or "- None"
    context_keywords = ", ".join(planning_context.issue_keywords) or "none"
    profile_languages = ", ".join(planning_context.repository_profile.detected_languages) or "unknown"
    profile_frameworks = ", ".join(planning_context.repository_profile.detected_frameworks) or "none"
    context_tests = "\n".join(f"- `{item}`" for item in planning_context.suggested_test_commands) or "- None"
    indexed_symbols = (
        "\n".join(
            f"- `{item.name}` ({item.kind}) in `{item.path}:{item.line}`"
            for item in planning_context.repository_index.top_symbols[:8]
        )
        or "- None"
    )
    ranked_files = (
        "\n".join(
            [
                f"- `{item.path}` (score={item.score})"
                + (f": {', '.join(item.reasons)}" if item.reasons else "")
                for item in planning_context.ranked_files
            ]
        )
        or "- None"
    )
    previews: list[str] = []
    for item in planning_context.ranked_files[:3]:
        if not item.preview:
            continue
        previews.extend(
            [
                f"### `{item.path}`",
                "",
                "```text",
                item.preview,
                "```",
                "",
            ]
        )
    status_block = snapshot.status_short or "(clean or not a git repository)"
    external_key = _string_value(external_ticket.get("key")) if external_ticket else ""
    external_system = _string_value(external_ticket.get("system")) if external_ticket else ""
    title_line = (
        f"# Ticket {external_key}: {issue.title}"
        if external_key
        else f"# Issue {issue.issue_number}: {issue.title}"
    )
    external_lines = (
        [
            "## External Ticket",
            "",
            f"- System: `{external_system or 'external'}`",
            f"- Key: `{external_key}`",
            f"- URL: {issue.url}",
            "",
        ]
        if external_key
        else []
    )

    return "\n".join(
        [
            title_line,
            "",
            f"- Run ID: `{run_id}`",
            f"- Repository: `{issue.repo_full_name}`",
            f"- Issue URL: {issue.url}",
            f"- Git repo detected: `{snapshot.is_git_repo}`",
            f"- Current branch: `{snapshot.branch or 'n/a'}`",
            f"- Execution mode: `{execution_mode.value}`",
            "",
            *external_lines,
            "## Issue Body",
            "",
            issue.body.strip() or "_No issue body provided._",
            "",
            "## Repo Snapshot",
            "",
            "### Git Status",
            "",
            "```text",
            status_block,
            "```",
            "",
            "### Tracked Files",
            "",
            tracked_files,
            "",
            "## Planning Context",
            "",
            planning_context.summary,
            "",
            "### Issue Keywords",
            "",
            f"- {context_keywords}",
            "",
            "### Repository Profile",
            "",
            f"- Primary language: `{planning_context.repository_profile.primary_language}`",
            f"- Detected languages: {profile_languages}",
            f"- Detected frameworks: {profile_frameworks}",
            f"- Complexity score: `{planning_context.repository_index.complexity_score}`",
            f"- Context evaluation: `{planning_context.evaluation.score}`",
            "",
            "### Indexed Symbols",
            "",
            indexed_symbols,
            "",
            "### Suggested Tests",
            "",
            context_tests,
            "",
            "### Ranked Files",
            "",
            ranked_files,
            "",
            "### Ranked File Previews",
            "",
            *previews,
            "## Plan Summary",
            "",
            plan.summary,
            "",
            "## Assumptions",
            "",
            assumptions,
            "",
            "## Risks",
            "",
            risks,
            "",
            "## Files To Inspect",
            "",
            inspect_files,
            "",
            "## Commands",
            "",
            commands,
            "",
            "## Tests",
            "",
            tests,
            "",
            "## Safety Review",
            "",
            command_assessments,
            "",
            "## Evaluation",
            "",
            planning_context.evaluation.summary,
            "",
            "\n".join(f"- {item}" for item in planning_context.evaluation.reasons) or "- None",
            "",
            "## PR Metadata",
            "",
            f"- Branch: `{plan.branch_name}`",
            f"- Title: {plan.pr_title}",
        ]
    )


def _build_audit_payload(
    *,
    run_id: str,
    created_at: str,
    issue: IssueContext,
    snapshot: RepoSnapshot,
    planning_context: PlanningContext,
    plan: AgentPlan,
    assessments: list[CommandAssessment],
    execution_mode: ExecutionMode,
    planner_provider: str,
    planner_model: str | None,
    status: str,
    report_path: Path,
    pr_draft_path: Path,
    audit_path: Path,
    error_message: str | None,
    external_ticket: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "created_at": created_at,
        "planner_provider": planner_provider,
        "planner_model": planner_model,
        "execution_mode": execution_mode.value,
        "status": status,
        "issue": {
            "repo_full_name": issue.repo_full_name,
            "issue_number": issue.issue_number,
            "title": issue.title,
            "body": issue.body,
            "labels": issue.labels,
            "url": issue.url,
        },
        "repo_snapshot": {
            "root": str(snapshot.root),
            "is_git_repo": snapshot.is_git_repo,
            "branch": snapshot.branch,
            "status_short": snapshot.status_short,
            "tracked_files": snapshot.tracked_files,
            "is_dirty": snapshot.is_dirty,
        },
        "planning_context": {
            "summary": planning_context.summary,
            "issue_keywords": planning_context.issue_keywords,
            "repository_profile": {
                "primary_language": planning_context.repository_profile.primary_language,
                "detected_languages": planning_context.repository_profile.detected_languages,
                "detected_frameworks": planning_context.repository_profile.detected_frameworks,
                "build_systems": planning_context.repository_profile.build_systems,
                "test_commands": planning_context.repository_profile.test_commands,
            },
            "repository_index": {
                "files_indexed": planning_context.repository_index.files_indexed,
                "symbol_count": planning_context.repository_index.symbol_count,
                "complexity_score": planning_context.repository_index.complexity_score,
                "index_version": planning_context.repository_index.index_version,
                "top_symbols": [
                    {
                        "name": item.name,
                        "kind": item.kind,
                        "path": item.path,
                        "line": item.line,
                        "signature": item.signature,
                    }
                    for item in planning_context.repository_index.top_symbols
                ],
            },
            "evaluation": {
                "score": planning_context.evaluation.score,
                "summary": planning_context.evaluation.summary,
                "reasons": planning_context.evaluation.reasons,
            },
            "ranked_files": [
                {
                    "path": item.path,
                    "score": item.score,
                    "reasons": item.reasons,
                    "preview": item.preview,
                }
                for item in planning_context.ranked_files
            ],
            "suggested_test_commands": planning_context.suggested_test_commands,
        },
        "plan": {
            "summary": plan.summary,
            "assumptions": plan.assumptions,
            "files_to_inspect": plan.files_to_inspect,
            "commands": plan.commands,
            "tests": plan.tests,
            "branch_name": plan.branch_name,
            "pr_title": plan.pr_title,
            "pr_body": plan.pr_body,
            "risks": plan.risks,
        },
        "command_assessments": [
            {
                "command": item.command,
                "decision": item.decision.value,
                "reason": item.reason,
            }
            for item in assessments
        ],
        "artifacts": {
            "report_path": str(report_path),
            "pr_draft_path": str(pr_draft_path),
            "audit_path": str(audit_path),
        },
        "external_ticket": external_ticket,
        "error_message": error_message,
    }


def _string_value(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
