const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('promptpilot/static/index.html', 'utf8');
const start = html.indexOf('function pipelineDeliveryFlow(');
const end = html.indexOf('\nasync function loadPipelineInsights(', start);
assert.ok(start >= 0 && end > start);
const context = {esc: value => String(value).replaceAll('<', '&lt;').replaceAll('>', '&gt;')};
vm.createContext(context);
vm.runInContext(html.slice(start, end), context);
const rendered = context.pipelineDeliveryFlow({delivery_flow: {
  reason: 'Доработка <unsafe>', active: [{title: '<script>', task_id: 42, elapsed_seconds: 3900}],
  review_rounds: [{number: 10, rounds: 11}],
}});
assert.match(rendered, /Что удерживает поставку/);
assert.match(rendered, /выполняется 1.1 ч/);
assert.match(rendered, /#10 — 11/);
assert.match(rendered, /&lt;script&gt;/);
assert.doesNotMatch(rendered, /<script>/);
assert.equal(context.pipelineDeliveryFlow({}), '');
console.log('Delivery flow: actual elapsed time, review rounds and escaping OK');
