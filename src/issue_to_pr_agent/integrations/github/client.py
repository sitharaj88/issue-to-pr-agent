from __future__ import annotations

import json
import time
from urllib.parse import urljoin
from urllib import error, request

from ...domain.entities import GitHubRepositoryInfo, IssueCommentSummary, IssueContext, PullRequestSummary
from ...infrastructure.config.settings import Settings


class GitHubClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def fetch_issue(self, repo_full_name: str, issue_number: int) -> IssueContext:
        url = self._api_url(f"/repos/{repo_full_name}/issues/{issue_number}")
        payload = self._request_json(url)
        if "pull_request" in payload:
            raise ValueError(f"{repo_full_name}#{issue_number} is a pull request, not an issue.")

        labels = [
            item["name"]
            for item in payload.get("labels", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        return IssueContext(
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            title=payload.get("title") or f"Issue {issue_number}",
            body=payload.get("body") or "",
            labels=labels,
            url=payload.get("html_url") or url,
        )

    def fetch_repository(self, repo_full_name: str) -> GitHubRepositoryInfo:
        url = self._api_url(f"/repos/{repo_full_name}")
        payload = self._request_json(url)
        default_branch = payload.get("default_branch")
        html_url = payload.get("html_url")
        if not isinstance(default_branch, str) or not default_branch.strip():
            raise RuntimeError(f"GitHub repository payload did not include a default branch for {repo_full_name}.")
        if not isinstance(html_url, str) or not html_url.strip():
            raise RuntimeError(f"GitHub repository payload did not include an html_url for {repo_full_name}.")
        return GitHubRepositoryInfo(
            repo_full_name=repo_full_name,
            default_branch=default_branch,
            html_url=html_url,
        )

    def fetch_branch_protection(self, repo_full_name: str, branch_name: str) -> bool:
        url = self._api_url(f"/repos/{repo_full_name}/branches/{branch_name}")
        payload = self._request_json(url)
        return bool(payload.get("protected", False))

    def create_pull_request(
        self,
        repo_full_name: str,
        *,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
        draft: bool = True,
    ) -> PullRequestSummary:
        payload = self._request_json(
            self._api_url(f"/repos/{repo_full_name}/pulls"),
            method="POST",
            payload={
                "title": title,
                "body": body,
                "head": head_branch,
                "base": base_branch,
                "draft": draft,
            },
        )
        number = payload.get("number")
        api_url = payload.get("url")
        html_url = payload.get("html_url")
        response_title = payload.get("title")
        if not isinstance(number, int):
            raise RuntimeError("GitHub pull request payload did not include a pull request number.")
        if not isinstance(api_url, str) or not api_url.strip():
            raise RuntimeError("GitHub pull request payload did not include an API URL.")
        if not isinstance(html_url, str) or not html_url.strip():
            raise RuntimeError("GitHub pull request payload did not include an HTML URL.")
        if not isinstance(response_title, str) or not response_title.strip():
            raise RuntimeError("GitHub pull request payload did not include a title.")
        return PullRequestSummary(number=number, url=api_url, html_url=html_url, title=response_title)

    def add_issue_comment(self, repo_full_name: str, issue_number: int, *, body: str) -> IssueCommentSummary:
        payload = self._request_json(
            self._api_url(f"/repos/{repo_full_name}/issues/{issue_number}/comments"),
            method="POST",
            payload={"body": body},
        )
        comment_id = payload.get("id")
        api_url = payload.get("url")
        html_url = payload.get("html_url")
        if not isinstance(comment_id, int):
            raise RuntimeError("GitHub issue comment payload did not include a comment id.")
        if not isinstance(api_url, str) or not api_url.strip():
            raise RuntimeError("GitHub issue comment payload did not include an API URL.")
        if not isinstance(html_url, str) or not html_url.strip():
            raise RuntimeError("GitHub issue comment payload did not include an HTML URL.")
        return IssueCommentSummary(comment_id=comment_id, url=api_url, html_url=html_url)

    def _api_url(self, path: str) -> str:
        base_url = self._settings.github_api_base_url.rstrip("/") + "/"
        return urljoin(base_url, path.lstrip("/"))

    def _request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": self._settings.user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._settings.github_token:
            headers["Authorization"] = f"Bearer {self._settings.github_token}"

        request_data = None
        if payload is not None:
            request_data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = request.Request(url, headers=headers, method=method.upper(), data=request_data)
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                with request.urlopen(req, timeout=20) as response:
                    body = response.read().decode("utf-8")
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise RuntimeError("GitHub API returned an unexpected payload.")
                return data
            except error.HTTPError as exc:
                if exc.code in {429, 500, 502, 503} and attempt < 3:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    delay = int(retry_after) if retry_after and retry_after.isdigit() else (2 ** attempt)
                    time.sleep(delay)
                    last_error = RuntimeError(f"GitHub API request failed: {exc.code}")
                    # Recreate the request since the previous one may be consumed
                    req = request.Request(url, headers=headers, method=method.upper(), data=request_data)
                    continue
                details = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"GitHub API request failed: {exc.code} {details}") from exc
            except error.URLError as exc:
                raise RuntimeError(f"GitHub API request failed: {exc.reason}") from exc
        raise last_error or RuntimeError("GitHub API request failed after retries.")
