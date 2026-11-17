const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
global.fetch = () => { throw new Error('Presentation must not request anything'); };
const ui = require('../promptpilot/static/workflow-progress.js');
const wf = {id:'wf_fixture',state_version:8,current_round:2,status:'awaiting_external',current_stage_id:'s1',config:{}};
const progress = {
  workflow_id:wf.id,state_version:8,
  current:{round_no:2,stage_index:1,stage_total:1,stage_code:'stage-01',stage_title:'Обновить документы',status:'awaiting_external',owner:'ZCode',headline:'Ждём, когда ZCode заберёт доработку',detail:'Новый результат ещё не получен',next_action:'Исполнителю нужно передать исправленный результат.'},
  steps:[{id:'plan',label:'План',state:'done'},{id:'execute',label:'Исполнение',state:'current'},{id:'gate',label:'Файлы',state:'waiting'},{id:'review',label:'Ревью',state:'waiting'},{id:'finish',label:'Готово',state:'waiting'}],
  last_return:{round_no:1,basis:'current_artifact_policy',kind:'artifact_path_mismatch',summary:'Файлы находятся в другой папке.',expected_paths:['changes.patch','RESULT.md'],received_paths:['.pp-run/stage-01-17/changes.patch','.pp-run/stage-01-17/RESULT.md'],action:'Передать файлы в корне пакета результата.'},
  history:[{round_no:1,status:'revision_required',summary:'Проверка результата не пройдена.'}],
};
test('R2 unclaimed external work shows current owner/action and inferred R1 comparison without a running claim',()=>{
  const value=ui.renderSummary({workflow:wf,progress});
  assert.match(value,/Этап 1 из 1 · Раунд 2/);assert.match(value,/Ждём, когда ZCode заберёт доработку/);
  assert.match(value,/Новый результат ещё не получен/);assert.match(value,/Сверка файлов по текущим правилам/);
  assert.match(value,/Сейчас ожидаются/);assert.match(value,/.pp-run\/stage-01-17\/changes.patch/);
  assert.doesNotMatch(value,/Причина зафиксирована|модель работает|\d+%|onclick=|<button/);
});
test('recorded reason is distinct and advanced review cannot retain stale R1 failure',()=>{
  const recorded=structuredClone(progress);recorded.last_return.basis='recorded';
  assert.match(ui.renderSummary({workflow:wf,progress:recorded}),/Причина зафиксирована/);
  const advanced={...wf,status:'reviewing',state_version:9};const newer={...recorded,state_version:9,current:{...recorded.current,status:'reviewing'}};
  assert.doesNotMatch(ui.renderSummary({workflow:advanced,progress:newer}),/Сверка результата|Возврат на доработку|Файлы находятся/);
});
test('foreign/stale/malformed DTO falls back without mixing another task reason',()=>{
  for(const bad of [{...progress,workflow_id:'wf_foreign'},{...progress,state_version:7},{...progress,state_version:'8'}]){
    const value=ui.renderSummary({workflow:wf,progress:bad});
    assert.doesNotMatch(value,/Сверка файлов|Файлы находятся|ZCode/);assert.match(value,/сохранённый статус/);
  }
});
test('all DTO strings are HTML data and unknown state/basis cannot claim recorded proof',()=>{
  const bad=structuredClone(progress);bad.current.headline='<img src=x onerror=alert(1)>';
  bad.current.owner='</p><script>attack</script>';bad.steps[0].state='done" onclick="attack';bad.last_return.received_paths=['<svg onload=attack>'];
  const value=ui.renderSummary({workflow:wf,progress:bad});
  assert.doesNotMatch(value,/<img|<script|<svg|onclick=/);assert.match(value,/&lt;img/);assert.match(value,/data-progress-state="unknown"/);
  bad.last_return.basis='untrusted';assert.doesNotMatch(ui.renderSummary({workflow:wf,progress:bad}),/Причина зафиксирована/);
});
test('history uses Russian non-active labels and excludes current round',()=>{
  const data={...progress,history:[{round_no:1,status:'gating',summary:'Проверка не пройдена.'},{round_no:2,status:'awaiting_external',summary:'current'}]};
  const value=ui.renderHistory({workflow:wf,progress:data});
  assert.match(value,/Раунд 1/);assert.match(value,/Проверка результата/);assert.doesNotMatch(value,/Проверяется|Раунд 2|current|gating/);
});
test('manual-gate fallback still requires owner action and never says automatic',()=>{
  const value=ui.renderSummary({workflow:{...wf,status:'gating',config:{automation:{enabled:true,auto_gate:false}}}});
  assert.match(value,/Нужно зафиксировать результат проверок/);assert.match(value,/Владелец/);assert.doesNotMatch(value,/автоматическую/);
});

const html=fs.readFileSync(path.join(__dirname,'../promptpilot/static/index.html'),'utf8');
const select=html.slice(html.indexOf('async function selectWorkflow('),html.indexOf('function workflowProviderOptions('));
function selectionContext(fetchJSON, cache){
  const rendered=[];const ctx={workflowSelectionSerial:0,selectedWorkflowId:null,selectedWorkflowData:null,workflowCache:cache,API:'/api',fetchJSON,
    document:{querySelectorAll:()=>[],getElementById:()=>({children:[]})},captureWorkflowOpenDetails:()=>new Set(),restoreWorkflowOpenDetails:()=>{},
    renderWorkflowDetail:(workflow,...args)=>rendered.push({workflow,progress:args[9]})};
  vm.createContext(ctx);vm.runInContext(select,ctx);return {ctx,rendered};
}
test('late response for the same workflow cannot overwrite a newer selected version',async()=>{
  let firstResolve;let calls=0;const requests=[];
  const {ctx,rendered}=selectionContext(async url=>{
    requests.push(url);
    if(url.endsWith('/progress')){calls++;if(calls===1)return new Promise(resolve=>{firstResolve=resolve;});return {...progress,state_version:9};}
    if(url.endsWith('/plan')||url.endsWith('/elicitation'))return null;return [];
  },[{...wf}]);
  const first=ctx.selectWorkflow(wf.id);ctx.workflowCache=[{...wf,state_version:9}];await ctx.selectWorkflow(wf.id);
  firstResolve(progress);await first;
  assert.equal(rendered.length,1);assert.equal(rendered[0].workflow.state_version,9);assert.equal(rendered[0].progress.state_version,9);
  assert.ok(requests.every(url=>url.startsWith('/api/workflows/')));
});
test('optional progress GET failure and mismatched version never break the legacy card',async()=>{
  for(const mode of ['error','stale']){
    const {ctx,rendered}=selectionContext(async url=>{
      if(url.endsWith('/progress')){if(mode==='error')throw Error('unavailable');return {...progress,state_version:7};}
      if(url.endsWith('/plan')||url.endsWith('/elicitation'))return null;return [];
    },[{...wf}]);
    await ctx.selectWorkflow(wf.id);assert.equal(rendered.length,1);assert.equal(rendered[0].progress,null);
  }
});
