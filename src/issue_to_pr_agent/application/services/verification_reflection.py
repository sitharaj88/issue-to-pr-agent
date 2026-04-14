from __future__ import annotations

from ...domain.entities import VerificationStopReason


class VerificationReflector:
    def reflect(
        self,
        *,
        exit_code: int,
        stdout: str,
        stderr: str,
        has_remaining_candidates: bool,
        attempts_used: int,
        max_attempts: int,
    ) -> tuple[bool, VerificationStopReason | None, str]:
        if exit_code == 0:
            return False, VerificationStopReason.SUCCESS, "Verification command succeeded."

        combined = f"{stdout}\n{stderr}".lower()
        if attempts_used >= max_attempts:
            return False, VerificationStopReason.MAX_ATTEMPTS_REACHED, "Maximum verification attempts reached."

        if has_remaining_candidates:
            if "no module named pytest" in combined or "command not found" in combined:
                return True, None, "Command appears unavailable in this environment; trying the next candidate."
            return True, None, "Command failed; trying the next verification candidate."

        return False, VerificationStopReason.CANDIDATE_COMMANDS_EXHAUSTED, "No verification candidates remain."
