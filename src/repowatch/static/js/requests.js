// --- recent client requests (syslog_listener) ---
const $reqBody = document.getElementById('requests-body');
const $reqFilter = document.getElementById('req-repo-filter');
const $reqRefresh = document.getElementById('req-refresh');

let requestCursor = null, requestRows = [], requestGeneration = 0, requestsBusy = false;
const requestMore = document.createElement('button');
requestMore.textContent = 'Show more';
requestMore.hidden = true;
$reqBody.closest('table').after(requestMore);
requestMore.onclick = () => loadRequests(true);
async function loadRequests(append = false) {
  if (append && requestsBusy) return;
  if (!append) { requestGeneration++; requestCursor = null; requestRows = []; }
  const generation = requestGeneration;
  requestsBusy = true; requestMore.disabled = true;
  const repoId = $reqFilter.value;
  const qs = new URLSearchParams({ limit: '50' });
  if (repoId) qs.set('repo_id', repoId);
  if (requestCursor) qs.set('cursor', requestCursor);

  try {
    const res = await fetch(`/api/requests?${qs}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const page = await res.json();
    if (generation !== requestGeneration) return;
    requestRows.push(...page.items);
    requestCursor = page.next_cursor;
    requestMore.hidden = !requestCursor;
    const rows = requestRows;
    $reqBody.innerHTML = rows.length
      ? rows.map(r => `
          <tr>
            <td class="mono">${fmtTs(r.ts)}</td>
            <td class="mono">${esc(r.client_ip ?? '—')}</td>
            <td class="mono">${esc(r.repo_id ?? '—')}</td>
            <td>${esc(r.method)}</td>
            <td class="mono">${esc(r.path)}</td>
            <td>${esc(r.status ?? '—')}</td>
            <td>${esc(r.cache_status ?? '—')}</td>
          </tr>`).join('')
      : `<tr><td colspan="7" class="empty">no data — enable syslog_listener in config.yaml</td></tr>`;
  } catch (err) {
    if (generation !== requestGeneration) return;
    $reqBody.innerHTML = `<tr><td colspan="7" class="empty">Load error: ${esc(err.message)}</td></tr>`;
  } finally {
    if (generation === requestGeneration) { requestsBusy = false; requestMore.disabled = false; }
  }
}
