from __future__ import annotations

import re

from ...domain.entities import AgentPlan, IssueContext, PlannerProvider, PlanningContext, RepoSnapshot
from .base import PlannerClient


class HeuristicPlanner(PlannerClient):
    provider = PlannerProvider.HEURISTIC
    model_name = None

    def plan(
        self,
        issue: IssueContext,
        repo_snapshot: RepoSnapshot,
        planning_context: PlanningContext,
        objective: str | None = None,
    ) -> AgentPlan:
        branch_name = _slugify(f"issue-{issue.issue_number}-{issue.title}")[:48]
        summary = objective or (
            f"Investigate and implement a fix for {issue.repo_full_name}#{issue.issue_number}. "
            f"{planning_context.summary}"
        )
        files_to_inspect = [item.path for item in planning_context.ranked_files[:8]] or repo_snapshot.tracked_files[:10]
        keyword = planning_context.issue_keywords[0] if planning_context.issue_keywords else "TODO"
        commands = [
            "git status --short",
            f"rg -n \"{keyword}\" .",
        ]
        if repo_snapshot.is_git_repo:
            commands.append("git diff --stat")
        tests = planning_context.suggested_test_commands[:2] or ["python3 -m unittest discover -s tests -v"]
        pr_title = f"Fix #{issue.issue_number}: {issue.title}"
        pr_body = "\n".join(
            [
                "## Summary",
                f"- Addresses issue #{issue.issue_number}: {issue.title}",
                f"- Repository context: {planning_context.summary}",
                "- Adds the minimum change necessary once root cause is confirmed",
                "",
                "## Validation",
                "- Run the relevant automated tests",
                "- Verify the affected workflow manually if needed",
            ]
        )
        assumptions = [
            "The local checkout matches the repository state you want to work on.",
            "The issue body contains enough context to start a focused investigation.",
            f"Primary language is likely '{planning_context.repository_profile.primary_language}'.",
        ]
        risks = []
        if repo_snapshot.is_dirty:
            risks.append("Local repository has uncommitted changes that may affect branch safety.")
        if not planning_context.ranked_files:
            risks.append("Repository context ranking did not identify strong file candidates.")
        return AgentPlan(
            summary=summary,
            assumptions=assumptions,
            files_to_inspect=files_to_inspect,
            commands=commands,
            tests=tests,
            branch_name=f"agent/{branch_name or 'issue-work'}",
            pr_title=pr_title,
            pr_body=pr_body,
            risks=risks,
        )


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return cleaned or "issue-work"
