const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('promptpilot/static/index.html', 'utf8');
const slice = (start, end) => {
  const a = html.indexOf(start), b = html.indexOf(end, a);
  assert.ok(a >= 0 && b > a);
  return html.slice(a, b);
};
const source = [
  slice('function providerLabel(', 'function effortOptions('),
  slice('function workflowProviderOptions(', 'function wfChecked('),
  slice('let provManageData = {}', 'async function provToggleHide('),
  slice('function updateProvidersForMachine(', 'function updateKeepPane('),
].join('\n');
const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
const hostile = '<svg/onload=alert(1)> "name"';
const server = {
  alpha: {human:{label:'Friendly A'}, ui_order:3, available:true, hidden:false},
  beta: {human:{label:hostile}, ui_order:1, available:true, hidden:false},
  hidden: {human:{label:'Saved hidden'}, ui_order:2, available:true, hidden:true},
  session: {human:{label:'Live'}, session_target:true, available:true, hidden:false},
};
const elements = {
  providerInput: {value:'alpha', innerHTML:''}, machineInput:{value:''},
  provList:{innerHTML:''},
};
const buttons = [{disabled:false}, {disabled:false}];
const requests = [], toasts = [];
let pending = null, fail = false, failSummary = false, failManage = false;
const context = {
  providersData:{...server}, machinesData:{remote:{providers:['beta','alpha']}},
  API:'/api', esc:escape, escAttr:escape,
  document:{getElementById:id=>elements[id], querySelectorAll:()=>buttons},
  localStorage:{getItem:()=> 'alpha', setItem:()=>{}},
  loadMachines:async()=>{}, toast:message=>toasts.push(message),
  updateSkillsToggle:()=>{}, updateModelSelect:()=>{}, updateEffortSelect:()=>{}, updateKeepPane:()=>{},
  closeSkillsPanel:()=>{}, skillsCache:{},
  fetchJSON:async(url, options)=>{
    requests.push([url,options]);
    if (url.endsWith('/reorder')) {
      if (pending) await pending;
      if (fail) throw Error('offline');
      const names = JSON.parse(options.body).names;
      names.forEach((name,index)=>{server[name].ui_order=index;});
      return {ok:true};
    }
    if (url.endsWith('/providers') && failSummary) throw Error('summary offline');
    if (url.endsWith('/providers/manage') && failManage) throw Error('manage offline');
    if (url.endsWith('/providers') || url.endsWith('/providers/manage')) return server;
    throw Error(`Unexpected request ${url}`);
  },
};
vm.createContext(context);
vm.runInContext(source, context);
const names = data => JSON.parse(JSON.stringify(context.orderedProviderEntries(data).map(([name])=>name)));
(async()=>{
  assert.deepEqual(names(server), ['beta','hidden','alpha','session']);
  assert.deepEqual(names({a:{},b:{ui_order:true},c:{ui_order:-1}}), ['a','b','c']);
  assert.equal(context.providerLabel('fallback', {human:{label:'  '}}), 'fallback');
  const options = context.providerOptions('hidden');
  assert.match(options, /value="hidden" selected/);
  assert.match(options, /Saved hidden/);
  assert.ok(!options.includes('<svg'));
  assert.ok(options.indexOf('value="beta"') < options.indexOf('value="alpha"'));
  const workflow = context.workflowProviderOptions('alpha', false);
  assert.ok(!workflow.includes('value="session"'));
  assert.match(workflow, /value="alpha" selected>Friendly A/);
  assert.match(context.workflowProviderOptions('missing'), /value="missing" selected/);
  await context.loadProviders();
  assert.equal(elements.providerInput.value, 'alpha');
  assert.ok(!elements.providerInput.innerHTML.includes('value="hidden"'));
  elements.machineInput.value = 'remote';
  context.updateProvidersForMachine();
  assert.equal(elements.providerInput.value, 'alpha');
  assert.ok(!elements.providerInput.innerHTML.includes('value="session"'));
  await context.renderProvList();
  assert.ok(elements.provList.innerHTML.includes('Friendly A'));
  assert.ok(!elements.provList.innerHTML.includes('<svg'));
  assert.match(elements.provList.innerHTML, /data-provider="alpha"/);

  let release;
  pending = new Promise(resolve=>{release=resolve;});
  const saving = context.provMove('alpha', -1);
  assert.ok(buttons.every(button=>button.disabled));
  await context.provMove('beta', 1); // a second click cannot race the in-flight save
  release(); await saving; pending=null;
  const posts = requests.filter(([url])=>url.endsWith('/reorder'));
  assert.equal(posts.length,1);
  assert.deepEqual(JSON.parse(posts[0][1].body).names,['beta','alpha','hidden','session']);
  assert.equal(elements.providerInput.value,'alpha', 'the selected execution ID survives reorder');
  assert.deepEqual(names(server),['beta','alpha','hidden','session']);

  fail=true;
  await context.provMove('alpha',-1);
  assert.equal(toasts.length,1);
  fail=false;
  await context.provMove('alpha',-1); // failed save releases the UI lock
  assert.deepEqual(names(server),['alpha','beta','hidden','session']);

  toasts.length=0; failSummary=true;
  await context.provMove('beta',-1);
  assert.deepEqual(names(server),['beta','alpha','hidden','session']);
  assert.equal(toasts.length,1);
  assert.match(toasts[0], /Порядок сохранён/);
  assert.doesNotMatch(toasts[0], /не сохранён/);
  failSummary=false;

  toasts.length=0; fail=true; failManage=true;
  await context.provMove('alpha',-1); // neither POST nor finally-refresh can strand the UI
  assert.equal(toasts.length,1);
  const orderButtons = elements.provList.innerHTML.match(/<button[^>]+data-provider[^>]*>/g);
  assert.equal(orderButtons.length,8);
  assert.equal(orderButtons.filter(markup=>markup.includes(' disabled')).length,2);
  fail=false; failManage=false;
  await context.provMove('alpha',-1);
  assert.deepEqual(names(server),['alpha','beta','hidden','session']);
  console.log('Provider labels/order: escaped display, stable execution IDs, hidden/session filters, serialized save and retry');
})().catch(error=>{console.error(error);process.exitCode=1;});
