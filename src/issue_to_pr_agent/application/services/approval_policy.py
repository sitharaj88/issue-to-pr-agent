from __future__ import annotations

from fnmatch import fnmatch
import json
from pathlib import Path

from ...domain.entities import ApprovalAction, ApprovalEvaluation, ApprovalRiskLevel
from ...shared.exceptions import ConfigurationError, PolicyError

_RISK_ORDER = {
    ApprovalRiskLevel.LOW: 0,
    ApprovalRiskLevel.MEDIUM: 1,
    ApprovalRiskLevel.HIGH: 2,
    ApprovalRiskLevel.CRITICAL: 3,
}

_DEFAULT_POLICY: dict[str, object] = {
    "default": {
        "required_approvals_by_risk": {
            "low": 0,
            "medium": 1,
            "high": 1,
            "critical": 2,
        },
        "allow_self_approval": False,
        "blocked_labels": ["do-not-merge", "wip"],
        "high_risk_labels": ["security", "infra", "production"],
        "critical_risk_labels": ["security-critical"],
        "high_risk_paths": [
            ".github/",
            "infra/",
            "terraform/",
            "Dockerfile",
            "docker-compose.yml",
            "pyproject.toml",
            "requirements",
            "package.json",
            "package-lock.json",
            "Cargo.toml",
            "go.mod",
        ],
        "critical_risk_paths": [
            "secrets/",
            "policies/",
            "deploy/prod/",
            "ops/",
        ],
        "required_reviewer_teams": [],
        "allowed_requester_teams": [],
        "allowed_reviewer_teams": [],
        "max_low_risk_changed_files": 2,
        "max_medium_risk_changed_files": 6,
    },
    "repos": {},
    "teams": {},
}


class ApprovalPolicyEvaluator:
    def __init__(
        self,
        policy_path: Path | None = None,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> None:
        self._policy_path = policy_path
        self._policy = self._load_policy(policy_path, policy_overrides=policy_overrides)

    def evaluate_delivery(
        self,
        *,
        repo_full_name: str,
        run_payload: dict[str, object],
        execution_payload: dict[str, object],
        verification_payload: dict[str, object],
        requester_team: str | None = None,
    ) -> ApprovalEvaluation:
        repo_policy = self._repo_policy(repo_full_name)
        labels = _lowercase_strings(_extract_issue_labels(run_payload))
        changed_paths = _extract_changed_paths(execution_payload)
        risk_level = ApprovalRiskLevel.LOW
        reasons: list[str] = []
        blocked_reasons: list[str] = []

        if requester_team is not None:
            self._evaluate_requester_permissions(
                repo_policy=repo_policy,
                requester_team=requester_team,
                blocked_reasons=blocked_reasons,
            )

        blocked_labels = sorted(label for label in labels if label in _lowercase_strings(repo_policy["blocked_labels"]))
        if blocked_labels:
            blocked_reasons.append("Repository policy blocks delivery for labels: " + ", ".join(blocked_labels))

        critical_labels = sorted(
            label for label in labels if label in _lowercase_strings(repo_policy["critical_risk_labels"])
        )
        if critical_labels:
            risk_level = _max_risk(risk_level, ApprovalRiskLevel.CRITICAL)
            reasons.append("Critical issue labels present: " + ", ".join(critical_labels))

        high_labels = sorted(
            label for label in labels if label in _lowercase_strings(repo_policy["high_risk_labels"])
        )
        if high_labels:
            risk_level = _max_risk(risk_level, ApprovalRiskLevel.HIGH)
            reasons.append("High-risk issue labels present: " + ", ".join(high_labels))

        critical_paths = _matching_paths(changed_paths, _string_list(repo_policy["critical_risk_paths"]))
        if critical_paths:
            risk_level = _max_risk(risk_level, ApprovalRiskLevel.CRITICAL)
            reasons.append("Critical delivery paths changed: " + ", ".join(sorted(critical_paths)))

        high_paths = _matching_paths(changed_paths, _string_list(repo_policy["high_risk_paths"]))
        if high_paths:
            risk_level = _max_risk(risk_level, ApprovalRiskLevel.HIGH)
            reasons.append("High-risk delivery paths changed: " + ", ".join(sorted(high_paths)))

        if len(changed_paths) > _int_value(repo_policy["max_medium_risk_changed_files"], default=6):
            risk_level = _max_risk(risk_level, ApprovalRiskLevel.HIGH)
            reasons.append(f"Delivery touches {len(changed_paths)} files, exceeding the medium-risk file threshold.")
        elif len(changed_paths) > _int_value(repo_policy["max_low_risk_changed_files"], default=2):
            risk_level = _max_risk(risk_level, ApprovalRiskLevel.MEDIUM)
            reasons.append(f"Delivery touches {len(changed_paths)} files, exceeding the low-risk file threshold.")

        assessments = run_payload.get("command_assessments")
        if isinstance(assessments, list):
            decisions = {item.get("decision") for item in assessments if isinstance(item, dict)}
            if "block" in decisions:
                risk_level = _max_risk(risk_level, ApprovalRiskLevel.HIGH)
                reasons.append("Planner proposed blocked commands that require human review before delivery.")
            elif "review" in decisions:
                risk_level = _max_risk(risk_level, ApprovalRiskLevel.MEDIUM)
                reasons.append("Planner proposed commands that were marked for manual review.")

        attempts = verification_payload.get("attempts")
        if isinstance(attempts, list) and len(attempts) > 1:
            risk_level = _max_risk(risk_level, ApprovalRiskLevel.MEDIUM)
            reasons.append("Verification required multiple attempts.")
        skipped_commands = verification_payload.get("skipped_commands")
        if isinstance(skipped_commands, list) and skipped_commands:
            risk_level = _max_risk(risk_level, ApprovalRiskLevel.MEDIUM)
            reasons.append("Verification skipped one or more candidate commands.")

        required_approvals_by_risk = _required_approvals(repo_policy["required_approvals_by_risk"])
        required_approvals = required_approvals_by_risk[risk_level]
        approval_required = required_approvals > 0 and not blocked_reasons
        summary = self._summary(
            risk_level=risk_level,
            required_approvals=required_approvals,
            blocked_reasons=blocked_reasons,
            changed_paths=changed_paths,
        )
        return ApprovalEvaluation(
            action=ApprovalAction.DELIVERY,
            risk_level=risk_level,
            approval_required=approval_required,
            required_approvals=required_approvals,
            required_reviewer_teams=_string_list(repo_policy["required_reviewer_teams"]),
            reasons=reasons,
            blocked_reasons=blocked_reasons,
            summary=summary,
            policy_snapshot={
                "repo_full_name": repo_full_name,
                "required_approvals_by_risk": {
                    key.value: value for key, value in required_approvals_by_risk.items()
                },
                "allow_self_approval": bool(repo_policy["allow_self_approval"]),
                "required_reviewer_teams": _string_list(repo_policy["required_reviewer_teams"]),
                "allowed_requester_teams": _string_list(repo_policy["allowed_requester_teams"]),
                "allowed_reviewer_teams": _string_list(repo_policy["allowed_reviewer_teams"]),
                "blocked_labels": _string_list(repo_policy["blocked_labels"]),
                "high_risk_labels": _string_list(repo_policy["high_risk_labels"]),
                "critical_risk_labels": _string_list(repo_policy["critical_risk_labels"]),
                "high_risk_paths": _string_list(repo_policy["high_risk_paths"]),
                "critical_risk_paths": _string_list(repo_policy["critical_risk_paths"]),
            },
        )

    def ensure_reviewer_can_decide(
        self,
        *,
        repo_full_name: str,
        requested_by: str,
        actor: str,
        reviewer_team: str,
        required_reviewer_teams: list[str],
        assigned_reviewers: list[str] | None = None,
        assigned_reviewer_teams: list[str] | None = None,
    ) -> None:
        repo_policy = self._repo_policy(repo_full_name)
        reviewer_team_normalized = reviewer_team.strip()
        if not reviewer_team_normalized:
            raise PolicyError("Reviewer team is required for approval decisions.")

        team_policy = self._team_policy(reviewer_team_normalized)
        if not bool(team_policy.get("can_review", True)):
            raise PolicyError(f"Team '{reviewer_team_normalized}' is not allowed to review approvals.")

        allowed_reviewer_teams = _lowercase_strings(_string_list(repo_policy["allowed_reviewer_teams"]))
        if allowed_reviewer_teams and reviewer_team_normalized.lower() not in allowed_reviewer_teams:
            raise PolicyError(
                f"Team '{reviewer_team_normalized}' is not allowed to review approvals for {repo_full_name}."
            )

        required_reviewer_lookup = _lowercase_strings(required_reviewer_teams)
        if required_reviewer_lookup and reviewer_team_normalized.lower() not in required_reviewer_lookup:
            raise PolicyError(
                f"Team '{reviewer_team_normalized}' does not satisfy the required reviewer teams."
            )

        assigned_actor_lookup = _lowercase_strings(assigned_reviewers or [])
        if assigned_actor_lookup and actor.lower() not in assigned_actor_lookup:
            raise PolicyError(f"Reviewer '{actor}' is not assigned to approval review.")

        assigned_team_lookup = _lowercase_strings(assigned_reviewer_teams or [])
        if assigned_team_lookup and reviewer_team_normalized.lower() not in assigned_team_lookup:
            raise PolicyError(
                f"Team '{reviewer_team_normalized}' is not assigned to approval review."
            )

        if not bool(repo_policy["allow_self_approval"]) and actor == requested_by:
            raise PolicyError("Self-approval is disabled by policy.")

    def _evaluate_requester_permissions(
        self,
        *,
        repo_policy: dict[str, object],
        requester_team: str,
        blocked_reasons: list[str],
    ) -> None:
        requester_team_normalized = requester_team.strip()
        if not requester_team_normalized:
            blocked_reasons.append("Requester team is required by the approval policy.")
            return

        team_policy = self._team_policy(requester_team_normalized)
        if not bool(team_policy.get("can_request", True)):
            blocked_reasons.append(f"Team '{requester_team_normalized}' is not allowed to request delivery approvals.")

        allowed_requester_teams = _lowercase_strings(_string_list(repo_policy["allowed_requester_teams"]))
        if allowed_requester_teams and requester_team_normalized.lower() not in allowed_requester_teams:
            blocked_reasons.append(
                f"Team '{requester_team_normalized}' is not allowed to request approvals for this repository."
            )

    def _summary(
        self,
        *,
        risk_level: ApprovalRiskLevel,
        required_approvals: int,
        blocked_reasons: list[str],
        changed_paths: list[str],
    ) -> str:
        if blocked_reasons:
            return "Delivery is blocked by approval policy."
        if required_approvals <= 0:
            return f"Low-risk delivery is auto-approved for {len(changed_paths)} changed file(s)."
        return (
            f"{risk_level.value.capitalize()}-risk delivery requires "
            f"{required_approvals} approval(s) for {len(changed_paths)} changed file(s)."
        )

    def _load_policy(
        self,
        policy_path: Path | None,
        *,
        policy_overrides: dict[str, object] | None,
    ) -> dict[str, object]:
        if policy_path is None:
            policy = json.loads(json.dumps(_DEFAULT_POLICY))
        else:
            try:
                data = json.loads(policy_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ConfigurationError(f"Approval policy file is not valid JSON: {policy_path}") from exc
            if not isinstance(data, dict):
                raise ConfigurationError("Approval policy file must contain a JSON object.")
            policy = json.loads(json.dumps(_DEFAULT_POLICY))
            for key in ("default", "repos", "teams"):
                if key in data:
                    if not isinstance(data[key], dict):
                        raise ConfigurationError(f"Approval policy section '{key}' must be a JSON object.")
                    policy[key] = _merge_dicts(policy[key], data[key])
        if policy_overrides is not None:
            if not isinstance(policy_overrides, dict):
                raise ConfigurationError("Tenant policy overrides must be a JSON object.")
            for key in ("default", "repos", "teams"):
                if key in policy_overrides:
                    if not isinstance(policy_overrides[key], dict):
                        raise ConfigurationError(f"Tenant policy override section '{key}' must be a JSON object.")
                    policy[key] = _merge_dicts(policy[key], policy_overrides[key])
        return policy

    def _repo_policy(self, repo_full_name: str) -> dict[str, object]:
        defaults = dict(self._policy.get("default", {}))
        repos = self._policy.get("repos", {})
        if isinstance(repos, dict):
            override = repos.get(repo_full_name)
            if isinstance(override, dict):
                defaults = _merge_dicts(defaults, override)
        return defaults

    def _team_policy(self, team_name: str) -> dict[str, object]:
        teams = self._policy.get("teams", {})
        if not isinstance(teams, dict):
            return {"can_request": True, "can_review": True}
        team_policy = teams.get(team_name)
        if not isinstance(team_policy, dict):
            return {"can_request": True, "can_review": True}
        return {
            "can_request": bool(team_policy.get("can_request", True)),
            "can_review": bool(team_policy.get("can_review", True)),
        }


def _merge_dicts(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _lowercase_strings(items: list[str]) -> set[str]:
    return {item.strip().lower() for item in items if item.strip()}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _int_value(value: object, *, default: int) -> int:
    return value if isinstance(value, int) and value >= 0 else default


def _required_approvals(value: object) -> dict[ApprovalRiskLevel, int]:
    defaults = {
        ApprovalRiskLevel.LOW: 0,
        ApprovalRiskLevel.MEDIUM: 1,
        ApprovalRiskLevel.HIGH: 1,
        ApprovalRiskLevel.CRITICAL: 2,
    }
    if not isinstance(value, dict):
        return defaults
    resolved = dict(defaults)
    for risk in ApprovalRiskLevel:
        raw = value.get(risk.value)
        if isinstance(raw, int) and raw >= 0:
            resolved[risk] = raw
    return resolved


def _max_risk(left: ApprovalRiskLevel, right: ApprovalRiskLevel) -> ApprovalRiskLevel:
    return right if _RISK_ORDER[right] > _RISK_ORDER[left] else left


def _extract_issue_labels(run_payload: dict[str, object]) -> list[str]:
    issue = run_payload.get("issue")
    if not isinstance(issue, dict):
        return []
    labels = issue.get("labels")
    return labels if isinstance(labels, list) else []


def _extract_changed_paths(execution_payload: dict[str, object]) -> list[str]:
    receipts = execution_payload.get("receipts")
    if not isinstance(receipts, list):
        return []
    paths: list[str] = []
    for item in receipts:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if item.get("changed") is True and isinstance(path, str) and path.strip():
            paths.append(path)
    return paths


def _matching_paths(paths: list[str], patterns: list[str]) -> set[str]:
    matches: set[str] = set()
    for path in paths:
        normalized_path = Path(path).as_posix()
        basename = Path(path).name
        for pattern in patterns:
            normalized_pattern = pattern.strip()
            if not normalized_pattern:
                continue
            if normalized_pattern.endswith("/"):
                if normalized_path.startswith(normalized_pattern):
                    matches.add(normalized_path)
                    break
            elif "*" in normalized_pattern or "?" in normalized_pattern:
                if fnmatch(normalized_path, normalized_pattern) or fnmatch(basename, normalized_pattern):
                    matches.add(normalized_path)
                    break
            elif normalized_path == normalized_pattern or basename == normalized_pattern or basename.startswith(
                normalized_pattern
            ):
                matches.add(normalized_path)
                break
    return matches
