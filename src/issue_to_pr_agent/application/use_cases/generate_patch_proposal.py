from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path

from ...agents.patcher.base import PatcherClient
from ...application.services.evaluation import PatchProposalEvaluator
from ...application.services.proposal_template import ProposalTemplateBuilder
from ...domain.entities import (
    AgentPlan,
    EvaluationScore,
    IndexedSymbol,
    IssueContext,
    PatchFileContext,
    PatchProposal,
    PatchProposalRecord,
    PlanningContext,
    PatcherProvider,
    RankedFile,
    RepositoryIndex,
    RepositoryProfile,
)
from ...infrastructure.persistence.run_repository import RunRepository


@dataclass(frozen=True)
class PatchProposalGenerationResult:
    proposal_id: str
    proposal: PatchProposal
    proposal_path: Path


class GeneratePatchProposalUseCase:
    def __init__(
        self,
        run_repository: RunRepository,
        patcher: PatcherClient,
        *,
        template_builder: ProposalTemplateBuilder | None = None,
        evaluator: PatchProposalEvaluator | None = None,
        max_files: int = 8,
        max_file_chars: int = 4000,
    ) -> None:
        self._run_repository = run_repository
        self._patcher = patcher
        self._template_builder = template_builder or ProposalTemplateBuilder()
        self._evaluator = evaluator or PatchProposalEvaluator()
        self._max_files = max_files
        self._max_file_chars = max_file_chars

    def generate(
        self,
        *,
        run_id: str,
        repo_root: Path,
        objective: str | None = None,
    ) -> PatchProposalGenerationResult:
        run = self._run_repository.get_run(run_id)
        if run is None:
            raise ValueError(f"Run not found: {run_id}")
        record, payload = run
        issue = _issue_from_payload(payload)
        plan = _plan_from_payload(payload)
        planning_context = _planning_context_from_payload(payload)
        template = self._template_builder.build(run_id=run_id, payload=payload)
        allowed_existing_paths = _string_list(template.get("allowed_existing_paths"))
        suggested_new_dirs = _string_list(template.get("suggested_new_file_directories"))
        files = self._load_file_contexts(
            repo_root=repo_root,
            plan=plan,
            planning_context=planning_context,
        )
        if not files:
            raise ValueError("Patch generation requires at least one existing file context.")

        generated = self._patcher.generate(
            linked_run_id=run_id,
            issue=issue,
            plan=plan,
            planning_context=planning_context,
            repo_root=repo_root,
            files=files,
            allowed_existing_paths=allowed_existing_paths,
            suggested_new_file_directories=suggested_new_dirs,
            objective=objective,
        )
        proposal = PatchProposal(
            proposal_id=generated.proposal_id,
            summary=generated.summary,
            linked_run_id=run_id,
            rationale=generated.rationale,
            operations=generated.operations,
        )
        self._validate_generated_proposal(
            proposal=proposal,
            allowed_existing_paths=allowed_existing_paths,
            suggested_new_file_directories=suggested_new_dirs,
        )
        evaluation = self._evaluator.evaluate(proposal=proposal, planning_context=planning_context)

        created_at = datetime.now(timezone.utc).isoformat()
        proposal_path = record.audit_path.parent / "patch-proposals" / f"{proposal.proposal_id}.json"
        proposal_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_payload = {
            "proposal_id": proposal.proposal_id,
            "created_at": created_at,
            "linked_run_id": run_id,
            "provider": self._patcher.provider.value,
            "model": getattr(self._patcher, "model_name", None),
            "summary": proposal.summary,
            "rationale": proposal.rationale,
            "evaluation": {
                "score": evaluation.score,
                "summary": evaluation.summary,
                "reasons": evaluation.reasons,
            },
            "proposal_path": str(proposal_path),
            "allowed_existing_paths": allowed_existing_paths,
            "suggested_new_file_directories": suggested_new_dirs,
            "context_files": [item.path for item in files],
            "operations": [
                {
                    "type": item.type.value,
                    "path": item.path,
                    "content": item.content,
                    "find_text": item.find_text,
                    "replace_text": item.replace_text,
                    "allow_overwrite": item.allow_overwrite,
                }
                for item in proposal.operations
            ],
        }
        proposal_path.write_text(json.dumps(artifact_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._run_repository.save_patch_proposal(
            PatchProposalRecord(
                proposal_id=proposal.proposal_id,
                created_at=created_at,
                linked_run_id=run_id,
                provider=self._patcher.provider,
                summary=proposal.summary,
                proposal_path=proposal_path,
            ),
            artifact_payload,
        )
        return PatchProposalGenerationResult(
            proposal_id=proposal.proposal_id,
            proposal=proposal,
            proposal_path=proposal_path,
        )

    def _load_file_contexts(
        self,
        *,
        repo_root: Path,
        plan: AgentPlan,
        planning_context: PlanningContext,
    ) -> list[PatchFileContext]:
        candidate_paths = _dedupe(
            plan.files_to_inspect
            + [item.path for item in planning_context.ranked_files]
        )
        files: list[PatchFileContext] = []
        for relative_path in candidate_paths:
            if len(files) >= self._max_files:
                break
            target = repo_root / relative_path
            if not target.exists() or not target.is_file():
                continue
            content = target.read_text(encoding="utf-8", errors="replace")
            files.append(
                PatchFileContext(
                    path=relative_path,
                    exists=True,
                    content=content[: self._max_file_chars],
                    preview=content[: min(self._max_file_chars, 500)],
                )
            )
        return files

    def _validate_generated_proposal(
        self,
        *,
        proposal: PatchProposal,
        allowed_existing_paths: list[str],
        suggested_new_file_directories: list[str],
    ) -> None:
        if not proposal.operations:
            raise ValueError("Generated patch proposal does not contain any operations.")
        allowed_existing = set(allowed_existing_paths)
        allowed_new_dirs = set(suggested_new_file_directories)
        allowed_new_dirs.add("tests")
        for operation in proposal.operations:
            if operation.type.value in {"replace_text", "append_text"} and operation.path not in allowed_existing:
                raise ValueError(
                    f"Generated proposal references an existing path outside the allowed scope: {operation.path}"
                )
            if operation.type.value == "write_file":
                parent = str(Path(operation.path).parent)
                if parent == ".":
                    parent = ""
                if parent not in allowed_new_dirs and not parent.startswith("tests"):
                    raise ValueError(
                        f"Generated proposal writes outside the suggested directories: {operation.path}"
                    )


def _issue_from_payload(payload: dict[str, object]) -> IssueContext:
    issue = payload.get("issue")
    if not isinstance(issue, dict):
        raise ValueError("Run payload does not contain an issue context.")
    return IssueContext(
        repo_full_name=_required_string(issue, "repo_full_name"),
        issue_number=_required_int(issue, "issue_number"),
        title=_required_string(issue, "title"),
        body=_string_value(issue.get("body")),
        labels=_string_list(issue.get("labels")),
        url=_required_string(issue, "url"),
    )


def _plan_from_payload(payload: dict[str, object]) -> AgentPlan:
    plan = payload.get("plan")
    if not isinstance(plan, dict):
        raise ValueError("Run payload does not contain a plan.")
    return AgentPlan.from_dict(plan)


def _planning_context_from_payload(payload: dict[str, object]) -> PlanningContext:
    context = payload.get("planning_context")
    if not isinstance(context, dict):
        raise ValueError("Run payload does not contain a planning context.")
    profile_payload = context.get("repository_profile")
    profile = RepositoryProfile(primary_language="unknown")
    repository_index = RepositoryIndex()
    evaluation = EvaluationScore(score=0, summary="Planning context evaluation unavailable.")
    if isinstance(profile_payload, dict):
        profile = RepositoryProfile(
            primary_language=_string_value(profile_payload.get("primary_language")) or "unknown",
            detected_languages=_string_list(profile_payload.get("detected_languages")),
            detected_frameworks=_string_list(profile_payload.get("detected_frameworks")),
            build_systems=_string_list(profile_payload.get("build_systems")),
            test_commands=_string_list(profile_payload.get("test_commands")),
        )
    index_payload = context.get("repository_index")
    if isinstance(index_payload, dict):
        top_symbols: list[IndexedSymbol] = []
        top_symbols_payload = index_payload.get("top_symbols")
        if isinstance(top_symbols_payload, list):
            for item in top_symbols_payload:
                if not isinstance(item, dict):
                    continue
                name = _string_value(item.get("name"))
                kind = _string_value(item.get("kind"))
                path = _string_value(item.get("path"))
                if not name or not kind or not path:
                    continue
                line = item.get("line")
                if not isinstance(line, int):
                    line = 1
                top_symbols.append(
                    IndexedSymbol(
                        name=name,
                        kind=kind,
                        path=path,
                        line=line,
                        signature=_string_value(item.get("signature")),
                    )
                )
        repository_index = RepositoryIndex(
            files_indexed=item_int(index_payload.get("files_indexed")),
            symbol_count=item_int(index_payload.get("symbol_count")),
            top_symbols=top_symbols,
            complexity_score=item_int(index_payload.get("complexity_score")),
            index_version=_string_value(index_payload.get("index_version")) or "v1",
        )
    evaluation_payload = context.get("evaluation")
    if isinstance(evaluation_payload, dict):
        evaluation = EvaluationScore(
            score=item_int(evaluation_payload.get("score")),
            summary=_string_value(evaluation_payload.get("summary")) or "Planning context evaluation unavailable.",
            reasons=_string_list(evaluation_payload.get("reasons")),
        )
    ranked_files_payload = context.get("ranked_files")
    ranked_files: list[RankedFile] = []
    if isinstance(ranked_files_payload, list):
        for item in ranked_files_payload:
            if not isinstance(item, dict):
                continue
            path = _string_value(item.get("path"))
            if not path:
                continue
            ranked_files.append(
                RankedFile(
                    path=path,
                    score=int(item.get("score", 0)) if isinstance(item.get("score"), int) else 0,
                    reasons=_string_list(item.get("reasons")),
                    preview=_string_value(item.get("preview")),
                )
            )
    return PlanningContext(
        summary=_string_value(context.get("summary")) or "",
        issue_keywords=_string_list(context.get("issue_keywords")),
        repository_profile=profile,
        repository_index=repository_index,
        evaluation=evaluation,
        ranked_files=ranked_files,
        suggested_test_commands=_string_list(context.get("suggested_test_commands")),
    )


def _string_value(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _required_string(data: dict[str, object], key: str) -> str:
    value = _string_value(data.get(key))
    if not value:
        raise ValueError(f"Run payload is missing required field: {key}")
    return value


def _required_int(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise ValueError(f"Run payload is missing required integer field: {key}")
    return value


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def item_int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
