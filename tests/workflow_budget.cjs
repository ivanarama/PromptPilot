const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('promptpilot/static/index.html','utf8');
const render = html.slice(html.indexOf('function workflowSettingsHtml('),html.indexOf('function workflowStatusInfo('));
const start = html.indexOf('async function wfSaveSettings(');
const save = html.slice(start,html.indexOf('function wfReindexStageCards(',start));
async function submit(wf, override) {
  const nodes={}, posts=[];
  const c={selectedWorkflowId:wf.id,selectedWorkflowData:wf,API:'/api',esc:x=>String(x),escAttr:x=>String(x),
    wfChecked:v=>v?' checked':'',workflowProviderOptions:()=>'',wfRoleSettings:(prefix,old)=>old||{},
    document:{getElementById:id=>nodes[id]||{value:'',checked:false}},confirm:()=>true,toast:()=>{},
    fetchJSON:async(u,o)=>posts.push(JSON.parse(o.body)),loadWorkflows:async()=>{}};
  vm.createContext(c);vm.runInContext(render+save,c);
  const form=c.workflowSettingsHtml(wf);
  for(const m of form.matchAll(/<input\b([^>]*)>/g)) {
    const a=m[1],id=a.match(/\bid="([^"]+)"/)?.[1];
    if(id)nodes[id]={value:a.match(/\bvalue="([^"]*)"/)?.[1]||'',checked:/\bchecked\b/.test(a)};
  }
  for(const m of form.matchAll(/<textarea\b[^>]*\bid="([^"]+)"[^>]*>([\s\S]*?)<\/textarea>/g))nodes[m[1]]={value:m[2],checked:false};
  const renderedBudget=nodes.wfCfgStageRevisions.value;
  if(override !== undefined)nodes.wfCfgStageRevisions.value=override;
  await c.wfSaveSettings();
  return {posts,renderedBudget};
}
(async()=>{
  if(process.argv[2]) {
    const wf=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
    const result=await submit(wf,process.argv.length > 3 ? process.argv[3] : undefined);
    assert.equal(result.posts.length,1);
    console.log(JSON.stringify(result.posts[0]));
    return;
  }
  const wf={id:'wf-settings',status:'queued',state_version:9,config:{planning:{enabled:true},automation:{enabled:false}}};
  const inherited=await submit(wf);
  assert.equal(inherited.renderedBudget,'');
  assert.equal(inherited.posts.length,1);
  assert.equal(Object.hasOwn(inherited.posts[0].config.planning,'max_revisions_per_stage'),false);
  const explicit=await submit(wf,'7');
  assert.equal(explicit.posts[0].config.planning.max_revisions_per_stage,7);
  const saved={...wf,config:{...wf.config,planning:{enabled:true,max_revisions_per_stage:5}}};
  assert.equal((await submit(saved)).posts[0].config.planning.max_revisions_per_stage,5);
  assert.equal(Object.hasOwn((await submit(saved,'')).posts[0].config.planning,'max_revisions_per_stage'),false);
  for(const bad of ['0','101','nonsense','1.5'])assert.equal((await submit(wf,bad)).posts.length,0);
  console.log('Workflow settings preserve inherited budgets, explicit overrides and clearing without silent default promotion');
})().catch(e=>{console.error(e);process.exitCode=1;});
