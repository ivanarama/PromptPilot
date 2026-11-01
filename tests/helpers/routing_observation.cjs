// API-response harness; invoked with stdin by test_routing_observation.py.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
const html = fs.readFileSync('promptpilot/static/index.html', 'utf8');
const start = html.indexOf('async function loadPipelineReport(');
const end = html.indexOf('\nfunction ', start);
const helpersStart = html.indexOf('function pipelineWaitReasons(');
const helpersEnd = html.indexOf('\nasync function loadPipelineInsights(', helpersStart);
const box = {innerHTML: ''};
const context = {
  document: {getElementById: () => box}, pipelineReportRequestId: 0, API: '/api',
  fetchJSON: async () => data,
  esc: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
  escAttr: value => String(value), pipelineReportNumber: value => value == null ? '—' : String(value),
  pipelineReportSigned: value => value == null ? '—' : String(value),
  pipelineReportPeriodLabel: () => '24 часа', pipelineReportDeliveryNotice: () => '',
  pipelineReportItem: item => `#${item.number}`,
};
vm.createContext(context);
vm.runInContext(html.slice(helpersStart, helpersEnd), context);
vm.runInContext(html.slice(start, end), context);
(async () => {
  await context.loadPipelineReport('example');
  assert.match(box.innerHTML, /Причины остановок и получатели/);
  for (const group of data.board.routing.groups) {
    if (!group.items.length) continue;
    assert.ok(box.innerHTML.includes(group.title));
    for (const item of group.items) {
      if (item.head) assert.ok(box.innerHTML.includes(item.head));
      for (const blocker of item.blockers || []) {
        for (const option of blocker.evidence.options || []) {
          assert.ok(box.innerHTML.includes(option.behavior));
          assert.ok(box.innerHTML.includes(option.consequences));
        }
        if (blocker.evidence.recommendation) assert.match(box.innerHTML, /Рекомендуется вариант/);
      }
    }
  }
  // Untrusted structured evidence and tool errors use the same escaped path.
  data.board.routing.groups[0].items = [{number: 7, head: '<img src=x onerror=bad()>',
    recipient: '<script>bad()</script>', reasons: ['<svg onload=bad()>'],
    blockers: [{summary: '<script>', evidence: {options: [
      {number: 1, behavior: '<iframe>', consequences: '<object>'}],
      recommendation: {number: 1, reason: '<embed>'}}}]}];
  await context.loadPipelineReport('example');
  assert.doesNotMatch(box.innerHTML, /<script>|<img src=x|<svg onload|<iframe>|<object>|<embed>/);
  assert.match(box.innerHTML, /&lt;iframe&gt;/);
  console.log('public API to real UI OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
