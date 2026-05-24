from __future__ import annotations

import json
from urllib import request

from ...infrastructure.config.settings import Settings


class TelemetrySinkClient:
    def __init__(self, settings: Settings | None) -> None:
        self._settings = settings

    def send_event(self, *, category: str, payload: dict[str, object]) -> None:
        if self._settings is None or not self._settings.telemetry_sink_url:
            return
        body = json.dumps({"category": category, "event": payload}, sort_keys=True).encode("utf-8")
        req = request.Request(
            self._settings.telemetry_sink_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": self._settings.user_agent,
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=5):
                return
        except Exception:
            return  # Telemetry is best-effort; never crash the worker.
