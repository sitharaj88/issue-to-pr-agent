from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

from ...domain.entities import AlertRecord, AlertSeverity, AlertStatus, QueueMetricsSnapshot
from ...infrastructure.config.settings import Settings
from ...infrastructure.persistence.run_repository import RunRepository
from ...integrations.telemetry import TelemetrySinkClient


class AlertManager:
    def __init__(
        self,
        repository: RunRepository,
        settings: Settings,
        *,
        sink_client: TelemetrySinkClient | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._sink_client = sink_client

    def emit(
        self,
        *,
        severity: AlertSeverity,
        source: str,
        summary: str,
        payload: dict[str, object],
        output_dir: Path,
        tenant_id: str | None = None,
        dedupe_window_seconds: int | None = None,
    ) -> AlertRecord | None:
        if self._is_duplicate(
            tenant_id=tenant_id,
            source=source,
            summary=summary,
            dedupe_window_seconds=dedupe_window_seconds or self._settings.alert_dedupe_seconds,
        ):
            return None
        alert_id = uuid4().hex[:12]
        created_at = datetime.now(timezone.utc).isoformat()
        folder = output_dir / (tenant_id or "global")
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"{alert_id}.json"
        body = {
            "alert_id": alert_id,
            "created_at": created_at,
            "tenant_id": tenant_id,
            "severity": severity.value,
            "source": source,
            "status": AlertStatus.OPEN.value,
            "summary": summary,
            "payload": payload,
        }
        dispatch_results = self._dispatch(body)
        if dispatch_results:
            body["dispatch_results"] = dispatch_results
        destination.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        record = AlertRecord(
            alert_id=alert_id,
            created_at=created_at,
            tenant_id=tenant_id,
            severity=severity,
            source=source,
            status=AlertStatus.OPEN,
            summary=summary,
            payload_path=destination,
        )
        self._repository.save_alert(record, {**body, "payload_path": str(destination)})
        return record

    def evaluate_queue_snapshot(self, snapshot: QueueMetricsSnapshot, *, output_dir: Path) -> list[AlertRecord]:
        alerts: list[AlertRecord] = []
        if snapshot.stale_leases >= self._settings.alert_stale_lease_threshold:
            record = self.emit(
                tenant_id=None,
                severity=AlertSeverity.WARNING,
                source="queue_metrics",
                summary=f"Detected {snapshot.stale_leases} stale queue lease(s).",
                payload={
                    "generated_at": snapshot.generated_at,
                    "stale_leases": snapshot.stale_leases,
                    "leased_jobs": snapshot.leased_jobs,
                },
                output_dir=output_dir,
            )
            if record is not None:
                alerts.append(record)
        failed_jobs = int(snapshot.queue_counts.get("failed", 0))
        if failed_jobs >= self._settings.alert_failed_jobs_threshold:
            record = self.emit(
                tenant_id=None,
                severity=AlertSeverity.ERROR,
                source="queue_metrics",
                summary=f"Queue has {failed_jobs} failed job(s).",
                payload={
                    "generated_at": snapshot.generated_at,
                    "failed_jobs": failed_jobs,
                    "queue_counts": snapshot.queue_counts,
                },
                output_dir=output_dir,
            )
            if record is not None:
                alerts.append(record)
        return alerts

    def _is_duplicate(
        self,
        *,
        tenant_id: str | None,
        source: str,
        summary: str,
        dedupe_window_seconds: int,
    ) -> bool:
        if dedupe_window_seconds <= 0:
            return False
        lower_bound = datetime.now(timezone.utc) - timedelta(seconds=dedupe_window_seconds)
        for alert in self._repository.list_alerts(tenant_id=tenant_id, limit=100):
            if alert.source != source or alert.summary != summary:
                continue
            try:
                if datetime.fromisoformat(alert.created_at) >= lower_bound:
                    return True
            except ValueError:
                continue
        return False

    def _dispatch(self, payload: dict[str, object]) -> list[dict[str, object]]:
        if self._sink_client is None:
            return []
        result = {"destination": "telemetry_sink", "status": "sent"}
        try:
            self._sink_client.send_event(category="alert", payload=payload)
        except Exception as exc:
            result["status"] = "failed"
            result["error_message"] = str(exc)
        return [result]
