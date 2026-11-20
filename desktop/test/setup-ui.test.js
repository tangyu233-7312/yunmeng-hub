'use strict';

// Execute actual setup scripts and events. This is a VM component test, not
// a browser layout or Electron IPC end-to-end test. Only DOM/IPC boundaries vary.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const config = require('../src/setup-config');
const validate = require('../src/setup-validate');
const html = fs.readFileSync(path.join(__dirname, '../src/setup.html'), 'utf8');
const executableHtml = html.replace(/<!--[\s\S]*?-->/g, '');
const scripts = [...executableHtml.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).filter(s => s.trim());

class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.events = new Map();
    this.style = {}; this.dataset = {}; this.attributes = {}; this.value = '';
    this.disabled = false; this._text = '';
  }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(n => n.textContent).join(''); }
  appendChild(node) { node.parent = this; this.children.push(node); return node; }
  removeChild(node) { this.children.splice(this.children.indexOf(node), 1); node.parent = null; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener(name, action) { this.events.set(name, [...(this.events.get(name) || []), action]); }
  async emit(name) { for (const action of this.events.get(name) || []) await action({ preventDefault() {} }); }
  matches(selector) {
    if (selector === '[data-db-only]') return this.dataset.dbOnly === '1';
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    const [tag, cls] = selector.split('.');
    return (!tag || this.tag === tag) && (!cls || (this.className || '').split(' ').includes(cls));
  }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [
      ...(selector.split(',').some(s => child.matches(s.trim())) ? [child] : []),
      ...child.querySelectorAll(selector),
    ]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return this.matches(selector) ? this : this.parent?.closest(selector) || null; }
  focus() { this.focused = true; }
  scrollIntoView() {}
  select() {}
}

async function page({ mode = '', result = { ok: true }, failure = false, values = {} } = {}) {
  const body = new Element('body');
  // IDs come from real markup: deleting a required container makes boot fail.
  for (const [, tag, id] of html.matchAll(/<([a-z][\w-]*)\b[^>]*\bid="([^"]+)"/g)) {
    const node = new Element(tag); node.id = id; body.appendChild(node);
  }
  const heading = body.appendChild(new Element('h1'));
  const document = {
    body, getElementById: id => body.querySelector('#' + id),
    createElement: tag => new Element(tag),
    createTextNode: text => { const node = new Element('#text'); node.textContent = text; return node; },
    querySelector: selector => selector === 'h1' ? heading : body.querySelector(selector),
  };
  const calls = []; const copied = [];
  const info = {
    fields: config.SETUP_FIELDS.map(field => ({ ...field, generate: undefined, generatable: typeof field.generate === 'function' })),
    values: { ...config.defaultValues(), ...values }, currentBackend: 'sqlite',
  };
  async function submit(kind, payload) {
    calls.push({ kind, payload });
    if (failure) throw new Error('test IPC failure');
    return result;
  }
  const api = {
    getFields: async () => info, switchFields: async () => info,
    generate: async key => { calls.push({ kind: 'generate', key }); return 'generated-secret-' + 'a'.repeat(32); },
    save: payload => submit('save', payload), switchBackend: payload => submit('switch', payload),
    test: payload => submit('test', payload), openConfigDir() {},
  };
  const context = vm.createContext({
    document, window: { yunmengSetup: api, HneSetupValidate: validate },
    location: { search: mode ? '?mode=' + mode : '' }, URLSearchParams,
    navigator: { clipboard: { writeText: async text => copied.push(text) } },
    setTimeout() {}, setInterval() { return 1; }, clearInterval() {},
  });
  for (const source of scripts) await vm.runInContext(source, context, { filename: 'setup.html' });
  return { document, calls, copied, get: id => document.getElementById(id) };
}

test('setup renders secret controls and generate/show/copy events use the displayed value', async () => {
  const ui = await page();
  const input = ui.get('f-HNE_SECRET_KEY');
  assert.ok(input);
  assert.equal(input.type, 'password');
  const row = input.closest('.row');
  const buttons = row.querySelectorAll('button');
  await buttons.find(b => b.textContent === '随机生成').emit('click');
  assert.equal(input.value, 'generated-secret-' + 'a'.repeat(32));
  assert.equal(input.type, 'text');
  await buttons.find(b => b.className.includes('eye')).emit('click');
  assert.equal(input.type, 'password');
  await buttons.find(b => b.textContent === '复制').emit('click');
  assert.deepEqual(ui.copied, [input.value]);
});

test('setup keeps the data directory available while changing the storage mode', async () => {
  const ui = await page();
  assert.ok(ui.get('datadir-grid').querySelector('#f-HNE_DATA_DIR'));
  assert.equal(ui.get('mysql-fieldset').style.display, 'none');
  const backend = ui.get('f-HNE_DB_BACKEND');
  backend.value = 'mysql'; await backend.emit('change');
  assert.equal(ui.get('mysql-fieldset').style.display, '');
  assert.equal(ui.get('f-HNE_MYSQL_HOST').disabled, false);
  assert.equal(ui.get('f-HNE_DATA_DIR').disabled, false);
});

for (const [mode, kind] of [['', 'save'], ['switch', 'switch']]) {
  test('setup submits ' + kind + ' with the edited configuration', async () => {
    const ui = await page({ mode });
    ui.get('f-HNE_DATA_DIR').value = 'D:/My Data';
    await ui.get('form').emit('submit');
    assert.equal(ui.calls.length, 1);
    assert.equal(ui.calls[0].kind, kind);
    assert.equal(ui.calls[0].payload.HNE_DATA_DIR, 'D:/My Data');
    assert.match(ui.get('status').className, /ok/);
  });
}

for (const failure of [false, true]) {
  test('setup reports ' + (failure ? 'IPC rejection' : 'failed validation') + ' and restores buttons', async () => {
    const ui = await page({ mode: 'switch', failure, result: { ok: false, message: 'test refused' } });
    await ui.get('form').emit('submit');
    assert.equal(ui.calls.length, 1);
    assert.equal(ui.get('btn-save').disabled, false);
    assert.equal(ui.get('btn-test').disabled, false);
    assert.match(ui.get('status').className, /err/);
    assert.match(ui.get('status').textContent, failure ? /test IPC failure/ : /test refused/);
  });
}

test('setup refuses incomplete MySQL input before calling IPC', async () => {
  const ui = await page({ values: { HNE_DB_BACKEND: 'mysql', HNE_MYSQL_PASSWORD: '' } });
  await ui.get('form').emit('submit');
  assert.deepEqual(ui.calls, []);
  assert.match(ui.get('status').className, /err/);
  assert.ok(ui.get('e-HNE_MYSQL_PASSWORD').textContent);
});
