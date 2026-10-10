'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../../../static/js/pwa-form-draft.js'), 'utf8');

function page({ storageAvailable = true } = {}) {
  const listeners = {};
  const opens = [];
  const navigations = [];
  const location = { search: '', pathname: '/dashboard/', hash: '' };
  Object.defineProperty(location, 'href', { set(value) { navigations.push(value); } });
  const indexedDB = { open() { const request = {}; opens.push(request); return request; } };
  const context = {
    document: {
      body: { getAttribute() { return '23'; } },
      querySelectorAll() { return []; },
      addEventListener(name, callback) { listeners[name] = callback; },
    },
    window: { indexedDB: storageAvailable ? indexedDB : undefined },
    indexedDB,
    location,
    navigator: {},
    URLSearchParams,
  };
  vm.runInNewContext(source, context);
  function clickLogout() {
    let prevented = false;
    listeners.click({
      target: { closest() { return { href: '/logout/' }; } },
      preventDefault() { prevented = true; },
    });
    assert.equal(prevented, true);
  }
  function completeCleanup(request, { fail = false } = {}) {
    if (fail) {
      request.error = new Error('storage unavailable');
      request.onerror();
      return;
    }
    const transaction = {
      objectStore() { return { openCursor() { return {}; } }; },
    };
    request.result = { transaction() { return transaction; }, close() {} };
    request.onsuccess();
    queueMicrotask(() => transaction.oncomplete());
  }
  return { opens, navigations, clickLogout, completeCleanup };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test('repeated logout taps wait for draft cleanup and navigate only once', async () => {
  const app = page();
  app.clickLogout();
  app.clickLogout();
  assert.equal(app.opens.length, 2, 'one expiry cleanup and one logout cleanup');
  assert.deepEqual(app.navigations, [], 'wait for cleanup before logout');
  app.opens.forEach((request) => app.completeCleanup(request));
  await settle();
  assert.deepEqual(app.navigations, ['/logout/']);
});

test('failed local storage still permits one logout navigation', async () => {
  const app = page({ storageAvailable: false });
  app.clickLogout();
  app.clickLogout();
  await settle();
  assert.deepEqual(app.navigations, ['/logout/']);
});
