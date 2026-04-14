from __future__ import annotations

import json
from urllib import error, request

from ...infrastructure.config.settings import Settings


class TeamsWebhookClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def send_event(self, *, event_type: str, summary: str, payload: dict[str, object]) -> None:
        webhook_url = self._settings.teams_webhook_url
        if not webhook_url:
            raise RuntimeError("Teams webhook is not configured.")
        body = {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": summary,
            "themeColor": "E0633A",
            "title": f"Issue-to-PR {event_type.replace('_', ' ').title()}",
            "text": summary,
            "sections": [
                {
                    "facts": [
                        {"name": str(key), "value": str(value)}
                        for key, value in payload.items()
                        if value not in (None, "", [], {})
                    ]
                }
            ],
        }
        self._post_json(webhook_url, body)

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
            raise RuntimeError(f"Teams webhook request failed: {exc.code} {details}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Teams webhook request failed: {exc.reason}") from exc
