'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.resolve(__dirname, '../../web/js/boot.js'), 'utf8');

// Execute the actual boot script with the browser APIs it uses. This checks
// observable calls and effects; it does not simulate browser layout or caching.
async function boot({ embedded = 'old', live = 'new', search = '', stamps = [], fail = false } = {}) {
  const redirects = [];
  const banners = [];
  const requests = [];
  const storage = new Map([['hne_web_version', JSON.stringify(stamps)]]);
  const context = {
    document: {
      body: { dataset: { webVersion: embedded }, appendChild: (node) => banners.push(node) },
      createElement: () => ({}),
    },
    window: { location: { search, replace: (url) => redirects.push(url) } },
    sessionStorage: { getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    URLSearchParams,
    fetch: async (url, options) => {
      requests.push({ url, cache: options.cache });
      if (fail) throw new Error('test network failure');
      return { headers: { get: (key) => key === 'X-HNE-Web-Version' ? live : null } };
    },
  };
  await vm.runInNewContext(source, context, { filename: 'web/js/boot.js' });
  return { redirects, banners, requests, storage };
}

test('web boot reloads a stale page using the live version and no-store', async () => {
  const result = await boot();
  assert.deepEqual(result.requests, [{ url: '/console/', cache: 'no-store' }]);
  assert.deepEqual(result.redirects, ['/console/?v=new']);
  assert.equal(result.banners.length, 0);
  assert.equal(JSON.parse(result.storage.get('hne_web_version')).length, 1);
});

test('web boot keeps a matching version without recording a reload', async () => {
  const result = await boot({ embedded: 'new' });
  assert.deepEqual(result.redirects, []);
  assert.equal(result.banners.length, 0);
  assert.equal(result.storage.get('hne_web_version'), '[]');
});

test('web boot shows a recovery banner if the requested version is still stale', async () => {
  const result = await boot({ search: '?v=new' });
  assert.deepEqual(result.redirects, []);
  assert.equal(result.banners.length, 1);
  assert.equal(result.banners[0].className, 'boot-stale');
  assert.match(result.banners[0].innerHTML, /Ctrl\+F5/);
});

test('web boot limits automatic reloads within one minute', async () => {
  const result = await boot({ stamps: [Date.now(), Date.now()] });
  assert.deepEqual(result.redirects, []);
  assert.equal(result.banners.length, 1);
});

test('web boot tolerates an unavailable server without reloading', async () => {
  const result = await boot({ fail: true });
  assert.deepEqual(result.redirects, []);
  assert.equal(result.banners.length, 0);
  assert.equal(result.requests.length, 1);
});
