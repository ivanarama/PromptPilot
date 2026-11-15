"""Real-browser acceptance against an isolated API and synthetic loopback peer.

No workers, real providers, mail, production peers, or live external clients.
Evidence is deliberately labelled test fixtures, not article execution proof.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    data, home, repository = [output / name for name in ("data", "home", "repository")]
    for directory in (data, home, repository):
        directory.mkdir()
    (repository / "README.md").write_text("UI acceptance fixture only.\n", encoding="utf-8")
    for command in (["git", "init", "-b", "main"], ["git", "add", "README.md"],
                    ["git", "-c", "user.name=PromptPilot UI", "-c", "user.email=ui@example.invalid",
                     "commit", "--no-gpg-sign", "-m", "UI fixture\n\nGenerated-with: Codex"]):
        subprocess.run(command, cwd=repository, capture_output=True, check=True, timeout=20)
    (data / "pipeline_profiles.json").write_text('{"profiles":{}}\n', encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if not (key.startswith(("PP_", "CODEX_", "OPENAI_", "ANTHROPIC_", "WORKORDERS_"))
                   or "TOKEN" in key or "API_KEY" in key)}
    env.update(HOME=str(home), USERPROFILE=str(home), PP_DATA_DIR=str(data),
               PP_CODEX_APP_SERVER="0", PP_API_TOKEN="", PP_PIPELINE_SNAPSHOT_INTERVAL="0",
               PP_PROJECTS_ROOT=str(output), PYTHONUTF8="1", PYTHONUNBUFFERED="1",
               UI_SMOKE_SOURCE_TOKEN="synthetic-ui-peer-credential")
    seed = r'''
import json,sys
from promptpilot import db
from promptpilot.models import TaskCreate,WorkflowCreate,WorkflowRoundCreate,WorkflowRunCreate
p=json.loads(sys.stdin.read());db.init_db()
w=db.create_workflow(WorkflowCreate(slug='one-business-task',objective='Проверка интерфейса: одна задача и вся история',repository_path=p['repository'],candidate_branch='main'))
for number in [1,2]:
 r=db.create_workflow_round(WorkflowRoundCreate(workflow_id=w.id,round_no=number))
 for role in ['planner','executor','reviewer']:
  t=db.create_task(TaskCreate(prompt='UI fixture internal '+role,provider='codex'))
  db.create_workflow_run(WorkflowRunCreate(workflow_id=w.id,round_id=r.id,role=role,attempt_no=1,task_id=t.id,input_sha256='fixture-input'))
with db._connect()as c:c.execute("UPDATE workflows SET status='reviewing',current_round=2 WHERE id=?",(w.id,))
standalone=db.create_task(TaskCreate(prompt='Проверка интерфейса: отдельное задание',provider='codex'))
with db._connect()as c:
 for n in range(501):db._insert_task(c,TaskCreate(prompt='Служебный пример '+str(n),provider='codex'))
print(json.dumps({'workflow_id':w.id,'task_id':standalone.id,'local_business_count':503}))
'''
    seeded = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", seed],
                            cwd=root, env=env, input=json.dumps({"repository": str(repository)}).encode(),
                            capture_output=True, check=True, timeout=60)
    fixture = json.loads(seeded.stdout)
    peer_workflow = {"id": "wf_newis_ui", "slug": "newis-fixture", "objective": "Тестовая заявка NewIS",
                     "repository_path": "fixture-only", "candidate_branch": "main",
                     "status": "awaiting_human", "current_round": 1, "state_version": 4,
                     "created_at": "2026-11-15T00:00:00+00:00", "updated_at": "2026-11-15T00:00:00+00:00",
                     "config": {"workorder_guard": {"lease_token": "NEVER-EXPOSE-UI-GUARD"}}}

    class Peer(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Authorization") != "Bearer synthetic-ui-peer-credential":
                return self.reply(401, {})
            parsed = urlsplit(self.path)
            if parsed.path == "/api/workflows":
                return self.reply(200, [peer_workflow] if int(parse_qs(parsed.query).get("offset", ["0"])[0]) == 0 else [])
            if parsed.path == "/api/workflows/wf_newis_ui":
                return self.reply(200, peer_workflow)
            if parsed.path in ["/api/workflows/wf_newis_ui/rounds", "/api/workflows/wf_newis_ui/events"]:
                return self.reply(200, [])
            return self.reply(404, {})

        def reply(self, status, value):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *unused):
            pass

    peer = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    sources = output / "task-sources.json"
    sources.write_text(json.dumps({"version": 1, "sources": [{
        "id": "newis-ui", "label": "NewIS · тестовый источник",
        "base_url": f"http://127.0.0.1:{peer.server_port}", "token_env": "UI_SMOKE_SOURCE_TOKEN",
    }]}), encoding="utf-8")
    env["PP_TASK_SOURCES_FILE"] = str(sources)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    stdout = (output / "api.stdout").open("wb")
    stderr = (output / "api.stderr").open("wb")
    command = [sys.executable, "-B", "-m", "uvicorn", "promptpilot.api:app",
               "--host", "127.0.0.1", "--port", str(port), "--no-access-log"]
    server = subprocess.Popen(command, cwd=root, env=env, stdout=stdout, stderr=stderr,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    stdout.close()
    stderr.close()
    checks, errors = [], []
    try:
        for _ in range(60):
            if server.poll() is not None:
                raise RuntimeError("Isolated API exited; inspect private stderr")
            try:
                with urlopen(url + "/api/task-feed?local_only=true&limit=1", timeout=2) as response:
                    assert response.status == 200
                break
            except OSError:
                time.sleep(.25)
        else:
            raise RuntimeError("Isolated API startup timed out")
        from playwright.sync_api import sync_playwright
        browser_path = next((path for path in [
            shutil.which("msedge"), r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            shutil.which("chromium"), shutil.which("google-chrome"),
        ] if path and Path(path).is_file()), None)
        if browser_path is None:
            raise RuntimeError("A local Chromium browser is required")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=browser_path, headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 960})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url, wait_until="domcontentloaded")
            page.locator("article.feed-row").first.wait_for()
            with urlopen(url + "/api/task-feed?limit=1", timeout=5) as response:
                payload = json.load(response)
            assert payload["total"] == fixture["local_business_count"] + 1
            assert "NEVER-EXPOSE-UI-GUARD" not in json.dumps(payload)
            checks.append("business_count_and_guard_redaction")
            page.locator("#feedSearch").fill("Проверка интерфейса")
            page.wait_for_function("document.querySelectorAll('article.feed-row').length===2")
            row = page.locator(f'article[data-feed-key="local:workflow:{fixture["workflow_id"]}"]')
            row.locator("button.feed-toggle").click()
            history = row.locator('details[data-feed-section="history"]')
            history.wait_for()
            assert history.get_attribute("open") is None
            history.locator("summary").first.click()
            assert row.locator(".feed-history-row").count() == 6
            assert page.locator("article.feed-row").count() == 2
            checks.append("six_role_attempts_stay_inside_one_row")
            standalone = page.locator(f'article[data-feed-key="local:task:{fixture["task_id"]}"]')
            standalone.locator("button.feed-toggle").click()
            standalone.locator('details[data-feed-section="history"]').wait_for()
            assert standalone.locator('details[data-feed-section="history"]').get_attribute("open") is None
            standalone.locator('details[data-feed-section="advanced"] > summary').click()
            standalone.locator(f'#note-{fixture["task_id"]}').fill("UI acceptance note")
            standalone.get_by_role("button", name="Записать", exact=True).click()
            page.wait_for_timeout(350)
            with urlopen(url + f'/api/tasks/{fixture["task_id"]}', timeout=3) as response:
                assert json.load(response)["note"] == "UI acceptance note"
            checks.append("history_defaults_and_existing_note_control")
            page.locator("#feedSearch").fill("")
            page.locator("#feedSource").select_option("newis-ui")
            page.wait_for_function("document.querySelectorAll('article.feed-row').length===1")
            peer_row = page.locator("article.feed-row")
            peer_row.locator("button.feed-toggle").click()
            peer_row.get_by_text("Просмотр подключённого источника", exact=True).wait_for()
            assert peer_row.get_by_role("button", name="Управление процессом").count() == 0
            assert "Источник показывает только процессы" in page.locator("#feedSourceStatus").inner_text()
            checks.append("legacy_peer_is_readonly_and_explicitly_partial")
            page.locator("#feedSource").select_option("")
            page.wait_for_function("document.querySelectorAll('article.feed-row').length===100")
            for _ in range(5):
                old = page.locator("#feedPageStatus").inner_text()
                page.locator("#feedMore").click()
                page.wait_for_function("previous=>document.getElementById('feedPageStatus').textContent!==previous", arg=old)
            assert page.locator("#feedPageStatus").inner_text().startswith("501")
            assert page.locator("article.feed-row").count() == 4
            assert page.locator("#feedMore").is_hidden()
            checks.append("business_rows_after_500_are_reachable")
            page.get_by_role("button", name="Новая задача", exact=True).click()
            page.locator("#wfNewSlug").fill("ui-browser-created")
            page.locator("#wfNewBranch").fill("ui-browser-created")
            page.locator("#wfNewRepo").fill(str(repository))
            page.locator("#wfNewObjective").fill("Подготовить инструкцию и сдать RESULT.md")
            page.locator("#wfWizardNext").click()
            page.locator("#wfNewExecutionMode").select_option("external")
            page.locator("#wfNewExternalClientId").fill("zcode")
            page.locator("#wfNewExternalDeliverables").fill("RESULT.md")
            page.locator("#wfNewReviewerProvider").select_option("codex")
            assert page.evaluate("wfWizardBody().config.planning.require_approval") is True
            assert page.evaluate("wfWizardBody().plan[0].execution_mode") == "external"
            page.locator("#wfWizardNext").click()
            page.locator("#wfNewGateCommands").fill("git diff --check")
            page.locator("#wfWizardNext").click()
            page.wait_for_function("!document.getElementById('wfWizardCreate').disabled")
            page.locator("#wfWizardCreate").click()
            page.wait_for_function("document.getElementById('wfList').textContent.includes('ui-browser-created')")
            with urlopen(url + "/api/workflows?limit=500", timeout=3) as response:
                created = next(value for value in json.load(response) if value["slug"] == "ui-browser-created")
            assert created["status"] == "awaiting_plan_approval"
            with urlopen(url + "/api/stats", timeout=3) as response:
                assert json.load(response)["running"] == 0
            checks.append("external_wizard_requires_explicit_plan_approval_no_provider_start")
            page.get_by_role("button", name="Закрыть", exact=True).filter(visible=True).last.click()
            page.locator("#feedSearch").fill("Проверка интерфейса")
            page.wait_for_function("document.querySelectorAll('article.feed-row').length===2")
            page.screenshot(path=str(output / "desktop-test-fixtures.png"))
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(output / "mobile-test-fixtures.png"))
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            checks.append("mobile_no_horizontal_overflow")
            browser.close()
        assert not errors, errors
        result = {"ok": True, "checks": checks, "browser_errors": errors,
                  "data_kind": "synthetic acceptance fixtures, not article execution proof",
                  "providers_started": 0, "production_connections": 0,
                  "at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=3)


if __name__ == "__main__":
    main()
