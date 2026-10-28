const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('promptpilot/static/index.html', 'utf8');
const start = html.indexOf('const FLOW_STATUS_RU =');
const end = html.indexOf('// --- Workflow orchestrator ---', start);
assert.ok(start >= 0 && end > start);
const posts = [];
const detail = {dataset: {}, innerHTML: '', textContent: ''};
const stop = {checked: false}, note = {value: ''}, approvalNote = {value: ''};
const elements = {flowsDetail: detail, flowsItems: {innerHTML: ''}, flowsStatus: {value: ''}};
let item = {id:7, flow:'safe', title:'<img src=x onerror=alert(1)>', status:'waiting_human', version:9,
  step_id:'first', author:'<svg/onload=alert(2)>', error:'', data:{input:{body:'<script>evil</script>'},steps:{}},
  wait:{approval:'first',text:'<script>question</script>',show:{result:'<img/onerror=evil>'}},events:[]};
let reject = false;
const esc = value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const context = {
  API:'/api', esc, toast:()=>{}, confirm:()=>true,
  document:{getElementById:id=>{
    if (id === 'flowStopChecked') return detail.innerHTML.includes('id="flowStopChecked"') ? stop : null;
    if (id === 'flowStopNote') return detail.innerHTML.includes('id="flowStopNote"') ? note : null;
    if (id === 'flowNote') return approvalNote;
    return elements[id];
  }},
  fetchJSON:async(url, options)=>{
    if (options?.method === 'POST') {
      posts.push({url, body:JSON.parse(options.body)});
      if (reject) throw Error('Stale observed version');
      return {status:url.endsWith('/cancel') ? 'cancelling' : 'active'};
    }
    if (url === '/api/flows/items/7') return JSON.parse(JSON.stringify(item));
    if (url.startsWith('/api/flows/items?')) return [];
    throw Error('Unexpected request '+url);
  },
};
vm.createContext(context);
vm.runInContext(html.slice(start,end), context);
(async()=>{
  await context.showFlowItem(7);
  assert.match(detail.innerHTML,/decideFlowItem\(7, 'approve', 9\)/);
  assert.doesNotMatch(detail.innerHTML,/<script>|<img src=x|<svg\/onload/);
  // A delayed callback must not borrow a newer version from a rerendered card.
  item.version=100;
  await context.showFlowItem(7);
  reject=true;
  await context.decideFlowItem(7,'approve',9);
  assert.equal(posts[0].body.expected_version,9);
  const count=posts.length;
  await context.decideFlowItem(7,'approve');
  await context.decideFlowItem(8,'approve',100);
  assert.equal(posts.length,count,'old/tokenless or mismatched-item controls must not submit');
  item={...item,status:'needs_human',version:21,error:'Verify old command stopped',wait:{command:'command',ambiguous:true}};
  await context.showFlowItem(7);
  assert.match(detail.innerHTML,/id="flowStopChecked"/);
  assert.match(detail.innerHTML,/flowItemAction\(7, 'retry', 21\)/);
  reject=false;
  await context.flowItemAction(7,'retry',21);
  assert.equal(posts.at(-1).body.expected_version,21);
  assert.equal(posts.at(-1).body.confirm_command_stopped,false);
  stop.checked=true;note.value='Inspected previous owned execution';
  await context.flowItemAction(7,'retry',21);
  assert.equal(posts.at(-1).body.confirm_command_stopped,true);
  assert.equal(posts.at(-1).body.note,'Inspected previous owned execution');
  item={...item,status:'active',version:31,wait:{}};
  await context.showFlowItem(7);
  await context.flowItemAction(7,'cancel',31);
  assert.equal(posts.at(-1).body.confirm_command_stopped,false,'a previous checkbox must not acknowledge a different phase');
  assert.equal(posts.at(-1).body.expected_version,31);
  console.log('Real flow controls bind rendered CAS, reject stale/tokenless actions, escape data, and require explicit stop verification');
})().catch(error=>{console.error(error);process.exitCode=1;});
