"""Actual renderer in an isolated browser; no live API/DB/provider/client calls."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "promptpilot/static"


def test_progress_node_contracts():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    result = subprocess.run([node, "--test", str(ROOT / "tests/workflow_progress.cjs")], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def functions(html, start, end):
    return html[html.index(start):html.index(end, html.index(start))]


@pytest.fixture
def workflow_page(tmp_path):
    if importlib.util.find_spec("playwright") is None:
        pytest.skip("Optional Playwright/browser is not installed")
    from playwright.sync_api import sync_playwright
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    script = functions(html, "function workflowStatusInfo(", "function workflowRoundRow(")
    script += functions(html, "function workflowRoundRow(", "function workflowStopReason(")
    script += functions(html, "function workflowPlanHtml(", "function workflowExternalFormHtml(")
    script += functions(html, "function workflowExternalFormHtml(", "function workflowElicitationForm(")
    script += functions(html, "function renderWorkflowDetail(", "async function wfProviderChanged(")
    script += functions(html, "function esc(s)", "function timeAgo(")
    with sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip("Optional Chromium is not installed")
        profile = tmp_path / "owned-browser-profile"
        args = ["--use-mock-keychain"] if sys.platform == "darwin" else []
        context = p.chromium.launch_persistent_context(str(profile), headless=True, args=args)
        try:
            page = context.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content('<!doctype html><html lang="ru"><head><meta charset="utf-8"></head><body><div class="modal"><div id="wfDetail"></div></div></body></html>')
            page.add_style_tag(content=html.split("<style>", 1)[1].split("</style>", 1)[0])
            page.add_style_tag(path=str(STATIC / "workflow-progress.css"))
            page.add_script_tag(path=str(STATIC / "workflow-progress.js"))
            page.add_script_tag(content=script)
            page.evaluate("""() => {
              window.fetch=()=>{throw Error('Network forbidden in renderer test');};
              window.setTimeout=()=>0;
              window.workflowStopReason=()=>'';window.workflowProviderOptions=()=>'';
              window.workflowSettingsHtml=()=>'';window.uiSnapScrolls=()=>[];window.uiRestoreScrolls=()=>{};
              window.wfProviderChanged=()=>{};window.wfReindexStageCards=()=>{};
              window.fixture = {
                workflow:{id:'wf_fixture',slug:'agent-opaque-id',state_version:8,current_round:2,current_stage_id:'stage-fixture',status:'awaiting_external',objective:'Длинная цель '.repeat(400),repository_path:'fixture-repository',candidate_branch:'fixture-branch',config:{agent_task_title:'Обновить документацию',automation:{enabled:true},planning:{enabled:true}}},
                rounds:[{id:'r1',round_no:1,status:'gating',summary:{}},{id:'r2',round_no:2,status:'awaiting_external',summary:{}}],
                stages:[{id:'stage-fixture',code:'stage-01',title:'Документы',objective:'Длинная цель '.repeat(400),status:'executing',execution_mode:'external',stage_type:'integration'}],
                runs:[{id:'run-fixture',round_id:'r2',role:'executor',attempt_no:1,status:'awaiting_external',input_sha256:'a'.repeat(64),input:{execution_mode:'external',assignment:'Большое задание '.repeat(300)}}],
                progress:{workflow_id:'wf_fixture',state_version:8,current:{round_no:2,stage_index:1,stage_total:1,stage_code:'stage-01',stage_title:'Документы',status:'awaiting_external',owner:'ZCode',headline:'Ждём, когда ZCode заберёт доработку',detail:'Новый результат ещё не получен',next_action:'Исполнителю нужно передать исправленный результат.'},
                  steps:[{id:'plan',label:'План',state:'done'},{id:'execute',label:'Исполнение',state:'current'},{id:'gate',label:'Файлы',state:'waiting'},{id:'review',label:'Ревью',state:'waiting'},{id:'finish',label:'Готово',state:'waiting'}],
                  last_return:{round_no:1,basis:'current_artifact_policy',kind:'artifact_path_mismatch',summary:'Файлы находятся в другой папке.',expected_paths:['changes.patch','RESULT.md'],received_paths:['.pp-run/stage-01-17/changes.patch','.pp-run/stage-01-17/RESULT.md'],action:'Передать файлы в корне пакета результата.'},
                  history:[{round_no:1,status:'revision_required',summary:'Проверка результата не пройдена.'}]}
              };
              window.renderFixture=()=>renderWorkflowDetail(fixture.workflow,fixture.rounds,fixture.runs,[],[],{status:'approved'},fixture.stages,null,null,null,fixture.progress);
              renderFixture();
            }""")
            yield page
        finally:
            context.close()


def test_real_card_answers_round_owner_return_reason_without_opening_journal(workflow_page):
    page = workflow_page
    assert page.get_by_text("Этап 1 из 1 · Раунд 2", exact=True).is_visible()
    assert page.get_by_role("heading", name="Ждём, когда ZCode заберёт доработку", exact=True).is_visible()
    assert page.get_by_text("Новый результат ещё не получен", exact=True).is_visible()
    assert page.get_by_text("Сверка файлов по текущим правилам.", exact=True).is_visible()
    assert page.locator(".wf-progress-return").get_by_text("changes.patch", exact=True).is_visible()
    assert page.locator(".wf-progress-return").get_by_text(".pp-run/stage-01-17/changes.patch", exact=True).is_visible()
    assert page.locator(".wf-progress-history").get_by_text("Возвращён на доработку", exact=True).is_visible()
    assert page.get_by_text("План этапов · завершено 0 из 1", exact=True).is_visible()
    assert page.locator('#wfStageList').get_by_text('Ждём внешний результат', exact=True).is_visible()
    assert 'Ждём внешний результат' in page.locator('.wf-round.current').inner_text()
    assert page.locator('details[data-wf-detail="stage-objective"]').get_attribute("open") is None
    assert page.locator('details[data-wf-detail="external-assignment"]').get_attribute("open") is None
    assert page.locator('details[data-wf-detail="technical-log"]').get_attribute("open") is None
    assert "50%" not in page.locator("#wfStageList").inner_text()
    assert page.get_by_role("button", name="Сдать результат", exact=True).is_visible()
    if folder := os.environ.get("PP_TEST_WORKFLOW_SCREENSHOT_DIR"):
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.locator(".wf-progress").screenshot(path=str(Path(folder) / "desktop-fixture.png"))


def test_mobile_width_and_details_preserve_full_goal(workflow_page):
    page = workflow_page
    page.set_viewport_size({"width": 360, "height": 740})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    if folder := os.environ.get("PP_TEST_WORKFLOW_SCREENSHOT_DIR"):
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / "mobile-fixture.png"), full_page=True)
    page.get_by_text("Цель этапа полностью", exact=True).click()
    assert page.locator('.wf-goal-details .wf-round-summary').inner_text().count("Длинная цель") == 400
    assert page.locator('.wf-goal-details .wf-round-summary').is_visible()


def test_passed_gate_hides_old_return_and_current_model_claim_is_just_server_text(workflow_page):
    page = workflow_page
    page.evaluate("""() => {
      fixture.workflow.status='reviewing';fixture.workflow.state_version=9;
      fixture.progress.state_version=9;fixture.progress.current.status='reviewing';
      fixture.progress.current.headline='Результат передан на независимое ревью';
      fixture.progress.current.owner='Проверяющий';renderFixture();
    }""")
    assert page.locator(".wf-progress-return").count() == 0
    assert page.get_by_role("heading", name="Результат передан на независимое ревью", exact=True).is_visible()
    assert page.locator(".wf-progress-history").is_visible()


def test_rendered_html_is_escaped_and_bad_version_cannot_show_foreign_reason(workflow_page):
    page = workflow_page
    page.evaluate("""() => {
      fixture.progress.current.headline='<img src=x onerror=alert(1)>';
      fixture.progress.last_return.received_paths=['<svg onload=alert(2)>'];
      renderFixture();
    }""")
    assert page.locator("img,svg,script[src=x]").count() == 0
    assert page.get_by_role("heading", name="<img src=x onerror=alert(1)>", exact=True).is_visible()
    page.evaluate("fixture.progress.state_version=7;renderFixture()")
    assert page.locator(".wf-progress-return").count() == 0
    assert page.get_by_text("Подробное состояние сейчас недоступно. Показан сохранённый статус задачи.", exact=True).is_visible()


def test_manual_gate_notice_and_existing_owner_buttons_remain_visible(workflow_page):
    page = workflow_page
    page.evaluate("""() => {
      fixture.workflow.status='gating';fixture.workflow.config.automation.auto_gate=false;
      fixture.progress=null;renderFixture();
    }""")
    assert page.get_by_role("heading", name="Нужно зафиксировать результат проверок", exact=True).is_visible()
    assert page.get_by_text("Gate в ручном режиме", exact=True).is_visible()
    assert page.get_by_role("button", name="Gate PASS", exact=True).is_visible()
    assert page.get_by_role("button", name="Gate FAIL", exact=True).is_visible()
