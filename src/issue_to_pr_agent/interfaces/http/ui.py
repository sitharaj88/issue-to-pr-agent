from __future__ import annotations

from textwrap import dedent


def render_operator_console() -> str:
    return dedent(
        """\
        <!doctype html>
        <html lang="en">
          <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Issue-to-PR Operator Console</title>
            <link rel="stylesheet" href="/ui/styles.css">
          </head>
          <body>
            <div class="shell">
              <header class="masthead">
                <div class="brand-block">
                  <p class="eyebrow">Enterprise Control Plane</p>
                  <h1>Issue-to-PR Operator Console</h1>
                  <p class="lede">Review risky deliveries, inspect live workflow receipts, and operate the queue from one surface.</p>
                </div>
                <div class="masthead-actions">
                  <button id="refresh-button" class="button button-primary" type="button">Refresh</button>
                </div>
              </header>

              <section class="control-strip panel">
                <label class="field">
                  <span>Bearer Token</span>
                  <input id="token-input" type="password" placeholder="Optional signed or static bearer token">
                </label>
                <label class="field">
                  <span>Tenant ID</span>
                  <input id="tenant-input" type="text" placeholder="tenant-1">
                </label>
                <label class="field">
                  <span>Actor</span>
                  <input id="actor-input" type="text" placeholder="alice">
                </label>
                <label class="field">
                  <span>Team</span>
                  <input id="team-input" type="text" placeholder="platform">
                </label>
              </section>

              <section class="hero-grid">
                <div class="hero panel">
                  <p class="eyebrow">Workflow Focus</p>
                  <h2>Pending reviews, queue pressure, and recent deliveries.</h2>
                  <p class="hero-copy">
                    The console is intentionally narrow: recent runs for context, pending approvals for reviewers,
                    queue jobs for operators, and the raw record payload for audit-grade inspection.
                  </p>
                  <div id="banner" class="banner banner-muted">Connect a token if your API is protected, then refresh the console.</div>
                </div>
                <div class="summary-stack">
                  <article class="stat-card">
                    <span class="stat-label">Runs</span>
                    <strong id="runs-count" class="stat-value">0</strong>
                    <span id="runs-meta" class="stat-meta">No recent runs loaded.</span>
                  </article>
                  <article class="stat-card">
                    <span class="stat-label">Pending approvals</span>
                    <strong id="approvals-count" class="stat-value">0</strong>
                    <span id="approvals-meta" class="stat-meta">No approval data loaded.</span>
                  </article>
                  <article class="stat-card">
                    <span class="stat-label">Queued jobs</span>
                    <strong id="jobs-count" class="stat-value">0</strong>
                    <span id="jobs-meta" class="stat-meta">No queue data loaded.</span>
                  </article>
                  <article class="stat-card">
                    <span class="stat-label">Deliveries</span>
                    <strong id="deliveries-count" class="stat-value">0</strong>
                    <span id="deliveries-meta" class="stat-meta">No delivery data loaded.</span>
                  </article>
                </div>
              </section>

              <main class="workspace">
                <section class="records-column">
                  <section class="panel">
                    <div class="panel-head">
                      <h2>Tenant Summary</h2>
                      <span class="panel-caption">Optional tenant-scoped metrics</span>
                    </div>
                    <div id="dashboard-summary" class="empty-state">Provide a tenant ID to load dashboard metrics.</div>
                  </section>

                  <section class="panel">
                    <div class="panel-head">
                      <h2>Pending Approvals</h2>
                      <span class="panel-caption">Reviewer workbench</span>
                    </div>
                    <div id="approvals-list" class="record-list empty-state">No approvals loaded.</div>
                  </section>

                  <section class="panel">
                    <div class="panel-head">
                      <h2>Queue Jobs</h2>
                      <span class="panel-caption">Operator queue control</span>
                    </div>
                    <div id="jobs-list" class="record-list empty-state">No jobs loaded.</div>
                  </section>

                  <section class="panel">
                    <div class="panel-head">
                      <h2>Recent Deliveries</h2>
                      <span class="panel-caption">Latest published changes</span>
                    </div>
                    <div id="deliveries-list" class="record-list empty-state">No deliveries loaded.</div>
                  </section>

                  <section class="panel">
                    <div class="panel-head">
                      <h2>Recent Runs</h2>
                      <span class="panel-caption">Planning context</span>
                    </div>
                    <div id="runs-list" class="record-list empty-state">No runs loaded.</div>
                  </section>
                </section>

                <aside class="side-column">
                  <section class="panel action-panel">
                    <div class="panel-head">
                      <h2>Review Approval</h2>
                      <span class="panel-caption">Approve or reject from the console</span>
                    </div>
                    <form id="approval-form" class="action-form">
                      <label class="field">
                        <span>Approval ID</span>
                        <input id="approval-id-input" name="approval_id" type="text" placeholder="approval-123">
                      </label>
                      <label class="field">
                        <span>Comment</span>
                        <textarea id="approval-comment-input" name="comment" rows="4" placeholder="Optional reviewer note"></textarea>
                      </label>
                      <div class="button-row">
                        <button class="button button-primary" data-decision="approve" type="submit">Approve</button>
                        <button class="button button-secondary" data-decision="reject" type="button" id="reject-approval-button">Reject</button>
                      </div>
                    </form>
                  </section>

                  <section class="panel action-panel">
                    <div class="panel-head">
                      <h2>Queue Actions</h2>
                      <span class="panel-caption">Cancel or resume queued work</span>
                    </div>
                    <form id="queue-form" class="action-form">
                      <label class="field">
                        <span>Job ID</span>
                        <input id="job-id-input" name="job_id" type="text" placeholder="job-123">
                      </label>
                      <div class="button-row">
                        <button class="button button-secondary" data-queue-action="cancel" type="submit">Cancel</button>
                        <button class="button button-primary" data-queue-action="resume" type="button" id="resume-job-button">Resume</button>
                      </div>
                    </form>
                  </section>

                  <section class="panel">
                    <div class="panel-head">
                      <h2>Notifications</h2>
                      <span class="panel-caption">Tenant lifecycle events</span>
                    </div>
                    <div id="notifications-list" class="record-list compact empty-state">No notifications loaded.</div>
                  </section>

                  <section class="panel">
                    <div class="panel-head">
                      <h2>Record Detail</h2>
                      <span class="panel-caption">Audit-grade payload view</span>
                    </div>
                    <pre id="detail-view" class="detail-view">Select a record to inspect its stored payload.</pre>
                  </section>
                </aside>
              </main>
            </div>
            <script src="/ui/app.js"></script>
          </body>
        </html>
        """
    )


def render_stylesheet() -> str:
    return dedent(
        """\
        :root {
          --bg: #f4efe4;
          --panel: rgba(255, 251, 242, 0.92);
          --panel-border: rgba(23, 45, 74, 0.14);
          --ink: #15253f;
          --muted: #5f6c80;
          --accent: #e0633a;
          --accent-strong: #ba4320;
          --highlight: #1f5c57;
          --shadow: 0 18px 40px rgba(27, 43, 63, 0.12);
          --radius-xl: 28px;
          --radius-lg: 20px;
          --radius-md: 14px;
          --body-font: "Avenir Next", "Segoe UI", "Helvetica Neue", sans-serif;
          --display-font: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Georgia, serif;
        }

        * { box-sizing: border-box; }

        body {
          margin: 0;
          min-height: 100vh;
          color: var(--ink);
          background:
            radial-gradient(circle at top left, rgba(224, 99, 58, 0.12), transparent 34%),
            radial-gradient(circle at top right, rgba(31, 92, 87, 0.12), transparent 28%),
            linear-gradient(180deg, #fbf7ef 0%, var(--bg) 100%);
          font-family: var(--body-font);
        }

        .shell {
          width: min(1480px, calc(100vw - 32px));
          margin: 0 auto;
          padding: 28px 0 40px;
        }

        .masthead,
        .control-strip,
        .hero-grid,
        .workspace {
          display: grid;
          gap: 18px;
        }

        .masthead {
          align-items: end;
          grid-template-columns: 1.7fr auto;
          margin-bottom: 18px;
        }

        .brand-block h1,
        .hero h2,
        .panel-head h2 {
          margin: 0;
          font-family: var(--display-font);
          font-weight: 700;
          letter-spacing: -0.03em;
        }

        .brand-block h1 {
          font-size: clamp(2.3rem, 4vw, 4.2rem);
          line-height: 0.96;
          max-width: 12ch;
        }

        .lede,
        .hero-copy,
        .panel-caption,
        .stat-meta,
        .record-meta,
        .empty-state,
        .banner {
          color: var(--muted);
        }

        .eyebrow {
          margin: 0 0 8px;
          color: var(--accent-strong);
          font-size: 0.82rem;
          letter-spacing: 0.18em;
          text-transform: uppercase;
          font-weight: 700;
        }

        .panel,
        .stat-card {
          background: var(--panel);
          border: 1px solid var(--panel-border);
          border-radius: var(--radius-xl);
          box-shadow: var(--shadow);
          backdrop-filter: blur(10px);
        }

        .control-strip {
          grid-template-columns: repeat(4, minmax(0, 1fr));
          padding: 18px;
          margin-bottom: 18px;
        }

        .field {
          display: grid;
          gap: 8px;
          font-size: 0.92rem;
          font-weight: 600;
        }

        .field input,
        .field textarea {
          width: 100%;
          border: 1px solid rgba(21, 37, 63, 0.12);
          border-radius: var(--radius-md);
          padding: 12px 14px;
          color: var(--ink);
          background: rgba(255, 255, 255, 0.78);
          font: inherit;
          transition: border-color 140ms ease, transform 140ms ease, box-shadow 140ms ease;
        }

        .field input:focus,
        .field textarea:focus {
          outline: none;
          border-color: rgba(224, 99, 58, 0.5);
          box-shadow: 0 0 0 4px rgba(224, 99, 58, 0.12);
          transform: translateY(-1px);
        }

        .hero-grid {
          grid-template-columns: 1.45fr 1fr;
          margin-bottom: 18px;
        }

        .hero {
          padding: 24px;
          position: relative;
          overflow: hidden;
        }

        .hero::after {
          content: "";
          position: absolute;
          inset: auto -72px -72px auto;
          width: 220px;
          height: 220px;
          border-radius: 50%;
          background: radial-gradient(circle, rgba(224, 99, 58, 0.18), rgba(224, 99, 58, 0));
          pointer-events: none;
        }

        .summary-stack {
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: 18px;
        }

        .stat-card {
          padding: 18px;
          min-height: 150px;
          display: grid;
          align-content: start;
          gap: 12px;
        }

        .stat-label {
          color: var(--muted);
          font-size: 0.84rem;
          text-transform: uppercase;
          letter-spacing: 0.12em;
        }

        .stat-value {
          font-size: clamp(2.1rem, 4vw, 3.6rem);
          line-height: 0.96;
          font-family: var(--display-font);
        }

        .workspace {
          grid-template-columns: minmax(0, 1.3fr) minmax(320px, 0.9fr);
          align-items: start;
        }

        .records-column,
        .side-column {
          display: grid;
          gap: 18px;
        }

        .panel {
          padding: 18px;
        }

        .panel-head {
          display: flex;
          align-items: baseline;
          justify-content: space-between;
          gap: 12px;
          margin-bottom: 14px;
        }

        .record-list {
          display: grid;
          gap: 12px;
        }

        .record-card {
          border: 1px solid rgba(21, 37, 63, 0.08);
          border-radius: var(--radius-lg);
          background: rgba(255, 255, 255, 0.72);
          padding: 14px;
          display: grid;
          gap: 10px;
        }

        .record-card.compact-card {
          padding: 12px 14px;
        }

        .record-heading {
          display: flex;
          justify-content: space-between;
          gap: 12px;
          align-items: start;
        }

        .record-title {
          font-weight: 700;
          line-height: 1.25;
        }

        .record-meta {
          font-size: 0.9rem;
        }

        .pill {
          align-self: start;
          border-radius: 999px;
          padding: 6px 10px;
          font-size: 0.78rem;
          font-weight: 700;
          text-transform: uppercase;
          letter-spacing: 0.08em;
          background: rgba(31, 92, 87, 0.12);
          color: var(--highlight);
        }

        .pill.status-pending,
        .pill.status-queued,
        .pill.status-running {
          background: rgba(224, 99, 58, 0.12);
          color: var(--accent-strong);
        }

        .pill.status-failed,
        .pill.status-rejected,
        .pill.status-cancelled {
          background: rgba(145, 33, 55, 0.12);
          color: #8a1f39;
        }

        .record-actions,
        .button-row {
          display: flex;
          flex-wrap: wrap;
          gap: 10px;
        }

        .button {
          border: 0;
          border-radius: 999px;
          padding: 11px 16px;
          font: inherit;
          font-weight: 700;
          cursor: pointer;
          transition: transform 140ms ease, opacity 140ms ease, box-shadow 140ms ease;
        }

        .button:hover {
          transform: translateY(-1px);
          box-shadow: 0 10px 18px rgba(21, 37, 63, 0.12);
        }

        .button-primary {
          color: white;
          background: linear-gradient(135deg, var(--accent), var(--accent-strong));
        }

        .button-secondary {
          color: var(--ink);
          background: rgba(21, 37, 63, 0.08);
        }

        .banner {
          border-radius: var(--radius-md);
          padding: 13px 14px;
          font-weight: 600;
        }

        .banner-muted {
          background: rgba(21, 37, 63, 0.06);
        }

        .banner-success {
          color: var(--highlight);
          background: rgba(31, 92, 87, 0.1);
        }

        .banner-error {
          color: #8a1f39;
          background: rgba(145, 33, 55, 0.12);
        }

        .detail-view {
          margin: 0;
          min-height: 320px;
          max-height: 680px;
          overflow: auto;
          border-radius: var(--radius-lg);
          padding: 16px;
          background: #14253c;
          color: #dfe8f2;
          font-size: 0.85rem;
          line-height: 1.5;
        }

        .dashboard-metrics {
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: 12px;
        }

        .metric {
          border-radius: var(--radius-lg);
          background: rgba(255, 255, 255, 0.74);
          border: 1px solid rgba(21, 37, 63, 0.08);
          padding: 14px;
          display: grid;
          gap: 4px;
        }

        .metric-label {
          font-size: 0.85rem;
          color: var(--muted);
        }

        .metric-value {
          font-size: 1.8rem;
          font-family: var(--display-font);
          line-height: 1;
        }

        @media (max-width: 1080px) {
          .masthead,
          .hero-grid,
          .workspace,
          .control-strip {
            grid-template-columns: 1fr;
          }

          .summary-stack,
          .dashboard-metrics {
            grid-template-columns: 1fr 1fr;
          }
        }

        @media (max-width: 720px) {
          .shell {
            width: min(100vw - 18px, 1480px);
            padding-top: 18px;
          }

          .summary-stack,
          .dashboard-metrics {
            grid-template-columns: 1fr;
          }

          .panel-head,
          .record-heading {
            flex-direction: column;
          }
        }
        """
    )


def render_script() -> str:
    return dedent(
        """\
        const storage = window.localStorage;
        const query = new URLSearchParams(window.location.search);
        const state = {
          token: query.get('token') || storage.getItem('issue_to_pr_token') || '',
          tenantId: query.get('tenant_id') || storage.getItem('issue_to_pr_tenant_id') || '',
          actor: query.get('actor') || storage.getItem('issue_to_pr_actor') || '',
          team: query.get('team') || storage.getItem('issue_to_pr_team') || '',
          activeApprovalDecision: 'approve',
          activeQueueAction: 'cancel',
        };

        const elements = {
          tokenInput: document.getElementById('token-input'),
          tenantInput: document.getElementById('tenant-input'),
          actorInput: document.getElementById('actor-input'),
          teamInput: document.getElementById('team-input'),
          refreshButton: document.getElementById('refresh-button'),
          banner: document.getElementById('banner'),
          runsCount: document.getElementById('runs-count'),
          approvalsCount: document.getElementById('approvals-count'),
          jobsCount: document.getElementById('jobs-count'),
          deliveriesCount: document.getElementById('deliveries-count'),
          runsMeta: document.getElementById('runs-meta'),
          approvalsMeta: document.getElementById('approvals-meta'),
          jobsMeta: document.getElementById('jobs-meta'),
          deliveriesMeta: document.getElementById('deliveries-meta'),
          dashboardSummary: document.getElementById('dashboard-summary'),
          approvalsList: document.getElementById('approvals-list'),
          jobsList: document.getElementById('jobs-list'),
          deliveriesList: document.getElementById('deliveries-list'),
          runsList: document.getElementById('runs-list'),
          notificationsList: document.getElementById('notifications-list'),
          detailView: document.getElementById('detail-view'),
          approvalForm: document.getElementById('approval-form'),
          approvalIdInput: document.getElementById('approval-id-input'),
          approvalCommentInput: document.getElementById('approval-comment-input'),
          rejectApprovalButton: document.getElementById('reject-approval-button'),
          queueForm: document.getElementById('queue-form'),
          jobIdInput: document.getElementById('job-id-input'),
          resumeJobButton: document.getElementById('resume-job-button'),
        };

        initialise();

        function initialise() {
          syncInputsFromState();
          bindEvents();
          refreshConsole();
        }

        function bindEvents() {
          elements.refreshButton.addEventListener('click', refreshConsole);
          for (const [key, input] of Object.entries({
            token: elements.tokenInput,
            tenantId: elements.tenantInput,
            actor: elements.actorInput,
            team: elements.teamInput,
          })) {
            input.addEventListener('change', () => {
              state[key] = input.value.trim();
              persistState();
            });
          }

          elements.approvalForm.addEventListener('submit', async (event) => {
            event.preventDefault();
            state.activeApprovalDecision = 'approve';
            await reviewApproval();
          });
          elements.rejectApprovalButton.addEventListener('click', async () => {
            state.activeApprovalDecision = 'reject';
            await reviewApproval();
          });
          elements.queueForm.addEventListener('submit', async (event) => {
            event.preventDefault();
            state.activeQueueAction = 'cancel';
            await updateQueueJob();
          });
          elements.resumeJobButton.addEventListener('click', async () => {
            state.activeQueueAction = 'resume';
            await updateQueueJob();
          });
        }

        function syncInputsFromState() {
          elements.tokenInput.value = state.token;
          elements.tenantInput.value = state.tenantId;
          elements.actorInput.value = state.actor;
          elements.teamInput.value = state.team;
        }

        function persistState() {
          storage.setItem('issue_to_pr_token', state.token);
          storage.setItem('issue_to_pr_tenant_id', state.tenantId);
          storage.setItem('issue_to_pr_actor', state.actor);
          storage.setItem('issue_to_pr_team', state.team);
        }

        async function refreshConsole() {
          persistState();
          setBanner('Loading workflow data…', 'muted');
          try {
            const results = await Promise.all([
              fetchJson('/v1/runs?limit=6'),
              fetchJson('/v1/approvals?status=pending&limit=6'),
              fetchJson('/v1/deliveries?limit=6'),
              fetchJson('/v1/queue-jobs?limit=6'),
              state.tenantId ? fetchJson(buildTenantPath('/v1/dashboard')) : Promise.resolve(null),
              state.tenantId ? fetchJson(buildTenantPath('/v1/notifications') + '&limit=6') : Promise.resolve(null),
            ]);
            const [runs, approvals, deliveries, jobs, dashboard, notifications] = results;
            renderRuns(runs.items || []);
            renderApprovals(approvals.items || []);
            renderDeliveries(deliveries.items || []);
            renderJobs(jobs.items || []);
            renderDashboard(dashboard);
            renderNotifications(notifications ? notifications.items || [] : []);
            renderStats({
              runs: runs.items || [],
              approvals: approvals.items || [],
              deliveries: deliveries.items || [],
              jobs: jobs.items || [],
              dashboard: dashboard ? dashboard.summary : null,
            });
            setBanner('Console refreshed successfully.', 'success');
          } catch (error) {
            setBanner(error.message || 'Failed to refresh console.', 'error');
          }
        }

        function buildTenantPath(basePath) {
          const params = new URLSearchParams();
          params.set('tenant_id', state.tenantId);
          if (!state.token) {
            if (state.actor) {
              params.set('actor', state.actor);
            }
            if (state.team) {
              params.set('team', state.team);
            }
          }
          return `${basePath}?${params.toString()}`;
        }

        async function reviewApproval() {
          const approvalId = elements.approvalIdInput.value.trim();
          if (!approvalId) {
            setBanner('Approval ID is required for review.', 'error');
            return;
          }
          try {
            await postJson('/v1/approvals/review', {
              approval_id: approvalId,
              actor: state.actor,
              team: state.team,
              decision: state.activeApprovalDecision,
              comment: elements.approvalCommentInput.value.trim(),
            });
            setBanner(`Approval ${approvalId} ${state.activeApprovalDecision}d.`, 'success');
            await refreshConsole();
            inspectRecord(`/v1/approvals/${approvalId}`);
          } catch (error) {
            setBanner(error.message || 'Approval review failed.', 'error');
          }
        }

        async function updateQueueJob() {
          const jobId = elements.jobIdInput.value.trim();
          if (!jobId) {
            setBanner('Job ID is required for queue control.', 'error');
            return;
          }
          const path = state.activeQueueAction === 'resume'
            ? `/v1/queue-jobs/${jobId}/resume`
            : `/v1/queue-jobs/${jobId}/cancel`;
          try {
            await postJson(path, {
              actor: state.actor,
              team: state.team,
              reset_attempts: state.activeQueueAction === 'resume',
            });
            setBanner(`Queue job ${jobId} updated with ${state.activeQueueAction}.`, 'success');
            await refreshConsole();
            inspectRecord(`/v1/queue-jobs/${jobId}`);
          } catch (error) {
            setBanner(error.message || 'Queue action failed.', 'error');
          }
        }

        async function inspectRecord(path) {
          try {
            const payload = await fetchJson(path);
            elements.detailView.textContent = JSON.stringify(payload, null, 2);
          } catch (error) {
            elements.detailView.textContent = error.message || 'Unable to load record.';
          }
        }

        function renderStats({ runs, approvals, deliveries, jobs, dashboard }) {
          elements.runsCount.textContent = String(runs.length);
          elements.approvalsCount.textContent = String(approvals.length);
          elements.deliveriesCount.textContent = String(deliveries.length);
          elements.jobsCount.textContent = String(jobs.length);
          if (dashboard) {
            elements.runsMeta.textContent = serialiseCounts(dashboard.run_counts);
            elements.approvalsMeta.textContent = serialiseCounts(dashboard.approval_counts);
            elements.deliveriesMeta.textContent = serialiseCounts(dashboard.delivery_counts);
            elements.jobsMeta.textContent = dashboard.tenant_name || 'Tenant summary loaded.';
          } else {
            elements.runsMeta.textContent = 'Recent planning runs.';
            elements.approvalsMeta.textContent = 'Pending reviewer actions.';
            elements.deliveriesMeta.textContent = 'Recent delivery receipts.';
            elements.jobsMeta.textContent = 'Recent queue workload.';
          }
        }

        function renderDashboard(payload) {
          if (!payload || !payload.summary) {
            elements.dashboardSummary.innerHTML = '<div class="empty-state">Provide a tenant ID to load dashboard metrics.</div>';
            return;
          }
          const summary = payload.summary;
          elements.dashboardSummary.innerHTML = `
            <div class="dashboard-metrics">
              ${metricCard('Tenant', summary.tenant_name)}
              ${metricCard('Runs', serialiseCounts(summary.run_counts))}
              ${metricCard('Approvals', serialiseCounts(summary.approval_counts))}
              ${metricCard('Deliveries', serialiseCounts(summary.delivery_counts))}
            </div>
          `;
        }

        function metricCard(label, value) {
          return `<article class="metric"><span class="metric-label">${escapeHtml(label)}</span><strong class="metric-value">${escapeHtml(value || '0')}</strong></article>`;
        }

        function renderRuns(items) {
          renderRecordList(elements.runsList, items, (item) => `
            <article class="record-card">
              <div class="record-heading">
                <div>
                  <div class="record-title">${escapeHtml(item.summary || item.run_id)}</div>
                  <div class="record-meta">${escapeHtml(item.repo_full_name || '')} · issue #${escapeHtml(String(item.issue_number || ''))}</div>
                </div>
                <span class="pill status-${escapeHtml(item.status || '')}">${escapeHtml(item.status || 'unknown')}</span>
              </div>
              <div class="record-actions">
                <button class="button button-secondary" data-inspect="/v1/runs/${encodeURIComponent(item.run_id)}">Inspect</button>
              </div>
            </article>
          `);
        }

        function renderApprovals(items) {
          renderRecordList(elements.approvalsList, items, (item) => `
            <article class="record-card">
              <div class="record-heading">
                <div>
                  <div class="record-title">${escapeHtml(item.repo_full_name || item.approval_id)}</div>
                  <div class="record-meta">${escapeHtml(item.summary || '')}</div>
                </div>
                <span class="pill status-${escapeHtml(item.status || '')}">${escapeHtml(item.status || 'pending')}</span>
              </div>
              <div class="record-meta">Risk: ${escapeHtml(item.risk_level || 'unknown')} · approvals ${escapeHtml(String(item.approved_count || 0))}/${escapeHtml(String(item.required_approvals || 0))}</div>
              <div class="record-actions">
                <button class="button button-primary" data-fill-approval="${encodeURIComponent(item.approval_id)}">Review</button>
                <button class="button button-secondary" data-inspect="/v1/approvals/${encodeURIComponent(item.approval_id)}">Inspect</button>
              </div>
            </article>
          `);
        }

        function renderDeliveries(items) {
          renderRecordList(elements.deliveriesList, items, (item) => `
            <article class="record-card compact-card">
              <div class="record-heading">
                <div>
                  <div class="record-title">${escapeHtml(item.repo_full_name || item.delivery_id)}</div>
                  <div class="record-meta">${escapeHtml(item.branch_name || '')} → ${escapeHtml(item.base_branch || '')}</div>
                </div>
                <span class="pill status-${escapeHtml(item.status || '')}">${escapeHtml(item.status || '')}</span>
              </div>
              <div class="record-actions">
                <button class="button button-secondary" data-inspect="/v1/deliveries/${encodeURIComponent(item.delivery_id)}">Inspect</button>
              </div>
            </article>
          `);
        }

        function renderJobs(items) {
          renderRecordList(elements.jobsList, items, (item) => `
            <article class="record-card compact-card">
              <div class="record-heading">
                <div>
                  <div class="record-title">${escapeHtml(item.repo_full_name || item.job_id)}</div>
                  <div class="record-meta">${escapeHtml(item.job_type || '')} · priority ${escapeHtml(String(item.priority || 0))}</div>
                </div>
                <span class="pill status-${escapeHtml(item.status || '')}">${escapeHtml(item.status || '')}</span>
              </div>
              <div class="record-actions">
                <button class="button button-primary" data-fill-job="${encodeURIComponent(item.job_id)}">Operate</button>
                <button class="button button-secondary" data-inspect="/v1/queue-jobs/${encodeURIComponent(item.job_id)}">Inspect</button>
              </div>
            </article>
          `);
        }

        function renderNotifications(items) {
          renderRecordList(elements.notificationsList, items, (item) => `
            <article class="record-card compact-card">
              <div class="record-heading">
                <div>
                  <div class="record-title">${escapeHtml(item.event_type || item.notification_id)}</div>
                  <div class="record-meta">${escapeHtml(item.summary || '')}</div>
                </div>
                <span class="pill status-${escapeHtml(item.status || '')}">${escapeHtml(item.status || '')}</span>
              </div>
            </article>
          `);
        }

        function renderRecordList(container, items, renderer) {
          if (!items.length) {
            container.innerHTML = '<div class="empty-state">No records available for this view.</div>';
            return;
          }
          container.innerHTML = items.map(renderer).join('');
          container.querySelectorAll('[data-inspect]').forEach((button) => {
            button.addEventListener('click', () => inspectRecord(button.dataset.inspect));
          });
          container.querySelectorAll('[data-fill-approval]').forEach((button) => {
            button.addEventListener('click', () => {
              const value = decodeURIComponent(button.dataset.fillApproval);
              elements.approvalIdInput.value = value;
              inspectRecord(`/v1/approvals/${encodeURIComponent(value)}`);
            });
          });
          container.querySelectorAll('[data-fill-job]').forEach((button) => {
            button.addEventListener('click', () => {
              const value = decodeURIComponent(button.dataset.fillJob);
              elements.jobIdInput.value = value;
              inspectRecord(`/v1/queue-jobs/${encodeURIComponent(value)}`);
            });
          });
        }

        async function fetchJson(path) {
          const response = await fetch(path, {
            headers: buildHeaders({ Accept: 'application/json' }),
          });
          const payload = await response.json();
          if (!response.ok) {
            throw new Error(payload.error || `Request failed: ${response.status}`);
          }
          return payload;
        }

        async function postJson(path, body) {
          const response = await fetch(path, {
            method: 'POST',
            headers: buildHeaders({ 'Content-Type': 'application/json', Accept: 'application/json' }),
            body: JSON.stringify(body),
          });
          const payload = await response.json();
          if (!response.ok) {
            throw new Error(payload.error || `Request failed: ${response.status}`);
          }
          return payload;
        }

        function buildHeaders(baseHeaders) {
          const headers = { ...baseHeaders };
          if (state.token) {
            headers.Authorization = `Bearer ${state.token}`;
          }
          return headers;
        }

        function serialiseCounts(counts) {
          if (!counts || !Object.keys(counts).length) {
            return '0';
          }
          return Object.entries(counts)
            .map(([key, value]) => `${key}: ${value}`)
            .join(' · ');
        }

        function setBanner(message, tone) {
          elements.banner.textContent = message;
          elements.banner.className = `banner banner-${tone}`;
        }

        function escapeHtml(value) {
          return String(value ?? '')
            .replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;')
            .replaceAll("'", '&#39;');
        }
        """
    )
