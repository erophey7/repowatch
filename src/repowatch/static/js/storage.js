function renderStorageStats(stats) {
  const bytes = n => `${n.toLocaleString()} bytes`;
  let html = `<p>state_db: ${bytes(stats.state_db_bytes)}</p><ul>`;
  for (const [table, count] of Object.entries(stats.tables)) {
    html += `<li>${esc(table)}: ${count.toLocaleString()} row(s)</li>`;
  }
  html += '</ul>';
  if (stats.cache_dir === undefined) {
    // not requested yet
  } else if (stats.cache_dir === null) {
    html += '<p class="dim">cache_dir: not set in config.yaml (nginx.cache_dir) — nothing to calculate</p>';
  } else if (stats.cache_dir.error) {
    html += `<p class="form-msg error">cache_dir: ${esc(stats.cache_dir.error)}</p>`;
  } else {
    const viaProbe = stats.cache_dir.source === 'cache_probe';
    html += `<p>cache_dir (${esc(stats.cache_dir.path)}): ${bytes(stats.cache_dir.size_bytes)}, ` +
      `${stats.cache_dir.file_count.toLocaleString()} file(s)` +
      (viaProbe ? ' <span class="dim">(via cache-probe, no permission gaps)</span>' : '') +
      '</p>';
    if (stats.cache_dir.inaccessible_directories) {
      html += `<p class="form-msg error">Warning: ${stats.cache_dir.inaccessible_directories.toLocaleString()} ` +
        'subdirectory/ies could not be read (permission denied) — the numbers above are an ' +
        'UNDERCOUNT. nginx creates proxy_cache_path\'s levels=1:2 subdirectories 0700, owned by ' +
        'the nginx worker user; the repowatch service user typically can\'t read them at all.</p>';
    }
    if (stats.cache_dir.unreadable_keys) {
      html += `<p class="form-msg error">Note: ${stats.cache_dir.unreadable_keys.toLocaleString()} ` +
        'file(s) had an unreadable stored key (not individually identifiable).</p>';
    }
    if (stats.cache_dir.unreadable_sizes) {
      html += `<p class="form-msg error">Warning: ${stats.cache_dir.unreadable_sizes.toLocaleString()} ` +
        'file size(s) could not be read; reported bytes are incomplete.</p>';
    }
    if (stats.cache_dir.incomplete_leaves) {
      html += `<p class="form-msg error">Warning: ${stats.cache_dir.incomplete_leaves.toLocaleString()} ` +
        'of 4096 cache directories could not be scanned (transient failure) — the numbers above ' +
        'are an UNDERCOUNT.</p>';
    }
  }
  return html;
}
async function loadStorageStats(includeCacheDir = false) {
  const body = document.getElementById('storage-body');
  const response = await fetch(`/api/stats${includeCacheDir ? '?cache_dir=1' : ''}`);
  if (!response.ok) { body.textContent = 'Failed to load storage stats'; return; }
  body.innerHTML = renderStorageStats(await response.json());
}
document.getElementById('storage-panel').addEventListener('toggle', () => {
  if (document.getElementById('storage-panel').open) loadStorageStats().catch(() => {
    document.getElementById('storage-body').textContent = 'Failed to load storage stats';
  });
});
document.getElementById('calc-cache-size').addEventListener('click', async () => {
  const button = document.getElementById('calc-cache-size');
  const note = document.getElementById('cache-size-note');
  button.disabled = true;
  note.textContent = 'Walking the cache directory — this can take a while for a large cache…';
  try {
    await loadStorageStats(true);
    note.textContent = '';
  } catch (err) {
    note.textContent = `Error: ${err.message}`;
  } finally {
    button.disabled = false;
  }
});
document.getElementById('token-form').addEventListener('submit', async event => {
  event.preventDefault();
  const expiry = document.getElementById('token-expiry').value;
  const button = event.target.querySelector('button');
  button.disabled = true;
  try {
    const response = await fetch('/api/tokens', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:document.getElementById('token-name').value, expires_at:expiry ? new Date(expiry).getTime()/1000 : null, repo_ids:document.getElementById('token-repos').value.trim() ? document.getElementById('token-repos').value.split(',').map(s => s.trim()).filter(Boolean) : null})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Failed to issue token');
    document.getElementById('token-secret').value = data.token;
    document.getElementById('token-secret-wrap').hidden = false;
    document.getElementById('token-message').textContent = 'Token issued';
    await loadTokens();
  } catch(error) { document.getElementById('token-message').textContent = error.message; }
  finally { button.disabled = false; }
});
document.getElementById('hide-token').addEventListener('click', () => {
  document.getElementById('token-secret').value = '';
  document.getElementById('token-secret-wrap').hidden = true;
});
