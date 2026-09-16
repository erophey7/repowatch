// --- request charts (our own SVG bars, no third-party libraries) ---
function truncateLabel(s, maxLen = 26) {
  return s.length <= maxLen ? s : '…' + s.slice(-(maxLen - 1));
}

function renderBarChart(container, rows) {
  if (!rows.length) {
    container.innerHTML = '<div class="empty">no data</div>';
    return;
  }
  const maxCount = Math.max(...rows.map(r => r.count));
  const barHeight = 16;
  const gap = 6;
  const labelWidth = 120;
  const barAreaWidth = 140;
  const width = labelWidth + barAreaWidth + 36;
  const height = rows.length * (barHeight + gap);

  const bars = rows.map((r, i) => {
    const y = i * (barHeight + gap);
    const w = Math.max(2, (r.count / maxCount) * barAreaWidth);
    const label = truncateLabel(r.key === null || r.key === undefined ? '(unmatched)' : String(r.key));
    return `
      <text x="${labelWidth - 6}" y="${y + barHeight - 4}" text-anchor="end" class="chart-label">${esc(label)}</text>
      <rect x="${labelWidth}" y="${y}" width="${w}" height="${barHeight - 2}" class="chart-bar" rx="2"></rect>
      <text x="${labelWidth + w + 6}" y="${y + barHeight - 4}" class="chart-count">${r.count}</text>
    `;
  }).join('');

  container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}">${bars}</svg>`;
}

// Horizontal bars sized by a ratio (numKey/denKey), e.g. cache hit ratio or
// prefetch efficiency — same layout as renderBarChart, but the bar length
// and the trailing label both come from a fraction, not a raw count.
function renderRatioChart(container, rows, labelKey, numKey, denKey) {
  rows = rows.filter(r => r[denKey] > 0);
  if (!rows.length) {
    container.innerHTML = '<div class="empty">no data</div>';
    return;
  }
  const barHeight = 16, gap = 6, labelWidth = 120, barAreaWidth = 140;
  const width = labelWidth + barAreaWidth + 80;
  const height = rows.length * (barHeight + gap);

  const bars = rows.map((r, i) => {
    const y = i * (barHeight + gap);
    const ratio = r[numKey] / r[denKey];
    const w = Math.max(2, ratio * barAreaWidth);
    const label = truncateLabel(r[labelKey] === null || r[labelKey] === undefined ? '(unmatched)' : String(r[labelKey]));
    const pct = Math.round(ratio * 100);
    return `
      <text x="${labelWidth - 6}" y="${y + barHeight - 4}" text-anchor="end" class="chart-label">${esc(label)}</text>
      <rect x="${labelWidth}" y="${y}" width="${w}" height="${barHeight - 2}" class="chart-bar" rx="2"></rect>
      <text x="${labelWidth + barAreaWidth + 6}" y="${y + barHeight - 4}" class="chart-count">${pct}% (${r[numKey]}/${r[denKey]})</text>
    `;
  }).join('');

  container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}">${bars}</svg>`;
}

// Vertical stacked bars, one per hourly bucket: full bar height is
// row.total, the accent-colored overlay on top of it is row.hits — so the
// HIT share within each hour is visible at a glance, not just the volume.
function renderTimelineChart(container, rows) {
  if (!rows.length) {
    container.innerHTML = '<div class="empty">no data</div>';
    return;
  }
  const maxTotal = Math.max(...rows.map(r => r.total), 1);
  const barWidth = 12, gap = 4, chartHeight = 90, topPad = 4;
  const width = rows.length * (barWidth + gap) + gap;
  const height = chartHeight + topPad + 14;

  const bars = rows.map((r, i) => {
    const x = gap + i * (barWidth + gap);
    const totalH = Math.max(1, (r.total / maxTotal) * chartHeight);
    const hitH = r.total ? (r.hits / r.total) * totalH : 0;
    const yTotal = topPad + (chartHeight - totalH);
    const yHit = topPad + (chartHeight - hitH);
    const hourLabel = r.hour.slice(-2);
    return `
      <rect x="${x}" y="${yTotal}" width="${barWidth}" height="${totalH}" class="chart-bar-total" rx="1"></rect>
      <rect x="${x}" y="${yHit}" width="${barWidth}" height="${hitH}" class="chart-bar" rx="1"></rect>
      <title>${esc(r.hour)}: ${r.total} requests, ${r.hits} cache HIT</title>
      ${i % 3 === 0 ? `<text x="${x}" y="${height - 2}" class="chart-axis-label">${esc(hourLabel)}h</text>` : ''}
    `;
  }).join('');

  container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}">${bars}</svg>`;
}

async function loadRequestsSummary() {
  const repoId = $reqFilter.value;
  const qs = new URLSearchParams();
  if (repoId) qs.set('repo_id', repoId);

  const panels = {
    by_client_ip: document.querySelector('[data-chart="by_client_ip"]'),
    by_path: document.querySelector('[data-chart="by_path"]'),
    by_repo: document.querySelector('[data-chart="by_repo"]'),
    timeline: document.querySelector('[data-chart="timeline"]'),
    cache_hit_stats: document.querySelector('[data-chart="cache_hit_stats"]'),
  };

  try {
    const res = await fetch(`/api/requests/summary?${qs}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderBarChart(panels.by_client_ip, data.by_client_ip);
    renderBarChart(panels.by_path, data.by_path);
    renderBarChart(panels.by_repo, data.by_repo);
    renderTimelineChart(panels.timeline, data.timeline);
    renderRatioChart(panels.cache_hit_stats, data.cache_hit_stats, 'repo_id', 'hits', 'total');
  } catch (err) {
    for (const panel of Object.values(panels)) {
      panel.innerHTML = `<div class="empty">Load error: ${esc(err.message)}</div>`;
    }
  }
}

async function loadPrefetchEfficiency() {
  const panel = document.querySelector('[data-chart="prefetch_efficiency"]');
  try {
    const res = await fetch('/api/prefetch-efficiency');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderRatioChart(panel, data.items, 'repo_id', 'used', 'prefetched');
  } catch (err) {
    panel.innerHTML = `<div class="empty">Load error: ${esc(err.message)}</div>`;
  }
}
