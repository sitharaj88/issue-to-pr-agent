from __future__ import annotations

import shutil
from pathlib import Path
from urllib.parse import urljoin

from ...domain.entities import ArtifactReference


class DeliverySummaryBuilder:
    def __init__(
        self,
        *,
        artifact_dir: Path,
        artifact_base_url: str | None = None,
        artifact_store_backend: str = "filesystem",
        artifact_store_dir: Path | None = None,
        artifact_store_base_url: str | None = None,
    ) -> None:
        self._artifact_dir = artifact_dir.resolve()
        self._artifact_base_url = artifact_base_url.rstrip("/") + "/" if artifact_base_url else None
        self._artifact_store_backend = artifact_store_backend
        self._artifact_store_dir = artifact_store_dir.resolve() if artifact_store_dir is not None else None
        self._artifact_store_base_url = (
            artifact_store_base_url.rstrip("/") + "/" if artifact_store_base_url else None
        )

    def build_summary(
        self,
        *,
        run_payload: dict[str, object],
        execution_payload: dict[str, object],
        verification_payload: dict[str, object],
    ) -> str:
        plan = _as_dict(run_payload.get("plan"))
        verification_status = _as_string(verification_payload.get("status"))
        attempts = verification_payload.get("attempts")
        attempt_count = len(attempts) if isinstance(attempts, list) else 0
        return (
            _as_string(plan.get("summary"))
            or f"Delivered verified change set with {attempt_count} verification attempt(s)"
            + (f" and verification status {verification_status}" if verification_status else "")
        )

    def build_commit_message(self, *, run_payload: dict[str, object]) -> str:
        plan = _as_dict(run_payload.get("plan"))
        issue = _as_dict(run_payload.get("issue"))
        pr_title = _as_string(plan.get("pr_title"))
        if pr_title:
            return pr_title
        issue_number = issue.get("issue_number")
        issue_title = _as_string(issue.get("title"))
        if isinstance(issue_number, int) and issue_title:
            return f"Issue #{issue_number}: {issue_title}"
        return _as_string(plan.get("summary")) or "Apply issue-to-pr agent patch"

    def build_pr_title(self, *, run_payload: dict[str, object]) -> str:
        plan = _as_dict(run_payload.get("plan"))
        issue = _as_dict(run_payload.get("issue"))
        pr_title = _as_string(plan.get("pr_title"))
        if pr_title:
            return pr_title
        issue_number = issue.get("issue_number")
        issue_title = _as_string(issue.get("title"))
        if isinstance(issue_number, int) and issue_title:
            return f"Issue #{issue_number}: {issue_title}"
        return _as_string(plan.get("summary")) or "Agent draft pull request"

    def build_artifact_references(
        self,
        *,
        run_payload: dict[str, object],
        execution_payload: dict[str, object],
        verification_payload: dict[str, object],
    ) -> list[ArtifactReference]:
        references: list[ArtifactReference] = []
        artifacts = _as_dict(run_payload.get("artifacts"))
        references.extend(
            self._artifact_refs_from_map(
                {
                    "plan_report": artifacts.get("report_path"),
                    "pr_draft": artifacts.get("pr_draft_path"),
                    "run_audit": artifacts.get("audit_path"),
                    "execution_receipt": execution_payload.get("receipt_path"),
                    "verification_receipt": verification_payload.get("receipt_path"),
                }
            )
        )
        attempts = verification_payload.get("attempts")
        if isinstance(attempts, list):
            for item in attempts:
                if not isinstance(item, dict):
                    continue
                index = item.get("attempt_index")
                suffix = str(index) if isinstance(index, int) else "x"
                references.extend(
                    self._artifact_refs_from_map(
                        {
                            f"verification_stdout_{suffix}": item.get("stdout_path"),
                            f"verification_stderr_{suffix}": item.get("stderr_path"),
                        }
                    )
                )
        return references

    def build_pr_body(
        self,
        *,
        run_payload: dict[str, object],
        verification_payload: dict[str, object],
        artifacts: list[ArtifactReference],
        branch_name: str,
        base_branch: str,
        commit_sha: str,
        rollout_stage: str | None = None,
        rollback_base_sha: str | None = None,
        branch_protection_verified: bool = False,
    ) -> str:
        plan = _as_dict(run_payload.get("plan"))
        issue = _as_dict(run_payload.get("issue"))
        issue_number = issue.get("issue_number")
        verification_status = _as_string(verification_payload.get("status")) or "unknown"
        stop_reason = _as_string(verification_payload.get("stop_reason")) or "unknown"
        body = _as_string(plan.get("pr_body"))
        lines: list[str] = []
        if body:
            lines.append(body.strip())
            lines.append("")
        else:
            title = self.build_pr_title(run_payload=run_payload)
            lines.extend(
                [
                    f"## {title}",
                    "",
                    _as_string(plan.get("summary")) or "Agent-generated implementation draft.",
                    "",
                ]
            )
        lines.extend(
            [
                "## Delivery Metadata",
                "",
                f"- Branch: `{branch_name}`",
                f"- Base branch: `{base_branch}`",
                f"- Commit: `{commit_sha}`",
                f"- Rollout stage: `{rollout_stage or 'unspecified'}`",
                f"- Base branch protection verified: `{str(branch_protection_verified).lower()}`",
                f"- Verification status: `{verification_status}`",
                f"- Verification stop reason: `{stop_reason}`",
            ]
        )
        if rollback_base_sha:
            lines.append(f"- Rollback base SHA: `{rollback_base_sha}`")
        if isinstance(issue_number, int):
            lines.append(f"- Linked issue: `#{issue_number}`")
        if artifacts:
            lines.extend(["", "## Delivery Artifacts", ""])
            lines.extend(self._format_artifact_lines(artifacts))
        return "\n".join(lines).strip() + "\n"

    def build_pr_comment(
        self,
        *,
        run_payload: dict[str, object],
        execution_payload: dict[str, object],
        verification_payload: dict[str, object],
        artifacts: list[ArtifactReference],
        commit_sha: str,
        rollout_stage: str | None = None,
        rollback_base_sha: str | None = None,
        branch_protection_verified: bool = False,
    ) -> str:
        summary = self.build_summary(
            run_payload=run_payload,
            execution_payload=execution_payload,
            verification_payload=verification_payload,
        )
        verification_status = _as_string(verification_payload.get("status")) or "unknown"
        stop_reason = _as_string(verification_payload.get("stop_reason")) or "unknown"
        changed_files = _changed_paths(execution_payload)
        lines = [
            "Agent delivery summary",
            "",
            f"- Summary: {summary}",
            f"- Commit: `{commit_sha}`",
            f"- Rollout stage: `{rollout_stage or 'unspecified'}`",
            f"- Base branch protection verified: `{str(branch_protection_verified).lower()}`",
            f"- Verification status: `{verification_status}`",
            f"- Verification stop reason: `{stop_reason}`",
            f"- Changed files: {', '.join(f'`{path}`' for path in changed_files) if changed_files else 'none'}",
        ]
        if rollback_base_sha:
            lines.append(f"- Rollback base SHA: `{rollback_base_sha}`")
        if artifacts:
            lines.extend(["", "Artifacts", ""])
            lines.extend(self._format_artifact_lines(artifacts))
        return "\n".join(lines).strip() + "\n"

    def _artifact_refs_from_map(self, items: dict[str, object]) -> list[ArtifactReference]:
        references: list[ArtifactReference] = []
        for label, raw_path in items.items():
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            path = Path(raw_path).resolve()
            published_path = self._publish_artifact(path)
            references.append(
                ArtifactReference(
                    label=label,
                    path=str(published_path),
                    url=self._artifact_url(published_path),
                )
            )
        return references

    def _publish_artifact(self, path: Path) -> Path:
        if self._artifact_store_backend != "shared" or self._artifact_store_dir is None:
            return path
        relative = self._relative_artifact_path(path)
        destination = self._artifact_store_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, destination)
        return destination

    def _artifact_url(self, path: Path) -> str | None:
        if self._artifact_store_backend == "shared":
            if self._artifact_store_base_url is None or self._artifact_store_dir is None:
                return None
            try:
                relative = path.relative_to(self._artifact_store_dir)
            except ValueError:
                return None
            return urljoin(self._artifact_store_base_url, relative.as_posix())
        if self._artifact_base_url is None:
            return None
        try:
            relative = path.relative_to(self._artifact_dir)
        except ValueError:
            return None
        return urljoin(self._artifact_base_url, relative.as_posix())

    def _relative_artifact_path(self, path: Path) -> Path:
        try:
            return path.relative_to(self._artifact_dir)
        except ValueError:
            return Path("external") / path.name

    def _format_artifact_lines(self, artifacts: list[ArtifactReference]) -> list[str]:
        lines: list[str] = []
        for artifact in artifacts:
            if artifact.url:
                lines.append(f"- [{artifact.label}]({artifact.url})")
            else:
                lines.append(f"- `{artifact.label}`: `{artifact.path}`")
        return lines


def _as_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _as_string(value: object) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _changed_paths(execution_payload: dict[str, object]) -> list[str]:
    receipts = execution_payload.get("receipts")
    if not isinstance(receipts, list):
        return []
    changed: list[str] = []
    for item in receipts:
        if not isinstance(item, dict):
            continue
        if item.get("changed") is True and isinstance(item.get("path"), str):
            changed.append(item["path"])
    return changed
