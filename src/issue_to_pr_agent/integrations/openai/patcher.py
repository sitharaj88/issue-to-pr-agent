from __future__ import annotations

import json
from pathlib import Path
from urllib import error, request

from ...agents.patcher.base import PatcherClient
from ...application.services.model_routing import ModelRoutingService
from ...domain.entities import AgentPlan, IssueContext, PatchFileContext, PatchProposal, PatcherProvider, PlanningContext
from ...infrastructure.config.settings import Settings


class OpenAIPatcher(PatcherClient):
    provider = PatcherProvider.OPENAI

    def __init__(self, settings: Settings) -> None:
        settings.require_openai()
        self._settings = settings
        self._router = ModelRoutingService(settings)
        self.model_name = settings.openai_model

    def generate(
        self,
        *,
        linked_run_id: str,
        issue: IssueContext,
        plan: AgentPlan,
        planning_context: PlanningContext,
        repo_root: Path,
        files: list[PatchFileContext],
        allowed_existing_paths: list[str],
        suggested_new_file_directories: list[str],
        objective: str | None = None,
    ) -> PatchProposal:
        decision = self._router.select_patch_model(planning_context=planning_context, files=files)
        self.model_name = decision.model_name
        prompt = self._build_prompt(
            linked_run_id=linked_run_id,
            issue=issue,
            plan=plan,
            planning_context=planning_context,
            repo_root=repo_root,
            files=files,
            allowed_existing_paths=allowed_existing_paths,
            suggested_new_file_directories=suggested_new_file_directories,
            objective=objective,
        )
        raw = self._complete(prompt, model_name=decision.model_name)
        payload = _extract_json_object(raw)
        return PatchProposal.from_dict(payload)

    def _build_prompt(
        self,
        *,
        linked_run_id: str,
        issue: IssueContext,
        plan: AgentPlan,
        planning_context: PlanningContext,
        repo_root: Path,
        files: list[PatchFileContext],
        allowed_existing_paths: list[str],
        suggested_new_file_directories: list[str],
        objective: str | None,
    ) -> str:
        data = {
            "linked_run_id": linked_run_id,
            "repo_root": str(repo_root),
            "objective": objective or "",
            "issue": {
                "repo_full_name": issue.repo_full_name,
                "issue_number": issue.issue_number,
                "title": issue.title,
                "body": issue.body,
                "labels": issue.labels,
                "url": issue.url,
            },
            "plan": {
                "summary": plan.summary,
                "assumptions": plan.assumptions,
                "files_to_inspect": plan.files_to_inspect,
                "tests": plan.tests,
                "risks": plan.risks,
            },
            "planning_context": {
                "summary": planning_context.summary,
                "issue_keywords": planning_context.issue_keywords,
                "suggested_test_commands": planning_context.suggested_test_commands,
            },
            "allowed_existing_paths": allowed_existing_paths,
            "suggested_new_file_directories": suggested_new_file_directories,
            "files": [
                {
                    "path": item.path,
                    "exists": item.exists,
                    "preview": item.preview,
                    "content": item.content,
                }
                for item in files
            ],
        }
        schema = {
            "proposal_id": "string",
            "linked_run_id": linked_run_id,
            "summary": "string",
            "rationale": "string",
            "operations": [
                {
                    "type": "replace_text|append_text|write_file",
                    "path": "string",
                    "find_text": "string when replace_text",
                    "replace_text": "string when replace_text",
                    "content": "string when append_text or write_file",
                    "allow_overwrite": "boolean only for write_file",
                }
            ],
        }
        return "\n".join(
            [
                "You are generating a minimal patch proposal for a codebase agent.",
                "Return only JSON. Do not use markdown fences.",
                "Use only these operations: replace_text, append_text, write_file.",
                "For replace_text, find_text must match exactly a contiguous substring from the provided file content.",
                "Prefer the smallest safe patch that addresses the issue and adds or updates tests when possible.",
                "Only modify paths listed in allowed_existing_paths, unless creating a new file inside suggested_new_file_directories or tests/.",
                "Do not invent file paths outside the allowed directories.",
                "The JSON must match this schema exactly:",
                json.dumps(schema, indent=2),
                "",
                json.dumps(data, indent=2),
            ]
        )

    def _complete(self, prompt: str, *, model_name: str) -> str:
        url = f"{self._settings.openai_base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": model_name,
            "temperature": 0.1,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a senior software engineer that returns strict JSON patch proposals.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        req = request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=90) as response:
                body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI request failed: {exc.code} {details}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"OpenAI request failed: {exc.reason}") from exc

        response_payload = json.loads(body)
        try:
            content = response_payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("OpenAI response had an unexpected structure.") from exc

        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            return "\n".join(part for part in text_parts if part)
        raise RuntimeError("OpenAI response content was not text.")


def _extract_json_object(text: str) -> dict[str, object]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError("Patcher output did not contain a JSON object.")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise RuntimeError("Patcher output did not decode to a JSON object.")
    return data
