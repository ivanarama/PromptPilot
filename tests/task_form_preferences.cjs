const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const html = fs.readFileSync('promptpilot/static/index.html', 'utf8');
const start = html.indexOf('// --- Load projects into dir select ---');
const end = html.indexOf('// --- Worker pause ---', start);
assert.ok(start >= 0 && end > start);
const checkboxMarkup = html.match(/<input[^>]*id="skipPermissions"[^>]*>/)[0];
assert.doesNotMatch(checkboxMarkup, /\bchecked\b/, 'a new browser keeps the safe default');
assert.match(checkboxMarkup, /onchange="rememberSkipPermissions\(\)"/);
assert.match(html, /\/\/ --- Init ---\s+restoreSkipPermissions\(\);/);

const stored = new Map();
const elements = {
  skipPermissions: {checked:false},
  skipPermsLabel: {style:{display:'flex'}},
  workdirSelect: {style:{display:''}, value:'', innerHTML:'',
    get options() {
      return [...this.innerHTML.matchAll(/value="([^"]*)"/g)].map(m => ({value:m[1]}));
    }},
  workdirInput: {style:{display:'none'}, value:'', focus() {}},
};
let requests = 0;
const context = {
  document:{getElementById:id => elements[id]},
  localStorage:{getItem:key => stored.get(key) ?? null,
    setItem:(key,value) => stored.set(key,String(value))},
  projectsByPath:{'/Projects/App':{path:'/Projects/App', name:'App', git:true}},
  API:'/api', fetchJSON:async () => { requests++; return []; },
  esc:value => String(value), escAttr:value => String(value), updateWorktree:() => {},
};
vm.createContext(context);
vm.runInContext(html.slice(start,end),context);

context.restoreSkipPermissions();
assert.equal(elements.skipPermissions.checked,false);
elements.skipPermissions.checked = true;
context.rememberSkipPermissions();
elements.skipPermissions.checked = false;
context.restoreSkipPermissions();
assert.equal(elements.skipPermissions.checked,true, 'an explicit choice survives reload');
elements.skipPermissions.checked = false;
context.rememberSkipPermissions();
elements.skipPermissions.checked = true;
context.restoreSkipPermissions();
assert.equal(elements.skipPermissions.checked,false, 'revoking the choice survives reload');

// A hidden session-target control never changes the stored choice.
elements.skipPermsLabel.style.display = 'none';
elements.skipPermissions.checked = true;
context.rememberSkipPermissions();
context.restoreSkipPermissions();
assert.equal(elements.skipPermissions.checked,false);
context.localStorage.getItem = () => { throw new Error('storage disabled'); };
elements.skipPermissions.checked = true;
context.restoreSkipPermissions();
assert.equal(elements.skipPermissions.checked,false, 'storage failure does not grant autonomy');
context.localStorage.getItem = key => stored.get(key) ?? null;

(async () => {
  await context.rememberWorkdir('/Projects/App');
  await context.rememberWorkdir('/Projects/app');
  const recent = context.recentWorkdirs();
  assert.equal(recent.length,2, 'different case can identify distinct server directories');
  assert.ok(recent.includes('/Projects/App') && recent.includes('/Projects/app'));
  assert.equal(requests,0, 'remembering a local choice does not refetch projects');
  assert.ok(elements.workdirSelect.innerHTML.includes('/Projects/App'));
  assert.ok(elements.workdirSelect.innerHTML.includes('/Projects/app'));
  assert.equal(elements.workdirSelect.value,'/Projects/app');
  console.log('Task form: explicit preferences persist, paths stay distinct, no extra request');
})().catch(error => {console.error(error);process.exitCode=1;});
