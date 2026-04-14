# Issue-to-PR Agent

`issue-to-pr-agent` is a product-oriented foundation for a GitHub issue workflow agent.

It is still intentionally narrow, but it now has the control surfaces an enterprise team expects:

- validated settings loaded from environment
- structured planning with heuristic or OpenAI backends
- persistent run history in SQLite
- run-specific artifacts and audit JSON
- command safety classification and branch guardrails
- operator CLI commands for planning, listing runs, and inspecting historical runs
- guarded patch proposal execution with dry-run and apply modes
- autonomous patch proposal generation from planning runs using the OpenAI patcher
- bounded autonomous autofix runs that generate, apply, verify, and retry with reflected failure context
- execution receipts persisted to SQLite and written to artifact folders
- verification runs with captured logs, retry decisions, and explicit stop reasons
- gated GitHub delivery with branch push, draft PR creation, PR summary comments, and delivery receipts
- human approval requests with reviewer decisions, repo policy evaluation, and delivery gates
- tenant-scoped platform controls with RBAC, dashboard summaries, and notification outbox events
- durable worker queueing with retries, cancellation/resume semantics, queue budgets, worker heartbeats, and metrics export
- leased queue scheduling with stale-job reclamation, worker-tag affinity, concurrency keys, and tenant-aware claim fairness
- isolated sandbox sessions for copied-workspace execution and sandboxed autofix runs
- dependency-free HTTP control-plane API with webhook ingestion and optional bearer-token auth
- Docker-backed verification runtime selection for `verify`, `autofix`, API, and queued verification jobs
- clean git repositories now materialize as cloned sandboxes with preserved remotes and commit identity for isolated execution
- expanded versioned workflow API with OpenAPI discovery, request IDs, idempotent POST handling, queue mutation routes, and per-client rate limiting
- Jira issue intake, Slack approval action ingestion, and Slack, Teams, or Jira notification fanout for enterprise workflow integration
- optional shared artifact-store publication for delivery summaries and PR artifact links
- persisted trace events, emitted alerts, audit bundle export, retention enforcement, and optional external telemetry fanout
- delivery governance policy-as-code with branch-protection checks, rollout-stage gates, model or command allowlists, and rollback metadata
- release-engineering controls for schema inspection, backup and restore, generated release manifests, and deterministic smoke tests
- advanced agent intelligence with repository symbol indexing, plan and patch evaluation scores, and cost-aware model routing

The system remains safe by default. Destructive side effects are behind explicit commands and linked receipts. Delivery only proceeds from a successful plan, apply-mode execution, successful verification, and an approved approval request when policy requires one.

## Product Baseline

The current baseline is designed around traceability and controlled expansion:

- every run gets a unique run ID
- every run produces isolated artifacts under `.issue-to-pr/runs/<run-id>/`
- every run is persisted to `.issue-to-pr/agent_runs.sqlite3`
- planned commands are classified as `allow`, `review`, or `block`
- branch creation is opt-in and validated against a managed prefix
- planning now uses repository profiling, ranked file candidates, and normalized test suggestions

This is the right starting point for later layers such as queue workers, sandboxed execution, and richer delivery governance.
The phase roadmap is now implemented through Phase 22, with release-management and agent-intelligence layers included in the baseline.

## Architecture

High-level component notes are in [docs/architecture.md](docs/architecture.md).
Phase planning is in [docs/phases.md](docs/phases.md).

Current modules:

```text
apps/
  api/
  cli/
  worker/
src/issue_to_pr_agent/
  application/
    services/
    use_cases/
  agents/
    context_builder/
    planner/
  domain/
    policies/
  infrastructure/
    config/
    persistence/
    scm/
  integrations/
    github/
    openai/
  interfaces/
    cli/
    http/
  observability/
    logging/
  shared/
  ...
tests/
  unit/
```

## Quickstart

1. Install the project:

```bash
python3 -m pip install -e .
```

2. Plan from a GitHub issue with the heuristic planner:

```bash
issue-to-pr plan \
  --repo octocat/Hello-World \
  --issue 1 \
  --repo-root . \
  --provider heuristic
```

3. Or plan with OpenAI:

```bash
export OPENAI_API_KEY=...
export OPENAI_MODEL=gpt-4.1-mini

issue-to-pr plan \
  --repo octocat/Hello-World \
  --issue 1 \
  --repo-root . \
  --provider openai
```

4. Review historical runs:

```bash
issue-to-pr runs
issue-to-pr show-run --run-id <run-id>
```

5. Draft and execute a patch proposal:

```bash
issue-to-pr draft-patch --run-id <run-id>

issue-to-pr execute-patch \
  --proposal-file .issue-to-pr/runs/<run-id>/patch-proposal.template.json \
  --repo-root . \
  --mode dry_run
```

Or generate an autonomous patch proposal first:

```bash
export OPENAI_API_KEY=...

issue-to-pr generate-patch \
  --run-id <run-id> \
  --repo-root .

issue-to-pr patch-proposals
issue-to-pr show-patch-proposal --proposal-id <proposal-id>
```

Or let the agent run the bounded autofix loop end to end:

```bash
issue-to-pr autofix \
  --run-id <run-id> \
  --repo-root .

issue-to-pr autofix-runs
issue-to-pr show-autofix-run --autofix-id <autofix-id>
issue-to-pr autofix-attempts --autofix-id <autofix-id>
```

Or isolate the workspace first and run autofix inside the sandbox copy:

```bash
issue-to-pr prepare-sandbox --repo-root . --run-id <run-id>
issue-to-pr sandboxes

issue-to-pr autofix \
  --run-id <run-id> \
  --repo-root . \
  --sandbox

issue-to-pr show-sandbox --sandbox-id <sandbox-id>
issue-to-pr cleanup-sandbox --sandbox-id <sandbox-id>
```

Or run verification inside Docker instead of on the host:

```bash
issue-to-pr verify --run-id <run-id> --repo-root . --runtime docker

issue-to-pr autofix \
  --run-id <run-id> \
  --repo-root . \
  --sandbox \
  --runtime docker
```

Or run patch apply, verify, and delivery against an isolated sandbox workspace:

```bash
issue-to-pr execute-patch \
  --proposal-file .issue-to-pr/runs/<run-id>/patch-proposal.template.json \
  --repo-root . \
  --mode apply \
  --sandbox

issue-to-pr verify --execution-id <execution-id> --sandbox-id <sandbox-id> --runtime docker

issue-to-pr deliver \
  --run-id <run-id> \
  --execution-id <execution-id> \
  --verification-id <verification-id> \
  --approval-id <approval-id> \
  --sandbox-id <sandbox-id> \
  --rollout-stage staging
```

6. Verify the run or a linked execution:

```bash
issue-to-pr verify --run-id <run-id> --repo-root .
issue-to-pr verifications
issue-to-pr show-verification --verification-id <verification-id>
```

7. Request and review approval when the policy requires it:

```bash
issue-to-pr request-approval \
  --run-id <run-id> \
  --execution-id <execution-id> \
  --verification-id <verification-id> \
  --actor alice \
  --team platform

issue-to-pr review-approval \
  --approval-id <approval-id> \
  --actor bob \
  --team platform \
  --decision approve

issue-to-pr approvals --status pending
issue-to-pr show-approval --approval-id <approval-id>
```

8. Deliver a verified execution to GitHub:

```bash
export GITHUB_TOKEN=...

issue-to-pr deliver \
  --run-id <run-id> \
  --execution-id <execution-id> \
  --verification-id <verification-id> \
  --approval-id <approval-id> \
  --repo-root . \
  --rollout-stage staging

issue-to-pr deliveries
issue-to-pr show-delivery --delivery-id <delivery-id>
```

9. Register a tenant and operate the platform control plane:

```bash
issue-to-pr register-tenant \
  --tenant-id acme \
  --name "Acme Corp" \
  --repo-pattern "acme/*" \
  --admin-actor alice \
  --admin-team platform

issue-to-pr add-member \
  --tenant-id acme \
  --actor alice \
  --member-actor bob \
  --role reviewer \
  --team platform

issue-to-pr dashboard --tenant-id acme --actor alice
issue-to-pr notifications --tenant-id acme --actor alice
```

10. Queue work for asynchronous execution:

```bash
issue-to-pr enqueue-plan \
  --repo octocat/Hello-World \
  --issue 1 \
  --repo-root . \
  --actor alice \
  --team platform \
  --worker-tag docker \
  --concurrency-key repo:octocat/Hello-World

issue-to-pr queue-jobs
issue-to-pr worker-run --worker-id worker-1 --worker-tag docker --max-jobs 5
issue-to-pr queue-attempts --job-id <job-id>
issue-to-pr worker-heartbeats
issue-to-pr metrics
issue-to-pr alerts --limit 20
issue-to-pr traces --limit 20
issue-to-pr export-audit --run-id <run-id>
issue-to-pr enforce-retention --dry-run
```

11. Inspect schema state, build a release manifest, or run the smoke workflow:

```bash
issue-to-pr schema-status --json
issue-to-pr backup-state
issue-to-pr release-manifest
issue-to-pr smoke-test
```

12. Run the HTTP control-plane API:

```bash
export ISSUE_TO_PR_API_TOKEN=local-dev-token
issue-to-pr-api --host 127.0.0.1 --port 8080
```

Example requests:

```bash
curl -H "Authorization: Bearer local-dev-token" \
  http://127.0.0.1:8080/healthz

curl -X POST \
  -H "Authorization: Bearer local-dev-token" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8080/v1/plan \
  -d '{"repo":"octocat/Hello-World","issue":1,"repo_root":".","provider":"heuristic"}'

curl -H "Authorization: Bearer local-dev-token" \
  http://127.0.0.1:8080/v1/openapi.json

curl -X POST \
  -H "Authorization: Bearer local-dev-token" \
  -H "Idempotency-Key: enqueue-plan-1" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8080/v1/queue/plan \
  -d '{"repo":"octocat/Hello-World","issue":1,"repo_root":".","actor":"alice","team":"platform"}'
```

## Phase 1 Outcome

This phase establishes the modular monolith baseline:

- `domain`: entities and policies
- `application`: use-case orchestration
- `agents`: planner logic that is internal to the product
- `integrations`: external system adapters such as GitHub and OpenAI
- `infrastructure`: config, persistence, and SCM adapters
- `interfaces`: operator-facing entrypoints
- `observability`: structured logging and later metrics/tracing
- `shared`: cross-cutting exceptions and common utilities

## Phase 2 Outcome

Phase 2 adds a real planning context layer:

- repository profiling for primary language, frameworks, and build/test hints
- ranked file candidates based on issue keywords and repository structure
- preview snippets for the highest-value files
- normalized plan output that filters unknown files and fills in missing test commands
- richer plan reports and audit payloads that include planning context

## Phase 3 Outcome

Phase 3 adds guarded execution:

- patch proposal schema with `replace_text`, `append_text`, and `write_file`
- workspace guardrails for path traversal, blocked directories, overwrite rules, and ambiguous replacements
- deterministic dry-run and apply execution modes
- file mutation receipts with before/after hashes and byte counts
- persisted execution receipts with operator CLI commands:
  - `executions`
  - `show-execution`
  - `draft-patch`
  - `execute-patch`

## Phase 4 Outcome

Phase 4 adds verification:

- repo-aware verification command selection from the planning context
- standardized stdout/stderr capture for each attempt
- bounded retry logic across candidate commands
- reflection notes explaining why the verifier retried or stopped
- explicit stop reasons such as `success`, `no_allowed_commands`, and `max_attempts_reached`
- persisted verification receipts with operator CLI commands:
  - `verify`
  - `verifications`
  - `show-verification`

## Phase 5 Outcome

Phase 5 adds GitHub delivery:

- validates that the linked plan, execution, and verification records all succeeded
- checks the current workspace for unexpected changes before commit or push
- creates the delivery branch when needed, commits verified changes, and pushes to the configured remote
- opens a draft pull request and posts an optional PR summary comment
- persists delivery receipts with operator CLI commands:
  - `deliver`
  - `deliveries`
  - `show-delivery`

## Phase 6 Outcome

Phase 6 adds human approval:

- evaluates delivery risk from labels, command assessments, changed paths, and verification behavior
- creates approval requests tied to specific run, execution, and verification receipts
- supports reviewer decisions with self-approval protection and team-based review checks
- blocks high-risk delivery until an approved approval request is supplied
- loads repo and team controls from an approval policy file such as [docs/approval-policy.example.json](docs/approval-policy.example.json)
- persists approval receipts with operator CLI commands:
  - `request-approval`
  - `review-approval`
  - `approvals`
  - `show-approval`

## Phase 7 Outcome

Phase 7 adds the enterprise platform layer:

- tenant registration with bootstrap admin membership and repo-pattern ownership
- tenant-scoped RBAC for approval requests, approval reviews, delivery, dashboards, and notification access
- tenant-specific approval policy overrides layered on top of the global approval policy
- dashboard summaries that aggregate runs, approvals, deliveries, and notification activity per tenant
- file-backed notification outbox events for approval and delivery lifecycle changes
- persisted tenant, membership, and notification records with operator CLI commands:
  - `register-tenant`
  - `tenants`
  - `set-tenant-policy`
  - `set-tenant-status`
  - `add-member`
  - `members`
  - `dashboard`
  - `notifications`

## Phase 8 Outcome

Phase 8 adds scale and reliability primitives:

- durable queue jobs for planning, verification, and delivery
- worker processing loop with persisted attempts, backoff-based retries, and cooperative cancellation
- queue job resume semantics with optional attempt and budget reset
- queue budgets that bound pending jobs, per-tenant pending jobs, per-job budget units, and per-attempt cost
- worker heartbeat receipts and exported queue metrics in both JSON and Prometheus text formats
- operator CLI commands:
  - `enqueue-plan`
  - `enqueue-verify`
  - `enqueue-deliver`
  - `queue-jobs`
  - `show-job`
  - `cancel-job`
  - `resume-job`
  - `queue-attempts`
  - `worker-run`
  - `worker-heartbeats`
  - `metrics`

## Phase 9 Outcome

Phase 9 adds the bounded autonomous repair loop:

- generates patch proposals directly from the planning run and current workspace state
- applies the generated patch in `apply` mode and captures execution receipts
- verifies each attempt against repo-aware test commands
- builds the next patch objective from execution and verification failure evidence
- persists autofix runs and attempt receipts with operator CLI commands:
  - `autofix`
  - `autofix-runs`
  - `show-autofix-run`
  - `autofix-attempts`

## Phase 10 Outcome

Phase 10 adds isolated sandbox execution:

- creates copied workspace sandboxes under the artifact directory
- excludes internal artifact folders and oversized files from the sandbox materialization
- persists sandbox session receipts for prepare, use, and cleanup lifecycle events
- supports sandboxed autofix so the source workspace stays untouched during autonomous repair
- operator CLI commands:
  - `prepare-sandbox`
  - `sandboxes`
  - `show-sandbox`
  - `cleanup-sandbox`

## Phase 11 Outcome

Phase 11 adds the HTTP control plane:

- dependency-free JSON API for planning, autofix, sandbox preparation, and workflow inspection
- optional static bearer-token protection for non-health and non-webhook routes
- GitHub issue webhook ingestion that validates `X-Hub-Signature-256` when a webhook secret is configured
- queued planning job creation from webhook events using repo-to-local-root mappings
- HTTP server entrypoint:
  - `issue-to-pr-api`

## Phase 12 Outcome

Phase 12 adds container-backed verification runtime isolation:

- configurable verification runtime selection for CLI, HTTP API, and queued verification jobs
- Docker-backed command execution with bind-mounted workspaces and configurable CPU, memory, and network limits
- runtime settings and validation for container execution
- operator-facing runtime flags:
  - `verify --runtime`
  - `autofix --runtime`

## Phase 13 Outcome

Phase 13 adds first-class isolated execution workspaces:

- clean git repositories now prepare sandboxes as local clones instead of plain file copies
- cloned sandboxes preserve git remotes plus local commit identity so delivery can happen from the isolated workspace
- `execute-patch` can now prepare and use a sandbox directly with `--sandbox`
- `verify` and `deliver` can now target an existing sandbox through `--sandbox-id`
- sandbox receipts now capture the materialization strategy and source git metadata

## Phase 14 Outcome

Phase 14 expands the control plane into a broader production API surface:

- exposes versioned JSON endpoints for planning, patch generation, patch execution, verification, approvals, delivery, queue operations, sandboxes, and workflow inspection
- publishes a machine-readable OpenAPI document at `GET /v1/openapi.json`
- adds request correlation through `X-Request-ID` response headers
- adds idempotent POST replay for `Idempotency-Key` and GitHub webhook delivery IDs
- adds per-client API rate limiting with response headers for remaining quota and reset time
- adds paginated list responses with `limit`, `offset`, and `next_offset`

## Phase 15 Outcome

Phase 15 adds enterprise identity and access foundations:

- signed bearer-token authentication with user and service principal claims
- principal-aware tenant RBAC for HTTP approval, delivery, queue, and identity operations
- identity-sync workflows for tenant membership upsert or replacement
- approval expiry windows plus explicit reviewer and reviewer-team assignment
- CLI support for `sync-memberships` and expiring approval requests

## Phase 16 Outcome

Phase 16 adds the first reviewer and operator UI surface:

- browser-based operator console at `GET /ui`
- tenant dashboard and notification APIs for UI consumption
- in-browser approval review and queue cancel or resume actions on top of the existing workflow API
- live record inspection for runs, approvals, deliveries, and queue jobs without leaving the control plane

## Phase 17 Outcome

Phase 17 adds the first enterprise integration layer:

- Jira issue webhooks can now enqueue planning jobs from configured project-to-repository mappings
- queue workers can plan from first-class external issue context without fetching a GitHub issue first
- Slack approval action webhooks can now review pending approvals through the same policy and persistence path as the CLI or API
- notification events can now fan out to Slack, Teams, and Jira comments while still being persisted locally for audit
- direct API approval and delivery operations now emit tenant notification records, not just CLI and worker flows

## Phase 18 Outcome

Phase 18 upgrades the queue and worker platform for distributed execution patterns:

- queue jobs are now claimed through bounded leases instead of simple long-running ownership
- expired running jobs are reclaimed automatically and requeued with resume metadata for later workers
- queue jobs can declare `required_worker_tags` and `concurrency_key` scheduling constraints
- claim selection now applies tenant-aware fairness so heavily loaded tenants do not monopolize workers
- worker heartbeats now publish active lease state, advertised worker tags, and queue capacity
- delivery artifacts can optionally be mirrored into a shared artifact store with externally resolvable URLs

## Phase 19 Outcome

Phase 19 adds observability and compliance controls:

- HTTP requests and worker job lifecycles now emit persisted trace events with shared trace identifiers
- queue failures and unhealthy queue snapshots now produce persisted alert receipts with dedupe windows
- alerts and traces can optionally fan out to an external telemetry sink over HTTP
- run-scoped audit bundles can now be exported with JSON bundle, artifact manifest, and zip archive outputs
- retention rules can evaluate and prune old notifications, worker heartbeats, alerts, and trace events
- new operator CLI commands:
  - `alerts`
  - `traces`
  - `export-audit`
  - `enforce-retention`

## Phase 20 Outcome

Phase 20 hardens delivery and change governance:

- delivery now evaluates a policy-as-code governance layer before commit, push, and PR creation
- governance can restrict repositories, changed paths, planner or patch providers, allowed models, and blocked command patterns
- high-risk path changes can require an explicit `--rollout-stage`, while production paths can force `production`
- delivery verifies base-branch protection through the GitHub API before proceeding when policy requires it
- successful delivery receipts now record rollout stage, rollback base SHA, branch-protection status, and the applied governance snapshot
- use [docs/delivery-governance.example.json](docs/delivery-governance.example.json) with `ISSUE_TO_PR_DELIVERY_GOVERNANCE_POLICY_PATH` to customize rollout and model policy

## Phase 21 Outcome

Phase 21 adds reliability and release-engineering primitives:

- schema migrations are now recorded as first-class receipts and exposed through `schema-status`
- operators can create backup bundles for the database and artifact store with `backup-state`
- backup manifests can be restored into a new target directory with `restore-state`
- release manifests capture runtime, schema, storage, and API route expectations with `release-manifest`
- `smoke-test` runs a deterministic local workflow that covers planning, patch generation, patch apply, and verification end to end

## Phase 22 Outcome

Phase 22 adds advanced agent intelligence:

- repository context now includes an indexed symbol catalog and a derived complexity score
- plans and patch proposals now carry evaluation summaries with scored reasoning about quality and scope
- OpenAI planning and patch generation now route between standard and complex models based on repository and patch-context complexity
- planning artifacts persist repository index, evaluation, and routed planner-model metadata
- generated patch artifacts persist evaluation and routed patch-model metadata for later governance and audit

## Environment Variables

- `APP_ENV`: `local`, `staging`, or `production`
- `GITHUB_TOKEN`: optional for public repos, recommended for private repos and higher rate limits
- `GITHUB_API_BASE_URL`: optional, defaults to `https://api.github.com`
- `OPENAI_API_KEY`: required when `--provider openai`
- `OPENAI_MODEL`: optional, defaults to `gpt-4.1-mini`
- `ISSUE_TO_PR_OPENAI_COMPLEX_MODEL`: optional higher-capability model for complex planning or patching, defaults to `OPENAI_MODEL`
- `OPENAI_BASE_URL`: optional, defaults to `https://api.openai.com/v1`
- `ISSUE_TO_PR_APPROVAL_POLICY_PATH`: optional path to a JSON approval policy file
- `ISSUE_TO_PR_DELIVERY_GOVERNANCE_POLICY_PATH`: optional path to a JSON delivery-governance policy file such as [docs/delivery-governance.example.json](docs/delivery-governance.example.json)
- `ISSUE_TO_PR_ARTIFACT_DIR`: optional, defaults to `.issue-to-pr`
- `ISSUE_TO_PR_ARTIFACT_BASE_URL`: optional external base URL used to convert local artifact paths into PR/comment links
- `ISSUE_TO_PR_DB_PATH`: optional, defaults to `<artifact-dir>/agent_runs.sqlite3`
- `ISSUE_TO_PR_DATABASE_BACKEND`: optional, `sqlite` or `postgres`, defaults to `sqlite`
- `ISSUE_TO_PR_DATABASE_URL`: optional connection URL for non-file database backends; required when `ISSUE_TO_PR_DATABASE_BACKEND=postgres`
- `ISSUE_TO_PR_NOTIFICATION_DIR`: optional, defaults to `<artifact-dir>/notifications`
- `ISSUE_TO_PR_METRICS_DIR`: optional, defaults to `<artifact-dir>/metrics`
- `ISSUE_TO_PR_TELEMETRY_DIR`: optional, defaults to `<artifact-dir>/telemetry`
- `ISSUE_TO_PR_AUDIT_EXPORT_DIR`: optional, defaults to `<artifact-dir>/audit-exports`
- `ISSUE_TO_PR_SANDBOX_DIR`: optional, defaults to `<artifact-dir>/sandboxes`
- `ISSUE_TO_PR_ARTIFACT_STORE_BACKEND`: optional, `filesystem` or `shared`, defaults to `filesystem`
- `ISSUE_TO_PR_ARTIFACT_STORE_DIR`: optional, defaults to `<artifact-dir>/artifact-store`
- `ISSUE_TO_PR_ARTIFACT_STORE_BASE_URL`: optional external base URL for shared artifact-store links
- `ISSUE_TO_PR_TELEMETRY_SINK_URL`: optional external HTTP(S) sink for alert and trace fanout
- `ISSUE_TO_PR_SANDBOX_MAX_FILE_BYTES`: optional per-file copy ceiling for sandbox materialization, defaults to `10485760`
- `ISSUE_TO_PR_ROUTER_PLANNER_COMPLEXITY_THRESHOLD`: optional routing threshold for planner model escalation, defaults to `14`
- `ISSUE_TO_PR_ROUTER_PATCH_COMPLEXITY_THRESHOLD`: optional routing threshold for patch-model escalation, defaults to `18`
- `ISSUE_TO_PR_VERIFICATION_RUNTIME`: optional, `local` or `docker`, defaults to `local`
- `ISSUE_TO_PR_DOCKER_BINARY`: optional, defaults to `docker`
- `ISSUE_TO_PR_DOCKER_IMAGE`: optional, defaults to `python:3.11-slim`
- `ISSUE_TO_PR_DOCKER_NETWORK`: optional, defaults to `none`
- `ISSUE_TO_PR_DOCKER_MEMORY_MB`: optional, defaults to `1024`
- `ISSUE_TO_PR_DOCKER_CPUS`: optional, defaults to `1.0`
- `ISSUE_TO_PR_API_HOST`: optional, defaults to `127.0.0.1`
- `ISSUE_TO_PR_API_PORT`: optional, defaults to `8080`
- `ISSUE_TO_PR_API_TOKEN`: optional bearer token for non-health HTTP API routes
- `ISSUE_TO_PR_AUTH_TOKEN_SECRET`: optional HMAC secret for signed bearer principals
- `ISSUE_TO_PR_AUTH_TOKEN_ISSUER`: optional issuer string expected on signed bearer principals
- `ISSUE_TO_PR_API_RATE_LIMIT_PER_MINUTE`: optional per-client HTTP request limit, defaults to `120`
- `ISSUE_TO_PR_APPROVAL_TTL_HOURS`: optional approval expiry window in hours, defaults to `24`
- `ISSUE_TO_PR_WEBHOOK_SECRET`: optional GitHub webhook HMAC secret
- `ISSUE_TO_PR_WEBHOOK_ACTOR`: optional queue actor used for webhook-created jobs, defaults to `webhook-bot`
- `ISSUE_TO_PR_WEBHOOK_TEAM`: optional queue team used for webhook-created jobs, defaults to `automation`
- `ISSUE_TO_PR_WEBHOOK_REPO_ROOTS_PATH`: optional JSON file mapping `owner/name` repositories to local repo roots for webhook plan ingestion
- `ISSUE_TO_PR_JIRA_BASE_URL`: optional Jira base URL used for webhook ticket URLs and Jira comment fanout
- `ISSUE_TO_PR_JIRA_TOKEN`: optional Jira API token used for Jira comment fanout
- `ISSUE_TO_PR_JIRA_PROJECT_MAPPINGS_PATH`: optional JSON file mapping Jira project keys to repository and repo-root targets for webhook planning
- `ISSUE_TO_PR_JIRA_WEBHOOK_SECRET`: optional shared secret expected on Jira webhook requests
- `ISSUE_TO_PR_SLACK_WEBHOOK_URL`: optional Slack incoming-webhook URL for notification fanout
- `ISSUE_TO_PR_SLACK_SIGNING_SECRET`: optional Slack signing secret for approval action webhooks
- `ISSUE_TO_PR_TEAMS_WEBHOOK_URL`: optional Microsoft Teams webhook URL for notification fanout
- `ISSUE_TO_PR_LOG_LEVEL`: optional, defaults to `INFO`
- `ISSUE_TO_PR_MAX_REPO_FILES`: optional, defaults to `200`
- `ISSUE_TO_PR_BRANCH_PREFIX`: optional, defaults to `agent/`
- `ISSUE_TO_PR_GIT_REMOTE`: optional, defaults to `origin`
- `ISSUE_TO_PR_QUEUE_BACKOFF_SECONDS`: optional, defaults to `30`
- `ISSUE_TO_PR_QUEUE_MAX_ATTEMPTS`: optional, defaults to `3`
- `ISSUE_TO_PR_QUEUE_LEASE_SECONDS`: optional queue lease duration in seconds, defaults to `900`
- `ISSUE_TO_PR_QUEUE_MAX_RUNNING_JOBS_PER_WORKER`: optional worker-side concurrent lease ceiling, defaults to `4`
- `ISSUE_TO_PR_QUEUE_MAX_RUNNING_JOBS_PER_TENANT`: optional concurrent running-job ceiling per tenant during claim selection, defaults to `2`
- `ISSUE_TO_PR_QUEUE_CANDIDATE_SCAN_LIMIT`: optional queued-job scan depth used during fair claim selection, defaults to `200`
- `ISSUE_TO_PR_ALERT_STALE_LEASE_THRESHOLD`: optional stale-lease alert threshold, defaults to `1`
- `ISSUE_TO_PR_ALERT_FAILED_JOBS_THRESHOLD`: optional failed-job alert threshold, defaults to `5`
- `ISSUE_TO_PR_ALERT_DEDUPE_SECONDS`: optional alert dedupe window, defaults to `3600`
- `ISSUE_TO_PR_RETENTION_NOTIFICATION_DAYS`: optional notification retention window, defaults to `30`
- `ISSUE_TO_PR_RETENTION_WORKER_HEARTBEAT_DAYS`: optional worker-heartbeat retention window, defaults to `7`
- `ISSUE_TO_PR_RETENTION_ALERT_DAYS`: optional alert retention window, defaults to `30`
- `ISSUE_TO_PR_RETENTION_TRACE_DAYS`: optional trace-event retention window, defaults to `14`
- `ISSUE_TO_PR_BUDGET_MAX_UNITS_PER_JOB`: optional, defaults to `20`
- `ISSUE_TO_PR_BUDGET_MAX_PENDING_JOBS`: optional, defaults to `100`
- `ISSUE_TO_PR_BUDGET_MAX_PENDING_JOBS_PER_TENANT`: optional, defaults to `25`
- `ISSUE_TO_PR_BUDGET_COST_PLAN_HEURISTIC`: optional, defaults to `1`
- `ISSUE_TO_PR_BUDGET_COST_PLAN_OPENAI`: optional, defaults to `5`
- `ISSUE_TO_PR_BUDGET_COST_VERIFY`: optional, defaults to `2`
- `ISSUE_TO_PR_BUDGET_COST_DELIVER`: optional, defaults to `3`

## Output

Each planning run creates a unique directory under `.issue-to-pr/runs/`:

- `plan.md`
- `pr.md`
- `run.json`

Run metadata is also stored centrally in SQLite for audit and operator workflows.

## What Still Needs Work

This is a product foundation, not a finished enterprise platform. The major gaps are:

- full containerized or VM-backed patch generation and patch application instead of host-side mutation inside local sandboxes
- full SSO, SCIM provisioning, and external identity-provider session integration
- rollback handling and approval escalation workflows
- Postgres-backed coordination and object-storage-backed artifacts beyond the upgraded leased SQLite queue
- deeper web UI coverage for tenant administration, delivery creation, and diff or log exploration
- external dashboards, SIEM-grade sinks, and stronger cross-system observability integrations
- production rollout, migrations, and multi-host operational hardening
