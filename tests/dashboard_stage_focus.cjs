const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const html = fs.readFileSync('promptpilot/static/index.html', 'utf8');
const start = html.indexOf('function pipelineStageFocus(');
const end = html.indexOf('\nasync function loadPipelineInsights(', start);
assert.ok(start >= 0 && end > start, 'stage focus helper is present');
const context = {};
vm.createContext(context);
vm.runInContext(html.slice(start, end), context);

const triage = {id: 'triage', title: 'TRIAGE', backlog: 3, parallel_capacity: 0, eta_hours: null};
const fix = {id: 'fix', title: 'FIX', backlog: 59, parallel_capacity: 1, eta_hours: 12};
const merge = {id: 'merge', title: 'MERGE', backlog: 2, parallel_capacity: 1, eta_hours: 2};
const focus = context.pipelineStageFocus([triage, fix, merge]);
assert.equal(focus.stopped.length, 1);
assert.equal(focus.stopped[0].id, 'triage');
assert.equal(focus.workingBottleneck.id, 'fix');

const idle = context.pipelineStageFocus([{id: 'review', backlog: 0, parallel_capacity: 0}]);
assert.equal(idle.stopped.length, 0);
assert.equal(idle.workingBottleneck, null);

const unknown = context.pipelineStageFocus([{id: 'plan', backlog: 2, parallel_capacity: null}]);
assert.equal(unknown.stopped.length, 0);
assert.equal(unknown.workingBottleneck, null);

assert.match(html, /Остановленные этапы/);
assert.match(html, /Узкое место работающих/);
assert.doesNotMatch(html, /активной очереди нет/);
console.log('Pipeline stage focus: stopped and working queues are distinct');
