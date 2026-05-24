# Operations Runbook

## Deployment

### Docker (Recommended)

```bash
# Build the image
docker build -t issue-to-pr-agent .

# Run with docker-compose
cp .env.example .env
# Edit .env with your configuration
docker compose up -d
```

### Bare Metal

```bash
pip install .
export ISSUE_TO_PR_ARTIFACT_DIR=/var/lib/issue-to-pr
export ISSUE_TO_PR_API_TOKEN=your-secret-token
export ISSUE_TO_PR_ENV=production

# Start the API server
issue-to-pr-api --host 0.0.0.0 --port 8080

# Start a worker (in a separate process)
issue-to-pr worker-run --worker-id worker-1 --max-jobs 10
```

## Configuration Reference

See `.env.example` for the complete list of environment variables with descriptions.

### Required for Production

| Variable | Description |
|----------|-------------|
| `ISSUE_TO_PR_ENV` | Must be `production` |
| `ISSUE_TO_PR_ARTIFACT_DIR` | Path to artifact storage |
| `ISSUE_TO_PR_API_TOKEN` or `ISSUE_TO_PR_AUTH_TOKEN_SECRET` | At least one auth mechanism |
| `GITHUB_TOKEN` | GitHub API access |
| `ISSUE_TO_PR_DATABASE_BACKEND` | Use `postgres` for multi-worker |
| `ISSUE_TO_PR_DATABASE_URL` | Postgres connection string |

## Scaling

### Single Instance
- SQLite backend is fine
- Run API and worker in the same process or on the same host
- Artifact directory can be a local filesystem path

### Multi-Worker
- **Database**: Must use Postgres (`ISSUE_TO_PR_DATABASE_BACKEND=postgres`)
- **Artifact storage**: Must be a shared filesystem or object store
- **Workers**: Each worker needs a unique `--worker-id`
- **Concurrency**: Configure `ISSUE_TO_PR_QUEUE_MAX_RUNNING_JOBS_PER_WORKER`

## Monitoring

### Health Check
```bash
curl http://localhost:8080/healthz
# Returns: {"status": "ok"}
```

### Metrics
```bash
curl -H "Authorization: Bearer $API_TOKEN" http://localhost:8080/metrics
# Returns Prometheus-formatted metrics
```

### Key Metrics to Monitor
- **Queue depth**: Number of QUEUED jobs
- **Job processing time**: Duration of plan/verify/deliver jobs
- **Error rate**: Failed jobs per hour
- **Worker heartbeat**: Workers reporting in regularly

### Alerts
Built-in alerts fire when:
- Failed jobs exceed `ISSUE_TO_PR_ALERT_FAILED_JOBS_THRESHOLD`
- Stale leases exceed `ISSUE_TO_PR_ALERT_STALE_LEASE_THRESHOLD`
- Alerts are deduplicated within `ISSUE_TO_PR_ALERT_DEDUPE_SECONDS`

## Backup and Restore

```bash
# Create a backup
issue-to-pr backup-state --output-dir /backups

# Restore from backup
issue-to-pr restore-state --manifest-path /backups/manifest.json --target-dir /data
```

## Incident Response

### Job Stuck in RUNNING State
1. Check worker heartbeats: `issue-to-pr worker-heartbeats`
2. If the worker is dead, the lease will expire and the job will be requeued automatically
3. To force-cancel: `issue-to-pr cancel-job --job-id <id> --actor admin`

### Database Issues
1. Check schema status: `issue-to-pr schema-status --json`
2. Migrations run automatically on startup
3. For manual recovery, restore from backup

### High Error Rate
1. Check alerts: `issue-to-pr alerts --status open --limit 20`
2. Check traces: `issue-to-pr traces --limit 20`
3. Check queue attempts: `issue-to-pr queue-attempts --job-id <id>`

### Worker Not Processing Jobs
1. Verify worker is running and healthy
2. Check worker tags match job requirements
3. Check concurrency limits: `ISSUE_TO_PR_QUEUE_MAX_RUNNING_JOBS_PER_WORKER`
4. Check budget limits: `ISSUE_TO_PR_BUDGET_MAX_PENDING_JOBS`

## Security Checklist

- [ ] `ISSUE_TO_PR_ENV=production` is set
- [ ] Auth is configured (API token or JWT secret)
- [ ] Webhook secrets are set for GitHub, Jira, Slack
- [ ] API is behind a reverse proxy with TLS
- [ ] CORS origin is restricted to your dashboard domain
- [ ] Docker network is set to `none` for sandboxed execution
- [ ] Database credentials are not in environment files committed to git
- [ ] `.env` is in `.gitignore`
