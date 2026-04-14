# Worker App

Phase 8 currently exposes the worker through the main CLI:

```bash
issue-to-pr worker-run --worker-id worker-1 --max-jobs 5
```

Current responsibilities:

- claim queued `plan`, `verify`, and `deliver` jobs through bounded leases
- respect worker-tag affinity and queue `concurrency_key` constraints during claim selection
- reclaim expired leases and requeue jobs with resume metadata for later workers
- persist queue attempts and cooperative cancellation state
- run verification jobs with either the local or Docker-backed runtime, based on configuration
- write worker heartbeat receipts with active lease, worker tags, and queue capacity under the metrics directory
- write queue trace events and emit queue-failure alerts under the telemetry directory
- export queue metrics in JSON and Prometheus text formats

Future responsibilities:

- Postgres-backed multi-host queue coordination and worker leasing
- object-storage-backed artifact publishing across workers
- full containerized patch execution and per-job container lifecycle management
- external telemetry sinks and alerting
