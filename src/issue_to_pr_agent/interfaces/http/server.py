from __future__ import annotations

import argparse
import json
from typing import Iterable
from wsgiref.simple_server import make_server

from ...application.services.queue_budget import QueueBudgetManager
from ...application.services.tenant_access import TenantAccessController
from ...infrastructure.config.settings import Settings
from ...infrastructure.persistence.run_repository import RunRepository
from ...observability.logging.config import configure_logging
from .app import ControlPlaneApi


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the issue-to-pr HTTP control-plane API.")
    parser.add_argument("--host", default=None, help="Bind host. Defaults to ISSUE_TO_PR_API_HOST.")
    parser.add_argument("--port", type=int, default=None, help="Bind port. Defaults to ISSUE_TO_PR_API_PORT.")
    return parser


def serve(api: ControlPlaneApi, *, host: str, port: int) -> None:
    with make_server(host, port, _wsgi_app(api)) as server:
        print(f"issue-to-pr API listening on http://{host}:{port}")
        server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    configure_logging(settings.log_level)
    repository = RunRepository(settings.database_path)
    access_controller = TenantAccessController(repository)
    budget_manager = QueueBudgetManager(settings, repository)
    api = ControlPlaneApi(
        settings=settings,
        repository=repository,
        access_controller=access_controller,
        budget_manager=budget_manager,
    )
    serve(
        api,
        host=args.host or settings.api_host,
        port=args.port or settings.api_port,
    )
    return 0


def _wsgi_app(api: ControlPlaneApi):
    def app(environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/")
        query_string = environ.get("QUERY_STRING", "")
        content_length_raw = environ.get("CONTENT_LENGTH", "0") or "0"
        try:
            content_length = int(content_length_raw)
        except ValueError:
            content_length = 0
        body = environ["wsgi.input"].read(content_length) if content_length > 0 else b""
        headers = _extract_headers(environ)
        response = api.handle_request(
            method=method,
            path=path,
            query_string=query_string,
            headers=headers,
            body=body,
        )
        content_type = response.headers.get("Content-Type", "application/json")
        if content_type.startswith("application/json"):
            body_bytes = json.dumps(response.body, indent=2, sort_keys=True).encode("utf-8")
        elif isinstance(response.body, bytes):
            body_bytes = response.body
        else:
            body_bytes = str(response.body).encode("utf-8")
        status_line = f"{response.status_code} {_reason_phrase(response.status_code)}"
        response_headers = [("Content-Type", content_type), ("Content-Length", str(len(body_bytes)))]
        for key, value in response.headers.items():
            if key.lower() in {"content-type", "content-length"}:
                continue
            response_headers.append((key, value))
        start_response(status_line, response_headers)
        return [body_bytes]

    return app


def _extract_headers(environ: dict[str, object]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in environ.items():
        if not isinstance(value, str):
            continue
        if key.startswith("HTTP_"):
            header_name = key[5:].replace("_", "-").title()
            headers[header_name] = value
    if isinstance(environ.get("CONTENT_TYPE"), str):
        headers["Content-Type"] = str(environ["CONTENT_TYPE"])
    return headers


def _reason_phrase(status_code: int) -> str:
    return {
        200: "OK",
        201: "Created",
        202: "Accepted",
        400: "Bad Request",
        403: "Forbidden",
        404: "Not Found",
        409: "Conflict",
        429: "Too Many Requests",
        500: "Internal Server Error",
    }.get(status_code, "OK")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
