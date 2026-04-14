from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from ...domain.entities import QueueJobStatus, QueueMetricsSnapshot, WorkerStatus
from ...infrastructure.persistence.run_repository import RunRepository


class QueueMetricsReporter:
    def __init__(self, repository: RunRepository) -> None:
        self._repository = repository

    def build_snapshot(self) -> QueueMetricsSnapshot:
        jobs = self._repository.list_queue_jobs(limit=1000)
        heartbeats = self._repository.list_worker_heartbeats(limit=200)

        queue_counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        tenant_counts: dict[str, int] = {}
        running_by_tenant: dict[str, int] = {}
        budget_reserved = 0
        budget_used = 0
        leased_jobs = 0
        stale_leases = 0
        now = datetime.now(timezone.utc)
        for job in jobs:
            queue_counts[job.status.value] = queue_counts.get(job.status.value, 0) + 1
            type_counts[job.job_type.value] = type_counts.get(job.job_type.value, 0) + 1
            if job.tenant_id:
                tenant_counts[job.tenant_id] = tenant_counts.get(job.tenant_id, 0) + 1
                if job.status == QueueJobStatus.RUNNING:
                    running_by_tenant[job.tenant_id] = running_by_tenant.get(job.tenant_id, 0) + 1
            budget_reserved += job.budget_units
            budget_used += job.budget_used
            if job.lease_token:
                leased_jobs += 1
            if job.lease_expires_at:
                try:
                    if datetime.fromisoformat(job.lease_expires_at) <= now:
                        stale_leases += 1
                except ValueError:
                    stale_leases += 1

        latest_by_worker: dict[str, object] = {}
        for heartbeat in heartbeats:
            latest_by_worker.setdefault(heartbeat.worker_id, heartbeat)
        worker_status_counts: dict[str, int] = {}
        active_workers = 0
        for heartbeat in latest_by_worker.values():
            worker_status_counts[heartbeat.status.value] = worker_status_counts.get(heartbeat.status.value, 0) + 1
            if heartbeat.status == WorkerStatus.RUNNING:
                active_workers += 1

        for status in QueueJobStatus:
            queue_counts.setdefault(status.value, 0)

        return QueueMetricsSnapshot(
            generated_at=datetime.now(timezone.utc).isoformat(),
            queue_counts=queue_counts,
            type_counts=type_counts,
            tenant_counts=tenant_counts,
            budget_reserved=budget_reserved,
            budget_used=budget_used,
            active_workers=active_workers,
            worker_status_counts=worker_status_counts,
            leased_jobs=leased_jobs,
            stale_leases=stale_leases,
            running_by_tenant=running_by_tenant,
        )

    def write_snapshot(self, output_dir: Path) -> tuple[Path, Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        snapshot = self.build_snapshot()
        json_path = output_dir / "queue-metrics.json"
        prom_path = output_dir / "queue-metrics.prom"
        json_path.write_text(json.dumps(snapshot.__dict__, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        prom_path.write_text(self.render_prometheus(snapshot), encoding="utf-8")
        return json_path, prom_path

    def render_prometheus(self, snapshot: QueueMetricsSnapshot) -> str:
        lines = [
            "# HELP issue_to_pr_queue_jobs Queue jobs by status.",
            "# TYPE issue_to_pr_queue_jobs gauge",
        ]
        for status, count in sorted(snapshot.queue_counts.items()):
            lines.append(f'issue_to_pr_queue_jobs{{status="{status}"}} {count}')
        lines.extend(
            [
                "# HELP issue_to_pr_queue_job_types Queue jobs by type.",
                "# TYPE issue_to_pr_queue_job_types gauge",
            ]
        )
        for job_type, count in sorted(snapshot.type_counts.items()):
            lines.append(f'issue_to_pr_queue_job_types{{job_type="{job_type}"}} {count}')
        lines.extend(
            [
                "# HELP issue_to_pr_queue_budget_reserved Reserved queue budget units.",
                "# TYPE issue_to_pr_queue_budget_reserved gauge",
                f"issue_to_pr_queue_budget_reserved {snapshot.budget_reserved}",
                "# HELP issue_to_pr_queue_budget_used Consumed queue budget units.",
                "# TYPE issue_to_pr_queue_budget_used gauge",
                f"issue_to_pr_queue_budget_used {snapshot.budget_used}",
                "# HELP issue_to_pr_queue_active_workers Active workers.",
                "# TYPE issue_to_pr_queue_active_workers gauge",
                f"issue_to_pr_queue_active_workers {snapshot.active_workers}",
                "# HELP issue_to_pr_queue_leased_jobs Leased queue jobs.",
                "# TYPE issue_to_pr_queue_leased_jobs gauge",
                f"issue_to_pr_queue_leased_jobs {snapshot.leased_jobs}",
                "# HELP issue_to_pr_queue_stale_leases Queue jobs whose leases appear stale.",
                "# TYPE issue_to_pr_queue_stale_leases gauge",
                f"issue_to_pr_queue_stale_leases {snapshot.stale_leases}",
            ]
        )
        if snapshot.running_by_tenant:
            lines.extend(
                [
                    "# HELP issue_to_pr_queue_running_by_tenant Running queue jobs by tenant.",
                    "# TYPE issue_to_pr_queue_running_by_tenant gauge",
                ]
            )
            for tenant_id, count in sorted(snapshot.running_by_tenant.items()):
                lines.append(f'issue_to_pr_queue_running_by_tenant{{tenant_id="{tenant_id}"}} {count}')
        return "\n".join(lines) + "\n"
