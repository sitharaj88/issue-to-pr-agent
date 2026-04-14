from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from ...domain.entities import TraceEventRecord
from ...infrastructure.persistence.run_repository import RunRepository
from ...integrations.telemetry import TelemetrySinkClient


class TraceRecorder:
    def __init__(
        self,
        repository: RunRepository,
        *,
        sink_client: TelemetrySinkClient | None = None,
    ) -> None:
        self._repository = repository
        self._sink_client = sink_client

    def record(
        self,
        *,
        trace_id: str,
        source: str,
        span_name: str,
        status: str,
        payload: dict[str, object],
        output_dir: Path,
        linked_run_id: str | None = None,
        linked_job_id: str | None = None,
    ) -> TraceEventRecord:
        event_id = uuid4().hex[:12]
        recorded_at = datetime.now(timezone.utc).isoformat()
        destination = output_dir / trace_id / f"{event_id}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "event_id": event_id,
            "trace_id": trace_id,
            "recorded_at": recorded_at,
            "source": source,
            "span_name": span_name,
            "status": status,
            "linked_run_id": linked_run_id,
            "linked_job_id": linked_job_id,
            "payload": payload,
        }
        dispatch_results = self._dispatch(body)
        if dispatch_results:
            body["dispatch_results"] = dispatch_results
        destination.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        record = TraceEventRecord(
            event_id=event_id,
            trace_id=trace_id,
            recorded_at=recorded_at,
            source=source,
            span_name=span_name,
            status=status,
            payload_path=destination,
            linked_run_id=linked_run_id,
            linked_job_id=linked_job_id,
        )
        self._repository.save_trace_event(record, {**body, "payload_path": str(destination)})
        return record

    def _dispatch(self, payload: dict[str, object]) -> list[dict[str, object]]:
        if self._sink_client is None:
            return []
        result = {"destination": "telemetry_sink", "status": "sent"}
        try:
            self._sink_client.send_event(category="trace", payload=payload)
        except Exception as exc:
            result["status"] = "failed"
            result["error_message"] = str(exc)
        return [result]
