const $status = document.getElementById('status');

// The server allows either an administrator or explicitly enabled guest reads.
// Cookies are HttpOnly; neither the password nor the session secret is kept in JS.
let csrfToken = null;
let isLoggedIn = false;
const $toggleAddFormBtn = document.getElementById('toggle-add-form');
const $toggleSettingsFormBtn = document.getElementById('toggle-settings-form');

try { sessionStorage.removeItem('repowatch_admin_password'); } catch (_) {}
const nativeFetch = window.fetch.bind(window);
const sessionReady = nativeFetch('/api/auth/session').then(async response => {
  if (!response.ok) { location.replace('/login'); throw new Error('Session expired'); }
  const session = await response.json();
  isLoggedIn = session.role === 'admin';
  csrfToken = session.csrf_token || null;
  document.body.dataset.role = isLoggedIn ? 'admin' : 'guest';
  document.getElementById('access-role').textContent = isLoggedIn ? 'Administrator' : 'Guest · read-only';
  document.getElementById('admin-login').hidden = isLoggedIn;
  document.getElementById('logout-btn').hidden = !isLoggedIn;
  document.getElementById('tokens-panel').hidden = !isLoggedIn;
  document.getElementById('storage-panel').hidden = !isLoggedIn;
  $toggleAddFormBtn.hidden = !isLoggedIn;
  $toggleSettingsFormBtn.hidden = false;
  $toggleSettingsFormBtn.textContent = isLoggedIn ? '⚙ Settings' : '⚙ View settings';
  document.querySelectorAll('#settings-form input').forEach(input => { input.readOnly = !isLoggedIn; });
  document.querySelector('#settings-form button[type="submit"]').hidden = !isLoggedIn;
  document.getElementById('s-bandwidth-add').hidden = !isLoggedIn;
  document.getElementById('cancel-settings-form').textContent = isLoggedIn ? 'Cancel' : 'Close';
});
const fetch = async function(url, options = {}) {
  await sessionReady;
  const headers = {...options.headers};
  if (options.method && options.method !== 'GET') {
    if (!isLoggedIn) throw new Error('Administrator login required');
    headers['X-CSRF-Token'] = csrfToken;
  }
  const response = await nativeFetch(url, {...options, headers});
  if (response.status === 401) { location.replace('/login'); throw new Error('Session expired'); }
  return response;
}
document.getElementById('logout-btn').addEventListener('click', async () => {
  try {
    const response = await fetch('/api/auth/logout', {method:'POST'});
    if (response.ok) location.replace('/login');
    else alert('Failed to end the session');
  } catch (error) { alert(error.message); }
});
async function loadTokens() {
  const response = await fetch('/api/tokens');
  if (!response.ok) throw new Error('Failed to load tokens');
  const tokens = await response.json();
  const list = document.getElementById('tokens-list');
  list.innerHTML = tokens.length ? tokens.map(token => {
    const date = value => value ? new Date(value * 1000).toLocaleString() : '—';
    const status = token.revoked_at ? 'Revoked' : token.expires_at && token.expires_at * 1000 <= Date.now() ? 'Expired' : 'Active';
    return `<p><strong>${esc(token.name)}</strong> — ${status}; repositories: ${token.repo_ids == null ? 'all' : esc(token.repo_ids.join(', ') || 'none')}; created: ${date(token.created_at)}; until: ${date(token.expires_at)}; last used: ${date(token.last_used_at)}
      ${!token.revoked_at ? `<button type="button" data-revoke="${esc(token.id)}">Revoke</button>` : ''}</p>`;
  }).join('') : '<p>No tokens issued yet.</p>';
  list.querySelectorAll('[data-revoke]').forEach(button => button.addEventListener('click', async () => {
    if (!confirm('Revoke this host\'s access?')) return;
    const response = await fetch(`/api/tokens/${encodeURIComponent(button.dataset.revoke)}/revoke`, {method:'POST'});
    if (!response.ok) { document.getElementById('token-message').textContent = 'Failed to revoke the token'; return; }
    await loadTokens();
  }));
}
document.getElementById('tokens-panel').addEventListener('toggle', () => {
  if (document.getElementById('tokens-panel').open) loadTokens().catch(error => {
    document.getElementById('token-message').textContent = error.message;
  });
});
