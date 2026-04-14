from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from issue_to_pr_agent.application.services.audit_export import RunAuditExporter
from issue_to_pr_agent.application.services.retention import RetentionEnforcer
from issue_to_pr_agent.domain.entities import (
    AlertSeverity,
    ExecutionMode,
    NotificationEventType,
    NotificationRecord,
    NotificationStatus,
    PlannerProvider,
    QueueMetricsSnapshot,
    RunRecord,
    RunStatus,
    WorkerHeartbeatRecord,
    WorkerStatus,
)
from issue_to_pr_agent.infrastructure.config.settings import Settings
from issue_to_pr_agent.infrastructure.persistence.run_repository import RunRepository
from issue_to_pr_agent.observability.alerts import AlertManager
from issue_to_pr_agent.observability.tracing import TraceRecorder


class _FakeTelemetrySink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def send_event(self, *, category: str, payload: dict[str, object]) -> None:
        self.events.append((category, payload))


class ObservabilityTests(unittest.TestCase):
    def test_trace_recorder_persists_trace_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings.from_env(cwd=root)
            repository = RunRepository(settings.database_path)
            sink = _FakeTelemetrySink()
            recorder = TraceRecorder(repository, sink_client=sink)  # type: ignore[arg-type]

            record = recorder.record(
                trace_id="trace-1",
                source="http_api",
                span_name="GET /v1/runs",
                status="completed",
                payload={"status_code": 200},
                linked_run_id="run-1",
                linked_job_id="job-1",
                output_dir=settings.telemetry_dir / "traces",
            )

            self.assertTrue(record.payload_path.exists())
            stored = repository.list_trace_events(trace_id="trace-1", limit=5)
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0].event_id, record.event_id)
            self.assertEqual(sink.events[0][0], "trace")

    def test_alert_manager_emits_and_dedupes_queue_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings.from_env(cwd=root)
            repository = RunRepository(settings.database_path)
            sink = _FakeTelemetrySink()
            manager = AlertManager(repository, settings, sink_client=sink)  # type: ignore[arg-type]

            snapshot = QueueMetricsSnapshot(
                generated_at="2026-04-14T12:00:00+00:00",
                queue_counts={"failed": 6},
                leased_jobs=2,
                stale_leases=1,
            )
            alerts = manager.evaluate_queue_snapshot(snapshot, output_dir=settings.telemetry_dir / "alerts")
            duplicate = manager.evaluate_queue_snapshot(snapshot, output_dir=settings.telemetry_dir / "alerts")

            self.assertEqual(len(alerts), 2)
            self.assertEqual(len(duplicate), 0)
            self.assertEqual(len(repository.list_alerts(limit=10)), 2)
            self.assertEqual(sink.events[0][0], "alert")

    def test_audit_exporter_writes_bundle_manifest_and_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings.from_env(cwd=root)
            repository = RunRepository(settings.database_path)
            run_dir = settings.artifact_dir / "runs" / "run-1"
            run_dir.mkdir(parents=True, exist_ok=True)
            report_path = run_dir / "plan.md"
            report_path.write_text("plan\n", encoding="utf-8")
            pr_path = run_dir / "pr.md"
            pr_path.write_text("pr\n", encoding="utf-8")
            audit_path = run_dir / "run.json"
            payload = {
                "run_id": "run-1",
                "artifacts": {
                    "report_path": str(report_path),
                    "pr_draft_path": str(pr_path),
                    "audit_path": str(audit_path),
                },
            }
            audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            repository.save_run(
                RunRecord(
                    run_id="run-1",
                    created_at="2026-04-14T10:00:00+00:00",
                    repo_full_name="acme/widgets",
                    issue_number=1,
                    planner_provider=PlannerProvider.HEURISTIC,
                    execution_mode=ExecutionMode.PLAN_ONLY,
                    status=RunStatus.SUCCEEDED,
                    branch_name="agent/issue-1",
                    summary="Plan",
                    issue_url="https://example.com/issues/1",
                    report_path=report_path,
                    pr_draft_path=pr_path,
                    audit_path=audit_path,
                ),
                payload,
            )

            result = RunAuditExporter(repository).export_run(run_id="run-1", output_dir=settings.audit_export_dir)

            self.assertTrue(result.bundle_path.exists())
            self.assertTrue(result.manifest_path.exists())
            self.assertTrue(result.archive_path.exists())
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(manifest["artifacts"]), 3)

    def test_retention_enforcer_dry_run_and_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings.from_env(cwd=root)
            repository = RunRepository(settings.database_path)
            old_time = "2025-01-01T00:00:00+00:00"

            notification_path = settings.notification_dir / "tenant-1" / "note.json"
            notification_path.parent.mkdir(parents=True, exist_ok=True)
            notification_path.write_text("{}\n", encoding="utf-8")
            repository.save_notification(
                NotificationRecord(
                    notification_id="note-1",
                    created_at=old_time,
                    tenant_id="tenant-1",
                    event_type=NotificationEventType.APPROVAL_REQUESTED,
                    status=NotificationStatus.EMITTED,
                    summary="Old note",
                    payload_path=notification_path,
                ),
                {"notification_id": "note-1"},
            )

            heartbeat_path = settings.metrics_dir / "workers" / "worker-1" / "old.json"
            heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            heartbeat_path.write_text("{}\n", encoding="utf-8")
            repository.save_worker_heartbeat(
                WorkerHeartbeatRecord(
                    worker_id="worker-1",
                    recorded_at=old_time,
                    status=WorkerStatus.IDLE,
                    current_job_id=None,
                    summary="Old heartbeat",
                    processed_jobs=0,
                    succeeded_jobs=0,
                    failed_jobs=0,
                    cancelled_jobs=0,
                    payload_path=heartbeat_path,
                ),
                {"worker_id": "worker-1"},
            )

            sink = _FakeTelemetrySink()
            recorder = TraceRecorder(repository, sink_client=sink)  # type: ignore[arg-type]
            recorder.record(
                trace_id="trace-retention",
                source="http_api",
                span_name="old",
                status="completed",
                payload={},
                output_dir=settings.telemetry_dir / "traces",
            )
            trace = repository.list_trace_events(trace_id="trace-retention", limit=1)[0]
            trace.payload_path.write_text("{}\n", encoding="utf-8")
            repository.save_trace_event(
                type(trace)(
                    event_id=trace.event_id,
                    trace_id=trace.trace_id,
                    recorded_at=old_time,
                    source=trace.source,
                    span_name=trace.span_name,
                    status=trace.status,
                    payload_path=trace.payload_path,
                    linked_run_id=trace.linked_run_id,
                    linked_job_id=trace.linked_job_id,
                ),
                {"event_id": trace.event_id},
            )

            alert_manager = AlertManager(repository, settings, sink_client=sink)  # type: ignore[arg-type]
            alert = alert_manager.emit(
                severity=AlertSeverity.ERROR,
                source="queue_worker",
                summary="Old alert",
                payload={},
                output_dir=settings.telemetry_dir / "alerts",
                dedupe_window_seconds=1,
            )
            assert alert is not None
            repository.save_alert(
                type(alert)(
                    alert_id=alert.alert_id,
                    created_at=old_time,
                    tenant_id=alert.tenant_id,
                    severity=alert.severity,
                    source=alert.source,
                    status=alert.status,
                    summary=alert.summary,
                    payload_path=alert.payload_path,
                ),
                {"alert_id": alert.alert_id},
            )

            enforcer = RetentionEnforcer(repository, settings)
            dry_run = enforcer.enforce(
                dry_run=True,
                now=datetime(2026, 4, 14, tzinfo=timezone.utc),
            )
            self.assertEqual(dry_run.notification_count, 1)
            self.assertEqual(dry_run.worker_heartbeat_count, 1)
            self.assertEqual(dry_run.alert_count, 1)
            self.assertEqual(dry_run.trace_count, 1)

            applied = enforcer.enforce(
                dry_run=False,
                now=datetime(2026, 4, 14, tzinfo=timezone.utc),
            )
            self.assertFalse(notification_path.exists())
            self.assertFalse(heartbeat_path.exists())
            self.assertEqual(applied.notification_count, 1)
            self.assertEqual(len(repository.list_notifications(limit=10)), 0)
            self.assertEqual(len(repository.list_alerts(limit=10)), 0)
            self.assertEqual(len(repository.list_trace_events(limit=10)), 0)
