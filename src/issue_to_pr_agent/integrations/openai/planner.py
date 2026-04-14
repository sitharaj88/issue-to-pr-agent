from __future__ import annotations

import json
from urllib import error, request

from ...agents.planner.base import PlannerClient
from ...application.services.model_routing import ModelRoutingService
from ...domain.entities import AgentPlan, IssueContext, PlannerProvider, PlanningContext, RepoSnapshot
from ...infrastructure.config.settings import Settings


class OpenAIPlanner(PlannerClient):
    provider = PlannerProvider.OPENAI

    def __init__(self, settings: Settings) -> None:
        settings.require_openai()
        self._settings = settings
        self._router = ModelRoutingService(settings)
        self.model_name = settings.openai_model

    def plan(
        self,
        issue: IssueContext,
        repo_snapshot: RepoSnapshot,
        planning_context: PlanningContext,
        objective: str | None = None,
    ) -> AgentPlan:
        decision = self._router.select_planner_model(planning_context=planning_context)
        self.model_name = decision.model_name
        prompt = self._build_prompt(issue, repo_snapshot, planning_context, objective)
        raw = self._complete(prompt, model_name=decision.model_name)
        payload = _extract_json_object(raw)
        return AgentPlan.from_dict(payload)

    def _build_prompt(
        self,
        issue: IssueContext,
        repo_snapshot: RepoSnapshot,
        planning_context: PlanningContext,
        objective: str | None,
    ) -> str:
        data = {
            "issue": {
                "repo_full_name": issue.repo_full_name,
                "issue_number": issue.issue_number,
                "title": issue.title,
                "body": issue.body,
                "labels": issue.labels,
                "url": issue.url,
            },
            "repo_snapshot": {
                "root": str(repo_snapshot.root),
                "is_git_repo": repo_snapshot.is_git_repo,
                "branch": repo_snapshot.branch,
                "status_short": repo_snapshot.status_short,
                "tracked_files": repo_snapshot.tracked_files[:30],
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
                "ranked_files": [
                    {
                        "path": item.path,
                        "score": item.score,
                        "reasons": item.reasons,
                        "preview": item.preview,
                    }
                    for item in planning_context.ranked_files[:8]
                ],
                "suggested_test_commands": planning_context.suggested_test_commands,
            },
            "objective": objective or "",
        }
        schema = {
            "summary": "string",
            "assumptions": ["string"],
            "files_to_inspect": ["string"],
            "commands": ["string"],
            "tests": ["string"],
            "branch_name": "string",
            "pr_title": "string",
            "pr_body": "string",
            "risks": ["string"],
        }
        return "\n".join(
            [
                "You are planning the next implementation pass for a GitHub issue workflow agent.",
                "Return only JSON. Do not use markdown fences.",
                "The JSON must match this schema exactly:",
                json.dumps(schema, indent=2),
                "Prefer files from planning_context.ranked_files when possible.",
                "Keep commands safe and specific to local inspection and testing.",
                "Use suggested_test_commands when they fit the repository profile.",
                "Avoid inventing file paths that are not in tracked_files or ranked_files.",
                "",
                json.dumps(data, indent=2),
            ]
        )

    def _complete(self, prompt: str, *, model_name: str) -> str:
        url = f"{self._settings.openai_base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": model_name,
            "temperature": 0.2,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a senior software engineer that returns strict JSON plans.",
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
            with request.urlopen(req, timeout=60) as response:
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
        raise RuntimeError("Planner output did not contain a JSON object.")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise RuntimeError("Planner output did not decode to a JSON object.")
    return data
