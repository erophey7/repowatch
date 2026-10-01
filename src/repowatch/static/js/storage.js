// Same unit thresholds/multipliers as sizes.py's format_bytes() — binary
// (KiB/MiB/GiB/TiB), one decimal place, values under 1 KiB shown as a
// whole number of bytes. Kept in JS too (rather than round-tripping
// through the server) so the dashboard can format instantly.
const DISPLAY_UNITS = [['TiB', 1024 ** 4], ['GiB', 1024 ** 3], ['MiB', 1024 ** 2], ['KiB', 1024]];
function formatBytes(n) {
  const sign = n < 0 ? '-' : '';
  n = Math.abs(n);
  for (const [unit, size] of DISPLAY_UNITS) {
    if (n >= size) return `${sign}${(n / size).toFixed(1)} ${unit}`;
  }
  return `${sign}${Math.round(n)} B`;
}
function formatByteRate(n) {
  return n == null ? 'unlimited' : `${formatBytes(n)}/s`;
}
function bytesWithExact(n) {
  return `${formatBytes(n)} (${n.toLocaleString()} bytes)`;
}

function renderStorageStats(stats) {
  const bytes = bytesWithExact;
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
const orphansList = document.getElementById('orphans-list');
const orphansActions = document.getElementById('orphans-actions');
const orphansMsg = document.getElementById('orphans-msg');

// silent=true (used right after a delete) updates the list/checkboxes
// without touching orphansMsg — the same reasoning as packages.js'
// scanForStaleEntries(silent): a re-scan's own "N found" would otherwise
// immediately overwrite the "Deleted: ..." summary the operator just
// clicked the button to see.
async function scanForOrphans(silent = false) {
  if (!silent) { orphansMsg.className = 'form-msg'; orphansMsg.textContent = 'Scanning…'; }
  orphansActions.hidden = true;
  try {
    const res = await fetch('/api/storage/orphans');
    const data = await res.json();
    if (!res.ok) {
      orphansList.hidden = true;
      if (!silent) { orphansMsg.className = 'form-msg error'; orphansMsg.textContent = data.error || `HTTP ${res.status}`; }
      return;
    }
    const ids = Object.keys(data.orphaned);
    orphansList.hidden = false;
    if (!ids.length) {
      orphansList.innerHTML = '<div class="empty">no orphaned repositories found</div>';
      if (!silent) orphansMsg.textContent = '';
      return;
    }
    // All checked by default — the operator opts OUT of ones they don't want deleted.
    orphansList.innerHTML = ids.map(id => {
      const rows = Object.entries(data.orphaned[id]).map(([table, count]) => `${esc(table)}: ${count}`).join(', ');
      return `<label class="pick-row"><input type="checkbox" value="${esc(id)}" checked> ` +
        `${esc(id)} <span class="dim">(${rows})</span></label>`;
    }).join('');
    orphansActions.hidden = false;
    if (!silent) orphansMsg.textContent = `${ids.length} orphaned repository/ies found.`;
  } catch (err) {
    if (!silent) { orphansMsg.className = 'form-msg error'; orphansMsg.textContent = `Network error: ${err.message}`; }
  }
}
document.getElementById('scan-orphans-btn').addEventListener('click', () => scanForOrphans());
document.getElementById('orphans-select-all-btn').addEventListener('click', () => {
  orphansList.querySelectorAll('input').forEach(cb => { cb.checked = true; });
});
document.getElementById('orphans-clear-btn').addEventListener('click', () => {
  orphansList.querySelectorAll('input').forEach(cb => { cb.checked = false; });
});
document.getElementById('orphans-delete-btn').addEventListener('click', async () => {
  const ids = [...orphansList.querySelectorAll('input:checked')].map(cb => cb.value);
  if (!ids.length) { orphansMsg.className = 'form-msg error'; orphansMsg.textContent = 'Nothing selected'; return; }
  if (!confirm(`Delete SQL bookkeeping for ${ids.length} repository/ies? This does not touch cache files on disk.`)) return;
  orphansMsg.className = 'form-msg';
  orphansMsg.textContent = 'Deleting…';
  try {
    const res = await fetch('/api/storage/orphans', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo_ids: ids }),
    });
    const data = await res.json();
    if (!res.ok) {
      orphansMsg.className = 'form-msg error';
      orphansMsg.textContent = data.error || `HTTP ${res.status}`;
      return;
    }
    orphansMsg.className = 'form-msg ok';
    orphansMsg.textContent = `Deleted: ${Object.keys(data.deleted).length} repository/ies.`;
    await scanForOrphans(true); // silent re-scan: keep the "Deleted: ..." summary on screen
    await loadStorageStats(); // row counts shown above just changed too
  } catch (err) {
    orphansMsg.className = 'form-msg error';
    orphansMsg.textContent = `Network error: ${err.message}`;
  }
});
const zombiesList = document.getElementById('zombies-list');
const zombiesActions = document.getElementById('zombies-actions');
const zombiesMsg = document.getElementById('zombies-msg');

// silent=true (used right after a purge) — same reasoning as
// scanForOrphans(silent) above: a re-scan's own "N found" would otherwise
// overwrite the "Purged: ..." summary the operator just clicked to see.
async function scanForZombies(silent = false) {
  if (!silent) { zombiesMsg.className = 'form-msg'; zombiesMsg.textContent = 'Scanning…'; }
  zombiesActions.hidden = true;
  try {
    const res = await fetch('/api/storage/zombies');
    const data = await res.json();
    if (!res.ok) {
      zombiesList.hidden = true;
      if (!silent) { zombiesMsg.className = 'form-msg error'; zombiesMsg.textContent = data.error || `HTTP ${res.status}`; }
      return;
    }
    if (!data.enable_purge) {
      zombiesList.hidden = true;
      if (!silent) {
        zombiesMsg.className = 'form-msg error';
        zombiesMsg.textContent = 'nginx.enable_purge is not enabled in config.yaml — nothing to purge.';
      }
      return;
    }
    zombiesList.hidden = false;
    if (!data.candidates.length) {
      zombiesList.innerHTML = '<div class="empty">no zombie packages found</div>';
      if (!silent) zombiesMsg.textContent = '';
      return;
    }
    // All checked by default — the operator opts OUT of ones they don't want purged.
    zombiesList.innerHTML = data.candidates.map(c =>
      `<label class="pick-row"><input type="checkbox" data-repo="${esc(c.repo_id)}" value="${esc(c.package_key)}" checked> ` +
      `${esc(c.repo_id)} / ${esc(c.package_key)} <span class="dim">(${esc(c.filename)})</span></label>`).join('');
    zombiesActions.hidden = false;
    if (!silent) zombiesMsg.textContent = `${data.candidates.length} zombie package(s) found.`;
  } catch (err) {
    if (!silent) { zombiesMsg.className = 'form-msg error'; zombiesMsg.textContent = `Network error: ${err.message}`; }
  }
}
document.getElementById('scan-zombies-btn').addEventListener('click', () => scanForZombies());
document.getElementById('zombies-select-all-btn').addEventListener('click', () => {
  zombiesList.querySelectorAll('input').forEach(cb => { cb.checked = true; });
});
document.getElementById('zombies-clear-btn').addEventListener('click', () => {
  zombiesList.querySelectorAll('input').forEach(cb => { cb.checked = false; });
});
document.getElementById('zombies-purge-btn').addEventListener('click', async () => {
  const items = [...zombiesList.querySelectorAll('input:checked')]
    .map(cb => ({ repo_id: cb.dataset.repo, package_key: cb.value }));
  if (!items.length) { zombiesMsg.className = 'form-msg error'; zombiesMsg.textContent = 'Nothing selected'; return; }
  zombiesMsg.className = 'form-msg';
  zombiesMsg.textContent = 'Purging…';
  try {
    const res = await fetch('/api/storage/zombies', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items }),
    });
    const data = await res.json();
    if (!res.ok) {
      zombiesMsg.className = 'form-msg error';
      zombiesMsg.textContent = data.error || `HTTP ${res.status}`;
      return;
    }
    const counts = { purged: 0, not_cached: 0, retained_shared: 0, error: 0 };
    for (const repoResults of Object.values(data.results)) {
      for (const outcome of Object.values(repoResults)) {
        const bucket = outcome.startsWith('error') ? 'error' : outcome;
        counts[bucket] = (counts[bucket] || 0) + 1;
      }
    }
    zombiesMsg.className = counts.error ? 'form-msg error' : 'form-msg ok';
    zombiesMsg.textContent = `Purged: ${counts.purged}, not cached: ${counts.not_cached}, shared artifacts retained: ${counts.retained_shared}` +
      (counts.error ? `, errors: ${counts.error}` : '');
    await scanForZombies(true); // silent re-scan: keep the "Purged: ..." summary on screen
  } catch (err) {
    zombiesMsg.className = 'form-msg error';
    zombiesMsg.textContent = `Network error: ${err.message}`;
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

function renderCompleteness(data) {
  const rows = data.items.map(row => {
    const percent = row.percent == null ? '—' : `${row.percent}%`;
    const reason = {no_snapshot: 'No index snapshot yet', source_changed: 'Source changed; awaiting a new index',
      incomplete_evidence: 'Unknown: incomplete scan, pending replacement or unresolved Nix closure'}[row.reason] || row.state;
    return `<tr><td>${esc(row.repo_id)}</td><td>${row.total_packages ?? '—'}</td>` +
      `<td>${row.cached_packages}</td><td>${row.missing_packages}</td><td>${row.unknown_packages}</td>` +
      `<td>${percent}</td><td>${esc(reason)}</td></tr>`;
  }).join('');
  return `<p class="dim">Observation: ${esc(data.started_at)} — ${esc(data.finished_at)}. ` +
    'Cache contents can change during and after the scan.</p>' +
    (data.failed_leaves || data.unreadable_entries ? `<p class="form-msg error">Incomplete inventory: ` +
      `${data.failed_leaves} directories failed, ${data.unreadable_entries} entries unreadable. ` +
      'Unobserved packages may be unknown rather than missing.</p>' : '') +
    (rows ? '<table><thead><tr><th>Repository</th><th>Catalog</th><th>Found</th><th>Missing</th>' +
      '<th>Unknown</th><th>On disk</th><th>State</th></tr></thead><tbody>' + rows + '</tbody></table>' :
      '<p class="dim">No repositories configured.</p>');
}
document.getElementById('scan-completeness-btn').addEventListener('click', async () => {
  const button = document.getElementById('scan-completeness-btn');
  const message = document.getElementById('completeness-msg');
  const results = document.getElementById('completeness-results');
  button.disabled = true;
  message.className = 'form-msg';
  message.textContent = 'Scanning the cache; large caches can take a while…';
  results.textContent = '';
  try {
    const response = await fetch('/api/storage/completeness?usage=1');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    results.innerHTML = renderCompleteness(data) + (data.storage_usage ? renderStorageUsage(data.storage_usage) : '');
    message.textContent = 'Measurement complete. Run again to refresh.';
  } catch (error) {
    message.className = 'form-msg error';
    message.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

function renderStorageUsage(usage) {
  const table = (rows, grouped) => '<table><thead><tr><th>' + (grouped ? 'Group' : 'Repository') +
    '</th><th>Physical bytes</th><th>Available bytes</th></tr></thead><tbody>' + rows.map(row => {
      const name = grouped ? (row.group == null ? '(ungrouped)' : row.group) : row.repo_id;
      const note = row.reason ? ' — attribution unavailable' :
        row.unknown_repositories ? ` — ${row.unknown_repositories} repository/ies unresolved` : '';
      return `<tr><td>${esc(name)}${esc(note)}</td><td>${row.reason ? '—' : bytesWithExact(row.physical_bytes)}</td>` +
        `<td>${row.reason ? '—' : bytesWithExact(row.available_bytes)}</td></tr>`;
    }).join('') + '</tbody></table>';
  return '<h3>Cache file sizes</h3><p class="dim">Physical bytes count each file once at its owner. ' +
    'Available bytes count shared files for each using repository, once within each group. ' +
    'Sizes include nginx headers; available bytes do not guarantee freshness or valid contents.</p>' +
    `<p>Observed total: ${bytesWithExact(usage.total_bytes)}. Attributed: ${bytesWithExact(usage.attributed_bytes)}. ` +
    `Unattributed: ${bytesWithExact(usage.unattributed_bytes)} (${usage.unattributed_files} files).</p>` +
    (!usage.inventory_complete ? `<p class="form-msg error">Partial inventory: ${usage.failed_leaves} directories failed, ` +
      `${usage.unreadable_sizes} sizes unreadable, ${usage.unreadable_keys} keys unreadable. ` +
      'Only known bytes are shown; repository and group values may be undercounts.</p>' : '') +
    table(usage.repositories, false) + '<h4>Groups</h4>' + table(usage.groups, true) +
    '<p class="dim">Unattributed files include unknown keys, metadata and files outside known catalogs/warm history. ' +
    'Available sizes overlap across repositories and groups; do not sum them as physical disk usage.</p>';
}

const dedupList = document.getElementById('dedup-list');
const dedupActions = document.getElementById('dedup-actions');
const dedupMsg = document.getElementById('dedup-msg');
let dedupGeneration = null;
function setDedupBusy(busy) {
  document.getElementById('scan-dedup-btn').disabled = busy;
  dedupActions.querySelectorAll('button').forEach(button => { button.disabled = busy; });
  dedupList.querySelectorAll('input').forEach(input => { input.disabled = busy; });
}
async function scanForDedupCopies() {
  setDedupBusy(true);
  dedupGeneration = null;
  dedupActions.hidden = true;
  dedupList.hidden = true;
  dedupMsg.className = 'form-msg';
  dedupMsg.textContent = 'Scanning the cache for redundant copies…';
  try {
    const res = await fetch('/api/storage/dedup');
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    dedupGeneration = data.generation;
    dedupList.innerHTML = data.candidates.map(row =>
      `<label class="pick-row"><input type="checkbox" value="${esc(row.key)}" checked> ` +
      `${esc(row.repo_id)} / ${esc(row.filename)} <span class="dim">` +
      `(${row.bytes == null ? 'unknown size' : formatBytes(row.bytes)})</span></label>`).join('');
    dedupList.hidden = false;
    dedupActions.hidden = !data.candidates.length;
    dedupMsg.textContent = `${data.total_candidates} redundant copies, ${formatBytes(data.total_bytes)} observed.` +
      (data.truncated ? ' Showing the first 1000; scan again after removal to continue.' : '');
  } catch (err) {
    dedupMsg.className = 'form-msg error';
    dedupMsg.textContent = err.message;
  } finally { setDedupBusy(false); }
}
async function purgeDedupCopies() {
  const keys = [...dedupList.querySelectorAll('input:checked')].map(input => input.value);
  if (!keys.length) { dedupMsg.textContent = 'Nothing selected'; return; }
  if (!dedupGeneration) { dedupMsg.textContent = 'Preview copies again before removal.'; return; }
  setDedupBusy(true);
  dedupMsg.className = 'form-msg';
  let purged = 0, skipped = 0, errors = 0, bytes = 0;
  let stopped = '';
  try {
    for (let offset = 0; offset < keys.length; offset += 256) {
      dedupMsg.textContent = `Removing redundant copies… ${offset}/${keys.length} checked`;
      const res = await fetch('/api/storage/dedup', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({generation: dedupGeneration, keys: keys.slice(offset, offset + 256)})});
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      for (const outcome of Object.values(data.results)) {
        if (outcome === 'purged') purged++;
        else if (outcome.startsWith('error')) errors++;
        else skipped++;
      }
      bytes += data.observed_removed_bytes;
      if (data.stopped) { stopped = data.stopped; break; }
    }
  } catch (err) { stopped = err.message; }
  finally {
    dedupGeneration = null;
    dedupActions.hidden = true;
    dedupList.hidden = true;
    setDedupBusy(false);
    dedupMsg.className = errors || stopped ? 'form-msg error' : 'form-msg ok';
    dedupMsg.textContent = `Removed: ${purged}, skipped: ${skipped}, errors: ${errors}; ` +
      `${formatBytes(bytes)} observed bytes removed.` + (stopped ? ` Stopped: ${stopped}.` : '') +
      ' Preview again to refresh the list.';
  }
}
document.getElementById('scan-dedup-btn').addEventListener('click', scanForDedupCopies);
document.getElementById('dedup-purge-btn').addEventListener('click', purgeDedupCopies);
document.getElementById('dedup-select-all-btn').addEventListener('click', () => {
  dedupList.querySelectorAll('input').forEach(input => { input.checked = true; });
});
document.getElementById('dedup-clear-btn').addEventListener('click', () => {
  dedupList.querySelectorAll('input').forEach(input => { input.checked = false; });
});
