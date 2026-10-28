const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('promptpilot/static/index.html', 'utf8');
assert.match(html, /id="provModal"[\s\S]*id="providerWizard"/);
assert.match(html, /async function openProviders\(\)[\s\S]*?await loadCatalog\(\);/);
assert.doesNotMatch(html, /id="settingsView"/);
const start = html.indexOf('let providerCatalog = []');
const end = html.indexOf('async function openProviders()', start);
class Element {
  constructor() { this.value = ''; this.children = []; this.textContent = ''; this.listeners = {}; }
  replaceChildren() { this.children = []; }
  appendChild(child) { this.children.push(child); if (!this.value && child.value) this.value = child.value; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  set innerHTML(_) { throw Error('Untrusted model must never become HTML'); }
}
const ids = ['pcStatus','pcCLI','pcBase','pcKey','pcModels','pcModel','pcTitle'];
const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
const hostile = "model');alert(1)//<svg/onload=alert(2)>";
const requests = [];
const context = {
  API:'/api', encodeURIComponent,
  document:{getElementById:id=>elements[id], createElement:()=>new Element()},
  renderProvList:async()=>{}, loadProviders:async()=>{},
  fetchJSON:async(url, opts)=>{
    requests.push([url, opts]);
    if (url.endsWith('provider-catalog')) return {items:[{id:'codex', title:'Codex', default_base:'https://example.com/v1', desc:'Agent'}]};
    if (url.endsWith('/models')) return {models:[hostile,'good-model']};
    if (url.endsWith('/create')) return {name:'new-agent', label:'New agent'};
    if (url.endsWith('/probe')) return {ok:true};
    throw Error('Unexpected request');
  },
};
vm.createContext(context);
vm.runInContext(html.slice(start,end), context);
(async()=>{
  await context.loadCatalog();
  elements.pcKey.value = 'private-key';
  await context.catalogModels();
  assert.equal(elements.pcModels.children[0].textContent, hostile);
  assert.equal(elements.pcModels.children[0].listeners.click, undefined);
  elements.pcModel.value = 'good-model';
  await context.catalogCreate();
  const body = JSON.parse(requests.find(([url])=>url.endsWith('/create'))[1].body);
  assert.equal(body.model,'good-model');
  assert.equal(body.key,'private-key');
  for (const id of ['pcKey','pcBase','pcModel','pcTitle']) assert.equal(elements[id].value,'');
  assert.equal(elements.pcModels.children.length,0);
  await elements.pcStatus.children[0].listeners.click();
  assert.ok(requests.some(([url])=>url==='/api/providers/new-agent/probe'));
  elements.pcKey.value = 'old-secret';
  context.catalogSelect();
  assert.equal(elements.pcKey.value,'');
  console.log('Reachable wizard creates, resets secrets, probes; external IDs stay text');
})().catch(error=>{console.error(error);process.exitCode=1;});
