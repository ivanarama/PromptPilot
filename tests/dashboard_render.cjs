// Exercise the real report renderer with stale data and a live task.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('promptpilot/static/index.html', 'utf8');
const start = html.indexOf('async function loadPipelineReport(');
const end = html.indexOf('\nfunction ', start);
const renderer = html.slice(start, end);
const helpersStart = html.indexOf('function pipelineWaitReasons(');
const helpersEnd = html.indexOf('\nasync function loadPipelineInsights(', helpersStart);
assert.ok(helpersStart >= 0 && helpersEnd > helpersStart, 'report helpers are present');
const box = {innerHTML: ''};
const data = {
  hours: 24, title: 'Example', summary: {errors: 0},
  coverage: {fresh: false, complete: false, data_through: new Date().toISOString()},
  current: {tasks: [
    {status: 'running', title: '<unsafe>', task_id: 42, started_at: new Date().toISOString()},
    {status: 'pending', title: 'MERGE', task_id: 43, error: 'waiting for integration <REVIEW>'},
  ]},
  runs: {total: 3, verdicts: {ready: 1, empty: 2}},
  decisions: {available: true, waiting_ship: [{number: 99, title: 'Ready PR'}]},
  attention: [{number: 10, title: 'Old merged PR', queues: ['tail'], labels: ['ship']}],
};
const context = {
  document: {getElementById: () => box}, pipelineReportRequestId: 0, API: '/api',
  fetchJSON: async () => data,
  esc: value => String(value).replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
  escAttr: value => String(value), pipelineReportNumber: value => value == null ? '—' : String(value),
  pipelineReportSigned: value => value == null ? '—' : String(value),
  pipelineReportPeriodLabel: () => '24 часа', pipelineReportDeliveryNotice: () => '',
  pipelineReportItem: item => `#${item.number} ${item.title}`,
};
vm.createContext(context);
vm.runInContext(html.slice(helpersStart, helpersEnd), context);
vm.runInContext(renderer, context);
(async () => {
  await context.loadPipelineReport('example');
  assert.match(box.innerHTML, /Что происходит сейчас/);
  assert.match(box.innerHTML, /&lt;unsafe&gt;/);
  assert.match(box.innerHTML, /Итоги запусков/);
  assert.match(box.innerHTML, /3 запусков/);
  assert.match(box.innerHTML, /MERGE<\/b>: waiting for integration &lt;REVIEW&gt;/);
  assert.match(box.innerHTML, /данные устарели/);
  assert.match(box.innerHTML, /Ещё не проверено/);
  assert.match(box.innerHTML, /Стоимость не измеряется/);
  const decisions = box.innerHTML.split('Что нужно от вас')[1].split('<details')[0];
  assert.match(decisions, /#99 Ready PR/);
  assert.doesNotMatch(decisions, /Old merged PR/);
  assert.match(box.innerHTML, /<details/);
  data.current.tasks = [];
  data.decisions = {available: false};
  await context.loadPipelineReport('example');
  assert.match(box.innerHTML, /Список решений ещё не получен/);
  assert.match(box.innerHTML, /ни один этап не выполняется/);
  console.log('Report renderer: live task, stale data, decisions, missing data OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
