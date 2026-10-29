// Exercise the real task-list renderer with actual persisted attribution fields.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('promptpilot/static/index.html', 'utf8');
const start = html.indexOf('function taskDisplayTitle(');
const end = html.indexOf('function providerLabel(', start);
assert.ok(start >= 0 && end > start);
const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
const base = {status:'completed', prompt:'same repository', working_dir:'/same/repo',
  retry_count:0, max_retries:5, exit_code:0, priority:5};
const tasks = [
  {...base, id:1, workflow_id:'wf', role:'executor', workflow_stage_code:'FIRST',
    workflow_stage_title:'<img src=x onerror=alert(1)> "old stage"', workflow_round_no:1},
  {...base, id:2, workflow_id:'wf', role:'reviewer', workflow_stage_code:'FIRST', workflow_round_no:1},
  {...base, id:3, status:'running', workflow_id:'wf', role:'executor', workflow_stage_code:'FINAL',
    workflow_stage_title:'Current stage', workflow_round_no:2},
  {...base, id:4, prompt:'reviewer FINAL same repository', workflow_id:null,
    role:'reviewer', workflow_stage_code:'FINAL'},
  {...base, id:5, workflow_id:'planning', role:'planner'},
];
const list = {innerHTML:''};
const calls = [];
const context = {
  API:'/api', currentFilter:'', expandedId:null, screenCache:{},
  document:{getElementById:id=>id === 'taskList' ? list : null},
  fetchJSON:async(url, options)=>{calls.push([url,options]);return tasks},
  timeAgo:()=> 'now', parseResult:()=>null, githubApiWait:()=>false,
  taskStatusLabel:task=>task.status, scheduleInfo:()=>'', verdictIcon:()=>'',
  uiSnapScrolls:()=>[], uiRestoreScrolls:()=>{}, esc:escape, escAttr:escape,
};
vm.createContext(context);
vm.runInContext(html.slice(start,end), context);
(async()=>{
  await context.loadTasks();
  const badges = [...list.innerHTML.matchAll(/class="task-meta task-workflow">(.*?)<\/div>/g)]
    .map(match=>match[1]);
  assert.equal(badges.length,4); // Same cwd/prompt is never a workflow link.
  assert.match(badges[0],/Исполнитель.*FIRST.*раунд 1/);
  assert.doesNotMatch(badges[0],/FINAL|Current stage/);
  assert.match(badges[0],/&lt;img/);
  assert.doesNotMatch(list.innerHTML,/<img|<svg|undefined/);
  assert.match(badges[1],/Ревью.*FIRST/);
  assert.match(badges[2],/Исполнитель.*FINAL.*раунд 2/);
  assert.equal(badges[3],'Планировщик');
  tasks[2].workflow_stage_code='LATER';
  await context.loadTasks();
  assert.match(list.innerHTML,/Исполнитель.*этап FIRST/);
  assert.match(list.innerHTML,/Исполнитель.*этап LATER/);
  assert.ok(calls.every(([url,options])=>url === '/api/tasks?limit=100' && options === undefined));
  console.log('Actual task renderer: historical stage, current run, planner, escaping, no inference, read-only redraw PASS');
})().catch(error=>{console.error(error);process.exitCode=1});
