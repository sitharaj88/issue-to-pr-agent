from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.infrastructure.config.settings import Settings
from issue_to_pr_agent.shared.exceptions import ConfigurationError


class SettingsTests(unittest.TestCase):
    def test_from_env_builds_default_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict("os.environ", {}, clear=True):
                settings = Settings.from_env(cwd=cwd)

            self.assertEqual(settings.environment, "local")
            self.assertEqual(settings.api_host, "127.0.0.1")
            self.assertEqual(settings.api_port, 8080)
            self.assertIsNone(settings.api_token)
            self.assertIsNone(settings.auth_token_secret)
            self.assertIsNone(settings.auth_token_issuer)
            self.assertEqual(settings.api_rate_limit_per_minute, 120)
            self.assertEqual(settings.approval_ttl_hours, 24)
            self.assertEqual(settings.openai_complex_model, "gpt-4.1-mini")
            self.assertEqual(settings.artifact_dir, (cwd / ".issue-to-pr").resolve())
            self.assertEqual(
                settings.database_path,
                (cwd / ".issue-to-pr" / "agent_runs.sqlite3").resolve(),
            )
            self.assertEqual(settings.github_api_base_url, "https://api.github.com")
            self.assertIsNone(settings.jira_base_url)
            self.assertIsNone(settings.jira_token)
            self.assertIsNone(settings.jira_project_mappings_path)
            self.assertIsNone(settings.jira_webhook_secret)
            self.assertIsNone(settings.slack_webhook_url)
            self.assertIsNone(settings.slack_signing_secret)
            self.assertIsNone(settings.teams_webhook_url)
            self.assertIsNone(settings.approval_policy_path)
            self.assertIsNone(settings.delivery_governance_policy_path)
            self.assertIsNone(settings.artifact_base_url)
            self.assertEqual(
                settings.notification_dir,
                (cwd / ".issue-to-pr" / "notifications").resolve(),
            )
            self.assertEqual(
                settings.metrics_dir,
                (cwd / ".issue-to-pr" / "metrics").resolve(),
            )
            self.assertEqual(
                settings.telemetry_dir,
                (cwd / ".issue-to-pr" / "telemetry").resolve(),
            )
            self.assertEqual(
                settings.audit_export_dir,
                (cwd / ".issue-to-pr" / "audit-exports").resolve(),
            )
            self.assertEqual(
                settings.sandbox_dir,
                (cwd / ".issue-to-pr" / "sandboxes").resolve(),
            )
            self.assertIsNone(settings.database_url)
            self.assertEqual(settings.database_backend, "sqlite")
            self.assertEqual(settings.artifact_store_backend, "filesystem")
            self.assertEqual(
                settings.artifact_store_dir,
                (cwd / ".issue-to-pr" / "artifact-store").resolve(),
            )
            self.assertIsNone(settings.artifact_store_base_url)
            self.assertIsNone(settings.telemetry_sink_url)
            self.assertEqual(settings.git_remote_name, "origin")
            self.assertEqual(settings.queue_max_attempts, 3)
            self.assertEqual(settings.queue_lease_seconds, 900)
            self.assertEqual(settings.queue_max_running_jobs_per_worker, 4)
            self.assertEqual(settings.queue_max_running_jobs_per_tenant, 2)
            self.assertEqual(settings.queue_candidate_scan_limit, 200)
            self.assertEqual(settings.alert_stale_lease_threshold, 1)
            self.assertEqual(settings.alert_failed_jobs_threshold, 5)
            self.assertEqual(settings.alert_dedupe_seconds, 3600)
            self.assertEqual(settings.retention_notification_days, 30)
            self.assertEqual(settings.retention_worker_heartbeat_days, 7)
            self.assertEqual(settings.retention_alert_days, 30)
            self.assertEqual(settings.retention_trace_days, 14)
            self.assertEqual(settings.budget_cost_plan_heuristic, 1)
            self.assertEqual(settings.router_planner_complexity_threshold, 14)
            self.assertEqual(settings.router_patch_complexity_threshold, 18)
            self.assertEqual(settings.sandbox_max_file_bytes, 10 * 1024 * 1024)
            self.assertEqual(settings.verification_runtime.value, "local")
            self.assertEqual(settings.docker_binary, "docker")
            self.assertEqual(settings.docker_image, "python:3.11-slim")
            self.assertEqual(settings.docker_network, "none")
            self.assertEqual(settings.docker_memory_mb, 1024)
            self.assertEqual(settings.docker_cpus, 1.0)
            self.assertEqual(settings.webhook_actor, "webhook-bot")
            self.assertEqual(settings.webhook_team, "automation")
            self.assertIsNone(settings.webhook_repo_roots_path)

    def test_validate_rejects_invalid_repo_file_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict("os.environ", {"ISSUE_TO_PR_MAX_REPO_FILES": "0"}, clear=True):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(cwd=cwd)

    def test_validate_rejects_invalid_queue_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict("os.environ", {"ISSUE_TO_PR_QUEUE_MAX_ATTEMPTS": "0"}, clear=True):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(cwd=cwd)

    def test_validate_rejects_invalid_queue_lease_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict("os.environ", {"ISSUE_TO_PR_QUEUE_LEASE_SECONDS": "0"}, clear=True):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(cwd=cwd)

    def test_validate_rejects_invalid_api_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict("os.environ", {"ISSUE_TO_PR_API_PORT": "70000"}, clear=True):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(cwd=cwd)

    def test_validate_rejects_invalid_api_rate_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict("os.environ", {"ISSUE_TO_PR_API_RATE_LIMIT_PER_MINUTE": "-1"}, clear=True):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(cwd=cwd)

    def test_validate_rejects_short_auth_token_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict("os.environ", {"ISSUE_TO_PR_AUTH_TOKEN_SECRET": "short-secret"}, clear=True):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(cwd=cwd)

    def test_validate_rejects_invalid_approval_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict("os.environ", {"ISSUE_TO_PR_APPROVAL_TTL_HOURS": "0"}, clear=True):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(cwd=cwd)

    def test_validate_rejects_invalid_sandbox_file_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict("os.environ", {"ISSUE_TO_PR_SANDBOX_MAX_FILE_BYTES": "0"}, clear=True):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(cwd=cwd)

    def test_validate_rejects_invalid_docker_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict("os.environ", {"ISSUE_TO_PR_DOCKER_MEMORY_MB": "0"}, clear=True):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(cwd=cwd)

    def test_validate_rejects_invalid_docker_cpus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict("os.environ", {"ISSUE_TO_PR_DOCKER_CPUS": "many"}, clear=True):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(cwd=cwd)

    def test_validate_rejects_missing_jira_mapping_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict(
                "os.environ",
                {"ISSUE_TO_PR_JIRA_PROJECT_MAPPINGS_PATH": str(cwd / "missing.json")},
                clear=True,
            ):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(cwd=cwd)

    def test_validate_rejects_invalid_slack_webhook_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict(
                "os.environ",
                {"ISSUE_TO_PR_SLACK_WEBHOOK_URL": "not-a-url"},
                clear=True,
            ):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(cwd=cwd)

    def test_validate_rejects_invalid_database_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict(
                "os.environ",
                {"ISSUE_TO_PR_DATABASE_BACKEND": "mysql"},
                clear=True,
            ):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(cwd=cwd)

    def test_validate_rejects_missing_postgres_database_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict(
                "os.environ",
                {"ISSUE_TO_PR_DATABASE_BACKEND": "postgres"},
                clear=True,
            ):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(cwd=cwd)

    def test_validate_rejects_invalid_artifact_store_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict(
                "os.environ",
                {"ISSUE_TO_PR_ARTIFACT_STORE_BASE_URL": "not-a-url"},
                clear=True,
            ):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(cwd=cwd)

    def test_validate_rejects_invalid_telemetry_sink_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict(
                "os.environ",
                {"ISSUE_TO_PR_TELEMETRY_SINK_URL": "not-a-url"},
                clear=True,
            ):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(cwd=cwd)

    def test_validate_rejects_missing_delivery_governance_policy_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict(
                "os.environ",
                {
                    "ISSUE_TO_PR_DELIVERY_GOVERNANCE_POLICY_PATH": str(cwd / "missing-governance.json"),
                },
                clear=True,
            ):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(cwd=cwd)

    def test_validate_rejects_invalid_router_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with mock.patch.dict(
                "os.environ",
                {"ISSUE_TO_PR_ROUTER_PLANNER_COMPLEXITY_THRESHOLD": "0"},
                clear=True,
            ):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(cwd=cwd)
