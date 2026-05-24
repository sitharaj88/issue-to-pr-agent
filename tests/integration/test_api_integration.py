from __future__ import annotations

import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.application.services.queue_budget import QueueBudgetManager
from issue_to_pr_agent.application.services.tenant_access import TenantAccessController
from issue_to_pr_agent.domain.entities import QueueJobStatus, QueueJobType
from issue_to_pr_agent.infrastructure.config.settings import Settings
from issue_to_pr_agent.infrastructure.persistence.run_repository import RunRepository
from issue_to_pr_agent.interfaces.http.app import ControlPlaneApi


class TestApiIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.artifact_dir = Path(self.temp_dir.name) / "artifacts"
        self.artifact_dir.mkdir()
        
        # Base environment setup
        self.env_patch = mock.patch.dict(
            "os.environ",
            {
                "ISSUE_TO_PR_ARTIFACT_DIR": str(self.artifact_dir),
                "APP_ENV": "local",
                "ISSUE_TO_PR_ENV": "development",
                "ISSUE_TO_PR_CORS_ALLOWED_ORIGIN": "https://dashboard.example.com",
            }
        )
        self.env_patch.start()
        
        self.settings = Settings.from_env()
        self.repository = RunRepository(self.settings.database_path)
        self.access_controller = TenantAccessController(self.repository)
        self.budget_manager = QueueBudgetManager(self.settings, self.repository)
        
        self.api = ControlPlaneApi(
            settings=self.settings,
            repository=self.repository,
            access_controller=self.access_controller,
            budget_manager=self.budget_manager,
        )

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def test_options_preflight_cors(self) -> None:
        # Preflight OPTIONS request
        response = self.api.handle_request(
            method="OPTIONS",
            path="/healthz",
            headers={"Origin": "https://dashboard.example.com"}
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.body, "")
        self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "https://dashboard.example.com")
        self.assertIn("Access-Control-Allow-Methods", response.headers)
        self.assertIn("Access-Control-Allow-Headers", response.headers)

    def test_get_cors_headers(self) -> None:
        # GET request should return CORS headers
        response = self.api.handle_request(
            method="GET",
            path="/healthz",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "https://dashboard.example.com")

    def test_metrics_endpoint_unauthorized_by_default(self) -> None:
        response = self.api.handle_request(
            method="GET",
            path="/metrics",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("issue_to_pr_queue_jobs_total", response.body)

    def test_mandatory_auth_in_production(self) -> None:
        # Re-initialize Settings and API under production environment
        with mock.patch.dict(
            "os.environ",
            {
                "ISSUE_TO_PR_ARTIFACT_DIR": str(self.artifact_dir),
                "APP_ENV": "production",
                "ISSUE_TO_PR_ENV": "production",
                "ISSUE_TO_PR_API_TOKEN": "secret-api-token-12345",
            }
        ):
            prod_settings = Settings.from_env()
            prod_api = ControlPlaneApi(
                settings=prod_settings,
                repository=self.repository,
                access_controller=self.access_controller,
                budget_manager=self.budget_manager,
            )
            
            # Unauthorized request to a protected path
            response = prod_api.handle_request(
                method="GET",
                path="/v1/runs",
            )
            self.assertEqual(response.status_code, 403)
            self.assertIn("error", response.body)

            # Authorized request
            response_auth = prod_api.handle_request(
                method="GET",
                path="/v1/runs",
                headers={"Authorization": "Bearer secret-api-token-12345"}
            )
            self.assertEqual(response_auth.status_code, 200)

    def test_production_settings_validation(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "ISSUE_TO_PR_ARTIFACT_DIR": str(self.artifact_dir),
                "ISSUE_TO_PR_ENV": "production",
            }
        ):
            from issue_to_pr_agent.shared.exceptions import ConfigurationError
            with self.assertRaises(ConfigurationError):
                Settings.from_env()
