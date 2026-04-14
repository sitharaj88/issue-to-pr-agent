from __future__ import annotations

import hashlib
import hmac
import json
from urllib import error, request

from ...infrastructure.config.settings import Settings
from ...shared.exceptions import PolicyError


class SlackWebhookClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def send_event(self, *, event_type: str, summary: str, payload: dict[str, object]) -> None:
        webhook_url = self._settings.slack_webhook_url
        if not webhook_url:
            raise RuntimeError("Slack webhook is not configured.")
        body = {
            "text": summary,
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": f"Issue-to-PR {event_type.replace('_', ' ').title()}"},
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Summary*\n{summary}"},
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": _payload_markdown(payload),
                    },
                },
            ],
        }
        self._post_json(webhook_url, body)

    def verify_signature(
        self,
        *,
        timestamp: str | None,
        signature: str | None,
        body: bytes,
    ) -> None:
        secret = self._settings.slack_signing_secret
        if not secret:
            return
        if not timestamp or not signature:
            raise PolicyError("Slack signature headers are missing.")
        expected = "v0=" + hmac.new(
            secret.encode("utf-8"),
            f"v0:{timestamp}:".encode("utf-8") + body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise PolicyError("Slack request signature is invalid.")

    def _post_json(self, url: str, payload: dict[str, object]) -> None:
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url,
            method="POST",
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": self._settings.user_agent,
            },
        )
        try:
            with request.urlopen(req, timeout=15):
                return
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Slack webhook request failed: {exc.code} {details}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Slack webhook request failed: {exc.reason}") from exc


def _payload_markdown(payload: dict[str, object]) -> str:
    if not payload:
        return "_No structured payload provided._"
    lines = []
    for key, value in payload.items():
        if value in (None, "", [], {}):
            continue
        lines.append(f"*{key}*: `{value}`")
    return "\n".join(lines) or "_No structured payload provided._"
