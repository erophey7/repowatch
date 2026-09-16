function fmtTs(ts) {
  return ts ? ts.replace('T', ' ').replace(/\+00:00$/, ' UTC') : '—';
}

// docs_dev/ROADMAP.md item 20 — trust state. repo.key_expires_at/
// key_expiring_soon come pre-computed from the last check cycle (see
// api.repos_list_payload); this just renders them, no live gpg call from
// the browser. '—' covers apk repos, repos without verify_signature, and
// "unknown" (e.g. the full gpg binary isn't installed on the host) alike —
// the dashboard can't tell those apart from key_expires_at alone, and
// doesn't need to: none of them call for operator attention right now.
function fmtKeyExpiry(repo) {
  if (!repo.key_expires_at) return '—';
  const days = Math.floor((new Date(repo.key_expires_at) - Date.now()) / 86400000);
  const label = days < 0 ? `expired ${-days}d ago` : `expires in ${days}d`;
  if (!repo.key_expiring_soon) return `<span class="mono">${fmtTs(repo.key_expires_at)}</span>`;
  return `<span class="badge badge-warn" title="${esc(fmtTs(repo.key_expires_at))}">${esc(label)}</span>`;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

let lastRepos = [];

async function loadRepos() {
  $status.textContent = 'Loading…';
  try {
    const res = await fetch('/api/repos');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const repos = await res.json();
    lastRepos = repos;
    render(repos);
    populateRepoFilter(repos);
    populateGroupList(repos);
    $status.textContent = `Updated: ${new Date().toLocaleTimeString()} · repositories: ${repos.length}`;
  } catch (err) {
    $status.textContent = `Load error: ${err.message}`;
  }
}

function populateRepoFilter(repos) {
  const select = document.getElementById('req-repo-filter');
  const current = select.value;
  select.innerHTML = '<option value="">all repositories</option>' +
    repos.map(r => `<option value="${esc(r.id)}">${esc(r.id)}</option>`).join('');
  select.value = current;
}

// Suggestions for the "group" field in the add/edit form — names already
// in use, so typos don't create accidental duplicates (the group itself is
// a free-text label, there's no separate entity/CRUD for it).
function populateGroupList(repos) {
  const names = [...new Set(
    repos.map(r => (r.config && r.config.group) || '').filter(Boolean)
  )].sort((a, b) => a.localeCompare(b));
  document.getElementById('group-list').innerHTML =
    names.map(n => `<option value="${esc(n)}">`).join('');
}

const UNGROUPED_LABEL = 'Ungrouped';

// Manual groups (RepoConfig.group) — not automatic grouping by type, but a
// label the operator assigns through the edit form. Each group is a
// collapsible <details>, no third-party libraries.
function groupRepos(repos) {
  const byName = new Map();
  for (const repo of repos) {
    const name = (repo.config && repo.config.group) || UNGROUPED_LABEL;
    if (!byName.has(name)) byName.set(name, []);
    byName.get(name).push(repo);
  }
  const names = [...byName.keys()].filter(n => n !== UNGROUPED_LABEL).sort((a, b) => a.localeCompare(b));
  if (byName.has(UNGROUPED_LABEL)) names.push(UNGROUPED_LABEL);
  return names.map(name => ({ name, repos: byName.get(name) }));
}

const REPOS_THEAD_HTML = `
  <tr>
    <th>id</th><th class="col-type">type</th><th class="col-upstream">upstream</th>
    <th class="col-prefetch">prefetch</th><th class="col-interval">interval</th>
    <th>packages</th><th class="col-warmed">warmed</th><th>last check</th>
    <th class="col-changed">changed at</th><th class="col-key">key</th><th>files</th><th>actions</th>
  </tr>
`;

// Group open/closed state survives auto-refresh (loadRepos redraws everything
// from scratch every 30s) — it's kept by group name, separately from the DOM.
// Also persisted to localStorage so it survives a page reload, not just the
// in-session redraw; per-browser/per-origin, not synced across devices.
// By default (a group never seen before, in this session or a past one),
// only groups with at least one changed repository start expanded.
const GROUP_STATE_KEY = 'repowatch:group-open';
function loadGroupOpenState() {
  try {
    const raw = localStorage.getItem(GROUP_STATE_KEY);
    return raw ? new Map(Object.entries(JSON.parse(raw))) : new Map();
  } catch {
    return new Map(); // private browsing / storage disabled — falls back to session-only state
  }
}
function saveGroupOpenState(map) {
  try { localStorage.setItem(GROUP_STATE_KEY, JSON.stringify(Object.fromEntries(map))); } catch {}
}
const groupOpenState = loadGroupOpenState();

function createRepoRow(repo) {
  const tr = document.createElement('tr');
  tr.className = 'repo-row';
  tr.dataset.repoId = repo.id;
  const changedBadge = repo.changed_at
    ? `<span class="badge badge-changed">has changes</span>`
    : `<span class="badge badge-stale">unchanged</span>`;
  tr.innerHTML = `
    <td class="mono">${esc(repo.id)}</td>
    <td class="col-type">${esc(repo.type)}</td>
    <td class="mono col-upstream">${esc(repo.upstream)}</td>
    <td class="col-prefetch">${repo.prefetch ? 'yes' : 'no'}</td>
    <td class="col-interval">${repo.check_interval ? `${repo.check_interval}s` : '—'}</td>
    <td>${repo.package_count ?? '—'}</td>
    <td class="col-warmed">${repo.warmed_count ?? '—'}</td>
    <td class="mono">${fmtTs(repo.last_check)}</td>
    <td class="col-changed">${changedBadge} <span class="mono">${fmtTs(repo.changed_at)}</span></td>
    <td class="col-key">${fmtKeyExpiry(repo)}</td>
    <td class="browse-link">${repo.browse_url
      ? `<a href="${esc(repo.browse_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">browse →</a>`
      : '—'}</td>
    <td>${isLoggedIn ? `
      <div class="row-actions">
        <button type="button" data-action="edit-repo">✎</button>
        <button type="button" class="danger" data-action="delete-repo">✕</button>
      </div>` : '—'}</td>
  `;
  tr.querySelector('[data-action="edit-repo"]')?.addEventListener('click', (ev) => {
    ev.stopPropagation();
    openEditForm(repo);
  });
  tr.querySelector('[data-action="delete-repo"]')?.addEventListener('click', (ev) => {
    ev.stopPropagation();
    deleteRepo(repo.id);
  });
  tr.addEventListener('click', () => toggleDetail(tr, repo.id));
  return tr;
}

function render(repos) {
  const $groups = document.getElementById('repos-groups');
  const openPanel = $groups.querySelector('.detail-row');
  $groups.innerHTML = '';
  if (!repos.length) {
    $groups.innerHTML = '<p class="empty">No repositories configured.' +
      (isLoggedIn ? ' Use + Add repository to connect your first source.' : '') + '</p>';
    return;
  }
  for (const { name, repos: groupItems } of groupRepos(repos)) {
    const details = document.createElement('details');
    details.className = 'repo-group';
    if (!groupOpenState.has(name)) {
      groupOpenState.set(name, groupItems.some(r => r.changed_at));
    }
    details.open = groupOpenState.get(name);
    details.addEventListener('toggle', () => {
      groupOpenState.set(name, details.open);
      saveGroupOpenState(groupOpenState);
    });

    const summary = document.createElement('summary');
    summary.innerHTML = `${esc(name)} <span class="group-count">(${groupItems.length})</span>`;
    details.appendChild(summary);

    const wrap = document.createElement('div');
    wrap.className = 'table-wrap';
    const table = document.createElement('table');
    table.innerHTML = `<thead>${REPOS_THEAD_HTML}</thead>`;
    const tbody = document.createElement('tbody');
    for (const repo of groupItems) {
      tbody.appendChild(createRepoRow(repo));
      if (openPanel?.dataset.repoId === repo.id && openPanel.dataset.auth === String(isLoggedIn)) {
        tbody.appendChild(openPanel);
      }
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    details.appendChild(wrap);
    $groups.appendChild(details);
  }
}
