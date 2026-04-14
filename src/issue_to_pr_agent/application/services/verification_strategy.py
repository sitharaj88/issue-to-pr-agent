from __future__ import annotations

from ...domain.entities import TestCommandCandidate


class VerificationStrategyResolver:
    def resolve(self, payload: dict[str, object]) -> list[TestCommandCandidate]:
        plan = payload.get("plan", {})
        planning_context = payload.get("planning_context", {})

        candidates: list[TestCommandCandidate] = []
        if isinstance(plan, dict):
            for command in plan.get("tests", []):
                if isinstance(command, str) and command.strip():
                    candidates.append(TestCommandCandidate(command=command, source="plan.tests"))

        if isinstance(planning_context, dict):
            for command in planning_context.get("suggested_test_commands", []):
                if isinstance(command, str) and command.strip():
                    candidates.append(
                        TestCommandCandidate(command=command, source="planning_context.suggested_test_commands")
                    )

            repository_profile = planning_context.get("repository_profile", {})
            if isinstance(repository_profile, dict):
                for command in repository_profile.get("test_commands", []):
                    if isinstance(command, str) and command.strip():
                        candidates.append(
                            TestCommandCandidate(command=command, source="planning_context.repository_profile.test_commands")
                        )

        return _dedupe_candidates(candidates)


def _dedupe_candidates(candidates: list[TestCommandCandidate]) -> list[TestCommandCandidate]:
    seen: set[str] = set()
    result: list[TestCommandCandidate] = []
    for candidate in candidates:
        if candidate.command in seen:
            continue
        seen.add(candidate.command)
        result.append(candidate)
    return result
