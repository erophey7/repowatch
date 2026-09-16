function renderWarmedList(items) {
  return items.length
    ? items.map(w => `
        <div>
          <label class="pick-row-inline"><input type="checkbox" value="${esc(w.package_key)}">
            ${w.status === 'ok' ? '✓' : '✗'} ${esc(w.package_key)} <span class="dim">· ${fmtTs(w.warmed_at)}</span></label>
        </div>`).join('')
    : '<div class="empty">nothing warmed yet</div>';
}

// One bounded page at a time. Generation counters discard stale search responses.
function pageLoader(container, url, render, afterRender = () => {}) {
  let generation = 0, items = [], cursor = null, query = '', busy = false;
  const more = document.createElement('button');
  more.textContent = 'Show more';
  more.hidden = true;
  container.after(more);
  async function load(reset = true, q = query) {
    if (!reset && busy) return;
    if (reset) { generation++; query = q; cursor = null; items = []; }
    const current = generation;
    busy = true; more.disabled = true;
    const params = new URLSearchParams({limit: '100', q: query});
    if (cursor) params.set('cursor', cursor);
    try {
      const res = await fetch(`${url}?${params}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const page = await res.json();
      if (current !== generation) return;
      items.push(...page.items);
      cursor = page.next_cursor;
      container.innerHTML = render(items);
      afterRender();
      more.hidden = !cursor;
    } catch (err) {
      if (current === generation) {
        container.textContent = `Load error: ${err.message}`;
        more.hidden = false; // allows retry
      }
    } finally {
      if (current === generation) { busy = false; more.disabled = false; }
    }
  }
  more.onclick = () => load(false);
  return load;
}

// Shared "checkbox selection over a paginated/filtered list" behavior for
// the warm-queue picker and the warmed-packages list: tracks selection in a
// Map (value -> extra data, e.g. package_name for bans) that survives page
// re-renders, wires "select all matching the current filter" (walks every
// page via the list's own paginated endpoint, not just what's on screen —
// selectedKeys is what bulk actions actually submit, independent of which
// checkboxes happen to be rendered) and "clear selection". Returns the Map
// so callers can read it for their own bulk-action buttons.
function wireSelection(container, {fetchUrl, getQuery, selectAllBtn, clearBtn, msgEl, extraOf = () => null}) {
  const selected = new Map();
  container.addEventListener('change', ev => {
    if (ev.target.type !== 'checkbox') return;
    if (ev.target.checked) selected.set(ev.target.value, ev.target.dataset.extra ?? null);
    else selected.delete(ev.target.value);
  });
  const reflectChecked = () => container.querySelectorAll('input').forEach(cb => { cb.checked = selected.has(cb.value); });
  const SELECT_ALL_CAP = 5000;
  selectAllBtn.addEventListener('click', async () => {
    const q = getQuery();
    selectAllBtn.disabled = true;
    let count = 0, cursor = null;
    try {
      do {
        const params = new URLSearchParams({ limit: '200', q });
        if (cursor) params.set('cursor', cursor);
        const res = await fetch(`${fetchUrl}?${params}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const page = await res.json();
        for (const item of page.items) selected.set(item.package_key, extraOf(item));
        count += page.items.length;
        cursor = page.next_cursor;
        selectAllBtn.textContent = `Selecting… (${count})`;
      } while (cursor && count < SELECT_ALL_CAP);
      reflectChecked();
      if (msgEl) {
        msgEl.className = 'form-msg ok';
        msgEl.textContent = cursor
          ? `Selected the first ${count} matching (stopped at the ${SELECT_ALL_CAP} safety limit — narrow the filter to select the rest).`
          : `Selected ${count} matching item(s).`;
      }
    } catch (err) {
      if (msgEl) { msgEl.className = 'form-msg error'; msgEl.textContent = `Selection error: ${err.message}`; }
    } finally {
      selectAllBtn.disabled = false;
      selectAllBtn.textContent = 'Select all matching';
    }
  });
  clearBtn.addEventListener('click', () => {
    selected.clear();
    reflectChecked();
    if (msgEl) { msgEl.className = 'form-msg'; msgEl.textContent = ''; }
  });
  return { selected, reflectChecked };
}

function renderBannedList(names) {
  return names.length
    ? names.map(n => `
        <div>
          <label class="pick-row-inline mono"><input type="checkbox" value="${esc(n)}"> ${esc(n)}</label>
        </div>`).join('')
    : '<div class="empty">no bans</div>';
}

function renderPicker(container, keys, filterText) {
  const q = filterText.trim().toLowerCase();
  const filtered = q ? keys.filter(k => k.toLowerCase().includes(q)) : keys;
  const LIMIT = 200;
  const shown = filtered.slice(0, LIMIT);
  container.innerHTML = shown.length
    ? shown.map(k => `<label class="pick-row"><input type="checkbox" value="${esc(k)}"> ${esc(k)}</label>`).join('')
      + (filtered.length > LIMIT ? `<div class="empty">showing the first ${LIMIT} of ${filtered.length}</div>` : '')
    : '<div class="empty">nothing found</div>';
}

async function toggleDetail(tr, repoId) {
  const repo = lastRepos.find(item => item.id === repoId) || {};
  const existing = tr.nextElementSibling;
  if (existing && existing.classList.contains('detail-row')) {
    existing.remove();
    return;
  }
  document.querySelectorAll('.detail-row').forEach(el => el.remove());

  const detailRow = document.createElement('tr');
  detailRow.className = 'detail-row';
  detailRow.dataset.repoId = repoId;
  detailRow.dataset.auth = String(isLoggedIn);
  const td = document.createElement('td');
  td.colSpan = tr.closest('table').querySelectorAll('thead th').length;
  td.innerHTML = `<div class="detail-panel">
    <div class="detail-col">
      <h3>Warmed packages</h3>
      <input type="text" class="pkg-search" data-slot="warmed-search" placeholder="filter by package name or filename…">
      <div class="scroll-list" data-slot="warmed">Loading…</div>
      <div class="bulk-actions">
        <button type="button" data-slot="warmed-remove-btn">Remove selected</button>
        <button type="button" data-slot="warmed-select-all-btn">Select all matching</button>
        <button type="button" data-slot="warmed-clear-btn">Clear selection</button>
      </div>
      <div class="form-msg" data-slot="warmed-msg"></div>

      <div class="purge-section">
        <div class="subsection-title">Cache purge (stale warmed entries)</div>
        <p class="field-hint">Warmed entries whose package no longer exists in the current
          index — real candidates for cleanup, but nginx is only asked when you purge, not
          before (see docs_dev/ROADMAP.md for why a live pre-check risks the cache itself).</p>
        <button type="button" data-slot="purge-scan-btn">Scan for stale entries</button>
        <div class="scroll-list checklist" data-slot="purge-list" hidden></div>
        <div class="bulk-actions" data-slot="purge-actions" hidden>
          <button type="button" data-slot="purge-selected-btn">Purge selected</button>
          <button type="button" data-slot="purge-select-all-btn">Select all</button>
          <button type="button" data-slot="purge-clear-btn">Clear selection</button>
        </div>
        <div class="form-msg" data-slot="purge-msg"></div>
      </div>
    </div>
    <div class="detail-col">
      <h3>${isLoggedIn ? 'Add to warm queue' : 'Packages'}</h3>
      <input type="text" class="pkg-search" data-slot="search" placeholder="filter by package name…">
      <div class="scroll-list checklist" data-slot="picker">Loading…</div>
      <div class="warm-actions">
        <button data-slot="warm-btn">Warm</button>
        <button type="button" data-slot="select-all-btn">Select all matching</button>
        <button type="button" data-slot="clear-selection-btn">Clear selection</button>
      </div>
      <div class="bulk-actions">
        <button type="button" data-slot="ban-selected-btn">Ban selected (by name)</button>
      </div>
      <div class="form-msg" data-slot="warm-msg"></div>

      <div class="subsection-title">Banned from auto-warm (by package name)</div>
      <div class="scroll-list" data-slot="bans">Loading…</div>
      <div class="ban-form">
        <input type="text" data-slot="ban-name" placeholder="package name(s), comma-separated, e.g. linux-headers, bash">
        <button data-slot="ban-btn">Ban</button>
      </div>
      <div class="bulk-actions">
        <button type="button" data-slot="bans-select-all-btn">Select all</button>
        <button type="button" data-slot="bans-clear-btn">Clear selection</button>
        <button type="button" data-slot="unban-selected-btn">Unban selected</button>
      </div>
      <div class="form-msg" data-slot="ban-msg"></div>
    </div>
    <div class="detail-col">
      <h3>Change history</h3>
      ${repo.pending_replacements ? `<p class="form-msg error">${repo.pending_replacements} replacement(s) awaiting cache refresh.</p>` : ''}
      <div class="scroll-list" data-slot="history">Loading…</div>
    </div>
  </div>`;
  detailRow.appendChild(td);
  tr.after(detailRow);

  const warmedSlot = td.querySelector('[data-slot="warmed"]');
  const warmedSearchInput = td.querySelector('[data-slot="warmed-search"]');
  const warmedRemoveBtn = td.querySelector('[data-slot="warmed-remove-btn"]');
  const warmedSelectAllBtn = td.querySelector('[data-slot="warmed-select-all-btn"]');
  const warmedClearBtn = td.querySelector('[data-slot="warmed-clear-btn"]');
  const warmedMsg = td.querySelector('[data-slot="warmed-msg"]');
  const pickerSlot = td.querySelector('[data-slot="picker"]');
  const searchInput = td.querySelector('[data-slot="search"]');
  const histSlot = td.querySelector('[data-slot="history"]');
  const warmBtn = td.querySelector('[data-slot="warm-btn"]');
  const selectAllBtn = td.querySelector('[data-slot="select-all-btn"]');
  const clearSelectionBtn = td.querySelector('[data-slot="clear-selection-btn"]');
  const banSelectedBtn = td.querySelector('[data-slot="ban-selected-btn"]');
  const warmMsg = td.querySelector('[data-slot="warm-msg"]');
  const bansSlot = td.querySelector('[data-slot="bans"]');
  const banNameInput = td.querySelector('[data-slot="ban-name"]');
  const banBtn = td.querySelector('[data-slot="ban-btn"]');
  const bansSelectAllBtn = td.querySelector('[data-slot="bans-select-all-btn"]');
  const bansClearBtn = td.querySelector('[data-slot="bans-clear-btn"]');
  const unbanSelectedBtn = td.querySelector('[data-slot="unban-selected-btn"]');
  const banMsg = td.querySelector('[data-slot="ban-msg"]');

  const loadWarmed = pageLoader(warmedSlot,
    `/api/repos/${encodeURIComponent(repoId)}/warmed`, renderWarmedList,
    () => warmedSlot.querySelectorAll('input').forEach(cb => { cb.checked = warmedSelection.selected.has(cb.value); }));
  let warmedSearchTimer;
  warmedSearchInput.addEventListener('input', () => {
    clearTimeout(warmedSearchTimer);
    warmedSearchTimer = setTimeout(() => loadWarmed(true, warmedSearchInput.value.trim()), 250);
  });
  const warmedSelection = wireSelection(warmedSlot, {
    fetchUrl: `/api/repos/${encodeURIComponent(repoId)}/warmed`,
    getQuery: () => warmedSearchInput.value.trim(),
    selectAllBtn: warmedSelectAllBtn, clearBtn: warmedClearBtn, msgEl: warmedMsg,
  });
  warmedRemoveBtn.addEventListener('click', async () => {
    const keys = [...warmedSelection.selected.keys()];
    if (!keys.length) {
      warmedMsg.className = 'form-msg error';
      warmedMsg.textContent = 'Nothing selected';
      return;
    }
    try {
      const res = await fetch(`/api/repos/${encodeURIComponent(repoId)}/warmed/remove`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ package_keys: keys }),
      });
      const data = await res.json();
      if (!res.ok) {
        warmedMsg.className = 'form-msg error';
        warmedMsg.textContent = data.error || `HTTP ${res.status}`;
        return;
      }
      warmedSelection.selected.clear();
      warmedMsg.className = 'form-msg ok';
      if (data.purge_results) {
        const outcomes = Object.values(data.purge_results);
        const purged = outcomes.filter(o => o === 'purged').length;
        const notCached = outcomes.filter(o => o === 'not_cached').length;
        const errors = outcomes.length - purged - notCached;
        warmedMsg.textContent = `Removed: ${data.removed} (purged: ${purged}, already gone: ${notCached}` +
          (errors ? `, errors: ${errors}` : '') + ')';
      } else {
        warmedMsg.textContent = `Removed: ${data.removed}`;
      }
      await loadWarmed();
    } catch (err) {
      warmedMsg.className = 'form-msg error';
      warmedMsg.textContent = `Network error: ${err.message}`;
    }
  });

  // Manual cache purge — step 1 (Scan) is DB-only, no nginx/network call;
  // step 2 (Purge selected) is the only point where nginx is actually
  // asked, and its response (200/404 per key) IS the "does this still
  // exist" answer — see prefetch.purge_selected for why there's no
  // separate non-destructive pre-check.
  const purgeScanBtn = td.querySelector('[data-slot="purge-scan-btn"]');
  const purgeListSlot = td.querySelector('[data-slot="purge-list"]');
  const purgeActionsSlot = td.querySelector('[data-slot="purge-actions"]');
  const purgeSelectedBtn = td.querySelector('[data-slot="purge-selected-btn"]');
  const purgeSelectAllBtn = td.querySelector('[data-slot="purge-select-all-btn"]');
  const purgeClearBtn = td.querySelector('[data-slot="purge-clear-btn"]');
  const purgeMsg = td.querySelector('[data-slot="purge-msg"]');

  // silent=true (used right after a purge) updates the list/checkboxes
  // without touching purgeMsg — otherwise the re-scan's own "N candidates
  // found" immediately overwrote the "Purged: X, not cached: Y" summary
  // the operator actually clicked the button to see (a real bug caught
  // while testing this feature, not just a hypothetical).
  async function scanForStaleEntries(silent = false) {
    if (!silent) {
      purgeMsg.className = 'form-msg';
      purgeMsg.textContent = 'Scanning…';
    }
    purgeActionsSlot.hidden = true;
    try {
      const res = await fetch(`/api/repos/${encodeURIComponent(repoId)}/purge-candidates`);
      const data = await res.json();
      if (!res.ok) {
        purgeListSlot.hidden = true;
        if (!silent) { purgeMsg.className = 'form-msg error'; purgeMsg.textContent = data.error || `HTTP ${res.status}`; }
        return;
      }
      if (!data.enable_purge) {
        purgeListSlot.hidden = true;
        if (!silent) {
          purgeMsg.className = 'form-msg error';
          purgeMsg.textContent = 'nginx.enable_purge is not enabled in config.yaml — nothing to purge.';
        }
        return;
      }
      purgeListSlot.hidden = false;
      if (!data.candidates.length) {
        purgeListSlot.innerHTML = '<div class="empty">no stale entries found</div>';
        if (!silent) purgeMsg.textContent = '';
        return;
      }
      // All checked by default — the operator opts OUT of ones they don't want purged.
      purgeListSlot.innerHTML = data.candidates.map(c =>
        `<label class="pick-row"><input type="checkbox" value="${esc(c.package_key)}" checked> ` +
        `${esc(c.package_key)} <span class="dim">(${esc(c.filename)})</span></label>`).join('');
      purgeActionsSlot.hidden = false;
      if (!silent) purgeMsg.textContent = `${data.candidates.length} candidate(s) found.`;
    } catch (err) {
      if (!silent) { purgeMsg.className = 'form-msg error'; purgeMsg.textContent = `Network error: ${err.message}`; }
    }
  }
  purgeScanBtn.addEventListener('click', () => scanForStaleEntries());
  purgeSelectAllBtn.addEventListener('click', () => {
    purgeListSlot.querySelectorAll('input').forEach(cb => { cb.checked = true; });
  });
  purgeClearBtn.addEventListener('click', () => {
    purgeListSlot.querySelectorAll('input').forEach(cb => { cb.checked = false; });
  });
  purgeSelectedBtn.addEventListener('click', async () => {
    const keys = [...purgeListSlot.querySelectorAll('input:checked')].map(cb => cb.value);
    if (!keys.length) {
      purgeMsg.className = 'form-msg error';
      purgeMsg.textContent = 'Nothing selected';
      return;
    }
    purgeMsg.className = 'form-msg';
    purgeMsg.textContent = 'Purging…';
    try {
      const res = await fetch(`/api/repos/${encodeURIComponent(repoId)}/purge`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ package_keys: keys }),
      });
      const data = await res.json();
      if (!res.ok) {
        purgeMsg.className = 'form-msg error';
        purgeMsg.textContent = data.error || `HTTP ${res.status}`;
        return;
      }
      const counts = { purged: 0, not_cached: 0, retained_shared: 0, error: 0 };
      for (const outcome of Object.values(data.results)) {
        counts[outcome.startsWith('error') ? 'error' : outcome] = (counts[outcome.startsWith('error') ? 'error' : outcome] || 0) + 1;
      }
      purgeMsg.className = counts.error ? 'form-msg error' : 'form-msg ok';
      purgeMsg.textContent = `Purged: ${counts.purged}, not cached: ${counts.not_cached}, shared artifacts retained: ${counts.retained_shared}` +
        (counts.error ? `, errors: ${counts.error}` : '');
      await scanForStaleEntries(true); // silent re-scan: keep the "Purged: ..." summary on screen
      await loadWarmed(); // reflect the same cleanup in the warmed list above
    } catch (err) {
      purgeMsg.className = 'form-msg error';
      purgeMsg.textContent = `Network error: ${err.message}`;
    }
  });

  const loadPackages = pageLoader(pickerSlot,
    `/api/repos/${encodeURIComponent(repoId)}/packages`,
    items => items.length ? items.map(p =>
      `<label class="pick-row"><input type="checkbox" value="${esc(p.package_key)}" data-extra="${esc(p.package_name || '')}"> ${esc(p.package_key)}</label>`).join('')
      : '<div class="empty">nothing found</div>',
    () => packageSelection.reflectChecked());
  let searchTimer;
  searchInput.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadPackages(true, searchInput.value.trim()), 250);
  });
  // "Select all matching" walks every page of the CURRENT filter (not just
  // the loaded page) via the same cursor the picker itself uses, so a
  // narrow search still selects packages beyond what's on screen. Remembers
  // each package's bare name too (extraOf), not just its key — "Ban
  // selected" needs names, and can't rely on the DOM having rendered every
  // selected item's checkbox (see wireSelection).
  const packageSelection = wireSelection(pickerSlot, {
    fetchUrl: `/api/repos/${encodeURIComponent(repoId)}/packages`,
    getQuery: () => searchInput.value.trim(),
    selectAllBtn, clearBtn: clearSelectionBtn, msgEl: warmMsg,
    extraOf: item => item.package_name || null,
  });
  banSelectedBtn.addEventListener('click', async () => {
    const names = [...new Set([...packageSelection.selected.values()].filter(Boolean))];
    if (!names.length) {
      warmMsg.className = 'form-msg error';
      warmMsg.textContent = 'Nothing selected (or selected packages have no known name)';
      return;
    }
    try {
      const res = await fetch(`/api/repos/${encodeURIComponent(repoId)}/bans`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ package_names: names }),
      });
      const data = await res.json();
      if (!res.ok) {
        warmMsg.className = 'form-msg error';
        warmMsg.textContent = data.error || `HTTP ${res.status}`;
        return;
      }
      warmMsg.className = 'form-msg ok';
      warmMsg.textContent = `Banned ${names.length} name(s)`;
      bansSlot.innerHTML = renderBannedList(data.banned);
    } catch (err) {
      warmMsg.className = 'form-msg error';
      warmMsg.textContent = `Network error: ${err.message}`;
    }
  });

  // Bans is a short, unpaginated list — "select all" just checks everything
  // currently rendered, no cursor-walking needed (unlike warmed/packages).
  const bansSelection = new Set();
  bansSlot.addEventListener('change', ev => {
    if (ev.target.type !== 'checkbox') return;
    if (ev.target.checked) bansSelection.add(ev.target.value);
    else bansSelection.delete(ev.target.value);
  });
  bansSelectAllBtn.addEventListener('click', () => {
    bansSlot.querySelectorAll('input').forEach(cb => { cb.checked = true; bansSelection.add(cb.value); });
  });
  bansClearBtn.addEventListener('click', () => {
    bansSelection.clear();
    bansSlot.querySelectorAll('input').forEach(cb => { cb.checked = false; });
  });
  unbanSelectedBtn.addEventListener('click', async () => {
    const names = [...bansSelection];
    if (!names.length) {
      banMsg.className = 'form-msg error';
      banMsg.textContent = 'Nothing selected';
      return;
    }
    try {
      const res = await fetch(`/api/repos/${encodeURIComponent(repoId)}/bans/remove`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ package_names: names }),
      });
      const data = await res.json();
      if (!res.ok) {
        banMsg.className = 'form-msg error';
        banMsg.textContent = data.error || `HTTP ${res.status}`;
        return;
      }
      bansSelection.clear();
      bansSlot.innerHTML = renderBannedList(data.banned);
    } catch (err) {
      banMsg.className = 'form-msg error';
      banMsg.textContent = `Network error: ${err.message}`;
    }
  });

  void loadWarmed();
  void loadPackages();
  const [histRes, bansRes] = await Promise.all([
    fetch(`/status/${encodeURIComponent(repoId)}/history?limit=15`),
    fetch(`/api/repos/${encodeURIComponent(repoId)}/bans`),
  ]);

  bansSlot.innerHTML = bansRes.ok
    ? renderBannedList(await bansRes.json())
    : '<div class="empty">failed to load</div>';

  if (histRes.ok) {
    const events = await histRes.json();
    histSlot.innerHTML = events.length
      ? events.map(e => `<div>${fmtTs(e.ts)} · +${e.new_packages.length} / -${e.removed_packages.length} / ~${(e.modified_packages || []).length} modified</div>`).join('')
      : '<div class="empty">no changes yet</div>';
  } else {
    histSlot.innerHTML = '<div class="empty">failed to load</div>';
  }

  warmBtn.addEventListener('click', async () => {
    const selected = [...packageSelection.selected.keys()];
    if (!selected.length) {
      warmMsg.textContent = 'Nothing selected';
      warmMsg.className = 'form-msg error';
      return;
    }
    warmMsg.textContent = 'Warming…';
    warmMsg.className = 'form-msg';
    try {
      const res = await fetch(`/api/repos/${encodeURIComponent(repoId)}/warm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ package_keys: selected }),
      });
      const data = await res.json();
      if (!res.ok) {
        warmMsg.textContent = data.error || `HTTP ${res.status}`;
        warmMsg.className = 'form-msg error';
        return;
      }
      warmMsg.textContent = `Warmed: ${data.warmed.length}` +
        (data.skipped?.length ? `, skipped: ${data.skipped.length}` : '') +
        (data.failed?.length ? `, failed: ${data.failed.length}` : '') +
        (data.not_found.length ? `, not found: ${data.not_found.length}` : '');
      warmMsg.className = data.failed?.length ? 'form-msg error' : 'form-msg ok';
      await loadWarmed();
    } catch (err) {
      warmMsg.textContent = `Network error: ${err.message}`;
      warmMsg.className = 'form-msg error';
    }
  });

  banBtn.addEventListener('click', async () => {
    // Comma-separated — a second way to ban several names at once, besides
    // selecting packages in the queue above and using "Ban selected".
    const names = banNameInput.value.split(',').map(n => n.trim()).filter(Boolean);
    if (!names.length) {
      banMsg.textContent = 'Enter a package name';
      banMsg.className = 'form-msg error';
      return;
    }
    banMsg.textContent = '';
    try {
      const res = await fetch(`/api/repos/${encodeURIComponent(repoId)}/bans`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ package_names: names }),
      });
      const data = await res.json();
      if (!res.ok) {
        banMsg.textContent = data.error || `HTTP ${res.status}`;
        banMsg.className = 'form-msg error';
        return;
      }
      banNameInput.value = '';
      bansSlot.innerHTML = renderBannedList(data.banned);
    } catch (err) {
      banMsg.textContent = `Network error: ${err.message}`;
      banMsg.className = 'form-msg error';
    }
  });
}
