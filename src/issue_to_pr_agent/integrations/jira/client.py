from __future__ import annotations

import json
from urllib.parse import quote, urljoin
from urllib import error, request

from ...infrastructure.config.settings import Settings


class JiraClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def build_issue_url(self, issue_key: str) -> str:
        if not self._settings.jira_base_url:
            return issue_key
        return self._settings.jira_base_url.rstrip("/") + f"/browse/{quote(issue_key)}"

    def add_comment(self, issue_key: str, body: str) -> dict[str, object]:
        if not self._settings.jira_base_url or not self._settings.jira_token:
            raise RuntimeError("Jira integration is not configured.")
        return self._request_json(
            self._api_url(f"/rest/api/3/issue/{quote(issue_key)}/comment"),
            method="POST",
            payload={"body": _adf_document(body)},
        )

    def _api_url(self, path: str) -> str:
        if not self._settings.jira_base_url:
            raise RuntimeError("Jira base URL is not configured.")
        return urljoin(self._settings.jira_base_url.rstrip("/") + "/", path.lstrip("/"))

    def _request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        headers = {
            "Accept": "application/json",
            "User-Agent": self._settings.user_agent,
            "Authorization": f"Bearer {self._settings.jira_token}",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(url, headers=headers, method=method.upper(), data=data)
        try:
            with request.urlopen(req, timeout=20) as response:
                body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Jira API request failed: {exc.code} {details}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Jira API request failed: {exc.reason}") from exc
        decoded = json.loads(body) if body else {}
        if not isinstance(decoded, dict):
            raise RuntimeError("Jira API returned an unexpected payload.")
        return decoded


def _adf_document(text: str) -> dict[str, object]:
    paragraphs: list[dict[str, object]] = []
    for block in text.split("\n\n"):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        paragraph = {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "\n".join(lines)},
            ],
        }
        paragraphs.append(paragraph)
    return {
        "type": "doc",
        "version": 1,
        "content": paragraphs or [{"type": "paragraph", "content": [{"type": "text", "text": text or " "}] }],
    }
