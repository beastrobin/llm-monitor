const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..');
const dom = new JSDOM(fs.readFileSync(path.join(root, 'frontend/index.html'), 'utf8'), {
  url: 'http://localhost/', runScripts: 'outside-only', pretendToBeVisual: true,
});
const win = dom.window;
win.eval(fs.readFileSync(path.join(root, 'frontend/app.js'), 'utf8').replace(/\nboot\(\);\s*$/, '') + `
window.probe = { rateReading, applyModel, endpointChip, initHistTabs, initCompactFolds,
  renderEndpointModels, renderFreshness,
  snapshot: function (value) { S.snapshot = value; } };
drawSpark = function () {};
`);
const api = win.probe;
assert.equal(api.rateReading(0, 120, 1).text, '0.0');
assert.equal(api.rateReading(null, 120, 1).text, '≈120.0');
for (const value of [null, undefined, NaN, Infinity, -1]) {
  assert.equal(api.rateReading(value, null, 1).text, '—');
  assert.equal(api.rateReading(value, null, 1).unit, '');
}
assert.match(api.endpointChip({ label: 'Basalt', engine_version: 'MegaCapybara', up: true }), /title="Basalt">MegaCapybara/);
const card = win.document.createElement('div');
card.innerHTML = '<div data-role="cards"></div>';
win.document.body.append(card);
const model = { id: 'fixture', n_ctx: 100, metrics: {}, slots: [] };
api.applyModel(card, {id: 'engine'}, model);
assert.equal(card.querySelector('[data-card="decode"] [data-role="value"]').textContent, '—');
assert.match(card.querySelector('[data-card="ctx"] .card-foot').textContent, /未上报上下文/);
assert.doesNotMatch(card.querySelector('[data-card="ctx"] .card-foot').textContent, /空闲残留/);
model.slots = [{ctx_ratio: 0.1, is_processing: false}, {ctx_ratio: 0.9, is_processing: true}];
api.applyModel(card, {id: 'engine'}, model);
assert.match(card.querySelector('[data-card="ctx"] .card-foot').textContent, /接近溢出/);
assert.doesNotMatch(card.querySelector('[data-card="ctx"] .card-foot').textContent, /空闲残留/);
model.slots[1].is_processing = false;
api.applyModel(card, {id: 'engine'}, model);
assert.match(card.querySelector('[data-card="ctx"] .card-foot').textContent, /空闲残留/);
api.snapshot({ ts: Date.now() / 1000 - 90, interval: 2 });
api.renderFreshness();
assert.equal(win.document.getElementById('stale-notice').hidden, false);
api.snapshot({ ts: Date.now() / 1000, interval: 2 });
api.renderFreshness();
assert.equal(win.document.getElementById('stale-notice').hidden, true);
api.snapshot({ ts: Date.now() / 1000 - 20, interval: 2, sample_ms: 12000 });
api.renderFreshness();
assert.equal(win.document.getElementById('stale-notice').hidden, true);
api.snapshot({ ts: Date.now() / 1000 - 60, interval: 2, sample_ms: 12000 });
api.renderFreshness();
assert.equal(win.document.getElementById('stale-notice').hidden, false);
api.snapshot(null);
api.renderFreshness();
assert.equal(win.document.getElementById('freshness').textContent, '等待首个采样');
api.initHistTabs();
const tabs = [...win.document.querySelectorAll('[role="tab"]')];
tabs[0].dispatchEvent(new win.KeyboardEvent('keydown', {key: 'ArrowLeft', bubbles: true}));
assert.equal(win.document.activeElement, tabs[4]);
assert.equal(tabs[4].getAttribute('aria-selected'), 'true');
assert.equal(win.document.getElementById('hist-panel-energy').hidden, false);
tabs[4].dispatchEvent(new win.KeyboardEvent('keydown', {key: 'Home', bubbles: true}));
assert.equal(win.document.activeElement, tabs[0]);
api.initCompactFolds();
const endpoints = win.document.getElementById('endpoints');
endpoints.innerHTML = '<div class="endpoint" data-endpoint="engine"><div data-role="models"></div></div>';
const modelBox = endpoints.querySelector('[data-role="models"]');
api.renderEndpointModels(modelBox, { id: 'engine', models: [model] });
const toggle = modelBox.querySelector('[data-role="model-toggle"]');
toggle.click();
assert.equal(toggle.getAttribute('aria-expanded'), 'true');
api.renderEndpointModels(modelBox, { id: 'engine', models: [model] });
assert.equal(modelBox.querySelector('.model').classList.contains('details-open'), true);
assert.equal(modelBox.querySelector('[data-role="model-toggle"]').getAttribute('aria-expanded'), 'true');
win.document.getElementById('resource-toggle').click();
win.document.getElementById('history-toggle').click();
assert.equal(win.document.getElementById('resource-info').classList.contains('details-open'), true);
assert.equal(win.document.getElementById('history-info').classList.contains('details-open'), true);
console.log('PASS: rates, engine identity, context, stale samples, keyboard tabs, compact folds and rerender persistence');
dom.window.close();
