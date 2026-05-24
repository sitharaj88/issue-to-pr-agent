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

    def test_wrong_signing_key_is_rejected(self) -> None:
        """A token signed with a different key must be rejected."""
        token = issue_bearer_token(
            secret="0123456789abcdef0123456789abcdef",
            subject="user-1",
            actor="alice",
            expires_in_seconds=600,
            now=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc),
        )
        with self.assertRaises(PolicyError):
            authenticate_bearer_token(
                token,
                secret="different-secret-key-1234567890ab",
                now=datetime(2026, 4, 14, 10, 5, tzinfo=timezone.utc),
            )

    def test_tampered_payload_is_rejected(self) -> None:
        """A token with a modified payload segment must be rejected."""
        token = issue_bearer_token(
            secret="0123456789abcdef0123456789abcdef",
            subject="user-1",
            actor="alice",
            expires_in_seconds=600,
            now=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc),
        )
        # Tamper with the payload segment
        parts = token.split(".")
        import base64
        tampered_payload = base64.urlsafe_b64encode(b'{"sub":"admin","actor":"eve","subject_type":"user","iat":1000,"exp":9999999999}').decode().rstrip("=")
        tampered_token = f"{parts[0]}.{tampered_payload}.{parts[2]}"
        with self.assertRaises(PolicyError):
            authenticate_bearer_token(
                tampered_token,
                secret="0123456789abcdef0123456789abcdef",
                now=datetime(2026, 4, 14, 10, 5, tzinfo=timezone.utc),
            )

    def test_malformed_token_structure_is_rejected(self) -> None:
        """A token without three dot-separated segments must be rejected."""
        with self.assertRaises(PolicyError):
            authenticate_bearer_token(
                "not-a-valid-token",
                secret="0123456789abcdef0123456789abcdef",
            )

    def test_missing_subject_claim_is_rejected(self) -> None:
        """A token without the required 'sub' claim must be rejected."""
        import base64, json as _json, hashlib, hmac as _hmac
        header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
        payload_data = {"actor": "alice", "subject_type": "user", "iat": 1000, "exp": 9999999999}
        payload = base64.urlsafe_b64encode(_json.dumps(payload_data, separators=(',', ':'), sort_keys=True).encode()).decode().rstrip("=")
        secret = "0123456789abcdef0123456789abcdef"
        signing_input = f"{header}.{payload}".encode()
        sig = base64.urlsafe_b64encode(_hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()).decode().rstrip("=")
        token = f"{header}.{payload}.{sig}"
        with self.assertRaises(PolicyError):
            authenticate_bearer_token(token, secret=secret)

    def test_missing_actor_claim_is_rejected(self) -> None:
        """A token without the required 'actor' claim must be rejected."""
        import base64, json as _json, hashlib, hmac as _hmac
        header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
        payload_data = {"sub": "user-1", "subject_type": "user", "iat": 1000, "exp": 9999999999}
        payload = base64.urlsafe_b64encode(_json.dumps(payload_data, separators=(',', ':'), sort_keys=True).encode()).decode().rstrip("=")
        secret = "0123456789abcdef0123456789abcdef"
        signing_input = f"{header}.{payload}".encode()
        sig = base64.urlsafe_b64encode(_hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()).decode().rstrip("=")
        token = f"{header}.{payload}.{sig}"
        with self.assertRaises(PolicyError):
            authenticate_bearer_token(token, secret=secret)

    def test_wrong_issuer_is_rejected(self) -> None:
        """A token with an unexpected issuer must be rejected."""
        token = issue_bearer_token(
            secret="0123456789abcdef0123456789abcdef",
            issuer="wrong-issuer",
            subject="user-1",
            actor="alice",
            expires_in_seconds=600,
            now=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc),
        )
        with self.assertRaises(PolicyError):
            authenticate_bearer_token(
                token,
                secret="0123456789abcdef0123456789abcdef",
                expected_issuer="correct-issuer",
                now=datetime(2026, 4, 14, 10, 5, tzinfo=timezone.utc),
            )

    def test_service_subject_type_round_trips(self) -> None:
        """Service accounts should correctly round-trip the subject_type."""
        token = issue_bearer_token(
            secret="0123456789abcdef0123456789abcdef",
            subject="svc-deploy",
            actor="deploy-bot",
            subject_type=AuthSubjectType.SERVICE,
            expires_in_seconds=600,
            now=datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc),
        )
        principal = authenticate_bearer_token(
            token,
            secret="0123456789abcdef0123456789abcdef",
            now=datetime(2026, 4, 14, 10, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(principal.subject_type, AuthSubjectType.SERVICE)
        self.assertEqual(principal.subject, "svc-deploy")
        self.assertEqual(principal.actor, "deploy-bot")


if __name__ == "__main__":
    unittest.main()
