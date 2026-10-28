const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('promptpilot/static/index.html', 'utf8');
const render = html.slice(html.indexOf('function workflowExternalFormHtml('), html.indexOf('function renderWorkflowDetail('));
const start = html.indexOf('function wfSubmitExternal(');
const submit = html.slice(start, html.indexOf('\n}', start) + 2);
const escaped = x => String(x).replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const nodes = new Map(), posts = [];
const context = {selectedWorkflowId:'wf-one', selectedWorkflowData:{state_version:999},
  esc:escaped, escAttr:escaped, document:{getElementById:id => nodes.get(id)}, toast:()=>{},
  wfPost:(path,body)=>posts.push([path,body])};
vm.createContext(context); vm.runInContext(render + submit, context);
const hash = 'a'.repeat(64);
const wf = {id:'wf-one',state_version:7};
const run = {id:'run-first',attempt_no:1,status:'awaiting_external',input_sha256:hash,
  input:{execution_mode:'external',assignment:'</textarea><script>attack</script>'}};
const first = context.workflowExternalFormHtml(wf,[run]);
assert.match(first,/wfExternalResult-run-first/);
assert.match(first,/wfSubmitExternal\(&quot;wf-one&quot;, &quot;run-first&quot;, 7,/);
assert.doesNotMatch(first,/<script>/);
function install(id, version, text) {
  nodes.clear();
  nodes.set('wfExternalForm-'+id,{dataset:{workflow:'wf-one',run:id,version:String(version),inputSha:hash}});
  nodes.set('wfExternalResult-'+id,{value:text});
  nodes.set('wfExternalPerformer-'+id,{value:'person'});
  nodes.set('wfExternalComment-'+id,{value:'comment'});
}
install(run.id,7,'draft first');
context.wfSubmitExternal('wf-one',run.id,7,hash);
assert.equal(posts.length,1);
assert.equal(posts[0][1].expected_version,7,'must never upgrade the observed token to selectedWorkflowData version');
assert.equal(posts[0][1].run_id,run.id);
assert.equal(posts[0][1].input_sha256,hash);
install(run.id,8,'same assignment refreshed');
context.wfSubmitExternal('wf-one',run.id,7,hash);
assert.equal(posts.length,1,'stale rendered button must not adopt refreshed CAS');
const second = context.workflowExternalFormHtml({...wf,state_version:10},[{...run,id:'run-second',attempt_no:2}]);
assert.match(second,/wfExternalResult-run-second/);
assert.doesNotMatch(second,/wfExternalResult-run-first/,'draft restoration by element id cannot carry prior attempt text');
install('run-second',10,'new work');
context.wfSubmitExternal('wf-one',run.id,7,hash);
assert.equal(posts.length,1,'old assignment button cannot submit a new form');
context.wfSubmitExternal('another-workflow','run-second',10,hash);
assert.equal(posts.length,1);
context.wfSubmitExternal('wf-one','run-second',10,hash);
assert.equal(posts.length,2);
console.log('External forms preserve rendered assignment identity, isolate drafts, reject stale and foreign callbacks');
