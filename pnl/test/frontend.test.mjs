import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { runInNewContext } from 'node:vm';
import { createPnlServer } from '../server.mjs';
import { digest } from '../lib/schema.mjs';
import { instructionFixture, modelResearchFixture } from './model-fixture.mjs';

// These tests execute the shipped browser entry, without exporting or replacing
// its private functions. The small DOM models state/events only: it deliberately
// makes no claim about CSS layout, pixel rendering, or browser accessibility APIs.
const [html, script] = await Promise.all([
  readFile(new URL('../public/index.html', import.meta.url), 'utf8'),
  readFile(new URL('../public/app.js', import.meta.url), 'utf8'),
]);
const decode = value => value.replace(/&(#x[\da-f]+|#\d+|amp|lt|gt|quot|apos|nbsp);/gi, (_whole, code) =>
  code[0] === '#' ? String.fromCodePoint(parseInt(code.slice(code[1]?.toLowerCase() === 'x' ? 2 : 1), code[1]?.toLowerCase() === 'x' ? 16 : 10))
    : ({ amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0' })[code.toLowerCase()]);

class TestNode {
  constructor(tag, owner, nodeType = 1) {
    this.tagName = tag.toUpperCase(); this.ownerDocument = owner;
    this.nodeType = nodeType; this.childNodes = []; this.attributes = new Map();
    this.listeners = new Map(); this.parentNode = null; this._text = '';
    this.style = {}; this.clientWidth = 340;
    this.dataset = new Proxy({}, {
      get: (_target, key) => this.getAttribute(`data-${String(key).replace(/[A-Z]/g, letter => `-${letter.toLowerCase()}`)}`) ?? undefined,
      set: (_target, key, value) => { this.setAttribute(`data-${String(key).replace(/[A-Z]/g, letter => `-${letter.toLowerCase()}`)}`, value); return true; },
    });
    this.classList = {
      contains: name => this.className.split(/\s+/).includes(name),
      toggle: (name, force) => {
        const names = new Set(this.className.split(/\s+/).filter(Boolean));
        const add = force === undefined ? !names.has(name) : force;
        if (add) names.add(name); else names.delete(name);
        this.className = [...names].join(' '); return add;
      },
      add: (...names) => { for (const name of names) this.classList.toggle(name, true); },
      remove: (...names) => { for (const name of names) this.classList.toggle(name, false); },
    };
  }
  get children() { return this.childNodes.filter(node => node.nodeType === 1); }
  get firstChild() { return this.childNodes[0] ?? null; }
  get parentElement() { return this.parentNode?.nodeType === 1 ? this.parentNode : null; }
  get id() { return this.getAttribute('id') ?? ''; }
  set id(value) { this.setAttribute('id', value); }
  get className() { return this.getAttribute('class') ?? ''; }
  set className(value) { this.setAttribute('class', value); }
  get hidden() { return this.hasAttribute('hidden'); }
  set hidden(value) { if (value) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); }
  get disabled() { return this.hasAttribute('disabled'); }
  set disabled(value) { if (value) this.setAttribute('disabled', ''); else this.removeAttribute('disabled'); }
  get tabIndex() { return Number(this.getAttribute('tabindex') ?? -1); }
  set tabIndex(value) { this.setAttribute('tabindex', value); }
  get href() { return this.getAttribute('href') ?? ''; }
  set href(value) { this.setAttribute('href', value); }
  get textContent() { return this.nodeType === 3 ? this._text : this.childNodes.map(child => child.textContent).join(''); }
  set textContent(value) {
    this.childNodes = []; this._text = String(value ?? '');
    if (this.nodeType !== 3 && this._text) this.append(this.ownerDocument.createTextNode(this._text));
  }
  set innerHTML(_value) { throw new Error('Dynamic HTML insertion is not permitted in this public-data view'); }
  get innerHTML() { return this.textContent; }
  setAttribute(key, value) { this.attributes.set(key, String(value)); }
  getAttribute(key) { return this.attributes.get(key) ?? null; }
  hasAttribute(key) { return this.attributes.has(key); }
  removeAttribute(key) { this.attributes.delete(key); }
  append(...values) {
    for (let node of values) {
      if (typeof node === 'string') node = this.ownerDocument.createTextNode(node);
      if (node.nodeType === 11) { this.append(...node.childNodes.slice()); continue; }
      if (node.parentNode) node.parentNode.childNodes = node.parentNode.childNodes.filter(child => child !== node);
      node.parentNode = this; this.childNodes.push(node);
    }
  }
  appendChild(node) { this.append(node); return node; }
  replaceChildren(...nodes) { for (const child of this.childNodes) child.parentNode = null; this.childNodes = []; this.append(...nodes); }
  matches(selector) {
    if (selector.includes(',')) return selector.split(',').some(part => this.matches(part.trim()));
    let rest = selector.trim();
    const tag = rest.match(/^[\w-]+/);
    if (tag) { if (this.tagName.toLowerCase() !== tag[0].toLowerCase()) return false; rest = rest.slice(tag[0].length); }
    const tokens = [...rest.matchAll(/([.#])([\w-]+)|\[([\w-]+)(?:([\^$*]?=)["']?([^\]"']*)["']?)?\]/g)];
    if (!tag && !tokens.length) throw new Error(`Unsupported test selector: ${selector}`);
    return tokens.every(([, prefix, name, attr, op, expected]) => {
      if (prefix === '#') return this.id === name;
      if (prefix === '.') return this.classList.contains(name);
      const actual = this.getAttribute(attr);
      return op === undefined ? actual !== null : actual !== null && (op === '=' ? actual === expected : op === '^=' ? actual.startsWith(expected) : op === '$=' ? actual.endsWith(expected) : actual.includes(expected));
    });
  }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
  closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) ?? null; }
  addEventListener(type, callback) { const callbacks = this.listeners.get(type) ?? []; callbacks.push(callback); this.listeners.set(type, callbacks); }
  dispatchEvent(event) {
    event.target ??= this; event.currentTarget = this;
    event.preventDefault ??= () => { event.defaultPrevented = true; };
    for (const callback of this.listeners.get(event.type) ?? []) callback(event);
    if (event.bubbles !== false && !event.cancelBubble) this.parentNode?.dispatchEvent(event);
    return !event.defaultPrevented;
  }
  click() { if (!this.disabled) this.dispatchEvent({ type: 'click' }); }
  focus() { this.ownerDocument.activeElement = this; }
  scrollIntoView() {}
}

function testDocument(markup, width) {
  const document = new TestNode('#document', null, 9); document.ownerDocument = document;
  document.createElement = tag => new TestNode(tag, document);
  document.createElementNS = (_namespace, tag) => document.createElement(tag);
  document.createTextNode = value => { const node = new TestNode('#text', document, 3); node._text = String(value); return node; };
  document.createDocumentFragment = () => new TestNode('#fragment', document, 11);
  document.getElementById = id => document.querySelectorAll('[id]').find(node => node.id === id) ?? null;
  document.hidden = false; document.activeElement = null;
  const stack = [document]; const voids = new Set(['meta', 'link', 'br', 'hr', 'img', 'input', 'source', 'wbr']);
  for (const match of markup.matchAll(/<!--[\s\S]*?-->|<![^>]*>|<\/?[a-zA-Z][^>]*>|[^<]+/g)) {
    const part = match[0];
    if (part.startsWith('<!')) continue;
    if (part.startsWith('</')) { if (stack.length > 1) stack.pop(); continue; }
    if (!part.startsWith('<')) { stack.at(-1).append(document.createTextNode(decode(part))); continue; }
    const tag = part.match(/^<([\w-]+)/)[1].toLowerCase(); const node = document.createElement(tag);
    for (const [, key, double, single, bare] of part.slice(tag.length + 1, -1).matchAll(/([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) node.setAttribute(key, decode(double ?? single ?? bare ?? ''));
    node.clientWidth = Math.min(width - 40, 1100); stack.at(-1).append(node);
    if (!voids.has(tag) && !part.endsWith('/>')) stack.push(node);
  }
  document.body = document.querySelector('body'); document.documentElement = document.querySelector('html');
  return document;
}

const settle = async () => { for (let turn = 0; turn < 8; turn++) await new Promise(resolve => setImmediate(resolve)); };
async function page(payload, { width = 1440, hash = '', now = '2026-01-03T22:00:00Z' } = {}) {
  const document = testDocument(html, width); const listeners = new Map(); const queue = [payload]; const requests = [];
  const fixed = Date.parse(now); class FixedDate extends Date { constructor(...args) { super(...(args.length ? args : [fixed])); } static now() { return fixed; } }
  const window = {
    document, innerWidth: width, location: { hash, pathname: '/', search: '' },
    history: { replaceState: (_state, _unused, target) => { window.location.hash = String(target).includes('#') ? `#${String(target).split('#')[1]}` : ''; } },
    addEventListener: (name, callback) => { const list = listeners.get(name) ?? []; list.push(callback); listeners.set(name, list); },
    matchMedia: () => ({ matches: width <= 740 }), setTimeout, clearTimeout,
    setInterval: () => 1, clearInterval: () => {}, scrollTo: () => {},
    requestAnimationFrame: callback => { queueMicrotask(() => callback(fixed)); return 1; },
  };
  const fetcher = async (url, options) => {
    requests.push({ url, options }); const next = await queue.shift();
    if (next instanceof Error) throw next;
    if (next?.httpStatus) return { ok: false, status: next.httpStatus, json: async () => ({}) };
    assert.ok(next, 'Unexpected duplicate dashboard fetch');
    return { ok: true, status: 200, json: async () => structuredClone(next) };
  };
  runInNewContext(script, { document, window, location: window.location, history: window.history, fetch: fetcher, Date: FixedDate, URL, Intl, AbortController, console }, { filename: 'public/app.js', timeout: 2000 });
  await settle();
  return {
    document, window, requests, byId: id => document.getElementById(id),
    all: selector => document.querySelectorAll(selector),
    async reload(next) { queue.push(next); for (const callback of listeners.get('online') ?? []) callback(); await settle(); },
    async hash(value) { window.location.hash = value; for (const callback of listeners.get('hashchange') ?? []) callback(); await settle(); },
  };
}

function account(index = 0) {
  return {
    schemaVersion: 1, idempotencyKey: `frontend-account-test-${index}`,
    asOf: `2026-01-${String(index + 2).padStart(2, '0')}T21:00:00Z`,
    account: { label: 'SYNTHETIC FRONTEND TEST ONLY', currency: 'USD', nav: 100000 + index * 1000, cash: 98000 + index * 1000 },
    positions: [{ symbol: 'AAPL', name: 'SYNTHETIC TEST ASSET', quantity: 20, avgCost: 90, price: 100, marketValue: 2000 }],
    trades: [], decisions: [], netExternalFlow: 0,
    provenance: { kind: 'operator_attested', evidenceSha256: digest(`SYNTHETIC FRONTEND TEST EVIDENCE ${index}`), sourceRef: 'frontend-unit-test-only', attestedBy: 'unit-test-operator-only' },
  };
}
async function api(t, { records = 0, research = false } = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'tiger-frontend-test-'));
  const app = await createPnlServer({ dataDir: directory, token: 'unit-test-only-not-a-production-credential-000000' });
  await new Promise(resolve => app.server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { await app.close(); await rm(directory, { recursive: true, force: true }); });
  for (let index = 0; index < records; index++) await app.ledger.append(account(index));
  if (research) await app.researchLedger.append(modelResearchFixture({ executionDecisions: [instructionFixture(), instructionFixture({ symbol: 'MSFT', action: 'buy' })] }));
  const read = async () => {
    const response = await fetch(`http://127.0.0.1:${app.server.address().port}/api/public/dashboard`);
    assert.equal(response.status, 200); return response.json();
  };
  return { ...app, read, data: await read() };
}

test('empty real API keeps account and research empty without invented performance or zero-price orders', async t => {
  const { data } = await api(t); const view = await page(data);
  assert.equal(view.byId('metric-nav').textContent, '—');
  assert.equal(view.byId('metric-return').textContent, '—');
  assert.match(view.byId('account-status').textContent, /待首笔/);
  assert.match(view.byId('instruction-status').textContent, /等待/);
  assert.equal(view.all('.instruction-entry').length, 0);
  assert.equal(view.byId('performance').getAttribute('aria-busy'), 'false');
  assert.equal(view.byId('connection-notice').hidden, true);
});

test('research-only API renders verified plans while account metrics still await genuine records', async t => {
  const { data } = await api(t, { research: true }); const view = await page(data);
  assert.equal(data.latest, null); assert.equal(view.byId('metric-nav').textContent, '—');
  assert.match(view.byId('instruction-status').textContent, /已核验/);
  assert.match(view.byId('decision-quicklist').textContent, /AAPL/);
  assert.match(view.byId('decision-quicklist').textContent, /MSFT/);
  assert.equal(view.byId('stock-selector').querySelectorAll('[role="tab"]').length, 2);
  assert.match(view.byId('decisions-list').textContent, /行情依据|行情快照/);
  assert.doesNotMatch(view.byId('decisions-list').textContent, /入场参考\$0\.00/);
  assert.match(view.byId('hero-account-time').textContent, /待|暂无|尚未/);
});

test('a new research generation changes its own clock without advancing genuine account NAV or its clock', async t => {
  const { data } = await api(t, { records: 1, research: true }); const view = await page(data);
  const before = { nav: view.byId('metric-nav').textContent, time: view.byId('hero-account-time').textContent, history: view.byId('history-body').textContent };
  const next = structuredClone(data); next.research.latest.asOf = '2026-01-04T21:00:00Z';
  next.research.latest.runId = 'new-synthetic-research-only'; next.research.history.unshift(structuredClone(next.research.latest));
  await view.reload(next);
  assert.equal(view.byId('metric-nav').textContent, before.nav);
  assert.equal(view.byId('hero-account-time').textContent, before.time);
  assert.equal(view.byId('history-body').textContent, before.history);
  assert.match(view.byId('hero-research-time').textContent, /2026/);
  assert.notEqual(view.byId('hero-research-time').textContent, view.byId('hero-account-time').textContent);
});

test('failed refresh retracts current instructions, preserves timestamped account facts, and recovers on the next good read', async t => {
  const { data } = await api(t, { records: 1, research: true }); const view = await page(data);
  const nav = view.byId('metric-nav').textContent; const clock = view.byId('as-of').textContent;
  await view.reload(new Error('synthetic network failure'));
  assert.equal(view.byId('metric-nav').textContent, nav); assert.equal(view.byId('as-of').textContent, clock);
  assert.equal(view.all('.instruction-entry').length, 0);
  assert.equal(view.byId('instruction-summary').hidden, true);
  assert.equal(view.byId('connection-notice').hidden, false);
  assert.match(view.byId('instruction-status').textContent, /不可核验/);
  assert.match(view.byId('legacy-research-body').textContent, /未核验|上次读取/);
  await view.reload(data);
  assert.equal(view.byId('connection-notice').hidden, true);
  assert.match(view.byId('instruction-status').textContent, /已核验/);
});

test('initial HTTP or malformed responses render an honest error state instead of a successful empty account', async t => {
  const { data } = await api(t);
  for (const invalid of [{ httpStatus: 503 }, { ...data, status: 'invented' }, { ...data, latest: { account: { nav: 5000000 } } }]) {
    const view = await page(invalid);
    assert.equal(view.byId('connection-notice').hidden, false);
    assert.equal(view.byId('metric-nav').textContent, '—');
    assert.match(view.byId('account-status').textContent, /不可用/);
    assert.equal(view.byId('performance').getAttribute('aria-busy'), 'false');
  }
});

test('main and research tabs have one selected control, linked visible panels, and keyboard movement', async t => {
  const { data } = await api(t, { research: true }); const view = await page(data);
  const assertGroup = (group, expected) => {
    const tabs = view.all(`[data-tab-group="${group}"]`); const selected = tabs.filter(tab => tab.getAttribute('aria-selected') === 'true');
    assert.equal(selected.length, 1); assert.equal(selected[0].dataset.tab, expected);
    assert.equal(selected[0].tabIndex, 0);
    for (const tab of tabs) {
      const panel = view.byId(tab.getAttribute('aria-controls')); assert.ok(panel, `${group}: linked panel missing`);
      assert.equal(panel.hidden, tab !== selected[0]); assert.equal(panel.getAttribute('aria-labelledby'), tab.id);
      if (tab !== selected[0]) assert.equal(tab.tabIndex, -1);
    }
  };
  assertGroup('main', 'research');
  view.byId('main-tab-research').dispatchEvent({ type: 'keydown', key: 'ArrowRight' });
  assertGroup('main', 'account'); assert.equal(view.document.activeElement, view.byId('main-tab-account'));
  view.byId('main-tab-account').dispatchEvent({ type: 'keydown', key: 'End' }); assertGroup('main', 'method');
  view.byId('main-tab-method').dispatchEvent({ type: 'keydown', key: 'Home' }); assertGroup('main', 'research');
  view.byId('research-tab-history').click(); assertGroup('research', 'history');
  view.byId('research-tab-history').dispatchEvent({ type: 'keydown', key: 'ArrowRight' }); assertGroup('research', 'overview');
});

test('old deep links reveal the correct parent panel and stock selection reveals only its own card', async t => {
  const { data } = await api(t, { records: 1, research: true }); const view = await page(data, { hash: '#whitepaper' });
  assert.equal(view.byId('main-panel-method').hidden, false);
  await view.hash('#positions'); assert.equal(view.byId('main-panel-account').hidden, false);
  await view.hash('#research'); assert.equal(view.byId('main-panel-research').hidden, false);
  view.byId('research-tab-stocks').click();
  const stocks = view.byId('stock-selector').querySelectorAll('[role="tab"]');
  stocks[1].click();
  assert.equal(stocks[1].getAttribute('aria-selected'), 'true');
  assert.equal(stocks[0].getAttribute('aria-selected'), 'false');
  const visible = view.all('.instruction-entry').filter(node => !node.hidden);
  assert.equal(visible.length, 1); assert.match(visible[0].textContent, /MSFT/);
  assert.match(visible[0].textContent, /\$100\.00/);
});

test('account history expansion and seven-day range use real observations without discarding the full ledger', async t => {
  const { data } = await api(t, { records: 9 }); const view = await page(data);
  assert.equal(view.byId('history-body').children.length, 7);
  view.byId('history-toggle').click(); assert.equal(view.byId('history-body').children.length, 9);
  assert.equal(view.byId('history-toggle').getAttribute('aria-expanded'), 'true');
  view.all('[data-range]').find(node => node.dataset.range === '7').click();
  assert.match(view.byId('performance-chart').getAttribute('aria-label'), /7 笔/);
  assert.equal(view.byId('history-body').children.length, 9);
  view.byId('history-toggle').click(); assert.equal(view.byId('history-body').children.length, 7);
});

test('unqualified old research remains inspectable while expired verified plans are visibly historical', async t => {
  const { data } = await api(t, { research: true }); const old = structuredClone(data.research.latest);
  old.schemaVersion = 1; old.modelMode = 'disabled'; old.runId = 'synthetic-disabled-history'; delete old.model; delete old.executionDecisions;
  const view = await page({ ...data, research: { latest: old, history: [old, data.research.latest] } });
  assert.equal(view.all('.instruction-entry').length, 0); assert.match(view.byId('legacy-research-body').textContent, /没有启用模型/);
  assert.match(view.byId('legacy-research-body').textContent, /AAPL/);
  const expired = await page(data, { now: '2026-01-07T22:00:00Z' });
  assert.match(expired.byId('instruction-status').textContent, /已过有效期/);
  assert.match(expired.byId('instruction-summary').textContent, /当时|已过/);
});

test('public strings render as text and unsafe source schemes never become clickable evidence', async t => {
  const { data } = await api(t, { records: 1, research: true });
  const malicious = '<img src=x onerror=globalThis.injected=true>';
  const textData = structuredClone(data); textData.latest.positions[0].name = malicious;
  textData.research.latest.summary = malicious; textData.research.latest.executionDecisions[0].why_this = malicious;
  const textView = await page(textData);
  assert.match(textView.byId('positions-body').textContent, /<img/);
  assert.match(textView.byId('instruction-summary').textContent, /<img/);
  assert.equal(textView.all('img').length, 0);
  for (const url of ['javascript:alert(1)', 'data:text/html,bad', 'https://username:password@example.com/leak', 'https://example.com/article?api_key=must-not-leave', 'https://example.com/article#credential']) {
    const unsafe = structuredClone(data); unsafe.research.latest.executionDecisions[0].sources[0].url = url;
    const view = await page(unsafe);
    assert.equal(view.all('.instruction-entry').length, 0);
    assert.equal(view.all('a').some(link => link.href === url), false);
  }
});

test('incomplete model proof and mismatched stock snapshots fail closed while independent account facts stay readable', async t => {
  const { data } = await api(t, { records: 1, research: true });
  const mutations = [
    research => { research.model.response_ids[1] = research.model.response_ids[0]; },
    research => { research.model.stage_counts['tech.risk'] = 0; },
    research => { research.model.search_count = 13; },
    research => { research.executionDecisions[0].market_evidence[0].symbol = 'MSFT'; },
    research => { research.executionDecisions[0].market_evidence[0].series_sha256 = '0'.repeat(64); },
    research => { research.executionDecisions[0].market_evidence[0].as_of = '2026-02-30'; },
  ];
  for (const mutate of mutations) {
    const invalid = structuredClone(data); mutate(invalid.research.latest);
    const view = await page(invalid);
    assert.equal(view.all('.instruction-entry').length, 0);
    assert.equal(view.byId('instruction-summary').hidden, true);
    assert.equal(view.byId('metric-nav').textContent, '$100,000.00');
    assert.match(view.byId('instruction-status').textContent, /历史运行/);
    assert.equal(view.byId('connection-notice').hidden, true);
  }
});

test('stock choice survives a refresh and falls back to the remaining stock when that symbol leaves the research', async t => {
  const { data } = await api(t, { research: true }); const view = await page(data);
  view.byId('stock-tab-1').click();
  await view.reload(data);
  assert.equal(view.byId('stock-tab-1').getAttribute('aria-selected'), 'true');
  const next = structuredClone(data);
  next.research.latest.executionDecisions = next.research.latest.executionDecisions.slice(0, 1);
  next.research.latest.decisions = next.research.latest.decisions.slice(0, 1);
  await view.reload(next);
  const tabs = view.byId('stock-selector').querySelectorAll('[role="tab"]');
  assert.equal(tabs.length, 1); assert.equal(tabs[0].getAttribute('aria-selected'), 'true');
  assert.equal(tabs[0].dataset.tab, 'AAPL'); assert.doesNotMatch(view.byId('decisions-list').textContent, /MSFT/);
});

test('an in-flight read disables manual refresh and coalesces online events without a second request', async t => {
  const { data } = await api(t); let finish;
  const pending = new Promise(resolve => { finish = resolve; });
  const view = await page(pending);
  try {
    assert.equal(view.byId('refresh-dashboard').disabled, true);
    view.byId('refresh-dashboard').click();
    await view.reload(undefined);
    assert.equal(view.requests.length, 1);
    assert.equal(view.byId('performance').getAttribute('aria-busy'), 'true');
  } finally { finish(data); }
  await settle();
  assert.equal(view.byId('refresh-dashboard').disabled, false);
  assert.equal(view.byId('performance').getAttribute('aria-busy'), 'false');
  assert.equal(view.byId('connection-notice').hidden, true);
});

test('previous actual executions stay visible when the next genuine snapshot contains no new fills', async t => {
  const service = await api(t);
  const first = account(0);
  first.trades = [{ id: 'frontend-only-prior-fill', executedAt: '2026-01-02T20:00:00Z', symbol: 'AAPL', side: 'buy', quantity: 20, price: 90, fees: 1 }];
  await service.ledger.append(first); await service.ledger.append(account(1));
  const data = await service.read(); assert.deepEqual(data.latest.trades, []);
  const view = await page(data);
  assert.equal(view.byId('trades-list').querySelectorAll('.trade-entry').length, 1);
  assert.match(view.byId('trades-list').textContent, /AAPL/);
  assert.match(view.byId('trades-list').textContent, /90\.00/);
});

test('narrow-screen chart has a bounded SVG viewBox and accessible textual facts while tables retain scroll regions', async t => {
  const { data } = await api(t, { records: 2, research: true }); const view = await page(data, { width: 390 });
  const chart = view.byId('performance-chart'); const svg = chart.querySelector('svg');
  assert.ok(svg); const values = svg.getAttribute('viewBox').split(' ').map(Number);
  assert.ok(values.every(Number.isFinite)); assert.ok(values[2] >= 270 && values[2] <= 390);
  assert.match(chart.getAttribute('aria-label'), /2 笔真实记录/);
  for (const region of view.all('.table-scroll')) { assert.equal(region.getAttribute('role'), 'region'); assert.equal(region.tabIndex, 0); assert.ok(region.getAttribute('aria-label')); }
});

test('the static no-script fallback keeps native whitepaper reading and download reachable outside hidden tabs', () => {
  const document = testDocument(html, 390); const fallback = document.querySelector('noscript');
  assert.ok(fallback); assert.equal(fallback.closest('[hidden]'), null);
  const links = fallback.querySelectorAll('a').filter(link => link.getAttribute('href') === '/whitepaper/tiger-trading-20260730.pdf');
  assert.equal(links.length, 2);
  assert.ok(links.some(link => !link.hasAttribute('download')));
  assert.ok(links.some(link => link.getAttribute('download') === 'Tiger-Trading-Whitepaper-20260730.pdf'));
});
