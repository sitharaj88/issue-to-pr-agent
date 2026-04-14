# Delivery Phases

## Phase 1: Foundation

Delivered in the current refactor:

- modular monolith package structure
- validated configuration
- run persistence and audit artifacts
- logging and command safety policy
- operator CLI

## Phase 2: Context And Planning

Delivered:

- richer code context builder
- symbol and file ranking
- repository heuristics by language and framework
- improved planning prompts and plan validation

## Phase 3: Execution

Delivered:

- patch proposal model
- safe patch application
- workspace guardrails
- execution receipts

## Phase 4: Verification

Delivered:

- repo-specific test strategy
- standardized test result capture
- reflection loop on failure
- bounded retries and stop reasons

## Phase 5: GitHub Delivery

Delivered:

- branch push
- draft PR creation
- PR comments and run summaries
- artifact linking

## Phase 6: Human Approval

Delivered:

- reviewer queue
- action approvals
- risk-based policy gates
- repo and team level controls

## Phase 7: Enterprise Platform

Delivered:

- tenant registration and bootstrap admin membership
- tenant-scoped RBAC for approval, review, delivery, dashboard, and notification access
- tenant policy overrides layered onto the approval policy engine
- dashboard summaries across runs, approvals, deliveries, and notifications
- file-backed notification outbox events

## Phase 8: Scale And Reliability

Delivered:

- durable queue jobs for plan, verify, and deliver workflows
- worker processing loop with persisted attempts, retries, and cooperative cancellation
- resume semantics for failed or cancelled jobs
- worker heartbeat receipts and queue metrics export
- queue budgets and cost controls across global, tenant, and per-job scopes

## Phase 9: Autonomous Repair Loop

Delivered:

- autonomous patch generation integrated into a bounded repair loop
- guarded apply-mode execution followed by verification on every attempt
- retry objective reflection from execution errors, failing commands, and verification logs
- persisted autofix runs and attempt receipts for operator audit

## Phase 10: Sandboxed Execution

Delivered:

- persisted sandbox sessions with prepare, use, and cleanup lifecycle states
- local copied-workspace materialization that excludes internal artifacts and oversized files
- sandbox-aware autofix execution so autonomous repair can run without mutating the source workspace

## Phase 11: HTTP Control Plane

Delivered:

- dependency-free JSON API for planning, sandbox preparation, autofix, and workflow inspection
- optional static bearer-token protection for non-health, non-webhook routes
- GitHub issue webhook ingestion with optional HMAC signature verification
- queued planning job creation from webhook events using configured repo-root mappings

## Phase 12: Container-backed Verification Runtime

Delivered:

- configurable verification runtime selection for CLI, HTTP API, and queued verification jobs
- Docker-backed verification command execution with bind-mounted workspaces
- configurable Docker CPU, memory, and network limits through validated settings
- runtime-resolver coverage and HTTP autofix runtime selection tests

## Phase 13: First-class Isolated Execution Workspaces

Delivered:

- clean git repositories now prepare sandboxes as local clones with preserved remotes and commit identity
- non-git or dirty repositories still fall back to filtered copy-mode sandbox materialization
- new sandboxed patch execution orchestration for isolated apply-mode execution
- `verify` and `deliver` can now resolve sandbox workspaces by sandbox identifier
- sandbox receipts now capture materialization strategy plus source branch and commit metadata

## Phase 14: Production API Surface

Delivered:

- versioned workflow endpoints for patch generation, patch execution, verification, approvals, delivery, queue enqueue, queue cancel, and queue resume
- machine-readable OpenAPI discovery at `GET /v1/openapi.json`
- `X-Request-ID` response headers for request correlation
- idempotent POST replay for API clients and GitHub webhooks
- per-client rate limiting with response headers plus paginated list responses

## Phase 15: Identity And Enterprise Access

Delivered:

- signed bearer-token authentication with principal claims for the HTTP control plane
- principal-aware tenant and repo permission checks for both user and service identities
- identity sync endpoint and use case for tenant membership upsert and replacement flows
- approval expiry and reviewer assignment support across request, review, and delivery workflows
- CLI support for expiring approvals and tenant membership sync operations

## Phase 16: Reviewer And Operator UI

Delivered:

- browser-based operator console hosted directly by the control-plane API at `/ui`
- tenant dashboard and notification endpoints for the browser console
- reviewer actions for approval inspection plus approve or reject workflows from the UI
- operator queue controls for cancel or resume directly from the UI
- live JSON record inspection for runs, approvals, deliveries, and queue jobs

## Phase 17: Enterprise Integrations

Delivered:

- Jira issue webhook ingestion with project-key mappings to repo roots and repositories
- queue-backed planning from first-class external issue context instead of GitHub-only issue fetches
- Slack approval action webhook handling wired into the existing approval review path
- notification fanout to Slack, Teams, and Jira comments while preserving local notification receipts
- API approval and delivery actions now emit the same tenant notification records as the CLI and workers

## Phase 18: Distributed Worker Platform

Delivered:

- lease-based queue claiming with explicit lease expiry and stale-job reclamation
- worker-tag affinity and `concurrency_key` scheduling controls on queued jobs
- tenant-aware fair job selection when multiple queued jobs are eligible
- richer worker heartbeats that include active lease, advertised tags, and queue capacity
- queue metrics for leased jobs, stale leases, and running jobs by tenant
- optional shared artifact-store publication for delivery artifacts and external links

## Phase 19: Observability And Compliance

Delivered:

- persisted trace events for HTTP requests and worker-side queue execution
- emitted alert receipts for queue failures and unhealthy queue snapshots
- optional external telemetry fanout for trace and alert payloads
- run-scoped audit bundle export with bundle, manifest, and archive outputs
- retention enforcement for notifications, worker heartbeats, alerts, and trace events
- API and CLI operator surfaces for alerts, traces, audit export, and retention runs

## Phase 20: Delivery And Change Governance

Delivered:

- policy-as-code delivery governance with repo, path, command, provider, and model rules
- explicit rollout-stage delivery metadata for CLI, API, queue, and PR summaries
- branch-protection validation on the target base branch before delivery
- rollback-aware delivery receipts that capture the pre-delivery base SHA
- tenant override support for delivery governance rules alongside approval policy overrides

## Phase 21: Reliability And Release Engineering

Delivered:

- persisted schema-migration ledger with current-schema inspection
- operator backup and restore workflows for database plus artifact state
- generated release manifests for deployment-time environment and route verification
- deterministic smoke-test workflow that exercises plan, patch generation, patch apply, and verification end to end
- CLI commands for `schema-status`, `backup-state`, `restore-state`, `release-manifest`, and `smoke-test`

## Phase 22: Advanced Agent Intelligence

Delivered:

- repository indexing with extracted symbol metadata and persisted complexity scoring
- plan evaluation signals that score context depth, file targeting, and validation coverage
- patch-proposal evaluation signals that score edit scope, rationale, and test coverage
- cost-aware model routing between standard and complex OpenAI models for planning and patch generation
- richer planning and patch artifacts that persist repository index, evaluation output, and selected model metadata
