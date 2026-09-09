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
        else if (url.pathname.endsWith('/summary')) data = {by_client_ip: [], by_path: [], by_repo: []};
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
        else if (url.startsWith('/api/requests/summary')) data = {by_client_ip:[], by_path:[], by_repo:[]};
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
        else if (url.startsWith('/api/requests/summary')) data = {by_client_ip:[], by_path:[], by_repo:[]};
        else if (url.includes('/packages?')) data = {items:[{package_key:'visible-package'}], next_cursor:null};
        else if (url.includes('/warmed?') || url.startsWith('/api/requests')) data = {items:[], next_cursor:null};
        return {ok:true, status:200, json:async () => data};
      };
    }
  });
  try {
    const w = dom.window, doc = w.document;
    await delay(50);
    assert.equal(doc.body.dataset.role, 'guest');
    assert.equal(doc.getElementById('admin-login').hidden, false);
    for (const id of ['tokens-panel', 'logout-btn', 'toggle-add-form']) {
      assert.equal(doc.getElementById(id).hidden, true);
    }
    assert.equal(doc.querySelector('[data-action="edit-repo"]'), null);
    doc.querySelector('.repo-row').click();
    await delay(30);
    assert.equal(w.getComputedStyle(doc.querySelector('.warm-actions')).display, 'none');
    assert.equal(w.getComputedStyle(doc.querySelector('.ban-form')).display, 'none');
    assert.match(doc.querySelector('[data-slot="picker"]').textContent, /visible-package/);
    assert.equal(w.getComputedStyle(doc.querySelector('[data-slot="picker"] input')).display, 'none');
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
      else if(url.startsWith('/api/requests/summary')) data={by_client_ip:[],by_path:[],by_repo:[]};
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
      else if(url.startsWith('/api/requests/summary')) data={by_client_ip:[],by_path:[],by_repo:[]};
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
