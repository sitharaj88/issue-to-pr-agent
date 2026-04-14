from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile

from ...agents.patcher.base import PatcherClient
from ...agents.planner.heuristic import HeuristicPlanner
from ...application.use_cases.execute_patch_proposal import ExecutePatchProposalUseCase
from ...application.use_cases.generate_patch_proposal import GeneratePatchProposalUseCase
from ...application.use_cases.plan_issue_to_pr import IssueToPRAgent
from ...application.use_cases.verify_run import VerifyRunUseCase
from ...domain.entities import (
    AgentPlan,
    IssueContext,
    PatchFileContext,
    PatchExecutionMode,
    PatchOperation,
    PatchOperationType,
    PatchProposal,
    PatcherProvider,
    PlanningContext,
)
from ...domain.policies.safety import SafetyPolicy
from ...infrastructure.config.settings import Settings
from ...infrastructure.persistence.run_repository import RunRepository
from ...infrastructure.verification import build_command_runner


@dataclass(frozen=True)
class SmokeTestResult:
    receipt_path: Path
    payload: dict[str, object]


class _SmokeGitHubClient:
    def fetch_issue(self, repo_full_name: str, issue_number: int) -> IssueContext:
        return IssueContext(
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            title="Enable the smoke-test feature flag",
            body="The feature flag must be enabled and verified by tests.",
            labels=["smoke"],
            url=f"https://example.com/{repo_full_name}/issues/{issue_number}",
        )


class _SmokePatcher(PatcherClient):
    provider = PatcherProvider.OPENAI
    model_name = "smoke-deterministic"

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
        target = next(item for item in files if item.path == "feature_flag.py")
        current_line = next(line for line in target.content.splitlines() if line.startswith("ENABLED ="))
        return PatchProposal(
            proposal_id=f"{linked_run_id}-smoke",
            linked_run_id=linked_run_id,
            summary="Enable the smoke-test feature flag",
            rationale="Deterministic smoke patch flips the feature flag to True.",
            operations=[
                PatchOperation(
                    type=PatchOperationType.REPLACE_TEXT,
                    path="feature_flag.py",
                    find_text=current_line,
                    replace_text="ENABLED = True",
                )
            ],
        )


class RunSmokeTestUseCase:
    def __init__(self, repository: RunRepository, settings: Settings) -> None:
        self._repository = repository
        self._settings = settings

    def run(self, *, output_dir: Path) -> SmokeTestResult:
        created_at = datetime.now(timezone.utc).isoformat()
        smoke_dir = output_dir.resolve() / "smoke-tests"
        smoke_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = smoke_dir / f"smoke-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            artifact_dir = Path(tmp) / ".issue-to-pr"
            repo_root.mkdir(parents=True, exist_ok=True)
            (repo_root / "feature_flag.py").write_text("ENABLED = False\n", encoding="utf-8")
            (repo_root / "tests").mkdir()
            (repo_root / "tests" / "test_feature_flag.py").write_text(
                "from pathlib import Path\n"
                "import unittest\n\n"
                "class FeatureFlagTests(unittest.TestCase):\n"
                "    def test_enabled(self):\n"
                "        namespace = {}\n"
                "        exec(Path('feature_flag.py').read_text(encoding='utf-8'), namespace)\n"
                "        self.assertIs(namespace['ENABLED'], True)\n",
                encoding="utf-8",
            )

            planner = HeuristicPlanner()
            plan_result = IssueToPRAgent(
                _SmokeGitHubClient(),
                planner,
                self._repository,
                SafetyPolicy(branch_prefix=self._settings.branch_prefix),
            ).run(
                repo_full_name="smoke/demo",
                issue_number=1,
                repo_root=repo_root,
                output_dir=artifact_dir,
            )

            proposal_result = GeneratePatchProposalUseCase(
                self._repository,
                _SmokePatcher(),
            ).generate(
                run_id=plan_result.run_id,
                repo_root=repo_root,
            )

            execution_result = ExecutePatchProposalUseCase(self._repository).execute(
                proposal=proposal_result.proposal,
                repo_root=repo_root,
                artifact_dir=artifact_dir,
                mode=PatchExecutionMode.APPLY,
            )

            verification_result = VerifyRunUseCase(
                self._repository,
                SafetyPolicy(branch_prefix=self._settings.branch_prefix),
                command_runner=build_command_runner(self._settings),
            ).verify(
                repo_root=repo_root,
                artifact_dir=artifact_dir,
                execution_id=execution_result.execution_id,
                max_attempts=1,
                timeout_seconds=120,
            )

            payload = {
                "created_at": created_at,
                "run_id": plan_result.run_id,
                "proposal_id": proposal_result.proposal_id,
                "execution_id": execution_result.execution_id,
                "verification_id": verification_result.verification_id,
                "verification_status": verification_result.receipt.status.value,
                "verification_stop_reason": verification_result.receipt.stop_reason.value,
                "summary": "Smoke test exercises plan, patch generation, patch apply, and verification.",
            }
            receipt_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return SmokeTestResult(receipt_path=receipt_path, payload=payload)
