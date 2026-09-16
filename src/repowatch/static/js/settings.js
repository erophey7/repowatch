// --- global settings form (safe fields, see api.SAFE_CONFIG_FIELDS) ---
const $settingsForm = document.getElementById('settings-form');
const $cancelSettingsBtn = document.getElementById('cancel-settings-form');
const $settingsMsg = document.getElementById('settings-msg');

const SETTINGS_FIELDS = {
  'check_interval': 's-check-interval',
  'event_retention_days': 's-event-retention',
  'request_retention_days': 's-request-retention',
  'warmed_retention_days': 's-warmed-retention',
  'event_max_rows_per_repo': 's-event-max-rows',
  'request_max_rows': 's-request-max-rows',
  'prefetch_concurrency': 's-prefetch-concurrency',
  'check_concurrency': 's-check-concurrency',
  'prefetch_bandwidth_limit': 's-prefetch-bandwidth-limit',
  'prefetch_bandwidth_timezone': 's-bandwidth-timezone',
  'cache_base_url': 's-cache-base-url',
  'public_cache_url': 's-public-cache-url',
  'notify_after_failures': 's-notify-after-failures',
  'key_expiry_warning_days': 's-key-expiry-warning-days',
};

function addBandwidthWindow(window = {}) {
  const row = document.createElement('div');
  row.className = 'bandwidth-window';
  const fields = [
    ['days', 'Days (mon,tue,wed,thu,fri,sat,sun)', (window.days || ['mon','tue','wed','thu','fri','sat','sun']).join(',')],
    ['start', 'Start (HH:MM)', window.start || '00:00'],
    ['end', 'End (HH:MM)', window.end || '24:00'],
    ['limit', 'Bytes/sec (empty = unlimited)', window.limit ?? ''],
  ];
  for (const [name, title, value] of fields) {
    const label = document.createElement('label');
    label.textContent = title + ' ';
    const input = document.createElement('input');
    input.type = 'text'; input.dataset.field = name; input.value = value; input.readOnly = !isLoggedIn;
    label.append(input); row.append(label);
  }
  const remove = document.createElement('button');
  remove.type = 'button'; remove.textContent = 'Remove window'; remove.hidden = !isLoggedIn;
  remove.addEventListener('click', () => row.remove());
  row.append(remove);
  document.getElementById('s-bandwidth-windows').append(row);
}
function showBandwidthSchedule(windows) {
  document.getElementById('s-bandwidth-windows').replaceChildren();
  for (const window of windows || []) addBandwidthWindow(window);
}
document.getElementById('s-bandwidth-add').addEventListener('click', () => addBandwidthWindow());

async function openSettingsForm() {
  $settingsMsg.textContent = 'Loading…';
  $settingsMsg.className = 'form-msg';
  $settingsForm.hidden = false;
  try {
    const res = await fetch('/api/config');
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    for (const [field, elId] of Object.entries(SETTINGS_FIELDS)) {
      document.getElementById(elId).value = data[field] ?? (field === 'prefetch_bandwidth_timezone' ? 'UTC' : '');
    }
    showBandwidthSchedule(data.prefetch_bandwidth_schedule);
    $settingsMsg.textContent = '';
  } catch (err) {
    $settingsMsg.textContent = `Load error: ${err.message}`;
    $settingsMsg.className = 'form-msg error';
  }
}

$toggleSettingsFormBtn.addEventListener('click', () => {
  if ($settingsForm.hidden) openSettingsForm(); else $settingsForm.hidden = true;
});
$cancelSettingsBtn.addEventListener('click', () => {
  $settingsForm.hidden = true;
  $settingsMsg.textContent = '';
});

$settingsForm.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  $settingsMsg.textContent = '';
  $settingsMsg.className = 'form-msg';

  const body = {};
  for (const [field, elId] of Object.entries(SETTINGS_FIELDS)) {
    body[field] = document.getElementById(elId).value.trim();
  }

  try {
    body.prefetch_bandwidth_schedule = [...document.querySelectorAll('.bandwidth-window')].map(row => {
      const value = name => row.querySelector(`[data-field="${name}"]`).value.trim();
      const rate = value('limit');
      if (rate && (!Number.isFinite(Number(rate)) || Number(rate) <= 0)) {
        throw new Error('Bandwidth limits must be positive numbers or empty');
      }
      return {days: value('days').split(',').map(s => s.trim()).filter(Boolean),
              start: value('start'), end: value('end'), limit: rate ? Number(rate) : null};
    });
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) {
      $settingsMsg.textContent = data.error || `HTTP ${res.status}`;
      $settingsMsg.className = 'form-msg error';
      return;
    }
    $settingsMsg.textContent = 'Saved';
    $settingsMsg.className = 'form-msg ok';
    for (const [field, elId] of Object.entries(SETTINGS_FIELDS)) {
      document.getElementById(elId).value = data[field] ?? (field === 'prefetch_bandwidth_timezone' ? 'UTC' : '');
    }
    showBandwidthSchedule(data.prefetch_bandwidth_schedule);
    loadRepos(); // check_interval may have changed — refresh the "interval" column
  } catch (err) {
    $settingsMsg.textContent = `Network error: ${err.message}`;
    $settingsMsg.className = 'form-msg error';
  }
});
