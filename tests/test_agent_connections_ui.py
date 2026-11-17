"""Node config checks, plus optional installed-browser TEMP owner-flow checks.

No application import, database, live API, external network or user browser profile.
Browser tests use an owned temporary profile and mocked owner HTTP transport.
"""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "promptpilot" / "static"


def test_agent_client_configs_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed on this test host")
    result = subprocess.run([node, "--test", str(Path(__file__).with_suffix(".cjs"))],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


FIXTURE = r"""() => {
  const projects = [{id:'other',name:'Other project',repository_url:'https://github.com/example/other',repository_path:'fixture-other',enabled:true}];
  const connections = [];
  window.fixture = {requests:[],copies:[],projects,connections,failCreate:false,delayCreate:false};
  const f = window.fixture;
  f.request = async (path, init = {}) => {
    const method = init.method || 'GET';
    const body = init.body ? JSON.parse(init.body) : null;
    f.requests.push({path,method,body});
    if (method === 'GET' && f.failLoadStatus) {const error = new Error('PRIVATE-ERROR-DO-NOT-RENDER'); error.status = f.failLoadStatus; throw error;}
    if (method === 'GET' && path.endsWith('/projects')) return {projects: structuredClone(projects)};
    if (method === 'GET' && path.endsWith('/connections')) return {connections: structuredClone(connections)};
    if (method === 'POST' && path.endsWith('/projects')) {
      const effective = {...body,workflow_config:{...body.workflow_config,fixture_effective:true}};
      const index = projects.findIndex(p => p.id === body.id);
      if (index >= 0) projects[index] = effective; else projects.push(effective);
      return {project: structuredClone(effective)};
    }
    if (method === 'POST' && path.endsWith('/connections')) {
      if (f.failCreate) throw new Error('PRIVATE-ERROR-DO-NOT-RENDER');
      if (f.delayCreate) await new Promise(resolve => {f.resolveCreate = resolve;});
      const connection = {id:'fixture-connection',...body,revoked_at:null};
      connections.push(connection);
      return {connection,token:'FIXTURE-ONE-TIME-SECRET'};
    }
    if (method === 'DELETE') {
      connections.find(c => path.endsWith('/' + c.id)).revoked_at = 'fixture-revoked';
      return {ok:true};
    }
    throw new Error('Unexpected owner request');
  };
  window.fetch = () => {throw new Error('Physical HTTP forbidden');};
  window.agentUI = AgentConnections.mount({request:f.request,copy:text=>f.copies.push(text),confirm:()=>true,baseUrl:'https://pilot.example:9443/'});
}"""


@pytest.fixture
def agent_page(tmp_path):
    if importlib.util.find_spec("playwright") is None:
        pytest.skip("Optional Playwright/browser is not installed")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip("Optional Chromium is not installed")
        profile = tmp_path / "owned-chromium-profile"
        args = ["--use-mock-keychain"] if sys.platform == "darwin" else []
        context = p.chromium.launch_persistent_context(str(profile), headless=True, args=args)
        try:
            page = context.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content('<!doctype html><html lang="ru"><head><meta charset="utf-8"></head><body></body></html>')
            page.add_style_tag(path=str(STATIC / "agent-connections.css"))
            # Existing host modal styles may appear later in index.html.
            page.add_style_tag(content=".modal { max-width:780px; width:100%; padding:20px; max-height:85vh; overflow-y:auto; } .modal h2 { font-size:16px; margin-bottom:14px; display:flex; }")
            page.add_script_tag(path=str(STATIC / "agent-connections.js"))
            page.evaluate(FIXTURE)
            yield page
        finally:
            context.close()


def save_project(page):
    page.locator('input[name="id"]').fill("my-project")
    page.locator('input[name="name"]').fill("<img src=x onerror=alert(1)>")
    page.locator('input[name="repository_url"]').fill("https://github.com/example/repository")
    page.locator('input[name="repository_path"]').fill("fixture-owned-repository")
    page.get_by_text("Расширенные настройки проекта", exact=True).click()
    page.locator('textarea[name="workflow_config"]').fill('{"require_approval":true}')
    page.get_by_role("button", name="Сохранить проект", exact=True).click()
    page.wait_for_function("fixture.projects.length === 2 && !document.querySelector('button[type=submit]').disabled")


def create_connection(page):
    page.locator('input[name="connection_name"]').fill("My client")
    page.locator('input[name="project_id"][value="my-project"]').check()
    page.get_by_role("button", name="Создать подключение", exact=True).click()
    page.wait_for_function("!document.querySelector('.agent-issued').hidden")


def test_owner_flow_scopes_one_time_token_config_copy_close_and_revoke(agent_page):
    page = agent_page
    assert page.evaluate("fixture.requests") == []
    page.evaluate("agentUI.open()")
    assert [r["method"] for r in page.evaluate("fixture.requests")] == ["GET", "GET"]
    save_project(page)
    project = page.evaluate("fixture.projects[1]")
    assert project["default_executor"] == "zcode" and project["reviewer"] == "codex"
    assert project["readable_paths"] == ["README.md", "AGENTS.md"]
    assert project["deliverables"] == ["RESULT.md"]
    assert project["workflow_config"] == {"require_approval": True, "fixture_effective": True}
    assert json.loads(page.locator('textarea[name="workflow_config"]').input_value()) == project["workflow_config"]
    assert page.locator("img").count() == 0  # owner names remain text, not HTML.
    create_connection(page)
    assert page.evaluate("fixture.connections[0].project_ids") == ["my-project"]
    token = page.get_by_label("Одноразовый токен подключения")
    assert token.get_attribute("type") == "password"
    assert token.input_value() == "FIXTURE-ONE-TIME-SECRET"
    assert "FIXTURE-ONE-TIME-SECRET" not in page.content()
    page.get_by_role("button", name="Скопировать HTTP MCP JSON", exact=True).click()
    page.get_by_role("button", name="Скопировать Codex TOML", exact=True).click()
    copies = page.evaluate("fixture.copies")
    assert json.loads(copies[0])["mcpServers"]["promptpilot"]["url"] == "https://pilot.example:9443/api/agents/mcp"
    assert 'http_headers = { Authorization = "Bearer FIXTURE-ONE-TIME-SECRET" }' in copies[1]
    page.get_by_role("button", name="Закрыть", exact=True).click()
    assert token.input_value() == ""
    page.evaluate("agentUI.open()")
    assert page.locator(".agent-issued").is_hidden()
    assert page.get_by_text("Токен скрыт", exact=True).is_visible()
    assert len([r for r in page.evaluate("fixture.requests") if r["method"] == "POST" and r["path"].endswith("/connections")]) == 1
    page.get_by_role("button", name="Отозвать", exact=True).click()
    page.get_by_text("Доступ отозван", exact=True).wait_for()
    assert len([r for r in page.evaluate("fixture.requests") if r["method"] == "DELETE"]) == 1


def test_mobile_layout_and_invalid_advanced_json_cannot_write(agent_page):
    page = agent_page
    page.set_viewport_size({"width": 320, "height": 640})
    page.evaluate("agentUI.open()")
    save_project(page)
    assert page.evaluate("document.querySelector('dialog').scrollWidth <= document.querySelector('dialog').clientWidth + 1")
    before = len(page.evaluate("fixture.requests"))
    page.locator('textarea[name="workflow_config"]').fill("[]")
    page.get_by_role("button", name="Сохранить проект", exact=True).click()
    page.get_by_text("Расширенные настройки должны быть JSON-объектом.", exact=True).wait_for()
    assert len(page.evaluate("fixture.requests")) == before


def test_immediate_close_and_reopen_does_not_lose_new_session(agent_page):
    page = agent_page
    page.evaluate("agentUI.open()")
    page.evaluate("async () => { agentUI.close(); await agentUI.open(); }")
    assert page.locator("dialog").is_visible()
    assert page.get_by_role("button", name="Сохранить проект", exact=True).is_enabled()
    assert all(r["method"] == "GET" for r in page.evaluate("fixture.requests"))


def test_objective_only_project_saves_without_github_and_preserves_write_scope(agent_page):
    page = agent_page
    page.evaluate("agentUI.open()")
    page.locator('input[name="id"]').fill("local-project")
    page.locator('input[name="name"]').fill("Local Git project")
    page.locator('input[name="repository_path"]').fill("fixture-local-git")
    page.get_by_text("Расширенные настройки проекта", exact=True).click()
    page.locator('textarea[name="allowed_paths"]').fill("docs/\nREADME.md")
    page.get_by_role("button", name="Сохранить проект", exact=True).click()
    page.wait_for_function("fixture.projects.length === 2")
    saved = page.evaluate("fixture.projects[1]")
    assert saved["repository_url"] is None
    assert saved["allowed_paths"] == ["docs/", "README.md"]
    assert saved["deliverables"] == ["RESULT.md"]
    assert saved["workflow_config"]["fixture_effective"] is True


def test_close_during_create_never_reveals_late_secret_or_retries(agent_page):
    page = agent_page
    page.evaluate("agentUI.open()")
    save_project(page)
    page.evaluate("fixture.delayCreate = true")
    page.locator('input[name="connection_name"]').fill("Delayed client")
    page.locator('input[name="project_id"][value="my-project"]').check()
    page.get_by_role("button", name="Создать подключение", exact=True).click()
    page.wait_for_function("typeof fixture.resolveCreate === 'function'")
    page.get_by_role("button", name="Закрыть", exact=True).click()
    page.evaluate("fixture.resolveCreate()")
    page.wait_for_function("fixture.connections.length === 1")
    assert page.get_by_label("Одноразовый токен подключения").input_value() == ""
    page.evaluate("agentUI.open()")
    assert page.locator(".agent-issued").is_hidden()
    assert len([r for r in page.evaluate("fixture.requests") if r["method"] == "POST" and r["path"].endswith("/connections")]) == 1


def test_navigation_clears_one_time_secret_without_any_new_request(agent_page):
    page = agent_page
    page.evaluate("agentUI.open()")
    save_project(page)
    create_connection(page)
    before = len(page.evaluate("fixture.requests"))
    page.evaluate("window.dispatchEvent(new Event('pagehide'))")
    assert page.get_by_label("Одноразовый токен подключения").input_value() == ""
    assert page.locator("dialog").is_hidden()
    assert len(page.evaluate("fixture.requests")) == before


def test_clipboard_fallback_cleans_transient_secret_field(agent_page):
    page = agent_page
    page.evaluate("""() => {
      agentUI.destroy();
      Object.defineProperty(navigator, 'clipboard', {value:undefined, configurable:true});
      document.execCommand = command => {
        if (command !== 'copy') throw new Error('Unexpected command');
        fixture.copies.push(document.querySelector('textarea[aria-hidden=true]').value);
        return true;
      };
      window.agentUI = AgentConnections.mount({request:fixture.request,confirm:()=>true,baseUrl:'https://pilot.example/'});
    }""")
    page.evaluate("agentUI.open()")
    save_project(page)
    create_connection(page)
    page.get_by_role("button", name="Скопировать HTTP MCP JSON", exact=True).click()
    assert json.loads(page.evaluate("fixture.copies[0]"))["mcpServers"]["promptpilot"]["headers"]["Authorization"] == "Bearer FIXTURE-ONE-TIME-SECRET"
    assert page.locator("textarea[aria-hidden=true]").count() == 0
    assert "FIXTURE-ONE-TIME-SECRET" not in page.content()


def test_unconfirmed_create_disables_mutation_without_rendering_private_error(agent_page):
    page = agent_page
    page.evaluate("agentUI.open()")
    save_project(page)
    page.evaluate("fixture.failCreate = true")
    page.locator('input[name="connection_name"]').fill("Unavailable client")
    page.locator('input[name="project_id"][value="my-project"]').check()
    page.get_by_role("button", name="Создать подключение", exact=True).click()
    page.wait_for_function("document.querySelector('.agent-status').dataset.state === 'error'")
    assert page.get_by_role("button", name="Создать подключение", exact=True).is_disabled()
    assert "PRIVATE-ERROR-DO-NOT-RENDER" not in page.content()
    assert page.locator(".agent-issued").is_hidden()


def test_missing_owner_bootstrap_explains_setup_and_keeps_readonly(agent_page):
    page = agent_page
    page.evaluate("fixture.failLoadStatus = 403")
    page.evaluate("agentUI.open()")
    assert "PP_API_TOKEN" in page.locator(".agent-status").inner_text()
    assert page.get_by_role("button", name="Создать подключение", exact=True).is_disabled()
    assert "PRIVATE-ERROR-DO-NOT-RENDER" not in page.content()
    assert all(r["method"] == "GET" for r in page.evaluate("fixture.requests"))
