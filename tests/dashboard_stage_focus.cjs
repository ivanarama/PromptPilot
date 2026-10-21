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

const waits = context.pipelineWaitReasons({
  queues: [
    {id:'fix', title:'Исправления', backlog:59, task_status:'pending', task_error:'WIP: 42 / 10'},
    {id:'review', title:'Ревью', backlog:1, task_status:'running', task_error:'old error'},
  ],
  diagnostics: {integration_owner:{number:1762, stage:'integration-review'}},
});
assert.equal(waits.length, 2);
assert.equal(waits[0].reason, 'WIP: 42 / 10');
assert.match(waits[1].reason, /#1762.*ревью/);
assert.equal(context.pipelineWaitReasons({
  queues: [], cache:{stale:true},
  diagnostics:{integration_owner:{number:1762, stage:'integration-review'}},
}).length, 0);
const reportWaits = context.pipelineWaitReasons({
  current:{tasks:[{task_id:17, title:'MERGE', status:'pending', error:'waiting for integration REVIEW'}]},
});
assert.equal(reportWaits.length, 1);
assert.equal(reportWaits[0].reason, 'waiting for integration REVIEW');

const breakdown = context.pipelineRunBreakdown({runs_5h:{
  runs:6, ready:2, empty:3, human:1,
}});
assert.equal(breakdown.total, 6);
assert.equal(breakdown.parts.find(part => part.key === 'empty').count, 3);
assert.equal(context.pipelineRunBreakdown({runs_5h:{runs:0}}).total, 0);

assert.match(html, /Остановленные этапы/);
assert.match(html, /Самая длинная очередь по ETA/);
assert.match(html, /Почему этапы ждут прямо сейчас/);
assert.match(html, /Итоги запусков по этапам · 5 часов/);
assert.doesNotMatch(html, /активной очереди нет/);
console.log('Pipeline stage focus: stopped and working queues are distinct');
