from __future__ import annotations

from fnmatch import fnmatch
import json
from pathlib import Path

from ...domain.entities import DeliveryGovernanceEvaluation
from ...shared.exceptions import ConfigurationError

_DEFAULT_POLICY: dict[str, object] = {
    "default": {
        "allowed_repo_patterns": ["*"],
        "blocked_repo_patterns": [],
        "blocked_path_patterns": [],
        "require_rollout_stage_for_paths": [
            ".github/workflows/*",
            "infra/*",
            "terraform/*",
            "deploy/*",
            "ops/*",
            "policies/*",
        ],
        "require_production_rollout_for_paths": [
            "deploy/prod/*",
            "ops/prod/*",
        ],
        "allowed_rollout_stages": ["dev", "staging", "production"],
        "blocked_command_patterns": [],
        "blocked_command_decisions": ["block"],
        "allowed_planner_providers": ["heuristic", "openai"],
        "allowed_patch_providers": [],
        "allowed_planner_models": [],
        "allowed_patch_models": [],
        "require_base_branch_protection": True,
    },
    "repos": {},
}


class DeliveryGovernancePolicyEvaluator:
    def __init__(
        self,
        policy_path: Path | None = None,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> None:
        self._policy = self._load_policy(policy_path, policy_overrides=policy_overrides)

    def evaluate_delivery(
        self,
        *,
        repo_full_name: str,
        base_branch: str,
        base_branch_protected: bool,
        rollout_stage: str | None,
        run_payload: dict[str, object],
        execution_payload: dict[str, object],
        patch_payload: dict[str, object] | None = None,
    ) -> DeliveryGovernanceEvaluation:
        repo_policy = self._repo_policy(repo_full_name)
        resolved_stage = _normalize_optional(rollout_stage)
        changed_paths = _extract_changed_paths(execution_payload)
        command_patterns = _string_list(repo_policy.get("blocked_command_patterns"))
        blocked_decisions = {item.lower() for item in _string_list(repo_policy.get("blocked_command_decisions"))}
        reasons: list[str] = []
        blocked_reasons: list[str] = []

        allowed_repo_patterns = _string_list(repo_policy.get("allowed_repo_patterns"))
        if allowed_repo_patterns and not any(fnmatch(repo_full_name, pattern) for pattern in allowed_repo_patterns):
            blocked_reasons.append(f"Repository '{repo_full_name}' is not allowed by delivery governance policy.")

        blocked_repo_patterns = _string_list(repo_policy.get("blocked_repo_patterns"))
        matching_repo_patterns = sorted(
            pattern for pattern in blocked_repo_patterns if fnmatch(repo_full_name, pattern)
        )
        if matching_repo_patterns:
            blocked_reasons.append(
                "Repository matches blocked governance patterns: " + ", ".join(matching_repo_patterns)
            )

        blocked_paths = _matching_paths(changed_paths, _string_list(repo_policy.get("blocked_path_patterns")))
        if blocked_paths:
            blocked_reasons.append(
                "Blocked paths cannot be delivered automatically: " + ", ".join(sorted(blocked_paths))
            )

        rollout_paths = _matching_paths(
            changed_paths,
            _string_list(repo_policy.get("require_rollout_stage_for_paths")),
        )
        if rollout_paths and resolved_stage is None:
            blocked_reasons.append(
                "Delivery requires an explicit rollout stage for paths: "
                + ", ".join(sorted(rollout_paths))
            )

        allowed_rollout_stages = _lowercase_strings(_string_list(repo_policy.get("allowed_rollout_stages")))
        if resolved_stage is not None and allowed_rollout_stages and resolved_stage.lower() not in allowed_rollout_stages:
            blocked_reasons.append(
                f"Rollout stage '{resolved_stage}' is not allowed by delivery governance policy."
            )

        production_paths = _matching_paths(
            changed_paths,
            _string_list(repo_policy.get("require_production_rollout_for_paths")),
        )
        if production_paths and resolved_stage != "production":
            blocked_reasons.append(
                "Delivery requires rollout stage 'production' for paths: "
                + ", ".join(sorted(production_paths))
            )

        planner_provider = _string_value(run_payload.get("planner_provider"))
        patch_provider = _string_value((patch_payload or {}).get("provider"))
        allowed_planner_providers = _lowercase_strings(_string_list(repo_policy.get("allowed_planner_providers")))
        allowed_patch_providers = _lowercase_strings(_string_list(repo_policy.get("allowed_patch_providers")))
        if planner_provider and allowed_planner_providers and planner_provider.lower() not in allowed_planner_providers:
            blocked_reasons.append(
                f"Planner provider '{planner_provider}' is not allowed by delivery governance policy."
            )
        if patch_provider and allowed_patch_providers and patch_provider.lower() not in allowed_patch_providers:
            blocked_reasons.append(
                f"Patch provider '{patch_provider}' is not allowed by delivery governance policy."
            )

        planner_model = _string_value(run_payload.get("planner_model"))
        patch_model = _string_value((patch_payload or {}).get("model"))
        allowed_planner_models = {item.lower() for item in _string_list(repo_policy.get("allowed_planner_models"))}
        allowed_patch_models = {item.lower() for item in _string_list(repo_policy.get("allowed_patch_models"))}
        if planner_model and allowed_planner_models and planner_model.lower() not in allowed_planner_models:
            blocked_reasons.append(
                f"Planner model '{planner_model}' is not allowed by delivery governance policy."
            )
        if patch_model and allowed_patch_models and patch_model.lower() not in allowed_patch_models:
            blocked_reasons.append(
                f"Patch model '{patch_model}' is not allowed by delivery governance policy."
            )

        plan_commands = _extract_plan_commands(run_payload)
        blocked_commands = sorted(
            command for command in plan_commands if any(fnmatch(command, pattern) for pattern in command_patterns)
        )
        if blocked_commands:
            blocked_reasons.append(
                "Planner commands are blocked by delivery governance policy: "
                + ", ".join(blocked_commands)
            )

        blocked_assessments = sorted(
            {
                _string_value(item.get("decision")).lower()
                for item in _list_dicts(run_payload.get("command_assessments"))
                if _string_value(item.get("decision")).lower() in blocked_decisions
            }
        )
        if blocked_assessments:
            blocked_reasons.append(
                "Planner command assessments are blocked for delivery: "
                + ", ".join(blocked_assessments)
            )

        branch_protection_required = bool(repo_policy.get("require_base_branch_protection", True))
        if branch_protection_required and not base_branch_protected:
            blocked_reasons.append(
                f"Base branch '{base_branch}' is not protected, but delivery governance requires branch protection."
            )

        if resolved_stage is not None:
            reasons.append(f"Rollout stage '{resolved_stage}' was declared for delivery.")
        if branch_protection_required and base_branch_protected:
            reasons.append(f"Base branch '{base_branch}' is protected.")
        if planner_provider:
            reasons.append(f"Planner provider '{planner_provider}' passed delivery governance.")
        if patch_provider:
            reasons.append(f"Patch provider '{patch_provider}' passed delivery governance.")

        summary = self._summary(
            blocked_reasons=blocked_reasons,
            base_branch=base_branch,
            base_branch_protected=base_branch_protected,
            rollout_stage=resolved_stage,
            changed_paths=changed_paths,
        )
        return DeliveryGovernanceEvaluation(
            rollout_stage=resolved_stage,
            branch_protection_required=branch_protection_required,
            branch_protection_verified=base_branch_protected,
            reasons=reasons,
            blocked_reasons=blocked_reasons,
            summary=summary,
            policy_snapshot={
                "repo_full_name": repo_full_name,
                "base_branch": base_branch,
                "allowed_repo_patterns": allowed_repo_patterns,
                "blocked_repo_patterns": blocked_repo_patterns,
                "blocked_path_patterns": _string_list(repo_policy.get("blocked_path_patterns")),
                "require_rollout_stage_for_paths": _string_list(repo_policy.get("require_rollout_stage_for_paths")),
                "require_production_rollout_for_paths": _string_list(
                    repo_policy.get("require_production_rollout_for_paths")
                ),
                "allowed_rollout_stages": _string_list(repo_policy.get("allowed_rollout_stages")),
                "blocked_command_patterns": command_patterns,
                "blocked_command_decisions": _string_list(repo_policy.get("blocked_command_decisions")),
                "allowed_planner_providers": _string_list(repo_policy.get("allowed_planner_providers")),
                "allowed_patch_providers": _string_list(repo_policy.get("allowed_patch_providers")),
                "allowed_planner_models": _string_list(repo_policy.get("allowed_planner_models")),
                "allowed_patch_models": _string_list(repo_policy.get("allowed_patch_models")),
                "require_base_branch_protection": branch_protection_required,
            },
        )

    def _summary(
        self,
        *,
        blocked_reasons: list[str],
        base_branch: str,
        base_branch_protected: bool,
        rollout_stage: str | None,
        changed_paths: list[str],
    ) -> str:
        if blocked_reasons:
            return "Delivery is blocked by governance policy."
        stage_summary = rollout_stage or "unspecified"
        protection_summary = "protected" if base_branch_protected else "unprotected"
        return (
            f"Delivery governance passed for {len(changed_paths)} changed file(s) "
            f"against base branch '{base_branch}' ({protection_summary}) at rollout stage '{stage_summary}'."
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
                raise ConfigurationError(f"Delivery governance policy file is not valid JSON: {policy_path}") from exc
            if not isinstance(data, dict):
                raise ConfigurationError("Delivery governance policy file must contain a JSON object.")
            policy = json.loads(json.dumps(_DEFAULT_POLICY))
            for key in ("default", "repos"):
                if key in data:
                    if not isinstance(data[key], dict):
                        raise ConfigurationError(
                            f"Delivery governance policy section '{key}' must be a JSON object."
                        )
                    policy[key] = _merge_dicts(policy[key], data[key])
        if policy_overrides is not None:
            if not isinstance(policy_overrides, dict):
                raise ConfigurationError("Tenant policy overrides must be a JSON object.")
            for key in ("default", "repos"):
                if key in policy_overrides:
                    if not isinstance(policy_overrides[key], dict):
                        raise ConfigurationError(
                            f"Tenant delivery governance override section '{key}' must be a JSON object."
                        )
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


def _merge_dicts(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _extract_changed_paths(execution_payload: dict[str, object]) -> list[str]:
    changed: list[str] = []
    for item in _list_dicts(execution_payload.get("receipts")):
        path = _string_value(item.get("path"))
        if item.get("changed") is True and path:
            changed.append(path)
    return changed


def _extract_plan_commands(run_payload: dict[str, object]) -> list[str]:
    plan = run_payload.get("plan")
    if not isinstance(plan, dict):
        return []
    return _string_list(plan.get("commands"))


def _matching_paths(paths: list[str], patterns: list[str]) -> list[str]:
    return [path for path in paths if any(_matches_path_pattern(path, pattern) for pattern in patterns)]


def _matches_path_pattern(path: str, pattern: str) -> bool:
    if not pattern:
        return False
    normalized_path = Path(path).as_posix()
    normalized_pattern = Path(pattern).as_posix()
    if any(token in normalized_pattern for token in "*?[]"):
        return fnmatch(normalized_path, normalized_pattern)
    trimmed = normalized_pattern.rstrip("/")
    return normalized_path == trimmed or normalized_path.startswith(f"{trimmed}/")


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _lowercase_strings(values: list[str]) -> list[str]:
    return [item.lower() for item in values]


def _list_dicts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_value(value: object) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip().lower()
    return stripped or None
