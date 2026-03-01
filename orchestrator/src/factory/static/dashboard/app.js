// Factory Dashboard - Main Application
(function() {
  'use strict';

  var API = '/api';
  var POLL_INTERVAL = 10000; // 10 seconds
  var _pollTimer = null;
  var state = {
    currentPage: 'dashboard',
    tasks: [],
    agents: [],
    workflows: [],
    messages: [],
    healthy: false,
    sidebarOpen: false,
    loading: false,
    taskDetail: null,
    taskLogs: [],
    user: null,
    oauthEnabled: false,
    settings: null
  };

  // ── Utilities ──────────────────────────────────────────────

  function esc(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function timeAgo(iso) {
    if (!iso) return '-';
    var d = new Date(iso);
    var now = new Date();
    var diff = now - d;
    if (diff < 60000) return 'just now';
    if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
    if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
    return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function formatDate(iso) {
    if (!iso) return '-';
    var d = new Date(iso);
    return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function statusBadge(status) {
    return '<span class="status-badge status-' + esc(status) + '">' + esc(status) + '</span>';
  }

  async function apiFetch(path) {
    try {
      var r = await fetch(API + path);
      if (!r.ok) return null;
      return await r.json();
    } catch (e) {
      console.error('API error:', path, e);
      return null;
    }
  }

  async function apiPost(path, body) {
    try {
      var r = await fetch(API + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      if (!r.ok) return null;
      return await r.json();
    } catch (e) {
      console.error('API error:', path, e);
      return null;
    }
  }

  // ── Settings ─────────────────────────────────────────────────

  async function loadSettings() {
    var data = await apiFetch('/settings');
    if (data) state.settings = data;
  }

  function planeIssueUrl(issueId) {
    if (!issueId || !state.settings) return '';
    var s = state.settings;
    if (!s.plane_base_url || !s.plane_workspace_slug || !s.plane_project_id) return '';
    return s.plane_base_url + '/' + s.plane_workspace_slug + '/projects/' + s.plane_project_id + '/issues/' + issueId;
  }

  // ── Auto-refresh Polling ────────────────────────────────────

  function startPolling() {
    stopPolling();
    _pollTimer = setInterval(function() {
      refreshCurrentPage();
    }, POLL_INTERVAL);
  }

  function stopPolling() {
    if (_pollTimer) {
      clearInterval(_pollTimer);
      _pollTimer = null;
    }
  }

  async function refreshCurrentPage() {
    var route = parseRoute();
    var content = document.getElementById('pageContent');
    if (!content) return;

    // Silently refresh without showing loading spinner
    switch (route.page) {
      case 'dashboard':
        await renderDashboard(content);
        break;
      case 'tasks':
        if (route.param) {
          await renderTaskDetail(content, route.param);
        } else {
          await renderTasks(content);
        }
        break;
      case 'agents':
        await renderAgents(content);
        break;
      case 'preview':
        await renderPreview(content);
        break;
      case 'analytics':
        await renderAnalytics(content);
        break;
    }
  }

  // ── Router ─────────────────────────────────────────────────

  function navigate(page, params) {
    var hash = '#/' + page;
    if (params) hash += '/' + params;
    window.location.hash = hash;
  }

  function parseRoute() {
    var hash = window.location.hash || '#/dashboard';
    var parts = hash.replace('#/', '').split('/');
    return { page: parts[0] || 'dashboard', param: parts[1] || null };
  }

  async function handleRoute() {
    var route = parseRoute();
    state.currentPage = route.page;
    updateNav();

    var content = document.getElementById('pageContent');
    var pageTitle = document.getElementById('pageTitle');

    content.innerHTML = '<div class="loading"><div class="spinner"></div> Loading...</div>';

    switch (route.page) {
      case 'dashboard':
        pageTitle.textContent = 'Dashboard';
        await renderDashboard(content);
        break;
      case 'tasks':
        if (route.param) {
          pageTitle.textContent = 'Task Detail';
          await renderTaskDetail(content, route.param);
        } else {
          pageTitle.textContent = 'Tasks';
          await renderTasks(content);
        }
        break;
      case 'agents':
        pageTitle.textContent = 'Agents';
        await renderAgents(content);
        break;
      case 'preview':
        pageTitle.textContent = 'Preview Environments';
        await renderPreview(content);
        break;
      case 'analytics':
        pageTitle.textContent = 'Analytics';
        await renderAnalytics(content);
        break;
      default:
        pageTitle.textContent = 'Not Found';
        content.innerHTML = '<div class="empty-state"><div class="icon">404</div><h3>Page not found</h3><p>The page you\'re looking for doesn\'t exist.</p></div>';
    }

    content.classList.remove('page-enter');
    void content.offsetWidth;
    content.classList.add('page-enter');

    // Start auto-refresh polling
    startPolling();
  }

  function updateNav() {
    var items = document.querySelectorAll('.nav-item');
    items.forEach(function(item) {
      var target = item.getAttribute('data-page');
      if (target === state.currentPage) {
        item.classList.add('active');
      } else {
        item.classList.remove('active');
      }
    });
  }

  // ── Dashboard Page ─────────────────────────────────────────

  async function renderDashboard(el) {
    var tasks = await apiFetch('/tasks');
    var agents = await apiFetch('/agents');
    var workflows = await apiFetch('/workflows');

    if (!tasks) tasks = [];
    if (!agents) agents = [];
    if (!workflows) workflows = [];

    state.tasks = tasks;
    state.agents = agents;
    state.workflows = workflows;

    var inProgress = tasks.filter(function(t) { return t.status === 'in_progress'; }).length;
    var queued = tasks.filter(function(t) { return t.status === 'queued'; }).length;
    var done = tasks.filter(function(t) { return t.status === 'done'; }).length;
    var failed = tasks.filter(function(t) { return t.status === 'failed'; }).length;

    var h = '';

    // Stats
    h += '<div class="stat-grid">';
    h += '<div class="stat-card"><div class="stat-label">Total Tasks</div><div class="stat-value">' + tasks.length + '</div><div class="stat-detail">' + done + ' completed</div></div>';
    h += '<div class="stat-card"><div class="stat-label">In Progress</div><div class="stat-value" style="color:var(--accent)">' + inProgress + '</div><div class="stat-detail">' + queued + ' queued</div></div>';
    h += '<div class="stat-card"><div class="stat-label">Active Agents</div><div class="stat-value" style="color:var(--green)">' + agents.length + '</div><div class="stat-detail">Running now</div></div>';
    h += '<div class="stat-card"><div class="stat-label">Failed</div><div class="stat-value" style="color:var(--red)">' + failed + '</div><div class="stat-detail">Need attention</div></div>';
    h += '</div>';

    // Recent tasks
    h += '<div class="card" style="margin-bottom:20px">';
    h += '<div class="card-header"><div class="card-title">Recent Tasks</div>';
    h += '<a class="btn btn-sm btn-secondary" onclick="event.preventDefault();window.location.hash=\'#/tasks\'" href="#/tasks">View All</a></div>';

    var recent = tasks.slice(0, 8);
    if (recent.length === 0) {
      h += '<div class="empty-state" style="padding:30px"><div class="icon">&#x1F4CB;</div><h3>No tasks yet</h3><p>Tasks will appear here when created via API or Plane webhook.</p></div>';
    } else {
      h += '<table class="data-table"><thead><tr>';
      h += '<th>ID</th><th>Title</th><th>Status</th><th>Created</th>';
      h += '</tr></thead><tbody>';
      recent.forEach(function(t) {
        h += '<tr onclick="window.location.hash=\'#/tasks/' + t.id + '\'">';
        h += '<td class="id-col">#' + t.id + '</td>';
        h += '<td class="title-col">' + esc(t.title) + '</td>';
        h += '<td>' + statusBadge(t.status) + '</td>';
        h += '<td class="time-col">' + timeAgo(t.created_at) + '</td>';
        h += '</tr>';
      });
      h += '</tbody></table>';
    }
    h += '</div>';

    // Active agents
    if (agents.length > 0) {
      h += '<div class="card">';
      h += '<div class="card-header"><div class="card-title">Active Agents</div>';
      h += '<a class="btn btn-sm btn-secondary" onclick="event.preventDefault();window.location.hash=\'#/agents\'" href="#/agents">View All</a></div>';
      h += '<div class="agent-cards">';
      agents.forEach(function(a) {
        h += '<div class="agent-card">';
        h += '<div class="agent-card-header">';
        h += '<div class="agent-card-title">Task #' + a.task_id + '</div>';
        h += statusBadge(a.status);
        h += '</div>';
        h += '<div class="agent-card-meta">';
        if (a.pid) h += '<div class="meta-row"><span class="label">PID</span><span class="value">' + a.pid + '</span></div>';
        h += '<div class="meta-row"><span class="label">Started</span><span class="value">' + timeAgo(a.started_at) + '</span></div>';
        h += '</div></div>';
      });
      h += '</div></div>';
    }

    // Update sidebar badges
    updateBadges(tasks, agents);

    el.innerHTML = h;
  }

  // ── Tasks Page ─────────────────────────────────────────────

  async function renderTasks(el) {
    // Preserve current filter selection during refresh
    var currentFilter = '';
    var filterEl = document.getElementById('taskStatusFilter');
    if (filterEl) currentFilter = filterEl.value;

    var tasks = await apiFetch('/tasks');
    if (!tasks) tasks = [];
    state.tasks = tasks;

    // Count visible tasks (matching filter)
    var visibleCount = tasks.length;
    if (currentFilter) {
      visibleCount = tasks.filter(function(t) { return t.status === currentFilter; }).length;
    }

    var h = '';

    // Toolbar
    h += '<div class="toolbar">';
    h += '<select id="taskStatusFilter" onchange="window.__filterTasks()">';
    h += '<option value=""' + (!currentFilter ? ' selected' : '') + '>All Statuses</option>';
    h += '<option value="queued"' + (currentFilter === 'queued' ? ' selected' : '') + '>Queued</option>';
    h += '<option value="in_progress"' + (currentFilter === 'in_progress' ? ' selected' : '') + '>In Progress</option>';
    h += '<option value="waiting_for_input"' + (currentFilter === 'waiting_for_input' ? ' selected' : '') + '>Waiting for Input</option>';
    h += '<option value="in_review"' + (currentFilter === 'in_review' ? ' selected' : '') + '>In Review</option>';
    h += '<option value="done"' + (currentFilter === 'done' ? ' selected' : '') + '>Done</option>';
    h += '<option value="failed"' + (currentFilter === 'failed' ? ' selected' : '') + '>Failed</option>';
    h += '<option value="cancelled"' + (currentFilter === 'cancelled' ? ' selected' : '') + '>Cancelled</option>';
    h += '</select>';
    h += '<button class="btn btn-sm btn-secondary" onclick="window.location.hash=\'#/tasks\'">Refresh</button>';
    h += '<span class="refresh-indicator" id="taskCount">' + visibleCount + ' of ' + tasks.length + ' tasks</span>';
    h += '<span class="auto-refresh-dot" title="Auto-refreshing every 10s"></span>';
    h += '</div>';

    // Table
    h += '<div class="card">';
    if (tasks.length === 0) {
      h += '<div class="empty-state"><div class="icon">&#x1F4CB;</div><h3>No tasks</h3><p>Tasks will appear here when created via API or Plane webhook.</p></div>';
    } else {
      h += '<table class="data-table" id="tasksTable"><thead><tr>';
      h += '<th>ID</th><th>Title</th><th>Status</th><th>Agent</th><th>Created</th>';
      h += '</tr></thead><tbody>';
      tasks.forEach(function(t) {
        var hidden = currentFilter && t.status !== currentFilter;
        h += '<tr onclick="window.location.hash=\'#/tasks/' + t.id + '\'" data-status="' + esc(t.status) + '"' + (hidden ? ' style="display:none"' : '') + '>';
        h += '<td class="id-col">#' + t.id + '</td>';
        h += '<td class="title-col">' + esc(t.title) + '</td>';
        h += '<td>' + statusBadge(t.status) + '</td>';
        h += '<td class="time-col">' + esc(t.agent_type || 'default') + '</td>';
        h += '<td class="time-col">' + timeAgo(t.created_at) + '</td>';
        h += '</tr>';
      });
      h += '</tbody></table>';
    }
    h += '</div>';

    updateBadges(tasks, state.agents);
    el.innerHTML = h;
  }

  window.__filterTasks = function() {
    var filter = document.getElementById('taskStatusFilter').value;
    var rows = document.querySelectorAll('#tasksTable tbody tr');
    var visible = 0;
    rows.forEach(function(row) {
      if (!filter || row.getAttribute('data-status') === filter) {
        row.style.display = '';
        visible++;
      } else {
        row.style.display = 'none';
      }
    });
    var countEl = document.getElementById('taskCount');
    if (countEl) countEl.textContent = visible + ' of ' + rows.length + ' tasks';
  };

  // ── Task Detail Page ───────────────────────────────────────

  async function renderTaskDetail(el, taskId) {
    var task = await apiFetch('/tasks/' + taskId);
    if (!task) {
      el.innerHTML = '<div class="empty-state"><div class="icon">&#x26A0;</div><h3>Task not found</h3><p>Task #' + esc(taskId) + ' does not exist.</p></div>';
      return;
    }

    state.taskDetail = task;
    var h = '';

    // Back link
    h += '<a class="back-link" onclick="window.location.hash=\'#/tasks\'">&#x2190; Back to Tasks</a>';

    // Header
    h += '<div class="detail-header"><div>';
    h += '<div class="detail-title">' + esc(task.title) + '</div>';
    h += '<div class="detail-meta">';
    h += '<span class="meta-item"><span class="label">ID:</span> #' + task.id + '</span>';
    h += statusBadge(task.status);
    if (task.agent_type) h += '<span class="meta-item"><span class="label">Agent:</span> ' + esc(task.agent_type) + '</span>';
    h += '<span class="auto-refresh-dot" title="Auto-refreshing every 10s"></span>';
    h += '</div></div>';
    h += '<div class="detail-actions">';
    if (task.status === 'queued') {
      h += '<button class="btn btn-primary btn-sm" onclick="window.__runTask(' + task.id + ')">Run</button>';
    }
    if (task.status === 'in_progress') {
      h += '<button class="btn btn-danger btn-sm" onclick="window.__cancelTask(' + task.id + ')">Cancel</button>';
    }
    if (task.status === 'waiting_for_input') {
      h += '<button class="btn btn-primary btn-sm" onclick="window.__resumeTask(' + task.id + ')">Resume</button>';
    }
    h += '</div></div>';

    // Description
    if (task.description) {
      h += '<div class="detail-section">';
      h += '<div class="detail-section-title">Description</div>';
      h += '<div class="detail-description">' + esc(task.description) + '</div>';
      h += '</div>';
    }

    // Info grid
    h += '<div class="detail-section">';
    h += '<div class="detail-section-title">Details</div>';
    h += '<div class="detail-grid">';
    h += '<div class="detail-field"><div class="label">Repository</div><div class="value">' + esc(task.repo || '-') + '</div></div>';
    h += '<div class="detail-field"><div class="label">Branch</div><div class="value">' + esc(task.branch_name || '-') + '</div></div>';
    if (task.pr_url) {
      h += '<div class="detail-field"><div class="label">Pull Request</div><div class="value"><a href="' + esc(task.pr_url) + '" target="_blank">' + esc(task.pr_url) + '</a></div></div>';
    }
    if (task.preview_url) {
      h += '<div class="detail-field"><div class="label">Preview URL</div><div class="value"><a href="' + esc(task.preview_url) + '" target="_blank">' + esc(task.preview_url) + '</a></div></div>';
    }

    // Plane issue link
    var issueUrl = planeIssueUrl(task.plane_issue_id);
    if (task.plane_issue_id) {
      h += '<div class="detail-field"><div class="label">Plane Issue</div><div class="value">';
      if (issueUrl) {
        h += '<a href="' + esc(issueUrl) + '" target="_blank">' + esc(task.plane_issue_id) + '</a>';
      } else {
        h += esc(task.plane_issue_id);
      }
      h += '</div></div>';
    }

    // Workflow link
    if (task.workflow_id) {
      h += '<div class="detail-field"><div class="label">Workflow</div><div class="value">';
      h += 'Workflow #' + task.workflow_id;
      if (task.workflow_step !== null && task.workflow_step !== undefined) {
        h += ' (step ' + task.workflow_step + ')';
      }
      h += '</div></div>';
    }

    h += '<div class="detail-field"><div class="label">Created</div><div class="value">' + formatDate(task.created_at) + '</div></div>';
    if (task.started_at) h += '<div class="detail-field"><div class="label">Started</div><div class="value">' + formatDate(task.started_at) + '</div></div>';
    if (task.completed_at) h += '<div class="detail-field"><div class="label">Completed</div><div class="value">' + formatDate(task.completed_at) + '</div></div>';
    h += '</div></div>';

    // Clarification history
    if (task.clarification_context) {
      var context = null;
      try { context = JSON.parse(task.clarification_context); } catch (e) { /* ignore */ }
      if (context) {
        var history = context.history || [];
        var pending = context.pending_question || '';

        if (history.length > 0 || pending) {
          h += '<div class="detail-section">';
          h += '<div class="detail-section-title">Clarification History</div>';
          h += '<div class="clarification-list">';

          history.forEach(function(entry, idx) {
            h += '<div class="clarification-entry">';
            h += '<div class="clarification-question">';
            h += '<span class="clarification-icon">&#x2753;</span>';
            h += '<div><div class="clarification-label">Agent asked' + (entry.asked_at ? ' <span class="clarification-time">' + timeAgo(entry.asked_at) + '</span>' : '') + '</div>';
            h += '<div class="clarification-text">' + esc(entry.question) + '</div></div>';
            h += '</div>';
            if (entry.response) {
              h += '<div class="clarification-response">';
              h += '<span class="clarification-icon">&#x1F4AC;</span>';
              h += '<div><div class="clarification-label">User responded' + (entry.responded_at ? ' <span class="clarification-time">' + timeAgo(entry.responded_at) + '</span>' : '') + '</div>';
              h += '<div class="clarification-text">' + esc(entry.response) + '</div></div>';
              h += '</div>';
            }
            h += '</div>';
          });

          // Show pending question if any
          if (pending) {
            h += '<div class="clarification-entry clarification-pending">';
            h += '<div class="clarification-question">';
            h += '<span class="clarification-icon">&#x23F3;</span>';
            h += '<div><div class="clarification-label">Waiting for response' + (context.asked_at ? ' <span class="clarification-time">' + timeAgo(context.asked_at) + '</span>' : '') + '</div>';
            h += '<div class="clarification-text">' + esc(pending) + '</div></div>';
            h += '</div>';
            h += '</div>';
          }

          h += '</div></div>';
        }
      }
    }

    // Error
    if (task.error) {
      h += '<div class="detail-section">';
      h += '<div class="detail-section-title" style="color:var(--red)">Error</div>';
      h += '<div class="log-entry" style="color:var(--red)">' + esc(task.error) + '</div>';
      h += '</div>';
    }

    el.innerHTML = h;
  }

  window.__runTask = async function(id) {
    await apiPost('/tasks/' + id + '/run', {});
    navigate('tasks', id);
  };

  window.__cancelTask = async function(id) {
    await apiPost('/tasks/' + id + '/cancel', {});
    navigate('tasks', id);
  };

  window.__resumeTask = async function(id) {
    var response = prompt('Enter response for the agent:');
    if (response) {
      await apiPost('/tasks/' + id + '/resume', { response: response });
      navigate('tasks', id);
    }
  };

  // ── Agents Page ────────────────────────────────────────────

  async function renderAgents(el) {
    var agents = await apiFetch('/agents');
    if (!agents) agents = [];
    state.agents = agents;

    var h = '';

    h += '<div class="toolbar">';
    h += '<button class="btn btn-sm btn-secondary" onclick="window.location.hash=\'#/agents\'">Refresh</button>';
    h += '<span class="refresh-indicator">' + agents.length + ' active agent' + (agents.length !== 1 ? 's' : '') + '</span>';
    h += '</div>';

    if (agents.length === 0) {
      h += '<div class="empty-state"><div class="icon">&#x1F916;</div><h3>No active agents</h3><p>Agents will appear here when tasks are running.</p></div>';
    } else {
      h += '<div class="agent-cards">';
      agents.forEach(function(a) {
        h += '<div class="agent-card">';
        h += '<div class="agent-card-header">';
        h += '<div class="agent-card-title">';
        h += (a.task_title || 'Task #' + a.task_id);
        h += '</div>';
        h += statusBadge(a.status);
        h += '</div>';
        h += '<div class="agent-card-meta">';
        h += '<div class="meta-row"><span class="label">Task ID</span><span class="value"><a style="color:var(--accent);cursor:pointer" onclick="window.location.hash=\'#/tasks/' + a.task_id + '\'">#' + a.task_id + '</a></span></div>';
        if (a.agent_type) h += '<div class="meta-row"><span class="label">Type</span><span class="value">' + esc(a.agent_type) + '</span></div>';
        if (a.repo) h += '<div class="meta-row"><span class="label">Repo</span><span class="value">' + esc(a.repo) + '</span></div>';
        if (a.pid) h += '<div class="meta-row"><span class="label">PID</span><span class="value">' + a.pid + '</span></div>';
        h += '<div class="meta-row"><span class="label">Started</span><span class="value">' + timeAgo(a.started_at) + '</span></div>';
        h += '</div></div>';
      });
      h += '</div>';
    }

    el.innerHTML = h;
  }

  // ── Preview Page ───────────────────────────────────────────

  function healthBadge(health) {
    var cls = 'health-badge health-' + esc(health);
    var icon = '';
    switch (health) {
      case 'healthy': icon = '&#x2705;'; break;
      case 'running': icon = '&#x1F7E2;'; break;
      case 'unhealthy': icon = '&#x274C;'; break;
      case 'starting': icon = '&#x1F7E1;'; break;
      case 'stopped': icon = '&#x26D4;'; break;
      case 'created': icon = '&#x1F535;'; break;
      default: icon = '&#x2753;';
    }
    return '<span class="' + cls + '">' + icon + ' ' + esc(health) + '</span>';
  }

  function formatAge(seconds) {
    if (!seconds || seconds < 0) return '-';
    if (seconds < 60) return seconds + 's';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
    if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ' + Math.floor((seconds % 3600) / 60) + 'm';
    return Math.floor(seconds / 86400) + 'd ' + Math.floor((seconds % 86400) / 3600) + 'h';
  }

  function envTypeBadge(envType) {
    if (envType === 'preview') {
      return '<span class="env-type-badge env-type-preview">preview</span>';
    }
    if (envType === 'test') {
      return '<span class="env-type-badge env-type-test">test</span>';
    }
    return '<span class="env-type-badge">' + esc(envType) + '</span>';
  }

  async function renderPreview(el) {
    var containers = await apiFetch('/preview-environments');
    if (!containers) containers = [];

    var h = '';

    h += '<div class="toolbar">';
    h += '<button class="btn btn-sm btn-secondary" onclick="window.location.hash=\'#/preview\'">Refresh</button>';
    h += '<span class="refresh-indicator">' + containers.length + ' environment' + (containers.length !== 1 ? 's' : '') + '</span>';
    h += '<span class="auto-refresh-dot" title="Auto-refreshing every 10s"></span>';
    h += '</div>';

    if (containers.length === 0) {
      h += '<div class="empty-state"><div class="icon">&#x1F310;</div><h3>No preview environments</h3><p>Docker containers with Factory labels will appear here when tasks spin up test or preview environments.</p></div>';
    } else {
      h += '<div class="preview-grid">';
      containers.forEach(function(c) {
        h += '<div class="preview-card">';

        // Header: name + health badge
        h += '<div class="preview-card-header">';
        h += '<div class="preview-card-title">' + esc(c.name) + '</div>';
        h += healthBadge(c.health);
        h += '</div>';

        // Type + Task ID
        h += '<div class="preview-card-tags">';
        h += envTypeBadge(c.env_type);
        if (c.task_id) {
          h += '<span class="preview-task-link" onclick="window.location.hash=\'#/tasks/' + esc(c.task_id) + '\'">Task #' + esc(c.task_id) + '</span>';
        }
        h += '</div>';

        // URL
        if (c.url) {
          h += '<a class="preview-url" href="' + esc(c.url) + '" target="_blank">' + esc(c.url) + '</a>';
        }

        // Metadata
        h += '<div class="agent-card-meta" style="margin-top:12px">';
        if (c.repo) {
          h += '<div class="meta-row"><span class="label">Repo</span><span class="value">' + esc(c.repo) + '</span></div>';
        }
        h += '<div class="meta-row"><span class="label">Status</span><span class="value">' + esc(c.status) + '</span></div>';
        h += '<div class="meta-row"><span class="label">Created</span><span class="value">' + esc(c.created_at) + '</span></div>';
        h += '<div class="meta-row"><span class="label">Age</span><span class="value">' + formatAge(c.age_seconds) + '</span></div>';
        h += '<div class="meta-row"><span class="label">Container</span><span class="value" style="font-family:monospace;font-size:11px">' + esc(c.container_id) + '</span></div>';
        h += '</div>';

        // Actions
        h += '<div class="preview-card-actions">';
        if (c.url) {
          h += '<a class="btn btn-sm btn-primary" href="' + esc(c.url) + '" target="_blank">Open URL</a>';
        }
        h += '<button class="btn btn-sm btn-danger" onclick="window.__teardownEnv(\'' + esc(c.container_id) + '\', \'' + esc(c.name) + '\')">Teardown</button>';
        h += '</div>';

        h += '</div>';
      });
      h += '</div>';
    }

    el.innerHTML = h;
  }

  window.__teardownEnv = async function(containerId, name) {
    if (!confirm('Tear down environment "' + name + '"?\n\nThis will stop and remove the container.')) {
      return;
    }
    try {
      var r = await fetch(API + '/preview-environments/' + containerId, {
        method: 'DELETE',
      });
      if (r.ok) {
        // Refresh the page
        var content = document.getElementById('pageContent');
        if (content) await renderPreview(content);
      } else {
        var data = await r.json();
        alert('Failed to tear down: ' + (data.detail || 'Unknown error'));
      }
    } catch (e) {
      alert('Failed to tear down: ' + e.message);
    }
  };

  // ── Analytics Page ─────────────────────────────────────────

  async function renderAnalytics(el) {
    var tasks = await apiFetch('/tasks');
    if (!tasks) tasks = [];

    var total = tasks.length;
    var done = tasks.filter(function(t) { return t.status === 'done'; }).length;
    var failed = tasks.filter(function(t) { return t.status === 'failed'; }).length;
    var cancelled = tasks.filter(function(t) { return t.status === 'cancelled'; }).length;
    var successRate = total > 0 ? Math.round((done / total) * 100) : 0;

    // Calculate average duration for completed tasks
    var durations = tasks.filter(function(t) {
      return t.status === 'done' && t.started_at && t.completed_at;
    }).map(function(t) {
      return new Date(t.completed_at) - new Date(t.started_at);
    });
    var avgDuration = durations.length > 0
      ? Math.round(durations.reduce(function(a, b) { return a + b; }, 0) / durations.length / 60000)
      : 0;

    var h = '';

    // Summary stats
    h += '<div class="stat-grid">';
    h += '<div class="stat-card"><div class="stat-label">Total Tasks</div><div class="stat-value">' + total + '</div></div>';
    h += '<div class="stat-card"><div class="stat-label">Success Rate</div><div class="stat-value" style="color:var(--green)">' + successRate + '%</div><div class="stat-detail">' + done + ' of ' + total + ' tasks</div></div>';
    h += '<div class="stat-card"><div class="stat-label">Avg Duration</div><div class="stat-value">' + (avgDuration > 0 ? avgDuration + 'm' : '-') + '</div><div class="stat-detail">For completed tasks</div></div>';
    h += '<div class="stat-card"><div class="stat-label">Failure Rate</div><div class="stat-value" style="color:var(--red)">' + (total > 0 ? Math.round((failed / total) * 100) : 0) + '%</div><div class="stat-detail">' + failed + ' failed, ' + cancelled + ' cancelled</div></div>';
    h += '</div>';

    // Status breakdown
    h += '<div class="analytics-grid">';
    h += '<div class="card">';
    h += '<div class="card-header"><div class="card-title">Status Breakdown</div></div>';
    var statuses = {};
    tasks.forEach(function(t) { statuses[t.status] = (statuses[t.status] || 0) + 1; });
    if (Object.keys(statuses).length === 0) {
      h += '<div class="empty-state" style="padding:20px"><p>No data yet</p></div>';
    } else {
      h += '<div style="display:flex;flex-direction:column;gap:8px">';
      var statusOrder = ['done', 'in_progress', 'queued', 'waiting_for_input', 'in_review', 'failed', 'cancelled'];
      statusOrder.forEach(function(s) {
        if (!statuses[s]) return;
        var pct = Math.round((statuses[s] / total) * 100);
        h += '<div style="display:flex;align-items:center;gap:10px">';
        h += '<div style="width:120px">' + statusBadge(s) + '</div>';
        h += '<div style="flex:1;background:var(--surface2);border-radius:4px;height:20px;overflow:hidden">';
        h += '<div style="height:100%;width:' + pct + '%;background:var(--accent);border-radius:4px;transition:width 0.3s"></div>';
        h += '</div>';
        h += '<div style="width:60px;text-align:right;font-size:13px;color:var(--text-dim)">' + statuses[s] + ' (' + pct + '%)</div>';
        h += '</div>';
      });
      h += '</div>';
    }
    h += '</div>';

    // Recent activity
    h += '<div class="card">';
    h += '<div class="card-header"><div class="card-title">Recent Activity</div></div>';
    var sorted = tasks.slice().sort(function(a, b) {
      return new Date(b.created_at) - new Date(a.created_at);
    }).slice(0, 10);
    if (sorted.length === 0) {
      h += '<div class="empty-state" style="padding:20px"><p>No activity yet</p></div>';
    } else {
      h += '<div class="log-entries">';
      sorted.forEach(function(t) {
        h += '<div class="log-entry">';
        h += '<span class="log-time">' + timeAgo(t.created_at) + '</span>';
        h += '#' + t.id + ' ' + esc(t.title) + ' - ' + esc(t.status);
        h += '</div>';
      });
      h += '</div>';
    }
    h += '</div>';

    h += '</div>';

    el.innerHTML = h;
  }

  // ── Badges & Health ────────────────────────────────────────

  function updateBadges(tasks, agents) {
    var badge;
    if (tasks) {
      badge = document.getElementById('badgeTasks');
      if (badge) badge.textContent = tasks.length;
    }
    if (agents) {
      badge = document.getElementById('badgeAgents');
      if (badge) badge.textContent = agents.length;
    }
  }

  async function checkHealth() {
    try {
      var r = await fetch('/health');
      state.healthy = r.ok;
    } catch (e) {
      state.healthy = false;
    }
    var dot = document.getElementById('healthDot');
    var text = document.getElementById('healthText');
    if (dot) {
      if (state.healthy) {
        dot.classList.add('healthy');
        text.textContent = 'System healthy';
      } else {
        dot.classList.remove('healthy');
        text.textContent = 'Unreachable';
      }
    }
  }

  // ── Sidebar Toggle ─────────────────────────────────────────

  window.__toggleSidebar = function() {
    var sidebar = document.getElementById('sidebar');
    var overlay = document.getElementById('sidebarOverlay');
    state.sidebarOpen = !state.sidebarOpen;
    if (state.sidebarOpen) {
      sidebar.classList.add('open');
      overlay.classList.add('open');
    } else {
      sidebar.classList.remove('open');
      overlay.classList.remove('open');
    }
  };

  window.__closeSidebar = function() {
    state.sidebarOpen = false;
    document.getElementById('sidebar').classList.remove('open');
    document.getElementById('sidebarOverlay').classList.remove('open');
  };

  // ── Auth ───────────────────────────────────────────────────

  async function checkAuth() {
    try {
      var r = await fetch('/auth/status');
      if (!r.ok) return;
      var data = await r.json();
      state.oauthEnabled = data.oauth_enabled;
      if (data.authenticated && data.user) {
        state.user = data.user;
        var userMenu = document.getElementById('userMenu');
        var userName = document.getElementById('userName');
        if (userMenu && userName) {
          var name = data.user.display_name || data.user.email || 'User';
          userName.textContent = name;
          userMenu.style.display = 'flex';
        }
      }
    } catch (e) {
      // Auth status check failed silently
    }
  }

  // ── Init ───────────────────────────────────────────────────

  window.addEventListener('hashchange', function() {
    window.__closeSidebar();
    stopPolling();
    handleRoute();
  });

  // Initial load
  loadSettings();
  checkAuth();
  checkHealth();
  handleRoute();

  // Periodic health check
  setInterval(checkHealth, 30000);
})();
