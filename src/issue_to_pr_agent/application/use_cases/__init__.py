"""Application use cases."""

from .dashboard import DashboardResult, DashboardUseCase
from .deliver_run import DeliverRunUseCase, DeliveryResult
from .execute_patch_proposal import ExecutePatchProposalUseCase, PatchExecutionResult
from .generate_patch_proposal import GeneratePatchProposalUseCase, PatchProposalGenerationResult
from .manage_tenant import ManageTenantUseCase, TenantAdminResult
from .manage_sandbox import ManageSandboxUseCase, SandboxResult
from .manage_approval import ApprovalWorkflowResult, RequestApprovalUseCase, ReviewApprovalUseCase
from .manage_queue import ManageQueueUseCase, QueueJobResult
from .plan_issue_to_pr import AgentRunResult, IssueToPRAgent
from .process_queue import ProcessQueueUseCase, WorkerRunResult
from .run_autofix import AutofixRunResult, RunAutofixUseCase
from .run_sandboxed_autofix import RunSandboxedAutofixUseCase, SandboxedAutofixResult
from .run_sandboxed_patch_execution import (
    RunSandboxedPatchExecutionUseCase,
    SandboxedPatchExecutionFailedError,
    SandboxedPatchExecutionResult,
)
from .verify_run import VerificationResult, VerifyRunUseCase

__all__ = [
    "AgentRunResult",
    "ApprovalWorkflowResult",
    "DashboardResult",
    "DashboardUseCase",
    "DeliverRunUseCase",
    "DeliveryResult",
    "ExecutePatchProposalUseCase",
    "GeneratePatchProposalUseCase",
    "IssueToPRAgent",
    "ManageSandboxUseCase",
    "ManageTenantUseCase",
    "ManageQueueUseCase",
    "SandboxResult",
    "SandboxedAutofixResult",
    "PatchExecutionResult",
    "PatchProposalGenerationResult",
    "ProcessQueueUseCase",
    "QueueJobResult",
    "RequestApprovalUseCase",
    "RunAutofixUseCase",
    "RunSandboxedAutofixUseCase",
    "RunSandboxedPatchExecutionUseCase",
    "ReviewApprovalUseCase",
    "SandboxedPatchExecutionFailedError",
    "SandboxedPatchExecutionResult",
    "AutofixRunResult",
    "TenantAdminResult",
    "VerificationResult",
    "VerifyRunUseCase",
    "WorkerRunResult",
]
