// Small DOM stand-in for regression checks; real browser checks complement it.
// Run with Node.js 18+: node --test tests/frontend.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor() {
    this.children = [];
    this.listeners = {};
    this.attributes = {};
    this.value = '';
    this.textContent = '';
    this.hidden = false;
    this.disabled = false;
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; this.textContent = ''; }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key] ?? null; }
  get childElementCount() { return this.children.length; }
  addEventListener(name, action) { (this.listeners[name] ||= []).push(action); }
  dispatch(name) { (this.listeners[name] || []).forEach((action) => action({ preventDefault() {} })); }
  focus() {}
}

const source = fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8');
const options = {
  cities: ['Алматы'], categories: ['Ведущий', 'Флорист'],
  event_formats: ['свадьба'], languages: ['русский', 'английский'],
  date_min: '2026-09-23', date_max: '2026-12-31',
  catalog: { source: 'demo_fallback', total_profiles: 66, synthetic_profiles: 66 }
};
const request = { city: 'Алматы', category: 'Ведущий', event_type: 'свадьба', date: '2026-10-02', budget_kzt: 300000 };
const card = (id) => ({ id, anon_name: id, category: 'Ведущий', city: 'Алматы', price_from_kzt: 150000, synthetic: true, explanation: 'Конкретное объяснение.' });
const response = {
  outcome: 'recommended', candidate_count: 10, total_eligible: 3,
  results: ['third', 'first', 'second'].map(card), requested: request,
  availability_summary: { date: request.date, busy_excluded: 4 },
  rejection_counts: { busy: 4, budget: 3 }, ranking: { mode: 'lexical' }
};

function page(fetch) {
  const elements = new Map();
  const element = (selector) => {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  };
  vm.runInNewContext(source, {
    document: { querySelector: element, createElement: () => new Element() },
    window: { setTimeout, clearTimeout }, fetch, AbortController, console, Intl
  });
  return element;
}

const json = (value) => ({ ok: true, json: async () => value });
const flush = () => new Promise((resolve) => setImmediate(resolve));
function text(element) { return element.textContent + element.children.map(text).join(' '); }
function fill(element, value) {
  const names = { event_type: 'event-type', budget_kzt: 'budget', duration_hours: 'duration' };
  Object.entries(value).forEach(([key, entry]) => { element('#' + (names[key] || key)).value = String(entry); });
}

test('eligible count excludes rejected candidates and preserves API card order', async () => {
  const element = page(async (url) => json(url === '/api/options' ? options : url === '/api/demo-scenarios' ? { scenarios: [] } : response));
  await flush();
  fill(element, request);
  element('#request-form').dispatch('submit');
  await flush();
  const summary = text(element('#result-state'));
  assert.match(summary, /прошли условия: 3/);
  assert.match(summary, /заняты: 4/);
  assert.doesNotMatch(summary, /Ещё подходящих/);
  assert.deepEqual(element('#result-list').children.map((item) => item.children[0].children[0].textContent), ['third', 'first', 'second']);
  assert.match(text(element('#result-list')), /Синтетический профиль/);
  element('#budget').value = '100000';
  element('#budget').dispatch('input');
  assert.match(text(element('#query-summary')), /Поля формы были изменены/);
});

test('demo button clears optional values before submitting exact scenario', async () => {
  const sent = [];
  const element = page(async (url, init) => {
    if (url === '/api/options') return json(options);
    if (url === '/api/demo-scenarios') return json({ scenarios: [{ title: 'Демо', request }] });
    sent.push(JSON.parse(init.body));
    return json(response);
  });
  await flush();
  element('#duration').value = '24';
  element('#language').value = 'английский';
  element('#demo-list').children[0].dispatch('click');
  await flush();
  assert.deepEqual(sent, [request]);
  assert.equal(element('#duration').value, '');
  assert.equal(element('#language').value, '');
});

test('failed options retry sends only one request and hides button on success', async () => {
  let calls = 0;
  let complete;
  const element = page(async (url) => {
    if (url !== '/api/options') return json({ scenarios: [] });
    calls += 1;
    if (calls === 1) throw new Error('Offline');
    return new Promise((resolve) => { complete = () => resolve(json(options)); });
  });
  await flush();
  assert.equal(element('#retry-actions').hidden, false);
  element('#retry-button').dispatch('click');
  element('#retry-button').dispatch('click');
  assert.equal(calls, 2);
  complete();
  await flush();
  assert.equal(element('#retry-actions').hidden, true);
  assert.equal(element('#submit-button').disabled, false);
});

test('sparse results describe rejections instead of promising filtered-out cards', async () => {
  const sparse = { ...response, total_eligible: 1, results: [card('one')], rejection_counts: { busy: 4, budget: 5 } };
  const element = page(async (url) => json(url === '/api/options' ? options : url === '/api/demo-scenarios' ? { scenarios: [] } : sparse));
  await flush();
  fill(element, request);
  element('#request-form').dispatch('submit');
  await flush();
  const summary = text(element('#result-state'));
  assert.match(summary, /Показываем 1/);
  assert.match(summary, /5 — выходят за бюджет/);
  assert.doesNotMatch(summary, /Ещё подходящих/);
});
