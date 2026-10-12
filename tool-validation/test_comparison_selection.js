/* Synthetic DOM test of the actual comparison-page script; no browser or game. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const sourcePath = path.resolve(__dirname, '..', 'tool', 'ac8compare.py');
const source = fs.readFileSync(sourcePath, 'utf8');
const htmlMatch = source.match(/_HTML\s*=\s*r'''([\s\S]*?)'''/);
assert(htmlMatch, 'Find the real Python _HTML template');
const html = htmlMatch[1];
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
assert(scriptMatch, 'Find the real executable embedded JavaScript');
const script = scriptMatch[1];

class Element {
  constructor(tag, document) {
    this.tagName = tag.toUpperCase(); this.ownerDocument = document;
    this.children = []; this.parentElement = null; this.dataset = {};
    this.style = {}; this.attributes = {}; this.listeners = new Map();
    this.id = ''; this.className = ''; this.value = ''; this.type = '';
    this.checked = false; this.indeterminate = false; this.disabled = false;
    this.hidden = false; this.readOnly = false; this._text = '';
    this.classList = {
      add: (...names) => { this.className = [...new Set([...this.className.split(/\s+/), ...names])].filter(Boolean).join(' '); },
      remove: (...names) => { this.className = this.className.split(/\s+/).filter(name => !names.includes(name)).join(' '); },
      contains: name => this.className.split(/\s+/).includes(name),
      toggle: (name, force) => {
        const present = this.classList.contains(name);
        const wanted = force === undefined ? !present : Boolean(force);
        if (wanted) this.classList.add(name); else this.classList.remove(name);
        return wanted;
      },
    };
  }
  set textContent(value) { this._text = String(value); this.replaceChildren(); }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  get options() { return this.children.filter(child => child.tagName === 'OPTION'); }
  append(...nodes) {
    for (let node of nodes) {
      if (typeof node === 'string') { const text = new Element('#text', this.ownerDocument); text.textContent = node; node = text; }
      if (node.tagName === '#FRAGMENT') { this.append(...node.children.slice()); continue; }
      node.remove(); node.parentElement = this; this.children.push(node);
    }
  }
  appendChild(node) { this.append(node); return node; }
  replaceChildren(...nodes) { for (const child of this.children) child.parentElement = null; this.children = []; this.append(...nodes); }
  removeChild(node) { assert.equal(node.parentElement, this); node.remove(); return node; }
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this); this.parentElement = null; }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = String(value);
    else if (name === 'readonly') this.readOnly = true;
    else if (['id', 'type', 'value'].includes(name)) this[name] = String(value);
  }
  getAttribute(name) {
    if (name.startsWith('data-')) return this.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] ?? null;
    return this.attributes[name] ?? this[name] ?? null;
  }
  addEventListener(type, callback) { const list = this.listeners.get(type) || []; list.push(callback); this.listeners.set(type, list); }
  matches(selector) {
    selector = selector.trim();
    const tag = selector.match(/^[a-z]+/i); if (tag && this.tagName !== tag[0].toUpperCase()) return false;
    const id = selector.match(/#([\w-]+)/); if (id && this.id !== id[1]) return false;
    const className = selector.match(/\.([\w-]+)/); if (className && !this.classList.contains(className[1])) return false;
    for (const match of selector.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g)) {
      const value = this.getAttribute(match[1]);
      if (value === null || (match[2] !== undefined && value !== match[2])) return false;
    }
    if (selector.includes(':checked') && !this.checked) return false;
    return true;
  }
  querySelectorAll(selector) {
    const result = [];
    const visit = node => { for (const child of node.children) { if (selector.split(',').some(part => child.matches(part))) result.push(child); visit(child); } };
    visit(this); return result;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node; return null; }
  focus() { this.ownerDocument.activeElement = this; }
  select() { this.selectionStart = 0; this.selectionEnd = this.value.length; }
}

function fixture() {
  const row = (short_id, domain, index, label) => ({
    short_id, domain, index, target: null, key: null, entry_id: 'hash-' + short_id,
    text_ids_ascii: ['fixture-' + label], status: 'different', format_source: 'ja',
    format_problems: [], selected_for_translation: true, excluded_policy: 'official',
    ja: '日本語 ' + label, en: 'English ' + label, official_zh: '官中 ' + label,
    translation: '自译 ' + label,
  });
  return {
    buildid: 'synthetic', official_column_visible: true,
    counts: {total_slots: 5, selected_slots: 5, excluded_slots: 0, filled_slots: 5,
      missing_slots: 0, format_issue_slots: 0, same_as_official_slots: 0, different_from_official_slots: 5},
    rows: [row(8, 'CP', 0, 'Alpha'), row(8, 'CP', 1, 'Alpha'),
      row(13, 'ON', 0, 'Beta'), row(3, 'CP', 2, 'Gamma'), row(21, 'ON', 1, 'Delta')],
  };
}

function page(clipboardMode = 'success', execSuccess = true) {
  const document = {
    activeElement: null, createElement: tag => new Element(tag, document),
    createDocumentFragment: () => new Element('#fragment', document),
    createTextNode: text => { const node = new Element('#text', document); node.textContent = text; return node; },
    getElementById: id => document.body.querySelector('#' + id),
    querySelectorAll: selector => document.body.querySelectorAll(selector),
    querySelector: selector => document.body.querySelector(selector),
  };
  document.body = new Element('body', document);
  for (const match of html.matchAll(/<([a-z]+)\b([^>]*\bid="([^"]+)"[^>]*)>/gi)) {
    const element = document.createElement(match[1]); element.id = match[3];
    for (const attribute of match[2].matchAll(/([\w-]+)="([^"]*)"/g)) element.setAttribute(attribute[1], attribute[2]);
    element.hidden = /(?:^|\s)hidden(?:\s|$)/.test(match[2]);
    element.readOnly = /(?:^|\s)readonly(?:\s|$)/.test(match[2]);
    document.body.append(element);
  }
  const get = id => { const node = document.getElementById(id); assert(node, 'Missing control #' + id); return node; };
  get('comparison-data').textContent = JSON.stringify(fixture());
  for (const id of ['status', 'domain', 'scope']) get(id).value = 'all';
  get('size').value = '2'; get('page').value = '1';
  const writes = [], copied = [], timers = new Map(); let timerId = 0;
  document.execCommand = command => {
    assert.equal(command, 'copy'); copied.push(document.activeElement && document.activeElement.value); return execSuccess;
  };
  const navigator = {};
  if (clipboardMode !== 'absent') navigator.clipboard = {writeText: async text => {
    writes.push(text); if (clipboardMode === 'reject') throw new Error('Synthetic clipboard denial');
  }};
  const context = vm.createContext({document, navigator, console, isSecureContext: true,
    setTimeout: callback => { const id = ++timerId; timers.set(id, callback); return id; },
    clearTimeout: id => timers.delete(id),
  });
  context.window = context;
  vm.runInContext(script, context, {filename: sourcePath + ':embedded-script', timeout: 3000});
  const dispatch = async (node, type) => {
    if (type === 'click' && node.disabled) return;
    const event = {target: node, currentTarget: node, preventDefault() {}, stopPropagation() { this.stopped = true; }};
    for (let current = node; current && !event.stopped; current = current.parentElement) {
      event.currentTarget = current;
      const callbacks = [...(current.listeners.get(type) || [])];
      if (typeof current['on' + type] === 'function') callbacks.push(current['on' + type]);
      for (const callback of callbacks) await callback(event);
    }
    for (const [id, callback] of [...timers]) { timers.delete(id); callback(); }
    await new Promise(resolve => setImmediate(resolve));
  };
  const boxes = () => get('body').querySelectorAll('input[data-select-id]');
  const count = expected => assert.match(get('selection-count').textContent, new RegExp('(^|\\D)' + expected + '(\\D|$)'), 'Selection count must count unique IDs');
  const selectFirst = async () => { const box = boxes()[0]; assert(box); box.checked = true; await dispatch(box, 'change'); };
  return {get, boxes, dispatch, writes, copied, count, selectFirst};
}

async function run() {
  const view = page();
  view.count(0); assert.equal(view.boxes().length, 2);
  await view.selectFirst();
  assert(view.boxes().every(box => box.checked), 'Duplicate slots of ID 8 must synchronize');
  view.count(1); assert.equal(view.get('select-page').checked, true);
  view.boxes()[1].checked = false; await view.dispatch(view.boxes()[1], 'change');
  assert(view.boxes().every(box => !box.checked), 'Duplicate slots must synchronize deselection too');
  view.count(0); await view.selectFirst();
  await view.dispatch(view.get('next'), 'click');
  assert.deepEqual(view.boxes().map(box => Number(box.dataset.selectId)), [13, 3]);
  assert(view.boxes().every(box => !box.checked)); view.count(1);
  view.get('select-page').checked = true; await view.dispatch(view.get('select-page'), 'change');
  assert(view.boxes().every(box => box.checked)); view.count(3);
  view.boxes()[0].checked = false; await view.dispatch(view.boxes()[0], 'change');
  assert.equal(view.get('select-page').indeterminate, true, 'Mixed page selection must show indeterminate');
  view.count(2);
  view.get('select-page').checked = false; await view.dispatch(view.get('select-page'), 'change');
  assert(view.boxes().every(box => !box.checked)); view.count(1);
  view.get('select-page').checked = true; await view.dispatch(view.get('select-page'), 'change');
  view.count(3);
  await view.dispatch(view.get('next'), 'click');
  assert.deepEqual(view.boxes().map(box => Number(box.dataset.selectId)), [21]);
  assert.equal(view.boxes()[0].checked, false, 'Page select-all must not select unseen IDs');
  view.get('domain').value = 'CP'; await view.dispatch(view.get('domain'), 'change');
  assert(view.boxes().every(box => box.checked), 'Filtering must retain selected IDs'); view.count(3);
  view.get('search').value = 'no matching synthetic row'; await view.dispatch(view.get('search'), 'input');
  assert.equal(view.boxes().length, 0); view.count(3);
  await view.dispatch(view.get('copy-ids'), 'click');
  assert.deepEqual(view.writes, ['[3,8,13]'], 'Copy numeric short IDs, sorted and deduplicated, even when filtered out');
  assert.deepEqual(view.copied, [], 'Successful clipboard needs no fallback');
  await view.dispatch(view.get('clear-selection'), 'click'); view.count(0);
  assert.doesNotMatch(view.get('copy-status').textContent, /已复制|复制成功/, 'Clear must remove stale copy success');
  await view.dispatch(view.get('copy-ids'), 'click');
  assert.equal(view.writes.length, 1, 'Empty selection must not copy or report a new success');
  assert.doesNotMatch(view.get('copy-status').textContent, /已复制|复制成功/, 'Empty selection must not claim success');
  view.get('search').value = ''; await view.dispatch(view.get('search'), 'input');
  assert(view.boxes().every(box => !box.checked), 'Clear must remove selection on hidden pages too');
  view.get('domain').value = 'all'; await view.dispatch(view.get('domain'), 'change');
  await view.dispatch(view.get('next'), 'click');
  assert(view.boxes().every(box => !box.checked), 'Previously selected page must be clear');

  for (const mode of ['reject', 'absent']) {
    const fallback = page(mode, true); await fallback.selectFirst();
    await fallback.dispatch(fallback.get('copy-ids'), 'click');
    assert.deepEqual(fallback.copied, ['[8]'], 'Fallback must copy the actual numeric ID JSON');
    assert.match(fallback.get('copy-status').textContent, /已复制|复制成功/, 'Report success only after successful fallback');
  }
  const manual = page('reject', false); await manual.selectFirst();
  await manual.dispatch(manual.get('copy-ids'), 'click');
  assert.deepEqual(manual.copied, ['[8]']);
  const textarea = manual.get('manual-ids');
  assert.equal(textarea.value, '[8]'); assert.equal(textarea.readOnly, true);
  assert.equal(textarea.hidden, false); assert.notEqual(textarea.style.display, 'none');
  assert.match(manual.get('copy-status').textContent, /Ctrl\s*\+\s*C/i);
  assert.doesNotMatch(manual.get('copy-status').textContent, /已复制|复制成功/, 'Both failures must not claim success');
  console.log(JSON.stringify({ok: true, synthetic_rows: 5,
    checks: ['duplicate slots', 'page-only selection', 'cross-page persistence', 'filter persistence',
      'numeric sorted unique ID copy', 'clear hidden selection', 'empty copy', 'clipboard success',
      'clipboard rejection and absence fallback', 'manual readonly fallback without false success'],
    browser_opened: false, game_accessed: false, user_translation_read: false}));
}

run().catch(error => { console.error(error); process.exitCode = 1; });
