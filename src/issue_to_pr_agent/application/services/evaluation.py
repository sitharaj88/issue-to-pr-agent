from __future__ import annotations

from ...domain.entities import AgentPlan, EvaluationScore, PatchProposal, PlanningContext


class PlanningEvaluator:
    def evaluate(self, *, planning_context: PlanningContext, plan: AgentPlan) -> EvaluationScore:
        score = 40
        reasons: list[str] = []
        if planning_context.repository_profile.test_commands:
            score += 15
            reasons.append("Repository profile includes runnable test commands.")
        if planning_context.ranked_files:
            score += min(20, len(planning_context.ranked_files) * 3)
            reasons.append("Planning context identified ranked file candidates.")
        if planning_context.repository_index.symbol_count > 0:
            score += min(10, planning_context.repository_index.symbol_count // 2)
            reasons.append("Repository index identified named symbols.")
        if plan.tests:
            score += 10
            reasons.append("Plan includes explicit validation commands.")
        if plan.files_to_inspect:
            score += 10
            reasons.append("Plan scopes inspection to concrete files.")
        if plan.risks:
            score -= min(20, len(plan.risks) * 5)
            reasons.append("Plan carries known execution risks.")
        score = max(0, min(100, score))
        summary = (
            f"Plan quality scored {score}/100 across context depth, file targeting, and validation coverage."
        )
        return EvaluationScore(score=score, summary=summary, reasons=reasons)


class PatchProposalEvaluator:
    def evaluate(self, *, proposal: PatchProposal, planning_context: PlanningContext) -> EvaluationScore:
        score = 50
        reasons: list[str] = []
        operation_count = len(proposal.operations)
        if operation_count:
            score += max(0, 20 - (operation_count - 1) * 4)
            reasons.append("Proposal keeps the edit set bounded.")
        touched_tests = any("/test" in item.path or item.path.startswith("tests/") for item in proposal.operations)
        if touched_tests:
            score += 15
            reasons.append("Proposal updates or adds tests.")
        if proposal.rationale.strip():
            score += 10
            reasons.append("Proposal includes implementation rationale.")
        if planning_context.repository_index.symbol_count > 0:
            score += 5
            reasons.append("Proposal was generated with indexed symbol context.")
        score = max(0, min(100, score))
        summary = f"Patch proposal quality scored {score}/100 across scope, tests, and rationale."
        return EvaluationScore(score=score, summary=summary, reasons=reasons)
