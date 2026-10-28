const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('promptpilot/static/index.html','utf8');
const statusStart = html.indexOf('function workflowStatusInfo(');
const statusEnd = html.indexOf('function workflowRoundRow(',statusStart);
const renderStart = html.indexOf('function renderWorkflowDetail(');
const renderEnd = html.indexOf('async function wfProviderChanged()',renderStart);
const pane = {innerHTML:'',querySelectorAll:()=>[],closest:()=>null};
const context = {
  document:{getElementById:id=>{assert.equal(id,'wfDetail');return pane;}},
  esc:v=>String(v),escAttr:v=>String(v),
  workflowStopReason:()=>'', workflowProviderOptions:()=>'', workflowSettingsHtml:()=>'',
  workflowPlanHtml:()=>'', workflowRoundRow:()=>'',
  uiSnapScrolls:()=>[],uiRestoreScrolls:()=>{},setTimeout:()=>{},
};
vm.createContext(context);
vm.runInContext(html.slice(statusStart,statusEnd)+html.slice(renderStart,renderEnd),context);
const wf = {id:'w',slug:'gating-test',status:'gating',current_round:1,objective:'goal',
            repository_path:'/tmp/test',candidate_branch:'work',config:{automation:{}}};
for (const automation of [{},{enabled:false,auto_gate:true},{enabled:true,auto_gate:false}]) {
  wf.config.automation=automation;
  context.renderWorkflowDetail(wf,[],[],[],[],null,[],null);
  assert.match(pane.innerHTML,/Ручные проверки/);
  assert.match(pane.innerHTML,/Gate в ручном режиме/);
  assert.doesNotMatch(pane.innerHTML,/ничего нажимать не нужно/);
  assert.match(pane.innerHTML,/Gate PASS/);
}
for (const automation of [{enabled:true},{enabled:true,auto_gate:true}]) {
  wf.config.automation=automation;
  context.renderWorkflowDetail(wf,[],[],[],[],null,[],null);
  assert.match(pane.innerHTML,/Автоматические проверки/);
  assert.match(pane.innerHTML,/ничего нажимать не нужно/);
  assert.doesNotMatch(pane.innerHTML,/Gate в ручном режиме/);
}
console.log('Real workflow renderer distinguishes manual and automatic gate; alert helpers are reached');
