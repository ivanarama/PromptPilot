const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const html=fs.readFileSync('promptpilot/static/index.html','utf8');
const source=html.slice(html.indexOf('function workflowElicitationForm('),html.indexOf('function renderWorkflowDetail('));
const c={esc:v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),escAttr:v=>String(v).replaceAll('"','&quot;')};
vm.createContext(c);vm.runInContext(source,c);
const rendered=c.workflowElicitationForm({id:'workflow-one'},{question:{question:'<script>question</script>',action:'echo <unsafe>',options:['yes','no']},
 expected_version:12,request_id:'request-one',binding_sha256:'a'.repeat(64)});
assert.match(rendered,/data-version="12"/);assert.match(rendered,/data-request="request-one"/);
assert.doesNotMatch(rendered,/<script>/);assert.match(rendered,/&lt;script>/);
assert.match(rendered,/wfAnswerElicitation/);
console.log('Real question form escapes untrusted data and carries exact request/version binding.');
