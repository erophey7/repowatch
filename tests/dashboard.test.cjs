// NODE_PATH=/tmp/repowatch-ui-check/node_modules node --test tests/dashboard.test.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const html = fs.readFileSync(require('node:path').join(__dirname, '../src/repowatch/static/dashboard.html'), 'utf8');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

test('dashboard searches, appends pages, keeps selection and ignores stale responses', async () => {
  const calls = [];
  const repo = {id: 'r', type: 'apt', upstream: 'example', config: {}, package_count: 1000};
  const dom = new JSDOM(html, {
    url: 'http://localhost/', runScripts: 'dangerously',
    beforeParse(w) {
      w.fetch = async raw => {
        const url = new URL(raw, 'http://localhost');
        calls.push(url);
        let data = [];
        if (url.pathname === '/api/auth/session') data = {role:'admin', csrf_token:'csrf'};
        else if (url.pathname === '/api/repos') data = [repo];
        else if (url.pathname.endsWith('/summary')) data = {by_client_ip: [], by_path: [], by_repo: [], timeline: [], cache_hit_stats: []};
        else if (url.pathname.endsWith('/packages')) {
          const q = url.searchParams.get('q');
          if (q === 'slow') await delay(450);
          data = {items: [{package_key: q || (url.searchParams.has('cursor') ? 'second' : 'first')}],
                  next_cursor: q || url.searchParams.has('cursor') ? null : 'next'};
        } else if (url.pathname.endsWith('/warmed')) {
          data = {items: [{package_key: url.searchParams.has('cursor') ? 'warm2' : 'warm1', status: 'ok'}],
                  next_cursor: url.searchParams.has('cursor') ? null : 'next'};
        } else if (url.pathname === '/api/requests') {
          data = {items: [{path: url.searchParams.has('cursor') ? '/second' : '/first'}],
                  next_cursor: url.searchParams.has('cursor') ? null : 'next'};
        }
        return {ok: true, json: async () => data};
      };
    }
  });
  try {
    const w = dom.window, doc = w.document;
    await delay(30);
    doc.querySelector('.repo-row').click();
    await delay(30);
    const picker = doc.querySelector('[data-slot="picker"]');
    const checkbox = picker.querySelector('input');
    checkbox.checked = true;
    checkbox.dispatchEvent(new w.Event('change', {bubbles: true}));
    picker.nextElementSibling.click();
    await delay(20);
    assert.equal(picker.querySelectorAll('input').length, 2);
    assert.equal(picker.querySelector('input').checked, true);
    const warmed = doc.querySelector('[data-slot="warmed"]');
    warmed.nextElementSibling.click();
    await delay(20);
    assert.match(warmed.textContent, /warm1/);
    assert.match(warmed.textContent, /warm2/);
    const search = doc.querySelector('[data-slot="search"]');
    search.value = 'slow';
    search.dispatchEvent(new w.Event('input'));
    await delay(280);
    search.value = 'fast';
    search.dispatchEvent(new w.Event('input'));
    await delay(550);
    assert.equal(picker.querySelector('input').value, 'fast');
    search.value = '';
    search.dispatchEvent(new w.Event('input'));
    await delay(280);
    assert.equal(picker.querySelector('input').checked, true);
    await w.loadRepos();
    assert.equal(doc.querySelector('[data-slot="picker"]'), picker);
    const requestMore = doc.querySelector('#requests-body').closest('table').nextElementSibling;
    requestMore.click();
    await delay(20);
    assert.equal(doc.querySelectorAll('#requests-body tr').length, 2);
    assert(calls.filter(u => u.pathname.endsWith('/packages')).every(u => u.searchParams.has('limit')));
  } finally {
    dom.window.close();
  }
});

test('dashboard filters warmed packages by search and selects all matching packages across pages', async () => {
  const calls = [];
  const repo = {id: 'r', type: 'apt', upstream: 'example', config: {}, package_count: 1000};
  const dom = new JSDOM(html, {
    url: 'http://localhost/', runScripts: 'dangerously',
    beforeParse(w) {
      w.fetch = async (raw, options = {}) => {
        const url = new URL(raw, 'http://localhost');
        calls.push({url, options});
        let data = [];
        if (url.pathname === '/api/auth/session') data = {role: 'admin', csrf_token: 'csrf'};
        else if (url.pathname === '/api/repos') data = [repo];
        else if (url.pathname.endsWith('/summary')) data = {by_client_ip: [], by_path: [], by_repo: [], timeline: [], cache_hit_stats: []};
        else if (url.pathname.endsWith('/warm') && options.method === 'POST') {
          data = {warmed: ['pkg-a-1'], skipped: ['pkg-a-2'], failed: ['pkg-a-3'], not_found: []};
        } else if (url.pathname.endsWith('/packages')) {
          const q = url.searchParams.get('q');
          const cursor = url.searchParams.get('cursor');
          if (q === 'pkg-a') {
            // Two pages: [pkg-a-1, pkg-a-2] then [pkg-a-3] — select-all must
            // walk both, not stop at whatever the picker itself rendered.
            data = cursor
              ? {items: [{package_key: 'pkg-a-3'}], next_cursor: null}
              : {items: [{package_key: 'pkg-a-1'}, {package_key: 'pkg-a-2'}], next_cursor: 'page2'};
          } else {
            data = {items: [{package_key: 'other'}], next_cursor: null};
          }
        } else if (url.pathname.endsWith('/warmed')) {
          const q = url.searchParams.get('q');
          data = q
            ? {items: [{package_key: 'warm-matching-' + q, status: 'ok'}], next_cursor: null}
            : {items: [{package_key: 'warm1', status: 'ok'}, {package_key: 'warm2', status: 'ok'}], next_cursor: null};
        } else if (url.pathname === '/api/requests') {
          data = {items: [], next_cursor: null};
        }
        return {ok: true, json: async () => data};
      };
    }
  });
  try {
    const w = dom.window, doc = w.document;
    await delay(30);
    doc.querySelector('.repo-row').click();
    await delay(30);

    // Warmed packages search — same debounced pattern as the picker's own search.
    const warmed = doc.querySelector('[data-slot="warmed"]');
    assert.match(warmed.textContent, /warm1/);
    assert.match(warmed.textContent, /warm2/);
    const warmedSearch = doc.querySelector('[data-slot="warmed-search"]');
    warmedSearch.value = 'linux';
    warmedSearch.dispatchEvent(new w.Event('input'));
    await delay(280);
    assert.match(warmed.textContent, /warm-matching-linux/);
    assert.doesNotMatch(warmed.textContent, /warm1|warm2/);
    assert(calls.some(c => c.url.pathname.endsWith('/warmed') && c.url.searchParams.get('q') === 'linux'));

    // Select all matching a filter — must walk both pages, not just the
    // one the picker itself has rendered so far.
    const search = doc.querySelector('[data-slot="search"]');
    search.value = 'pkg-a';
    search.dispatchEvent(new w.Event('input'));
    await delay(280);
    const picker = doc.querySelector('[data-slot="picker"]');
    assert.equal(picker.querySelectorAll('input').length, 2); // only page 1 rendered so far
    doc.querySelector('[data-slot="select-all-btn"]').click();
    await delay(60);
    assert.match(doc.querySelector('[data-slot="warm-msg"]').textContent, /Selected 3 matching/);
    // Every checkbox actually rendered so far reflects the selection...
    assert([...picker.querySelectorAll('input')].every(cb => cb.checked));
    // ...and the selection itself (not just what's rendered) includes the
    // second page's package too — proven by what Warm actually submits.
    doc.querySelector('[data-slot="warm-btn"]').click();
    await delay(20);
    assert.match(doc.querySelector('[data-slot="warm-msg"]').textContent, /Warmed: 1, skipped: 1, failed: 1/);
    assert(doc.querySelector('[data-slot="warm-msg"]').classList.contains('error'));
    const warmCall = calls.find(c => c.url.pathname.endsWith('/warm') && c.options.method === 'POST');
    assert.deepEqual(JSON.parse(warmCall.options.body).package_keys.sort(), ['pkg-a-1', 'pkg-a-2', 'pkg-a-3']);

    // Clear selection — unchecks everything and forgets the selection.
    doc.querySelector('[data-slot="clear-selection-btn"]').click();
    assert.equal(picker.querySelectorAll('input:checked').length, 0);
    doc.querySelector('[data-slot="warm-btn"]').click();
    await delay(20);
    assert.match(doc.querySelector('[data-slot="warm-msg"]').textContent, /Nothing selected/);
  } finally {
    dom.window.close();
  }
});

test('dashboard bulk-removes warmed packages, bulk-bans/unbans, and shows storage stats', async () => {
  const calls = [];
  const repo = {id: 'r', type: 'apt', upstream: 'example', config: {}, package_count: 1000};
  let warmed = [{package_key: 'a-1', status: 'ok'}, {package_key: 'b-1', status: 'ok'}, {package_key: 'c-1', status: 'ok'}];
  let banned = ['existing-ban'];
  const dom = new JSDOM(html, {
    url: 'http://localhost/', runScripts: 'dangerously',
    beforeParse(w) {
      w.fetch = async (raw, options = {}) => {
        const url = new URL(raw, 'http://localhost');
        calls.push({url, options, body: options.body ? JSON.parse(options.body) : null});
        let data = [];
        if (url.pathname === '/api/auth/session') data = {role: 'admin', csrf_token: 'csrf'};
        else if (url.pathname === '/api/repos') data = [repo];
        else if (url.pathname.endsWith('/summary')) data = {by_client_ip: [], by_path: [], by_repo: [], timeline: [], cache_hit_stats: []};
        else if (url.pathname === '/api/stats') {
          data = {state_db_bytes: 1234, tables: {repo_packages: 5, repo_events: 1, request_events: 0, warmed_packages: 3, prefetch_bans: 1}};
          if (url.searchParams.get('cache_dir') === '1') data.cache_dir = {path: '/cache', size_bytes: 999, file_count: 7};
        } else if (url.pathname.endsWith('/warmed/remove') && options.method === 'POST') {
          const keys = JSON.parse(options.body).package_keys;
          const before = warmed.length;
          warmed = warmed.filter(w => !keys.includes(w.package_key));
          data = {removed: before - warmed.length};
        } else if (url.pathname.endsWith('/warmed')) {
          data = {items: warmed, next_cursor: null};
        } else if (url.pathname.endsWith('/packages')) {
          data = {items: [{package_key: 'a-1', package_name: 'a'}, {package_key: 'a-2', package_name: 'a'},
                           {package_key: 'b-1', package_name: 'b'}], next_cursor: null};
        } else if (url.pathname.endsWith('/bans/remove') && options.method === 'POST') {
          const names = JSON.parse(options.body).package_names;
          banned = banned.filter(n => !names.includes(n)).sort();
          data = {banned};
        } else if (url.pathname.endsWith('/bans') && options.method === 'POST') {
          const names = JSON.parse(options.body).package_names;
          banned = [...new Set([...banned, ...names])].sort();
          data = {banned};
        } else if (url.pathname.endsWith('/bans')) {
          data = [...banned].sort();
        } else if (url.pathname === '/api/requests') {
          data = {items: [], next_cursor: null};
        }
        return {ok: true, json: async () => data};
      };
    }
  });
  try {
    const w = dom.window, doc = w.document;
    await delay(30);

    // Storage panel — opens on toggle, cache dir size only on explicit request.
    doc.getElementById('storage-panel').open = true;
    doc.getElementById('storage-panel').dispatchEvent(new w.Event('toggle'));
    await delay(20);
    let storageBody = doc.getElementById('storage-body').textContent;
    assert.match(storageBody, /1,234 bytes/);
    assert.match(storageBody, /warmed_packages: 3 row/);
    assert(!calls.some(c => c.url.pathname === '/api/stats' && c.url.searchParams.get('cache_dir') === '1'));
    doc.getElementById('calc-cache-size').click();
    await delay(20);
    assert.match(doc.getElementById('storage-body').textContent, /999 bytes/);
    assert.match(doc.getElementById('storage-body').textContent, /7 file/);

    doc.querySelector('.repo-row').click();
    await delay(30);

    // Bulk "remove selected" from Warmed packages.
    const warmedList = doc.querySelector('[data-slot="warmed"]');
    warmedList.querySelectorAll('input').forEach(cb => { if (['a-1', 'b-1'].includes(cb.value)) cb.click(); });
    doc.querySelector('[data-slot="warmed-remove-btn"]').click();
    await delay(20);
    assert.match(doc.querySelector('[data-slot="warmed-msg"]').textContent, /Removed: 2/);
    assert.deepEqual(warmed.map(x => x.package_key), ['c-1']);

    // Bulk-remove what's left too — no more per-row ✕ shortcut, bulk selection
    // is the only path now that "Select all"/"Remove selected" cover it.
    warmedList.querySelectorAll('input').forEach(cb => cb.click());
    doc.querySelector('[data-slot="warmed-remove-btn"]').click();
    await delay(20);
    assert.deepEqual(warmed, []);

    // "Ban selected (by name)" from the warm-queue picker — dedupes names
    // (a-1 and a-2 share bare name "a") and doesn't require a-2 to ever be
    // individually checked if selected via "select all matching" (not
    // exercised in isolation here, but the same selection Map is used).
    const picker = doc.querySelector('[data-slot="picker"]');
    picker.querySelectorAll('input').forEach(cb => { if (['a-1', 'b-1'].includes(cb.value)) cb.click(); });
    doc.querySelector('[data-slot="ban-selected-btn"]').click();
    await delay(20);
    assert.match(doc.querySelector('[data-slot="warm-msg"]').textContent, /Banned 2 name/);
    assert.deepEqual(banned, ['a', 'b', 'existing-ban']);

    // Comma-separated names in the plain "Ban" input.
    const banInput = doc.querySelector('[data-slot="ban-name"]');
    banInput.value = 'c, d';
    doc.querySelector('[data-slot="ban-btn"]').click();
    await delay(20);
    assert.deepEqual(banned, ['a', 'b', 'c', 'd', 'existing-ban']);

    // Bulk "select all" + "unban selected" on the (unpaginated) bans list.
    doc.querySelector('[data-slot="bans-select-all-btn"]').click();
    const bansList = doc.querySelector('[data-slot="bans"]');
    assert([...bansList.querySelectorAll('input')].every(cb => cb.checked));
    doc.querySelector('[data-slot="unban-selected-btn"]').click();
    await delay(20);
    assert.deepEqual(banned, []);

    assert(calls.every(c => !c.body || !('package_key' in c.body) && !('package_name' in c.body)));
  } finally {
    dom.window.close();
  }
});

test('dashboard manual cache purge: scan shows candidates all-checked, purge reports outcomes and cleans up', async () => {
  const calls = [];
  const repo = {id: 'r', type: 'apt', upstream: 'example', config: {}, package_count: 1000};
  let candidates = [{package_key: 'gone-1', filename: 'gone-1.deb'}, {package_key: 'gone-2', filename: 'gone-2.deb'}];
  const dom = new JSDOM(html, {
    url: 'http://localhost/', runScripts: 'dangerously',
    beforeParse(w) {
      w.fetch = async (raw, options = {}) => {
        const url = new URL(raw, 'http://localhost');
        calls.push({url, options, body: options.body ? JSON.parse(options.body) : null});
        let data = [];
        if (url.pathname === '/api/auth/session') data = {role: 'admin', csrf_token: 'csrf'};
        else if (url.pathname === '/api/repos') data = [repo];
        else if (url.pathname.endsWith('/summary')) data = {by_client_ip: [], by_path: [], by_repo: [], timeline: [], cache_hit_stats: []};
        else if (url.pathname.endsWith('/purge-candidates')) data = {enable_purge: true, candidates};
        else if (url.pathname.endsWith('/purge') && options.method === 'POST') {
          const keys = JSON.parse(options.body).package_keys;
          data = {
            results: Object.fromEntries(keys.map(k => [k, k === 'gone-1' ? 'purged' : 'not_cached'])),
            not_found: [],
          };
          candidates = candidates.filter(c => !keys.includes(c.package_key));
        } else if (url.pathname.endsWith('/packages')) data = {items: [], next_cursor: null};
        else if (url.pathname.endsWith('/warmed')) data = {items: [], next_cursor: null};
        else if (url.pathname === '/api/requests') data = {items: [], next_cursor: null};
        else if (url.pathname.endsWith('/bans')) data = [];
        return {ok: true, json: async () => data};
      };
    }
  });
  try {
    const w = dom.window, doc = w.document;
    await delay(30);
    doc.querySelector('.repo-row').click();
    await delay(30);

    const purgeList = doc.querySelector('[data-slot="purge-list"]');
    assert.equal(purgeList.hidden, true);
    doc.querySelector('[data-slot="purge-scan-btn"]').click();
    await delay(20);
    assert.equal(purgeList.hidden, false);
    const checkboxes = [...purgeList.querySelectorAll('input')];
    assert.equal(checkboxes.length, 2);
    assert(checkboxes.every(cb => cb.checked)); // all checked by default
    assert.equal(doc.querySelector('[data-slot="purge-actions"]').hidden, false);

    // Uncheck one, purge the other.
    checkboxes.find(cb => cb.value === 'gone-2').checked = false;
    doc.querySelector('[data-slot="purge-selected-btn"]').click();
    await delay(30);
    const purgeCall = calls.find(c => c.url.pathname.endsWith('/purge') && c.options.method === 'POST');
    assert.deepEqual(purgeCall.body.package_keys, ['gone-1']);
    assert.match(doc.querySelector('[data-slot="purge-msg"]').textContent, /Purged: 1, not cached: 0/);
    // Re-scan happened automatically — the purged one is gone from the list.
    assert.deepEqual([...purgeList.querySelectorAll('input')].map(cb => cb.value), ['gone-2']);
  } finally {
    dom.window.close();
  }
});

test('dashboard manual cache purge shows a clear message when enable_purge is off', async () => {
  const repo = {id: 'r', type: 'apt', upstream: 'example', config: {}, package_count: 1000};
  const dom = new JSDOM(html, {
    url: 'http://localhost/', runScripts: 'dangerously',
    beforeParse(w) {
      w.fetch = async (raw) => {
        const url = new URL(raw, 'http://localhost');
        let data = [];
        if (url.pathname === '/api/auth/session') data = {role: 'admin', csrf_token: 'csrf'};
        else if (url.pathname === '/api/repos') data = [repo];
        else if (url.pathname.endsWith('/summary')) data = {by_client_ip: [], by_path: [], by_repo: [], timeline: [], cache_hit_stats: []};
        else if (url.pathname.endsWith('/purge-candidates')) data = {enable_purge: false, candidates: []};
        else if (url.pathname.endsWith('/packages')) data = {items: [], next_cursor: null};
        else if (url.pathname.endsWith('/warmed')) data = {items: [], next_cursor: null};
        else if (url.pathname === '/api/requests') data = {items: [], next_cursor: null};
        else if (url.pathname.endsWith('/bans')) data = [];
        return {ok: true, json: async () => data};
      };
    }
  });
  try {
    const w = dom.window, doc = w.document;
    await delay(30);
    doc.querySelector('.repo-row').click();
    await delay(30);
    doc.querySelector('[data-slot="purge-scan-btn"]').click();
    await delay(20);
    assert.match(doc.querySelector('[data-slot="purge-msg"]').textContent, /enable_purge is not enabled/);
    assert.equal(doc.querySelector('[data-slot="purge-list"]').hidden, true);
    assert.equal(doc.querySelector('[data-slot="purge-actions"]').hidden, true);
  } finally {
    dom.window.close();
  }
});

test('dashboard issues one-time host tokens with CSRF and supports revocation', async () => {
  const calls = [];
  let issued = false, revoked = false;
  const dom = new JSDOM(html, {
    url: 'http://localhost/', runScripts: 'dangerously',
    beforeParse(w) {
      w.confirm = () => true;
      w.fetch = async (url, options = {}) => {
        calls.push({url, options});
        let data = {};
        if (url === '/api/auth/session') data = {role:'admin', csrf_token: 'test-csrf'};
        else if (url === '/api/tokens' && options.method === 'POST') {
          issued = true;
          data = {id:'id1', token:'rw_once_only'};
        } else if (url === '/api/tokens') data = issued ? [{id:'id1', name:'host-a', created_at:1, revoked_at:revoked ? 2 : null}] : [];
        else if (url === '/api/tokens/id1/revoke') { revoked = true; data = {ok:true}; }
        else if (url === '/api/repos') data = [];
        else if (url.startsWith('/api/requests/summary')) data = {by_client_ip:[], by_path:[], by_repo:[], timeline:[], cache_hit_stats:[]};
        else if (url.startsWith('/api/requests')) data = {items:[], next_cursor:null};
        return {ok:true, status:200, json:async () => data};
      };
    }
  });
  try {
    const w = dom.window, doc = w.document;
    await delay(30);
    doc.getElementById('token-name').value = 'host-a';
    doc.getElementById('token-repos').value = 'arch, debian';
    doc.getElementById('token-form').dispatchEvent(new w.Event('submit', {bubbles:true, cancelable:true}));
    await delay(40);
    assert.equal(doc.getElementById('token-secret').value, 'rw_once_only');
    const request = calls.find(call => call.url === '/api/tokens' && call.options.method === 'POST');
    assert.equal(request.options.headers['X-CSRF-Token'], 'test-csrf');
    assert.equal(JSON.parse(request.options.body).expires_at, null);
    assert.deepEqual(JSON.parse(request.options.body).repo_ids, ['arch', 'debian']);
    assert(!('X-Repowatch-Password' in request.options.headers));
    assert.equal(w.sessionStorage.getItem('repowatch_admin_password'), null);
    doc.getElementById('hide-token').click();
    assert.equal(doc.getElementById('token-secret').value, '');
    doc.querySelector('[data-revoke]').click();
    await delay(40);
    assert(revoked);
    assert.match(doc.getElementById('tokens-list').textContent, /Revoked/);
  } finally { dom.window.close(); }
});

test('guest dashboard reads data and hides administrative controls', async () => {
  const calls = [];
  const dom = new JSDOM(html, {
    url: 'http://localhost/', runScripts: 'dangerously',
    beforeParse(w) {
      w.fetch = async (url, options = {}) => {
        calls.push({url, options});
        let data = [];
        if (url === '/api/auth/session') data = {role:'guest'};
        else if (url === '/api/repos') data = [{id:'r', type:'apt', config:{}, upstream:'example'}];
        else if (url.startsWith('/api/requests/summary')) data = {by_client_ip:[], by_path:[], by_repo:[], timeline:[], cache_hit_stats:[]};
        else if (url.includes('/packages?')) data = {items:[{package_key:'visible-package'}], next_cursor:null};
        else if (url.includes('/warmed?')) data = {items:[{package_key:'visible-warmed', status:'ok'}], next_cursor:null};
        else if (url.startsWith('/api/requests')) data = {items:[], next_cursor:null};
        else if (url.includes('/bans')) data = ['visible-ban'];
        return {ok:true, status:200, json:async () => data};
      };
    }
  });
  try {
    const w = dom.window, doc = w.document;
    await delay(50);
    assert.equal(doc.body.dataset.role, 'guest');
    assert.equal(doc.getElementById('admin-login').hidden, false);
    for (const id of ['tokens-panel', 'storage-panel', 'logout-btn', 'toggle-add-form']) {
      assert.equal(doc.getElementById(id).hidden, true);
    }
    assert.equal(doc.querySelector('[data-action="edit-repo"]'), null);
    doc.querySelector('.repo-row').click();
    await delay(30);
    assert.equal(w.getComputedStyle(doc.querySelector('.warm-actions')).display, 'none');
    assert.equal(w.getComputedStyle(doc.querySelector('.ban-form')).display, 'none');
    assert.equal(w.getComputedStyle(doc.querySelector('.purge-section')).display, 'none');
    for (const el of doc.querySelectorAll('.bulk-actions')) {
      assert.equal(w.getComputedStyle(el).display, 'none');
    }
    assert.match(doc.querySelector('[data-slot="picker"]').textContent, /visible-package/);
    assert.equal(w.getComputedStyle(doc.querySelector('[data-slot="picker"] input')).display, 'none');
    assert.equal(w.getComputedStyle(doc.querySelector('[data-slot="warmed"] input')).display, 'none');
    assert.equal(w.getComputedStyle(doc.querySelector('[data-slot="bans"] input')).display, 'none');
    assert(calls.every(call => !call.options.method || call.options.method === 'GET'));
    assert(!calls.some(call => call.url === '/api/tokens'));
    assert.equal(doc.getElementById('toggle-settings-form').hidden, false);
    doc.getElementById('toggle-settings-form').click();
    await delay(30);
    assert(calls.some(call => call.url === '/api/config'));
    assert.equal(doc.getElementById('settings-form').hidden, false);
    assert([...doc.querySelectorAll('#settings-form input')].every(input => input.readOnly));
    assert.equal(doc.querySelector('#settings-form button[type="submit"]').hidden, true);
  } finally { dom.window.close(); }
});

test('repository editor preserves ALT type and custom URL scheme on save', async () => {
  const calls = [];
  const cfg = {id:'alt', type:'apt-rpm', upstream:'https://example.test/p11/x86_64', arch:'x86_64',
               component:'classic', prefetch:false, url_template:'/{distro}/{branch}/{arch}/',
               url_variables:{distro:'altlinux', branch:'p11'}};
  const dom = new JSDOM(html, {url:'http://localhost/', runScripts:'dangerously', beforeParse(w) {
    w.fetch = async (url, options={}) => {
      calls.push({url,options});
      let data = [];
      if (url==='/api/auth/session') data={role:'admin',csrf_token:'csrf'};
      else if(url==='/api/repos') data=[{...cfg,config:cfg}];
      else if(url==='/api/repos/alt') data={id:'alt'};
      else if(url.startsWith('/api/requests/summary')) data={by_client_ip:[],by_path:[],by_repo:[],timeline:[],cache_hit_stats:[]};
      else if(url.startsWith('/api/requests')) data={items:[],next_cursor:null};
      return {ok:true,status:200,json:async()=>data};
    };
  }});
  try {
    const w=dom.window, doc=w.document;
    await delay(50);
    doc.querySelector('[data-action="edit-repo"]').click();
    assert.equal(doc.getElementById('f-type').value,'apt-rpm');
    assert.equal(doc.getElementById('f-component').value,'classic');
    assert.equal(doc.getElementById('f-component').closest('.field').hidden,false);
    assert.equal(doc.getElementById('f-url-template').value,cfg.url_template);
    doc.getElementById('add-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
    await delay(50);
    const saved=calls.find(c=>c.url==='/api/repos/alt' && c.options.method==='POST');
    assert(saved);
    const body=JSON.parse(saved.options.body);
    assert.equal(body.type,'apt-rpm');
    assert.equal(body.component,'classic');
    assert.equal(body.url_template,cfg.url_template);
    assert.deepEqual(body.url_variables,cfg.url_variables);
  } finally { dom.window.close(); }
});


test('APK editor preserves signature backend and trusted keys', async () => {
  const calls=[];
  const cfg={id:'alpine',type:'apk',upstream:'https://example.org/alpine',arch:'x86_64',prefetch:false,
    verify_signature:true,apk_keys_dir:'/etc/repowatch/keys/alpine',apk_signature_backend:'apk-tools'};
  const dom=new JSDOM(html,{url:'http://localhost/',runScripts:'dangerously',beforeParse(w){
    w.fetch=async(url,options={})=>{
      calls.push({url,options});
      let data=[];
      if(url==='/api/auth/session') data={role:'admin',csrf_token:'csrf'};
      else if(url==='/api/repos') data=[{...cfg,config:cfg}];
      else if(url.startsWith('/api/requests/summary')) data={by_client_ip:[],by_path:[],by_repo:[],timeline:[],cache_hit_stats:[]};
      else if(url.startsWith('/api/requests')) data={items:[],next_cursor:null};
      return {ok:true,status:200,json:async()=>data};
    };
  }});
  try {
    const w=dom.window,doc=w.document;
    await delay(50);
    doc.querySelector('[data-action="edit-repo"]').click();
    assert.equal(doc.getElementById('f-verify-signature').checked,true);
    assert.equal(doc.getElementById('f-apk-backend').value,'apk-tools');
    doc.getElementById('add-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
    await delay(50);
    const saved=calls.find(c=>c.url==='/api/repos/alpine' && c.options.method==='POST');
    const body=JSON.parse(saved.options.body);
    assert.equal(body.verify_signature,true);
    assert.equal(body.apk_signature_backend,'apk-tools');
    assert.equal(body.apk_keys_dir,cfg.apk_keys_dir);
  } finally {dom.window.close();}
});


test('dashboard shows a warning badge for a repo with an expiring GPG key, plain date otherwise', async () => {
  const soon = {id: 'soon', type: 'apt', upstream: 'example', config: {},
    key_expires_at: '2026-01-05T00:00:00+00:00', key_expiring_soon: true};
  const fine = {id: 'fine', type: 'apt', upstream: 'example', config: {},
    key_expires_at: '2030-01-01T00:00:00+00:00', key_expiring_soon: false};
  const none = {id: 'none', type: 'apk', upstream: 'example', config: {}};
  const dom = new JSDOM(html, {
    url: 'http://localhost/', runScripts: 'dangerously',
    beforeParse(w) {
      w.fetch = async raw => {
        const url = new URL(raw, 'http://localhost');
        let data = [];
        if (url.pathname === '/api/auth/session') data = {role: 'admin', csrf_token: 'csrf'};
        else if (url.pathname === '/api/repos') data = [soon, fine, none];
        else if (url.pathname.endsWith('/summary')) data = {by_client_ip: [], by_path: [], by_repo: [], timeline: [], cache_hit_stats: []};
        return {ok: true, json: async () => data};
      };
    }
  });
  try {
    const w = dom.window, doc = w.document;
    await delay(30);
    const rows = [...doc.querySelectorAll('.repo-row')];
    const cell = id => rows.find(r => r.dataset.repoId === id).querySelector('.col-key');
    assert.match(cell('soon').innerHTML, /badge-warn/);
    assert.doesNotMatch(cell('fine').innerHTML, /badge-warn/);
    assert.match(cell('fine').textContent, /2030-01-01/);
    assert.equal(cell('none').textContent.trim(), '—');
  } finally { dom.window.close(); }
});

test('Nix repository editor preserves source, outputs and signature policy', async () => {
  let submitted;
  const cfg = {id: 'n', type: 'nix', upstream: 'https://cache.test', arch: 'x86_64-linux',
    nix_source: 'https://source.test/nixexprs.tar.xz', nix_attributes: ['hello', 'jq'],
    nix_public_keys: ['cache.test-1:' + 'A'.repeat(43) + '='], nix_timeout: 900,
    nix_max_paths: 20000, verify_signature: true, prefetch: true};
  const repo = {id: 'n', type: 'nix', upstream: cfg.upstream, config: cfg};
  const dom = new JSDOM(html, {url: 'http://localhost/', runScripts: 'dangerously',
    beforeParse(w) {
      w.HTMLElement.prototype.scrollIntoView = () => {};
      w.fetch = async (raw, options = {}) => {
        const url = new URL(raw, 'http://localhost');
        let data = [];
        if (url.pathname === '/api/auth/session') data = {role: 'admin', csrf_token: 'csrf'};
        else if (url.pathname === '/api/repos') data = [repo];
        else if (url.pathname === '/api/repos/n' && options.method === 'POST') {
          submitted = JSON.parse(options.body); data = {ok: true};
        } else if (url.pathname.endsWith('/summary')) {
          data = {by_client_ip: [], by_path: [], by_repo: [], timeline: [], cache_hit_stats: []};
        }
        return {ok: true, json: async () => data};
      };
    }});
  try {
    await delay(30);
    dom.window.openEditForm(repo);
    const doc = dom.window.document;
    assert.equal(doc.getElementById('f-nix-source').closest('[data-for]').hidden, false);
    assert.equal(doc.getElementById('f-keyring-path').closest('.field').hidden, true);
    doc.getElementById('f-nix-source').closest('form').dispatchEvent(
      new dom.window.Event('submit', {bubbles: true, cancelable: true}));
    await delay(30);
    for (const key of ['nix_source', 'nix_attributes', 'nix_public_keys', 'nix_timeout', 'nix_max_paths', 'verify_signature']) {
      assert.deepEqual(submitted[key], cfg[key]);
    }
  } finally {dom.window.close();}
});

test('bandwidth schedule editor saves windows and timezone', async () => {
  let submitted;
  const settings = {prefetch_bandwidth_limit: 5000, prefetch_bandwidth_timezone: 'UTC',
    prefetch_bandwidth_schedule: [{days: ['mon','fri'], start: '23:00', end: '06:00', limit: 10000}]};
  const dom = new JSDOM(html, {url: 'http://localhost/', runScripts: 'dangerously',
    beforeParse(w) {
      w.fetch = async (raw, options = {}) => {
        const url = new URL(raw, 'http://localhost');
        let data = [];
        if (url.pathname === '/api/auth/session') data = {role: 'admin', csrf_token: 'csrf'};
        else if (url.pathname === '/api/config') {
          if (options.method === 'POST') submitted = JSON.parse(options.body);
          data = submitted || settings;
        } else if (url.pathname.endsWith('/summary')) {
          data = {by_client_ip: [], by_path: [], by_repo: [], timeline: [], cache_hit_stats: []};
        }
        return {ok: true, json: async () => data};
      };
    }});
  try {
    await delay(30);
    const doc = dom.window.document;
    doc.getElementById('toggle-settings-form').click();
    await delay(30);
    assert.equal(doc.querySelectorAll('.bandwidth-window').length, 1);
    doc.getElementById('s-bandwidth-timezone').value = 'Asia/Vladivostok';
    doc.getElementById('s-bandwidth-add').click();
    const row = doc.querySelectorAll('.bandwidth-window')[1];
    row.querySelector('[data-field="days"]').value = 'sun';
    row.querySelector('[data-field="limit"]').value = '';
    doc.getElementById('settings-form').dispatchEvent(new dom.window.Event('submit', {bubbles:true,cancelable:true}));
    await delay(30);
    assert.equal(submitted.prefetch_bandwidth_timezone, 'Asia/Vladivostok');
    assert.deepEqual(submitted.prefetch_bandwidth_schedule, [settings.prefetch_bandwidth_schedule[0],
      {days:['sun'],start:'00:00',end:'24:00',limit:null}]);
    doc.querySelector('.bandwidth-window button').click();
    assert.equal(doc.querySelectorAll('.bandwidth-window').length, 1);
  } finally {dom.window.close();}
});

for (const [type, component] of [['gentoo', null], ['slackware', 'patches'], ['slackware', null]]) {
  test(`${type} editor preserves component ${component} and signature controls`, async () => {
    let submitted;
    const cfg = {id: 'binpkg', type, upstream: 'https://example.org/repo', arch: 'x86_64',
      component, verify_signature: type === 'slackware', keyring_path: '/keys.gpg',
      prefetch_whitelist: ['linux-*', 'app-editors/*'], prefetch_blacklist: ['*-debug']};
    const repo = {...cfg, config: cfg};
    const dom = new JSDOM(html, {url: 'http://localhost/', runScripts: 'dangerously',
      beforeParse(w) {
        w.HTMLElement.prototype.scrollIntoView = () => {};
        w.fetch = async (raw, options = {}) => {
          const path = new URL(raw, 'http://localhost').pathname;
          let data = [];
          if (path === '/api/auth/session') data = {role: 'admin', csrf_token: 'csrf'};
          else if (path === '/api/repos') data = [repo];
          else if (path === '/api/repos/binpkg' && options.method === 'POST') {
            submitted = JSON.parse(options.body); data = {};
          } else if (path.endsWith('/summary')) data = {by_client_ip: [], by_path: [], by_repo: [], timeline: [], cache_hit_stats: []};
          return {ok: true, json: async () => data};
        };
      }});
    try {
      await delay(30);
      dom.window.openEditForm(repo);
      const doc = dom.window.document;
      assert.equal(doc.getElementById('f-verify-signature-field').hidden, type === 'gentoo');
      assert.equal(doc.getElementById('f-component').closest('.field').hidden, type !== 'slackware');
      assert.equal(doc.getElementById('f-prefetch-whitelist').value, 'linux-*\napp-editors/*');
      assert.equal(doc.getElementById('f-prefetch-blacklist').value, '*-debug');
      doc.getElementById('f-prefetch-blacklist').value = '';
      doc.getElementById('add-form').dispatchEvent(new dom.window.Event('submit', {bubbles: true, cancelable: true}));
      await delay(30);
      assert.equal(submitted.type, type);
      assert.deepEqual(submitted.prefetch_whitelist, ['linux-*', 'app-editors/*']);
      assert.deepEqual(submitted.prefetch_blacklist, []);
      if (type === 'slackware') {
        assert.equal(submitted.component, component);
        assert.equal(submitted.keyring_path, '/keys.gpg');
      } else assert.notEqual(submitted.verify_signature, true);
    } finally {dom.window.close();}
  });
}
