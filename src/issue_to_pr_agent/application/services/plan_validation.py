from __future__ import annotations

from dataclasses import replace

from ...domain.entities import AgentPlan, PlanningContext, RepoSnapshot


class PlanValidator:
    def __init__(self, *, max_files_to_inspect: int = 8, max_commands: int = 8, max_tests: int = 4) -> None:
        self._max_files_to_inspect = max_files_to_inspect
        self._max_commands = max_commands
        self._max_tests = max_tests

    def normalize(
        self,
        plan: AgentPlan,
        repo_snapshot: RepoSnapshot,
        planning_context: PlanningContext,
    ) -> AgentPlan:
        known_paths = set(repo_snapshot.tracked_files)
        risks = list(plan.risks)

        files_to_inspect = _dedupe(plan.files_to_inspect)
        unknown_files = [path for path in files_to_inspect if path not in known_paths]
        if unknown_files:
            risks.append(
                "Planner proposed files not present in the repository snapshot and they were removed: "
                + ", ".join(unknown_files[:4])
            )
        files_to_inspect = [path for path in files_to_inspect if path in known_paths]
        if not files_to_inspect:
            files_to_inspect = [item.path for item in planning_context.ranked_files[: self._max_files_to_inspect]]
        files_to_inspect = files_to_inspect[: self._max_files_to_inspect]

        tests = _dedupe(plan.tests)
        if not tests:
            tests = planning_context.suggested_test_commands[: self._max_tests]
        tests = tests[: self._max_tests]

        commands = _dedupe(plan.commands)[: self._max_commands]
        assumptions = _dedupe(plan.assumptions)
        if planning_context.repository_profile.primary_language != "unknown":
            assumptions.append(
                f"Repository profile suggests primary language '{planning_context.repository_profile.primary_language}'."
            )
        risks = _dedupe(risks)

        return replace(
            plan,
            assumptions=assumptions,
            files_to_inspect=files_to_inspect,
            commands=commands,
            tests=tests,
            risks=risks,
        )


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
