# Architecture

## Goal

Turn a GitHub issue into an auditable engineering work packet that can later drive autonomous execution safely.

## Current Control Flow

1. CLI or HTTP receives a planning, execution, verification, queue, approval, tenant-admin, dashboard, delivery, or webhook request.
2. Settings are loaded and validated.
3. Tenant ownership and RBAC are resolved when the repository is assigned to a tenant.
4. The local repository is inspected for branch, git state, and tracked files.
5. The GitHub issue or repository metadata is fetched when the workflow needs it.
6. A planner generates the proposed investigation and PR draft.
7. Safety policy classifies suggested commands and validates branch naming.
8. Artifacts are written to run-specific directories.
9. Planning, execution, verification, sandbox, queue, approval, delivery, tenant, notification, and worker receipts are persisted to SQLite for audit and operator inspection.

## Components

`domain/`
- entities, enums, and policy rules

`application/`
- use-case orchestration such as `plan_issue_to_pr`, `execute_patch_proposal`, `verify_run`, `manage_approval`, and `deliver_run`
- platform orchestration such as `manage_tenant` and `dashboard`

`agents/`
- product-native agent logic such as context building, heuristic planning, and patch generation

`integrations/`
- external adapters for GitHub and OpenAI, including the model-backed patcher

`infrastructure/`
- settings, persistence, local SCM operations, workspace mutation adapters, sandbox materialization, local and Docker verification runners, and notification outbox adapters

`interfaces/`
- CLI entrypoints plus the HTTP control-plane and webhook surfaces

`observability/`
- structured logging plus queue metrics export and worker heartbeat artifacts

`shared/`
- cross-cutting exceptions and common primitives

## Phase 2 Additions

`agents/context_builder/`
- repository profiling, keyword extraction, ranked file selection, and snippet previews

`application/services/`
- plan normalization and output shaping before policy review

The planning flow is now:

1. inspect the repository snapshot
2. build a structured planning context
3. generate a plan from that context
4. normalize the plan against known files and suggested tests
5. apply safety policy and persist the audited result

## Phase 3 Additions

`application/use_cases/execute_patch_proposal.py`
- validates and executes patch proposals, then persists execution receipts

`application/services/proposal_template.py`
- scaffolds operator-editable patch proposals from prior planning runs

`domain/policies/workspace.py`
- enforces execution guardrails around paths, file size, overwrites, and operation validity

`infrastructure/workspace/mutator.py`
- applies deterministic text mutations and records before/after metadata

Execution flow:

1. load a patch proposal
2. resolve a linked planning run when present
3. validate each operation against workspace guardrails
4. simulate or apply the file mutations
5. persist a structured execution receipt

## Phase 4 Additions

`application/services/verification_strategy.py`
- derives ordered verification command candidates from plan and repository context

`application/services/verification_reflection.py`
- decides whether to stop or continue after a failed attempt

`application/use_cases/verify_run.py`
- runs verification commands, captures artifacts, persists receipts, and records stop reasons

`infrastructure/verification/command_runner.py`
- executes bounded verification commands either on the host or inside Docker with captured stdout/stderr

`infrastructure/verification/runtime.py`
- resolves the configured verification runtime into a concrete runner implementation

Verification flow:

1. load candidate test commands from the planning run
2. filter commands through the safety policy
3. execute allowed commands one attempt at a time
4. capture stdout/stderr logs for every attempt
5. reflect on failures and either retry the next candidate or stop
6. persist a verification receipt with status and stop reason

## Phase 5 Additions

`application/services/delivery_summary.py`
- builds commit messages, PR content, PR comments, and artifact references from prior receipts

`application/use_cases/deliver_run.py`
- validates the linked run, execution, and verification records, then commits, pushes, creates the draft PR, and persists a delivery receipt

`infrastructure/scm/local_repo.py`
- now supports change discovery, staging, committing, remote checks, and branch push operations

`integrations/github/client.py`
- now supports repository metadata fetches, draft PR creation, and issue-style PR comments

Delivery flow:

1. load the linked plan, execution, and verification receipts
2. verify that all linked records succeeded and target the same repository root
3. fetch repository metadata and validate the delivery branch against policy
4. block if the current workspace contains changes outside the execution receipt
5. create or reuse the delivery branch, stage the verified files, commit, and push
6. open a draft pull request and optionally post a PR summary comment
7. persist a delivery receipt with artifact references and GitHub URLs

## Phase 6 Additions

`application/services/approval_policy.py`
- evaluates repo risk, merges repo/team approval policy overrides, and enforces reviewer permissions

`application/use_cases/manage_approval.py`
- creates approval requests, records reviewer decisions, and persists approval receipts

`infrastructure/persistence/run_repository.py`
- now stores approval requests as first-class records alongside runs, executions, verifications, and deliveries

Approval flow:

1. load the linked run, execution, and verification receipts
2. evaluate risk from labels, changed files, command assessments, and verification behavior
3. create a pending, approved, or rejected approval request depending on policy
4. collect reviewer decisions while enforcing self-approval and team restrictions
5. require an approved approval request before high-risk delivery can proceed

## Phase 7 Additions

`application/services/tenant_access.py`
- resolves tenant ownership from repo patterns and enforces tenant-scoped RBAC

`application/use_cases/manage_tenant.py`
- registers tenants, bootstraps admins, updates tenant status, and manages tenant-specific approval policy overrides

`application/use_cases/dashboard.py`
- builds tenant dashboards from persisted run, approval, delivery, and notification records

`infrastructure/notifications/file_outbox.py`
- emits file-backed notification events and stores notification receipts

`infrastructure/persistence/run_repository.py`
- now stores tenants, tenant memberships, and notifications alongside workflow receipts

Platform flow:

1. register a tenant with repo ownership patterns and a bootstrap admin
2. resolve tenant context whenever a repo-scoped approval, delivery, or dashboard action is requested
3. enforce role permissions before approval, review, delivery, or admin actions proceed
4. merge tenant policy overrides into the global approval policy when the repo is tenant-managed
5. emit notification events for approval and delivery changes
6. aggregate tenant-scoped workflow activity into dashboard summaries

## Phase 8 Additions

`application/services/queue_budget.py`
- enforces pending-job budgets, per-job budget ceilings, and per-attempt cost estimates

`application/use_cases/manage_queue.py`
- enqueues plan, verification, and delivery jobs and handles cancel/resume semantics

`application/use_cases/process_queue.py`
- claims queued jobs, runs retries with backoff, writes attempts, and emits worker heartbeats

`observability/metrics/queue.py`
- derives queue metrics and exports JSON plus Prometheus text snapshots

`infrastructure/persistence/run_repository.py`
- now stores queue jobs, queue attempts, and worker heartbeats as first-class records

Queue flow:

1. enqueue a plan, verification, or delivery job with queue metadata and a bounded budget
2. persist the queued job and expose it through operator inspection commands
3. worker claims the next eligible job through a bounded lease, subject to worker-tag affinity, concurrency keys, and tenant fairness
4. worker dispatches the existing plan, verification, or delivery use case
5. on failure, the worker either requeues with backoff or marks the job failed when attempts are exhausted
6. expired leases are reclaimed automatically and requeued with resume metadata for later workers
7. on cancel requests, queued jobs stop immediately and running jobs cancel cooperatively before retry
8. metrics snapshots summarize queue state, worker state, lease health, and budget usage for external scraping

## Current Autonomous Patching Slice

`agents/patcher/`
- patch-generation provider interface for autonomous code-change drafting

`application/use_cases/generate_patch_proposal.py`
- loads a planning run, reads live file context, generates a structured patch proposal, validates path scope, and persists the result

`integrations/openai/patcher.py`
- uses the OpenAI chat completions API to produce structured `replace_text`, `append_text`, and `write_file` proposals

Autonomous patch generation flow:

1. load the linked planning run and derive the allowed patch scope
2. read the highest-value file contents from the current repository root
3. ask the patcher to produce a minimal structured patch proposal
4. validate the generated proposal against the allowed existing paths and suggested new-file directories
5. persist the proposal for operator review and later execution

## Autonomous Repair Loop

`application/services/patch_reflection.py`
- builds the next patch objective from execution and verification evidence, including failing commands and log excerpts

`application/use_cases/run_autofix.py`
- orchestrates bounded generate/apply/verify retries, persists autofix receipts, and records every attempt

`infrastructure/persistence/run_repository.py`
- now stores autofix runs and autofix attempts as first-class records

Autonomous repair flow:

1. load the linked planning run and create an autofix receipt
2. generate a structured patch proposal from the live workspace
3. apply the proposal in guarded `apply` mode and capture the execution receipt
4. verify the resulting workspace using the existing verification strategy
5. on failure, reflect on execution or verification evidence to produce the next attempt objective
6. stop on success or after the configured maximum attempts, then persist the final autofix receipt

## Sandbox Isolation

`application/use_cases/manage_sandbox.py`
- prepares, links, and cleans up persisted sandbox sessions

`application/use_cases/run_sandboxed_autofix.py`
- combines sandbox preparation with the bounded autofix loop so repairs run against a copied workspace

`application/use_cases/run_sandboxed_patch_execution.py`
- prepares an isolated workspace, applies the patch there, and records the execution-linked sandbox receipt

`infrastructure/sandbox/local.py`
- materializes a local sandbox copy for non-git sources and a git clone for clean repositories while excluding internal artifact directories, cache directories, symlinks, and oversized files from copy-mode sandboxes

Sandbox flow:

1. prepare a sandbox session from a source repository root
2. clone clean git repositories into a dedicated workspace or fall back to copy mode when cloning is unsafe
3. preserve git remotes and local commit identity inside cloned sandboxes so later delivery can run from the isolated workspace
4. persist materialization strategy, copy statistics, and skipped-entry metadata for audit
5. run patch execution or autonomous repair against the sandbox workspace instead of the source repository
6. mark the sandbox as used or cleaned up through explicit lifecycle updates

## HTTP Control Plane

`interfaces/http/app.py`
- routes versioned JSON API requests to the existing planning, patching, verification, approval, delivery, sandbox, and queue orchestration

`interfaces/http/app.py` also provides:
- OpenAPI discovery
- request correlation headers
- idempotent POST replay
- lightweight per-client rate limiting

`interfaces/http/server.py`
- serves the control-plane API through the standard library WSGI server

HTTP flow:

1. accept a JSON API request or GitHub webhook event
2. assign or propagate a request ID and enforce a per-client rate limit
3. optionally enforce a static bearer token for non-webhook routes
4. replay idempotent POST requests when the same `Idempotency-Key` or GitHub delivery ID is seen again
5. validate GitHub webhook signatures when a webhook secret is configured
6. dispatch the request into the existing use-case layer without duplicating orchestration logic
7. return JSON receipts, pagination metadata, and identifiers for later inspection

## Phase 12 Runtime Isolation

`infrastructure/verification/command_runner.py`
- now supports a Docker-backed runner that bind-mounts the repository into `/workspace`

`infrastructure/config/settings.py`
- configures verification runtime selection plus Docker binary, image, CPU, memory, and network limits

`application/use_cases/verify_run.py`
- accepts an injected runtime-aware command runner instead of assuming host execution

`application/use_cases/process_queue.py`
- resolves the configured verification runtime for queued verification jobs

Runtime-isolated verification flow:

1. resolve the verification runtime from CLI flags, HTTP payloads, or environment defaults
2. choose either the host runner or Docker-backed runner
3. execute the verification command in the selected runtime against the same workspace path
4. capture stdout, stderr, exit code, and duration regardless of runtime
5. persist the verification receipt and continue the existing reflection logic unchanged

## Phase 13 Isolated Execution Workspaces

`interfaces/cli/main.py`
- can now prepare sandboxes during patch execution and resolve `verify` or `deliver` repo roots from sandbox identifiers

`application/use_cases/run_sandboxed_patch_execution.py`
- turns sandboxes into first-class execution workspaces for guarded patch application

Isolated execution flow:

1. prepare a sandbox from the source repository root
2. prefer a cloned git workspace when the source repository is clean enough to clone safely
3. apply the patch in the isolated workspace and persist the execution receipt
4. verify and deliver against the sandbox workspace by sandbox identifier instead of the source tree
5. keep the source workspace unchanged until a final delivery step is explicitly invoked

## Phase 14 Production API Surface

`interfaces/http/app.py`
- now exposes direct workflow endpoints for patch generation, patch execution, verification, approval, delivery, and queue mutation

`interfaces/http/server.py`
- forwards request and rate-limit headers from the control plane responses

Production API flow:

1. clients call versioned routes under `/v1/...` for both direct and queued workflows
2. the API emits `X-Request-ID` on every response for traceability
3. POST requests can be replayed safely with `Idempotency-Key`
4. paginated read endpoints return `items` plus `pagination` metadata with `next_offset`
5. OpenAPI discovery exposes the current route surface to operators and tooling

## Phase 15 Identity And Enterprise Access

`application/services/authentication.py`
- issues and validates signed HMAC bearer tokens with user or service principal claims

`application/services/tenant_access.py`
- now authorizes both raw actor requests and authenticated principals against tenant-scoped RBAC

`application/use_cases/sync_identity.py`
- provides SCIM-like tenant membership synchronization with optional replace semantics

`application/use_cases/manage_approval.py`
- now supports approval expiry windows plus reviewer and reviewer-team assignment

Identity flow:

1. HTTP clients authenticate with either the legacy static bearer token or a signed principal token
2. signed principals carry actor, team, group, scope, and tenant claims into the control plane
3. tenant access control resolves repo ownership, then enforces either membership-based RBAC or scoped service access
4. identity sync can upsert or replace tenant memberships from an external directory source
5. approval requests expire automatically by timestamp and can constrain review to assigned actors or teams

## Phase 16 Reviewer And Operator UI

`interfaces/http/ui.py`
- serves the operator console HTML, stylesheet, and browser-side control logic without adding a frontend build dependency

`interfaces/http/app.py`
- now exposes tenant dashboard and notification endpoints in addition to the UI shell routes

`interfaces/http/server.py`
- now serves both JSON and text responses so the control plane can host its own browser console

UI flow:

1. operators open `/ui` and optionally provide a bearer token plus tenant, actor, and team context
2. the browser console loads recent runs, pending approvals, deliveries, queue jobs, and tenant summary data from the existing API
3. reviewers can inspect approval receipts and submit approve or reject decisions directly from the UI
4. operators can inspect queue jobs and submit cancel or resume actions directly from the UI
5. the raw JSON payload for any selected record stays visible alongside actions for audit-oriented review

## Phase 17 Enterprise Integrations

`application/use_cases/manage_queue.py`
- now supports plan jobs sourced from external issue systems while preserving the same queue and budget model

`application/use_cases/process_queue.py`
- can plan from a provided external issue context instead of always fetching a GitHub issue first

`interfaces/http/app.py`
- now exposes Jira issue intake and Slack approval action webhooks in addition to the GitHub webhook surface

`infrastructure/notifications/file_outbox.py`
- still persists local notification receipts first, then performs best-effort Slack, Teams, and Jira fanout

Integration flow:

1. Jira sends an issue webhook that resolves through a project-key mapping to a repository and local repo root
2. the control plane enqueues a normal plan job with an attached external issue context
3. the queue worker plans against that context and stores the external ticket reference on the run payload
4. Slack can submit approval actions through a signed webhook that reuses the standard approval review use case
5. persisted notification events can fan out to Slack, Teams, and Jira comments without changing the local audit trail

## Phase 18 Distributed Worker Platform

`application/use_cases/manage_queue.py`
- now lets enqueue operations attach `required_worker_tags` and `concurrency_key` scheduling hints to jobs

`application/use_cases/process_queue.py`
- now claims leased jobs with worker-tag awareness, emits richer heartbeats, and requeues expired work before each claim cycle

`infrastructure/persistence/run_repository.py`
- now persists lease state, rehydration count, worker affinity metadata, and stale-lease reclamation logic

`observability/metrics/queue.py`
- now exports lease-oriented metrics, including stale leases and running jobs per tenant

`application/services/delivery_summary.py`
- can now mirror delivery artifacts into a shared artifact-store directory and emit shared URLs

Distributed worker flow:

1. enqueue a job with optional worker tags and a concurrency key
2. worker advertises its tags and queue capacity through heartbeats
3. the repository reclaims expired leases before choosing the next claimable job
4. claim selection filters by worker tags, blocks active concurrency keys, and prefers tenants with less running load
5. the worker acquires a fresh lease token and executes the normal workflow
6. on crash or stall, the lease expires and the job is requeued with `resume_state` metadata
7. delivery can optionally publish artifacts to a shared store so later workers and external systems resolve consistent links

## Phase 19 Observability And Compliance

`observability/tracing/recorder.py`
- records persisted trace events for API requests and worker execution spans

`observability/alerts/manager.py`
- emits persisted alert receipts and evaluates queue snapshots against alert thresholds

`application/services/audit_export.py`
- exports run-scoped audit bundles with linked payloads and artifact integrity manifests

`application/services/retention.py`
- evaluates or enforces retention windows across notifications, worker heartbeats, alerts, and traces

`integrations/telemetry/client.py`
- provides optional HTTP fanout for trace and alert events to an external telemetry sink

Observability flow:

1. the API assigns a request identifier and records request-level trace events
2. the worker records queue-job lifecycle trace events and terminal failures
3. queue failures and unhealthy queue snapshots emit persisted alerts with dedupe windows
4. alert and trace payloads can optionally fan out to an external telemetry sink
5. operators can export a run-scoped audit bundle with a manifest of referenced artifact hashes
6. retention enforcement prunes old observability records and their payload files on disk

## Phase 20 Delivery And Change Governance

`application/services/delivery_governance.py`
- evaluates policy-as-code rules for allowed repos, blocked paths, rollout-stage requirements, command restrictions, allowed providers, allowed models, and branch protection

`application/use_cases/deliver_run.py`
- now merges approval and delivery-governance checks, resolves rollback metadata, and persists governance state into delivery receipts

`integrations/github/client.py`
- validates base-branch protection through the GitHub branches API before delivery

Governance flow:

1. resolve the verified run, execution, and verification records plus any linked patch proposal metadata
2. fetch repository default-branch metadata and explicit base-branch protection state from GitHub
3. evaluate delivery governance against repo, path, command, provider, model, and rollout-stage policy
4. block delivery before commit or push when governance fails, even if approvals are already satisfied
5. persist rollout stage, rollback base SHA, branch-protection status, and the effective governance snapshot with the delivery receipt

## Phase 21 Reliability And Release Engineering

`application/use_cases/manage_release.py`
- exposes schema inspection, backup, restore, and release-manifest workflows over the existing persistence layer

`application/use_cases/run_smoke_test.py`
- runs a deterministic local end-to-end smoke flow that exercises planning, patch generation, patch execution, and verification together

`infrastructure/persistence/run_repository.py`
- now records applied schema migrations as first-class repository metadata

Reliability flow:

1. inspect the current schema ledger and expose the active schema version to operators
2. create a backup bundle containing the database, optional artifacts, and a manifest that records source paths and schema version
3. restore a backup manifest into a fresh target directory when state recovery is needed
4. generate a release manifest that captures runtime, storage, schema, and API route expectations for deployment review
5. run a deterministic smoke test that proves the core agent loop still functions in a clean temporary workspace

## Phase 22 Advanced Agent Intelligence

`agents/context_builder/repository.py`
- now extracts indexed symbols, computes repository complexity, and carries richer context signals into planning

`application/services/evaluation.py`
- scores plans and generated patch proposals with explicit quality and scope reasoning

`application/services/model_routing.py`
- routes planning and patch generation between standard and complex models using repository and patch-context complexity

`integrations/openai/planner.py` and `integrations/openai/patcher.py`
- now record the routed model actually used for each planning or patch-generation call

Intelligence flow:

1. inspect the repository and extract lightweight symbol metadata from relevant source files
2. derive a bounded complexity score from repository breadth, ranked files, and symbol density
3. route planning and patch generation to the standard or complex model according to configurable thresholds
4. evaluate the resulting plan or proposal against context depth, validation coverage, scope, and rationale
5. persist repository index, evaluation output, and routed model metadata into operator-visible artifacts

## Project Structure

```text
apps/
  api/
  cli/
  worker/
src/issue_to_pr_agent/
  application/
    use_cases/
  agents/
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
tests/
  unit/
docs/
  architecture.md
  phases.md
```

## Enterprise Direction

The current roadmap is implemented through Phase 22. Further hardening beyond the roadmap should focus on:

1. Full containerized or VM-backed patch generation and apply stages instead of host-side mutation inside local sandboxes.
2. External Postgres and object-storage backends replacing the current local-first defaults in production deployments.
3. Vendor-specific telemetry, SIEM, and identity-provider integrations on top of the generic connectors already in place.
2. Full SSO/SAML/OIDC session flows and SCIM provisioning beyond signed service tokens.
3. Tenant administration, richer reviewer workflows, and diff or log exploration in the web UI.
4. Distributed worker coordination and queue fairness across hosts.
5. Tracing, alert routing, and richer external telemetry sinks.
