from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ...infrastructure.config.settings import Settings
from ...infrastructure.persistence.run_repository import RunRepository


@dataclass(frozen=True)
class RetentionRunResult:
    dry_run: bool
    notification_count: int
    worker_heartbeat_count: int
    alert_count: int
    trace_count: int
    deleted_paths: list[str]


class RetentionEnforcer:
    def __init__(self, repository: RunRepository, settings: Settings) -> None:
        self._repository = repository
        self._settings = settings

    def enforce(self, *, dry_run: bool, now: datetime | None = None) -> RetentionRunResult:
        current_time = now or datetime.now(timezone.utc)
        notification_cutoff = (current_time - timedelta(days=self._settings.retention_notification_days)).isoformat()
        heartbeat_cutoff = (current_time - timedelta(days=self._settings.retention_worker_heartbeat_days)).isoformat()
        alert_cutoff = (current_time - timedelta(days=self._settings.retention_alert_days)).isoformat()
        trace_cutoff = (current_time - timedelta(days=self._settings.retention_trace_days)).isoformat()

        notifications = [
            item
            for item in self._repository.list_notifications(limit=5000)
            if item.created_at < notification_cutoff
        ]
        heartbeats = [
            item
            for item in self._repository.list_worker_heartbeats(limit=5000)
            if item.recorded_at < heartbeat_cutoff
        ]
        alerts = [
            item
            for item in self._repository.list_alerts(limit=5000)
            if item.created_at < alert_cutoff
        ]
        traces = [
            item
            for item in self._repository.list_trace_events(limit=5000)
            if item.recorded_at < trace_cutoff
        ]
        deleted_paths: list[str] = []
        if not dry_run:
            deleted_notifications = self._repository.prune_notifications_before(notification_cutoff)
            deleted_heartbeats = self._repository.prune_worker_heartbeats_before(heartbeat_cutoff)
            deleted_alerts = self._repository.prune_alerts_before(alert_cutoff)
            deleted_traces = self._repository.prune_trace_events_before(trace_cutoff)
            for record in [*deleted_notifications, *deleted_heartbeats, *deleted_alerts, *deleted_traces]:
                if _safe_delete(record.payload_path):
                    deleted_paths.append(str(record.payload_path))
        return RetentionRunResult(
            dry_run=dry_run,
            notification_count=len(notifications),
            worker_heartbeat_count=len(heartbeats),
            alert_count=len(alerts),
            trace_count=len(traces),
            deleted_paths=deleted_paths,
        )


def _safe_delete(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True
