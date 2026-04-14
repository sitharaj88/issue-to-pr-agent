# API App

This app now hosts the control-plane HTTP API.

Current responsibilities:

- browser-hosted reviewer and operator console at `GET /ui`
- versioned JSON API for planning, patch generation, patch execution, verification, approvals, delivery, sandbox management, and queue workflows
- delivery and queued-delivery endpoints accept rollout-stage metadata and enforce delivery governance before commit or PR creation
- queue APIs that accept worker-affinity tags and concurrency keys for distributed scheduling
- read APIs for runs, proposals, executions, verifications, approvals, deliveries, sandboxes, queue jobs, queue attempts, tenant dashboards, and tenant notifications
- observability APIs for alerts, traces, audit export, and retention execution
- machine-readable OpenAPI document at `GET /v1/openapi.json`
- request correlation through `X-Request-ID`
- idempotent POST replay via `Idempotency-Key` and GitHub delivery IDs
- lightweight per-client rate limiting
- optional static bearer-token authentication via `ISSUE_TO_PR_API_TOKEN`
- signed bearer-principal authentication via `ISSUE_TO_PR_AUTH_TOKEN_SECRET`
- GitHub issue webhook ingestion with optional HMAC verification via `ISSUE_TO_PR_WEBHOOK_SECRET`
- Jira issue webhook ingestion via project-to-repository mappings
- Slack approval action webhook ingestion with optional signing-secret verification
- notification fanout support for Slack, Teams, and Jira-backed workflows
- delivery responses that can reference shared artifact-store URLs when configured
- request tracing plus persisted trace-event receipts under the telemetry directory

Run it with:

```bash
issue-to-pr-api --host 127.0.0.1 --port 8080
```

Key routes:

- `GET /healthz`
- `GET /ui`
- `GET /v1/openapi.json`
- `GET /v1/identity/me`
- `POST /v1/identity/sync`
- `GET /v1/dashboard`
- `GET /v1/notifications`
- `GET /v1/alerts`
- `GET /v1/traces`
- `POST /v1/plan`
- `POST /v1/patch-proposals/generate`
- `POST /v1/patch-executions`
- `POST /v1/verify`
- `POST /v1/autofix`
- `POST /v1/sandboxes`
- `POST /v1/approvals/request`
- `POST /v1/approvals/review`
- `POST /v1/deliver`
- `POST /v1/audits/exports`
- `POST /v1/retention/enforce`
- `POST /v1/queue/plan`
- `POST /v1/queue/verify`
- `POST /v1/queue/deliver`
- `GET /v1/runs`
- `GET /v1/patch-proposals`
- `GET /v1/executions`
- `GET /v1/verifications`
- `GET /v1/approvals`
- `GET /v1/deliveries`
- `GET /v1/autofix-runs`
- `GET /v1/sandboxes`
- `GET /v1/queue-jobs`
- `POST /v1/webhooks/github/issues`
- `POST /v1/webhooks/jira/issues`
- `POST /v1/webhooks/slack/approvals`
