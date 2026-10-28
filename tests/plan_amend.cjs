const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('promptpilot/static/index.html','utf8');
const editorStart = html.indexOf('function workflowStageEditorCard(');
const editorEnd = html.indexOf('function renderWorkflowDetail(', editorStart);
const controlsStart = html.indexOf('function wfReindexStageCards(');
const controlsEnd = html.indexOf('async function wfSavePlan(', controlsStart);
const source = html.slice(editorStart,editorEnd) + html.slice(controlsStart,controlsEnd);
const escaped = x=>String(x).replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;');
const posts = [];
let cards=[];
const list = {querySelectorAll:()=>cards, insertBefore:()=>{throw Error('Frozen boundary must not move');}};
const context = {
  window:{}, selectedWorkflowId:'wf-test', API:'/api', esc:escaped, escAttr:escaped,
  document:{querySelectorAll:()=>cards, getElementById:()=>list},
  toast:()=>{}, prompt:()=> 'split pending work', loadWorkflows:async()=>{},
  fetchJSON:async(url,opts)=>{assert.equal(opts?.method,'POST','must not replace stale CAS with fresh GET');posts.push([url,JSON.parse(opts.body)]);},
};
vm.createContext(context);
vm.runInContext(source,context);
const wf={id:'wf-test',status:'executing',state_version:7,current_stage_id:'one',
          config:{planning:{enabled:true,allow_editing_approved_plan:true}}};
const stages=[{id:'one',code:'S1',title:'Frozen',objective:'Original',status:'executing',stage_type:'implementation'},
              {id:'two',code:'S2',title:'Tail',objective:'Tail goal',status:'pending',stage_type:'integration'}];
const rendered=context.workflowPlanHtml(wf,{status:'approved'},stages);
assert.match(rendered,/data-frozen="1"[\s\S]*?<fieldset disabled/);
assert.match(rendered,/wfAmendPlan\(\)/);
assert.equal(context.window.__wfAmendVersion,7);
let removed=false;
cards=[{dataset:{index:'0',frozen:'1'},querySelector:()=>({value:'FORGED'}),remove:()=>{removed=true;}},
       {dataset:{index:'1',frozen:'0'},querySelector:selector=>({value:({code:'S3',title:'New tail',objective:'New goal',stage_type:'integration'})[selector.match(/"(.*?)"/)[1]] || ''})}];
context.wfMoveStage(1,-1);
context.wfDeleteStage(0);
assert.equal(removed,false);
const collected=context.wfCollectAmendStages();
assert.equal(collected[0].objective,'Original','disabled prefix comes from original data');
(async()=>{
  await context.wfAmendPlan();
  assert.equal(posts.length,1);
  assert.equal(posts[0][1].expected_version,7);
  assert.equal(posts[0][1].stages[0].objective,'Original');
  assert.equal(posts[0][1].stages[1].code,'S3');
  wf.current_stage_id=null;
  assert.doesNotMatch(context.workflowPlanHtml(wf,{status:'approved'},stages),/wfAmendPlan\(\)/);
  wf.current_stage_id='one';wf.config.planning.allow_editing_approved_plan=false;
  assert.doesNotMatch(context.workflowPlanHtml(wf,{status:'approved'},stages),/wfAmendPlan\(\)/);
  console.log('Amendment UI freezes history, guards moves, preserves rendered CAS, stays opt-in');
})().catch(error=>{console.error(error);process.exitCode=1;});
