const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
global.fetch = () => { throw new Error('Script load must not request anything'); };
const ui = require(path.join(__dirname, '../promptpilot/static/agent-connections.js'));

test('endpoint uses actual browser origin without copying URL query or credentials', () => {
  assert.equal(ui.endpoint('https://pilot.example:9443/settings?private=do-not-copy#x'), 'https://pilot.example:9443/api/agents/mcp');
  assert.equal(ui.endpoint('http://localhost:8420/'), 'http://localhost:8420/api/agents/mcp');
  assert.throws(() => ui.endpoint('https://user:password@pilot.example/'));
  assert.throws(() => ui.endpoint('file:///private/index.html'));
});
test('HTTP JSON imports as one scoped server, escaping the one-time secret as data', () => {
  const token = 'fixture"\n<script>secret</script>';
  const result = JSON.parse(ui.clientConfig('http', 'https://pilot.example/api/agents/mcp', token));
  assert.deepEqual(result, {mcpServers: {promptpilot: {type: 'http', url: 'https://pilot.example/api/agents/mcp', headers: {Authorization: 'Bearer ' + token}}}});
  assert.throws(() => ui.clientConfig('http', 'https://pilot.example/', ''));
});
test('Codex uses official TOML HTTP fields and does not inject a shell command', () => {
  const value = ui.clientConfig('codex', 'https://pilot.example/api/agents/mcp', 'fixture"\nsecret');
  assert.match(value, /^\[mcp_servers\.promptpilot\]\nurl = /);
  assert.match(value, /http_headers = \{ Authorization = "Bearer fixture\\"\\nsecret" \}/);
  assert.ok(!value.includes('command ='));
  assert.throws(() => ui.clientConfig('unsupported', 'https://pilot.example/', 'token'));
});
