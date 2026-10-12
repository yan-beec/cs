import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { chatwootTarget, sameAccount, createBrowserController, verifyChatwootSession } from '../core.mjs';

const URL = 'https://support.example/app/accounts/1/conversations/56';

function browser(tabs = [], sessions = {}, allowed = true) {
  const calls = [];
  const api = {
    calls,
    storage: { local: { get: async () => ({ site: 'https://support.example' }) } },
    permissions: { contains: async () => allowed },
    scripting: { executeScript: async request => {
      calls.push(['verify', request.target.tabId]);
      return [{ result: { status: sessions[request.target.tabId] || 'logged_in' } }];
    } },
    tabs: {
      query: async () => tabs,
      get: async id => { const tab = tabs.find(tab => tab.id === id); if (!tab) throw new Error('Tab closed'); return tab; },
      update: async (id, options) => { calls.push(['update', id, options]); return { ...tabs.find(tab => tab.id === id), ...options }; },
      create: async options => { calls.push(['create', options]); return { id: 100, windowId: 9, ...options }; },
    },
    windows: {
      get: async () => ({ state: 'minimized' }),
      update: async (id, options) => calls.push(['window', id, options]),
    },
  };
  return api;
}

test('manifest public key and native host have the same stable extension identity', () => {
  const manifest = JSON.parse(readFileSync(new globalThis.URL('../manifest.json', import.meta.url)));
  const hash = createHash('sha256').update(Buffer.from(manifest.key, 'base64')).digest().subarray(0, 16);
  const id = [...hash].map(value => 'abcdefghijklmnop'[value >> 4] + 'abcdefghijklmnop'[value & 15]).join('');
  const bridge = readFileSync(new globalThis.URL('../../client/bridge.py', import.meta.url), 'utf8');
  assert.ok(bridge.includes(`EXTENSION_ID = "${id}"`));
  assert.equal(manifest.manifest_version, 3);
  assert.equal(manifest.host_permissions, undefined, 'Site access must be granted by the user');
});

test('match excludes different site, scheme, port, account and deployment path', () => {
  const target = chatwootTarget(URL);
  assert.ok(sameAccount('https://support.example/app/accounts/1/dashboard', target));
  for (const value of [
    'https://other.example/app/accounts/1', 'http://support.example/app/accounts/1',
    'https://support.example:444/app/accounts/1', 'https://support.example/app/accounts/12',
    'https://support.example/other/app/accounts/1', 'https://support.example/app/login',
  ]) assert.equal(sameAccount(value, target), false, value);
  for (const value of ['javascript:alert(1)', `${URL}?token=x`, 'https://user:pass@support.example/app/accounts/1/conversations/56']) {
    assert.throws(() => chatwootTarget(value));
  }
});

test('probe verifies only matching tabs and prefers the exact conversation', async () => {
  const api = browser([
    { id: 1, url: 'https://support.example/app/accounts/1/dashboard', active: true },
    { id: 2, url: URL, active: false },
    { id: 3, url: 'https://other.example/app/accounts/1/conversations/56' },
    { id: 4, url: 'https://support.example/app/accounts/12/dashboard' },
  ]);
  const result = await createBrowserController(api).probe(URL);
  assert.deepEqual(result, { status: 'candidate', tabId: 2, exact: true, active: false });
  assert.deepEqual(api.calls, [['verify', 1], ['verify', 2]]);
});

test('open reuses the original tab and restores its minimized window', async () => {
  const api = browser([{ id: 7, windowId: 3, url: URL }]);
  assert.deepEqual(await createBrowserController(api).open(URL, 7), { status: 'reused' });
  assert.deepEqual(api.calls, [
    ['verify', 7], ['update', 7, { url: URL, active: true }], ['window', 3, { focused: true, state: 'normal' }],
  ]);
});

test('test reminder opens the existing account dashboard without creating a tab', async () => {
  const dashboard = 'https://support.example/app/accounts/1/dashboard';
  const api = browser([{ id: 7, windowId: 3, url: dashboard, active: true }]);
  const controller = createBrowserController(api);
  const candidate = await controller.probe(dashboard);
  assert.equal(candidate.status, 'candidate');
  assert.equal(candidate.tabId, 7);
  assert.deepEqual(await controller.open(dashboard, candidate.tabId), { status: 'reused' });
  assert.ok(api.calls.some(call => call[0] === 'update' && call[2].url === dashboard));
  assert.equal(api.calls.filter(call => call[0] === 'create').length, 0);
});

test('local development uses the exact localhost origin and account', async () => {
  const dashboard = 'http://localhost:3000/app/accounts/1/dashboard';
  const api = browser([
    { id: 7, windowId: 3, url: dashboard, active: true },
    { id: 8, url: 'http://127.0.0.1:3000/app/accounts/1/dashboard' },
    { id: 9, url: 'http://localhost:3000/app/accounts/2/dashboard' },
  ]);
  api.storage.local.get = async () => ({ site: 'http://localhost:3000' });
  const result = await createBrowserController(api).probe(dashboard);
  assert.equal(result.status, 'candidate');
  assert.equal(result.tabId, 7);
  assert.deepEqual(api.calls, [['verify', 7]]);
});

test('a different configured origin reports mismatch without reading tabs or navigating', async () => {
  const api = browser([{ id: 7, url: URL }]);
  const controller = createBrowserController(api);
  const dashboard = 'http://localhost:3000/app/accounts/1/dashboard';
  assert.deepEqual(await controller.probe(dashboard), { status: 'site_mismatch' });
  assert.deepEqual(await controller.open(dashboard, 7), { status: 'site_mismatch' });
  assert.deepEqual(api.calls, []);
});

test('no workbench creates one tab inside this browser', async () => {
  const api = browser();
  assert.deepEqual(await createBrowserController(api).open(URL, null), { status: 'opened' });
  assert.equal(api.calls.filter(call => call[0] === 'create').length, 1);
});

test('expired login and failed validation never navigate a tab', async () => {
  for (const status of ['logged_out', 'error']) {
    const api = browser([{ id: 7, url: URL }], { 7: status });
    const controller = createBrowserController(api);
    assert.equal((await controller.probe(URL)).status, status === 'logged_out' ? 'login_required' : 'error');
    assert.deepEqual(await controller.open(URL, 7), { status: 'error' });
    assert.equal(api.calls.filter(call => ['create', 'update'].includes(call[0])).length, 0);
  }
});

test('missing authorization does not read tabs or run page scripts', async () => {
  const api = browser([{ id: 7, url: URL }], {}, false);
  assert.deepEqual(await createBrowserController(api).probe(URL), { status: 'no_permission' });
  assert.deepEqual(api.calls, []);
});

test('changed tab is not overwritten and a new workbench prevents duplicate creation', async () => {
  const api = browser([{ id: 7, url: 'https://other.example' }]);
  assert.deepEqual(await createBrowserController(api).open(URL, 7), { status: 'error' });
  api.tabs.query = async () => [{ id: 9, url: URL }];
  assert.deepEqual(await createBrowserController(api).open(URL, null), { status: 'error' });
  assert.equal(api.calls.filter(call => ['create', 'update'].includes(call[0])).length, 0);
});

test('login verifier calls actual session endpoint and exposes no credentials or profile data', async () => {
  globalThis.location = { origin: 'https://support.example' };
  const calls = [];
  globalThis.window = { axios: { get: async value => {
    calls.push(value);
    return { data: { payload: { data: { id: 11, email: 'private@example.test', accounts: [{ id: 1 }] } } } };
  } } };
  try {
    assert.deepEqual(await verifyChatwootSession('https://support.example', '1', ''), { status: 'logged_in' });
    assert.deepEqual(calls, ['https://support.example/auth/validate_token']);
    assert.deepEqual(await verifyChatwootSession('https://support.example', '12', ''), { status: 'logged_out' });
    window.axios.get = async () => { throw { response: { status: 401 } }; };
    assert.deepEqual(await verifyChatwootSession('https://support.example', '1', ''), { status: 'logged_out' });
    window.axios.get = async () => { throw new Error('Network unavailable'); };
    assert.deepEqual(await verifyChatwootSession('https://support.example', '1', ''), { status: 'error' });
  } finally { delete globalThis.location; delete globalThis.window; }
});
