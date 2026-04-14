from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.application.services.authentication import (  # noqa: E402
    authenticate_bearer_token,
    issue_bearer_token,
)
from issue_to_pr_agent.domain.entities import AuthSubjectType  # noqa: E402
from issue_to_pr_agent.shared.exceptions import PolicyError  # noqa: E402


class AuthenticationTests(unittest.TestCase):
    def test_signed_bearer_token_round_trips_principal_claims(self) -> None:
        token = issue_bearer_token(
            secret="0123456789abcdef0123456789abcdef",
            issuer="issue-to-pr",
            subject="user-123",
            actor="alice",
            team="platform",
            groups=["platform", "reviewers"],
            scopes=["manage_membership"],
            tenant_ids=["tenant-1"],
            expires_in_seconds=600,
            now=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc),
        )

        principal = authenticate_bearer_token(
            token,
            secret="0123456789abcdef0123456789abcdef",
            expected_issuer="issue-to-pr",
            now=datetime(2026, 4, 14, 10, 5, tzinfo=timezone.utc),
        )

        self.assertEqual(principal.subject, "user-123")
        self.assertEqual(principal.actor, "alice")
        self.assertEqual(principal.subject_type, AuthSubjectType.USER)
        self.assertEqual(principal.team, "platform")
        self.assertEqual(principal.groups, ["platform", "reviewers"])
        self.assertEqual(principal.scopes, ["manage_membership"])
        self.assertEqual(principal.tenant_ids, ["tenant-1"])

    def test_signed_bearer_token_rejects_expired_claims(self) -> None:
        token = issue_bearer_token(
            secret="0123456789abcdef0123456789abcdef",
            subject="svc-sync",
            actor="sync-bot",
            subject_type=AuthSubjectType.SERVICE,
            expires_in_seconds=60,
            now=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc),
        )

        with self.assertRaises(PolicyError):
            authenticate_bearer_token(
                token,
                secret="0123456789abcdef0123456789abcdef",
                now=datetime(2026, 4, 14, 10, 2, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
