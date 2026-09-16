// --- add/edit repository form (shared — switched by the editingRepoId
// field: null means "adding", otherwise the id of the repository currently
// being edited, and the form posts to /api/repos/<id> instead of
// /api/repos) ---
const $addForm = document.getElementById('add-form');
const $addFormTitle = document.getElementById('add-form-title');
const $addFormSubmit = document.getElementById('add-form-submit');
const $toggleBtn = document.getElementById('toggle-add-form');
const $cancelBtn = document.getElementById('cancel-add-form');
const $typeSelect = document.getElementById('f-type');
const $idInput = document.getElementById('f-id');
const $verifySignature = document.getElementById('f-verify-signature');
const $formMsg = document.getElementById('form-msg');
let editingRepoId = null;

function resetAddFormMode() {
  editingRepoId = null;
  $idInput.disabled = false;
  $addFormTitle.textContent = 'New repository';
  $addFormSubmit.textContent = 'Add';
}

$toggleBtn.addEventListener('click', () => {
  resetAddFormMode();
  $addForm.reset();
  updateConditionalFields();
  $addForm.hidden = !$addForm.hidden;
});
$cancelBtn.addEventListener('click', () => {
  $addForm.hidden = true;
  $addForm.reset();
  resetAddFormMode();
  $formMsg.textContent = '';
});

function openEditForm(repo) {
  const cfg = repo.config || {};
  resetAddFormMode();
  editingRepoId = repo.id;
  $addFormTitle.textContent = `Edit repository: ${repo.id}`;
  $addFormSubmit.textContent = 'Save';

  $idInput.value = cfg.id ?? repo.id;
  $idInput.disabled = true; // id can't be changed via edit, see api.update_repo_payload
  $typeSelect.value = cfg.type ?? repo.type;
  document.getElementById('f-upstream').value = cfg.upstream ?? repo.upstream;
  document.getElementById('f-arch').value = cfg.arch ?? '';
  document.getElementById('f-group').value = cfg.group ?? '';
  document.getElementById('f-url-template').value = cfg.url_template ?? '';
  document.getElementById('f-url-variables').value = Object.keys(cfg.url_variables || {}).length ? JSON.stringify(cfg.url_variables) : '';
  document.getElementById('f-nix-source').value = cfg.nix_source ?? '';
  document.getElementById('f-nix-attributes').value = (cfg.nix_attributes || []).join(', ');
  document.getElementById('f-nix-keys').value = (cfg.nix_public_keys || []).join(' ');
  document.getElementById('f-nix-timeout').value = cfg.nix_timeout ?? 600;
  document.getElementById('f-nix-max-paths').value = cfg.nix_max_paths ?? 500000;
  document.getElementById('f-repo-name').value = cfg.repo_name ?? '';
  document.getElementById('f-distribution').value = cfg.distribution ?? '';
  document.getElementById('f-component').value = cfg.component ?? '';
  document.getElementById('f-prefetch').checked = cfg.prefetch ?? repo.prefetch;
  document.getElementById('f-prefetch-whitelist').value = (cfg.prefetch_whitelist || []).join('\n');
  document.getElementById('f-prefetch-blacklist').value = (cfg.prefetch_blacklist || []).join('\n');
  $verifySignature.checked = cfg.verify_signature ?? false;
  document.getElementById('f-keyring-path').value = cfg.keyring_path ?? '';
  document.getElementById('f-apk-keys').value = cfg.apk_keys_dir ?? '';
  document.getElementById('f-apk-backend').value = cfg.apk_signature_backend ?? 'openssl';
  document.getElementById('f-check-interval').value = cfg.check_interval ?? '';
  document.getElementById('f-prefetch-bandwidth-limit').value = cfg.prefetch_bandwidth_limit ?? '';

  updateConditionalFields();
  $formMsg.textContent = '';
  $addForm.hidden = false;
  $addForm.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function deleteRepo(repoId) {
  if (!confirm(`Delete repository "${repoId}" from config.yaml? Its stored history in sqlite is not removed.`)) {
    return;
  }
  try {
    const res = await fetch(`/api/repos/${encodeURIComponent(repoId)}/delete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    const data = await res.json();
    if (!res.ok) {
      alert(data.error || `HTTP ${res.status}`);
      return;
    }
    if (editingRepoId === repoId) {
      $addForm.hidden = true;
      resetAddFormMode();
    }
    loadRepos();
  } catch (err) {
    alert(`Network error: ${err.message}`);
  }
}

function updateConditionalFields() {
  const type = $typeSelect.value;
  document.querySelectorAll('#add-form [data-for]').forEach(el => {
    el.hidden = !el.dataset.for.split(' ').includes(type);
  });
  const verifyField = document.getElementById('f-verify-signature-field');
  const keyringField = document.getElementById('f-keyring-path-field');
  // XBPS and Gentoo use package-level signatures; the server rejects
  // verify_signature=true outright (see config.py) — so the checkbox isn't
  // just hidden but force-unchecked, otherwise a stale "checked" state
  // from a previously selected type would submit a doomed request.
  verifyField.hidden = type === 'xbps' || type === 'gentoo';
  if (type === 'xbps' || type === 'gentoo') $verifySignature.checked = false;
  keyringField.hidden = type === 'apk' || type === 'xbps' || type === 'nix' || !$verifySignature.checked;
}
$typeSelect.addEventListener('change', updateConditionalFields);
$verifySignature.addEventListener('change', updateConditionalFields);
updateConditionalFields();

$addForm.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  $formMsg.textContent = '';
  $formMsg.className = 'form-msg';

  const type = $typeSelect.value;
  const body = {
    id: $idInput.value.trim(),
    type,
    upstream: document.getElementById('f-upstream').value.trim(),
    url_template: document.getElementById('f-url-template').value.trim() || null,
    arch: document.getElementById('f-arch').value.trim(),
    prefetch: document.getElementById('f-prefetch').checked,
    prefetch_whitelist: document.getElementById('f-prefetch-whitelist').value.split(/\r?\n/).map(s => s.trim()).filter(Boolean),
    prefetch_blacklist: document.getElementById('f-prefetch-blacklist').value.split(/\r?\n/).map(s => s.trim()).filter(Boolean),
  };
  if (type === 'pacman') {
    body.repo_name = document.getElementById('f-repo-name').value.trim();
  } else if (type === 'apt') {
    body.distribution = document.getElementById('f-distribution').value.trim();
    body.component = document.getElementById('f-component').value.trim() || null;
  }

  if (type === 'nix') {
    body.nix_source = document.getElementById('f-nix-source').value.trim();
    body.nix_attributes = document.getElementById('f-nix-attributes').value.split(',').map(s => s.trim()).filter(Boolean);
    body.nix_public_keys = document.getElementById('f-nix-keys').value.trim().split(/\s+/).filter(Boolean);
    body.nix_timeout = Number(document.getElementById('f-nix-timeout').value);
    body.nix_max_paths = Number(document.getElementById('f-nix-max-paths').value);
  }

  if (type === 'apt-rpm' || type === 'slackware') body.component = document.getElementById('f-component').value.trim() || null;

  const checkInterval = document.getElementById('f-check-interval').value.trim();
  if (checkInterval) body.check_interval = Number(checkInterval);

  const bandwidthLimit = document.getElementById('f-prefetch-bandwidth-limit').value.trim();
  if (bandwidthLimit) body.prefetch_bandwidth_limit = Number(bandwidthLimit);

  const group = document.getElementById('f-group').value.trim();
  if (group) body.group = group;

  if ($verifySignature.checked) {
    body.verify_signature = true;
    if (type === 'apk') {
      body.apk_keys_dir = document.getElementById('f-apk-keys').value.trim();
      body.apk_signature_backend = document.getElementById('f-apk-backend').value;
    } else if (type !== 'nix') body.keyring_path = document.getElementById('f-keyring-path').value.trim();
  }

  const url = editingRepoId ? `/api/repos/${encodeURIComponent(editingRepoId)}` : '/api/repos';
  try {
    body.url_variables = JSON.parse(document.getElementById('f-url-variables').value.trim() || '{}');
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) {
      $formMsg.textContent = data.error || `HTTP ${res.status}`;
      $formMsg.className = 'form-msg error';
      return;
    }
    $formMsg.textContent = editingRepoId ? `Saved: ${data.id}` : `Added: ${data.id}`;
    $formMsg.className = 'form-msg ok';
    $addForm.reset();
    resetAddFormMode();
    updateConditionalFields();
    loadRepos();
  } catch (err) {
    $formMsg.textContent = `Network error: ${err.message}`;
    $formMsg.className = 'form-msg error';
  }
});
