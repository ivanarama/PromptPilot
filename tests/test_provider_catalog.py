"""Каталог известных CLI (визард подключения помощников): каталог цел,
подбор моделей с сервера (ключ не светится), создание помощника с
коротким именем «Инструмент · Модель», уникальность имени."""

import asyncio
import json

import httpx

import pytest

from promptpilot.api import app
from promptpilot.provider_catalog import (
    CATALOG,
    build_provider_entry,
    catalog_entry,
    pretty_model,
    slugify,
)


def _request(method, path, **kwargs):
    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8420") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(request())


def test_catalog_entries_are_complete():
    ids = set()
    for e in CATALOG:
        assert e["id"] not in ids, "дубликат id в каталоге"
        ids.add(e["id"])
        assert e["title"] and e["desc"] and e["exe"]
        assert e["install"], f"{e['id']}: без install-команды визард не поставит CLI"
        assert e["cmd"], f"{e['id']}: без cmd-шаблона помощник не запустится"
        assert e["api"] in ("openai", "anthropic")
        assert e["env_key"] and e["env_base"]


def test_slug_and_pretty_model():
    assert slugify("Astra-3.1 Pro!") == "astra-3-1-pro"
    assert pretty_model("astra-3.1-pro") == "Astra 3.1 Pro"
    assert pretty_model("") == ""


def test_build_provider_entry_covers_choice():
    entry = catalog_entry("codex")
    prov = build_provider_entry(entry, "https://api.example.com/v1/",
                                "sk-xxx", "astra-3.1-pro")
    assert prov["cmd"] == ("codex exec --skip-git-repo-check "
                           "--model astra-3.1-pro --json -")
    assert prov["prompt_stdin"] is True
    assert prov["env"]["OPENAI_BASE_URL"] == "https://api.example.com/v1"
    assert prov["env"]["OPENAI_API_KEY"] == "sk-xxx"
    assert prov["human"]["label"] == "Кодекс · Astra 3.1 Pro"
    assert prov["models"] == ["astra-3.1-pro"]


class _FakeResp:
    def __init__(self, payload):
        self._p = json.dumps(payload).encode()

    def read(self, size=-1):
        return self._p if size < 0 else self._p[:size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOpener:
    def __init__(self, payload, captured):
        self.payload, self.captured = payload, captured

    def open(self, req, timeout=None):
        self.captured["url"] = req.full_url
        self.captured["auth"] = req.headers.get("Authorization")
        # urllib капитализирует имена заголовков: «x-api-key» -> «X-api-key»
        self.captured["xapi"] = req.headers.get("X-api-key")
        return _FakeResp(self.payload)


@pytest.fixture()
def isolated_providers(tmp_path, monkeypatch):
    from promptpilot import config

    monkeypatch.setattr(config, "DB_DIR", tmp_path)
    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path))
    get_cache = getattr(config, "_providers_cache", None)
    if isinstance(get_cache, dict):
        get_cache.clear()
    yield tmp_path


def test_catalog_listing_marks_installed(isolated_providers, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda exe: exe == "codex" and "codex.CMD" or None)
    r = _request("GET", "/api/provider-catalog")
    assert r.status_code == 200
    by_id = {it["id"]: it for it in r.json()["items"]}
    assert by_id["codex"]["installed"] is True
    assert by_id["claude"]["installed"] is False


def test_models_fetch_openai_style_key_stays_server_side(isolated_providers, monkeypatch):
    captured = {}
    payload = {"data": [{"id": "astra-3.1-pro"}, {"id": "zeta"}, {"id": "astra-3.1-pro"}]}
    monkeypatch.setattr("urllib.request.build_opener",
                        lambda *a, **k: _FakeOpener(payload, captured))
    r = _request("POST", "/api/provider-catalog/codex/models",
                    json={"base": "https://api.example.com/v1", "key": "sk-secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["models"] == ["astra-3.1-pro", "zeta"]
    assert "sk-secret" not in r.text
    assert captured["url"].endswith("/models")
    assert captured["auth"] == "Bearer sk-secret"


def test_models_fetch_anthropic_style(isolated_providers, monkeypatch):
    captured = {}
    payload = {"data": [{"id": "claude-sonnet-5"}]}
    monkeypatch.setattr("urllib.request.build_opener",
                        lambda *a, **k: _FakeOpener(payload, captured))
    r = _request("POST", "/api/provider-catalog/claude/models",
                    json={"base": "https://api.anthropic.com", "key": "sk-ant"})
    assert r.status_code == 200
    assert r.json()["models"] == ["claude-sonnet-5"]
    assert captured["url"].endswith("/v1/models")
    assert captured["xapi"] == "sk-ant"


def test_models_fetch_empty_answer_is_error(isolated_providers, monkeypatch):
    monkeypatch.setattr("urllib.request.build_opener",
                        lambda *a, **k: _FakeOpener({"data": []}, {}))
    r = _request("POST", "/api/provider-catalog/codex/models",
                    json={"base": "https://api.example.com/v1", "key": "k"})
    assert r.status_code == 502


def test_create_provider_short_name_and_uniqueness(isolated_providers):
    r = _request("POST", "/api/provider-catalog/goose/create",
                    json={"base": "https://z.example/v1", "key": "sk-1",
                          "model": "glm-5.3"})
    assert r.status_code == 200
    first = r.json()
    assert first["name"] == "goose-glm-5-3"
    assert first["label"] == "Гусь · Glm 5.3"

    saved = json.loads((isolated_providers / "providers.json").read_text(encoding="utf-8"))
    prov = saved["goose-glm-5-3"]
    assert prov["cmd"] == "goose run --model glm-5.3 -i -"
    assert prov["prompt_stdin"] is True
    assert prov["env"]["GOOSE_PROVIDER"] == "openai"
    assert prov["env"]["OPENAI_API_KEY"] == "sk-1"

    r2 = _request("POST", "/api/provider-catalog/goose/create",
                     json={"base": "https://z.example/v1", "key": "sk-2",
                           "model": "glm-5.3"})
    assert r2.status_code == 200
    assert r2.json()["name"] == "goose-glm-5-3-2"


def test_create_requires_model(isolated_providers):
    r = _request("POST", "/api/provider-catalog/codex/create", json={"base": "b", "key": "k"})
    assert r.status_code == 400


@pytest.mark.parametrize("model", ["--help", "good --dangerously-skip-permissions", "a\\nb", "x');alert(1)//", "<svg/onload=alert(1)>"])
def test_model_argument_injection_rejected(isolated_providers, model):
    r = _request("POST", "/api/provider-catalog/codex/create", json={"base": "https://a.example/v1", "model": model})
    assert r.status_code == 400
    assert not (isolated_providers / "providers.json").exists()


@pytest.mark.parametrize("base", ["file:///etc/passwd", "https://user:secret@example.com/v1", "https://example.com/v1?key=secret", "https://example.com/#x", "http://a.example:999999", "https://a.example/{file:/secret}"])
def test_unsafe_endpoint_rejected_without_echoing_credentials(isolated_providers, base):
    r = _request("POST", "/api/provider-catalog/codex/models", json={"base": base, "key": "sk-secret"})
    assert r.status_code == 400
    assert "sk-secret" not in r.text and "user:secret" not in r.text


@pytest.mark.parametrize("cid", [e["id"] for e in CATALOG])
def test_each_adapter_delivers_selected_model_once(isolated_providers, cid):
    from promptpilot import config
    response = _request("POST", f"/api/provider-catalog/{cid}/create", json={"base": "https://a.example/v1", "key": "k", "model": "selected-model"})
    assert response.status_code == 200
    name = response.json()["name"]
    prov = config.load_providers()[name]
    cmd = config.build_cmd(name, "prompt", guard=False)
    assert cmd.count("--model") == 1
    expected = "promptpilot/selected-model" if cid == "opencode" else "selected-model"
    assert cmd[cmd.index("--model") + 1] == expected
    override = config.build_cmd(name, "prompt", model="other-model", guard=False)
    assert override.count("--model") == 1 and override[override.index("--model") + 1] == "other-model"
    if cid == "claude":
        assert prov["prompt_stdin"] and prov["supports_skills"] and prov["supports_effort"]
        assert "--resume" in config.build_cmd(name, "prompt", session_id="s", guard=False)
    if cid == "goose":
        assert prov["env"]["OPENAI_HOST"] == "https://a.example"
        assert prov["env"]["OPENAI_BASE_PATH"] == "v1/chat/completions"
    if cid == "opencode":
        options = json.loads(prov["env"]["OPENCODE_CONFIG_CONTENT"])["provider"]["promptpilot"]["options"]
        assert options["baseURL"] == "https://a.example/v1" and options["apiKey"] == "{env:OPENAI_API_KEY}"
    if cid == "pi":
        from pathlib import Path
        path = Path(prov["env"]["PI_CODING_AGENT_DIR"]) / "models.json"
        assert path.is_relative_to(isolated_providers)
        assert json.loads(path.read_text())["providers"]["promptpilot"]["apiKey"] == "$OPENAI_API_KEY"


def test_reorder_and_hidden_builtin_keep_command_and_secret(isolated_providers):
    from promptpilot import config
    config.save_catalog_provider("claude", {"cmd": "claude -p {prompt}", "models": ["m"], "description": "c", "env": {"ANTHROPIC_AUTH_TOKEN": "real-secret"}})
    before = config.load_providers()["claude"]
    assert _request("POST", "/api/providers/reorder", json={"names": ["claude", "codex"]}).status_code == 200
    assert config.load_providers_detailed()["codex"]["cmd"] == config.BUILTIN_PROVIDERS["codex"]["cmd"]
    assert _request("DELETE", "/api/providers/claude").status_code == 409
    after = config.load_providers()["claude"]
    assert before["cmd"] == after["cmd"] and after["env"]["ANTHROPIC_AUTH_TOKEN"] == "real-secret"


def test_busy_provider_cannot_be_deleted(isolated_db, isolated_providers):
    from promptpilot import config
    from promptpilot.models import TaskCreate, WorkflowCreate
    config.save_provider("busy", "echo {prompt}")
    t = isolated_db.create_task(TaskCreate(prompt="x", provider="busy"))
    assert _request("DELETE", "/api/providers/busy").status_code == 409
    isolated_db.mark_completed(t.id, "done")
    isolated_db.create_workflow(WorkflowCreate(slug="uses-busy", objective="x", repository_path=str(isolated_providers), candidate_branch="main", config={"roles": {"reviewer": {"provider": "busy"}}}))
    assert _request("DELETE", "/api/providers/busy").status_code == 409
    assert "busy" in config.load_providers()


def test_model_response_bounded_and_untrusted_ids_filtered(isolated_providers, monkeypatch):
    captured = {}
    monkeypatch.setattr("urllib.request.build_opener", lambda *a, **k: _FakeOpener({"data": [{"id": "ok-model"}, {"id": "x');alert(1)//"}, {"id": "--help"}]}, captured))
    response = _request("POST", "/api/provider-catalog/codex/models", json={"base": "https://example.com/v1"})
    assert response.json()["models"] == ["ok-model"]
    monkeypatch.setattr("urllib.request.build_opener", lambda *a, **k: _FakeOpener({"padding": "x" * (1024 * 1024 + 100)}, {}))
    assert _request("POST", "/api/provider-catalog/codex/models", json={"base": "https://example.com/v1"}).status_code == 502


def test_redirect_does_not_forward_key(isolated_providers):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/stolen")
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        r = _request("POST", "/api/provider-catalog/codex/models", json={"base": f"http://127.0.0.1:{server.server_port}", "key": "sk-secret"})
        assert r.status_code == 502 and "sk-secret" not in r.text
        assert received == [("/models", "Bearer sk-secret")]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_goose_installer_selects_host_platform():
    from promptpilot.provider_catalog import install_command
    entry = catalog_entry("goose")
    assert install_command(entry, "darwin") == ["brew", "install", "block-goose-cli"]
    assert install_command(entry, "linux")[0] == "bash"
    assert install_command(entry, "win32")[0] == "powershell"


def test_edit_and_clone_keep_stdin_and_original_env_names(isolated_providers):
    from promptpilot import config
    r = _request("POST", "/api/provider-catalog/goose/create", json={
        "base": "https://example.com/v1", "key": "sk-secret", "model": "chosen"})
    name = r.json()["name"]
    managed = _request("GET", "/api/providers/manage").json()[name]
    managed["env"]["LLM_BASE_URL"] = managed["env"].pop("OPENAI_BASE_URL")
    for target in [name, "copy-of-goose"]:
        r = _request("POST", "/api/providers", json={
            "name": target, "source_name": name, "cmd": managed["cmd"],
            "description": "edited", "env": managed["env"], "models": managed["models"]})
        assert r.status_code == 201
        saved = config.load_providers()[target]
        assert saved["prompt_stdin"] is True
        assert saved["env"]["LLM_BASE_URL"] == "https://example.com/v1"
        assert saved["env"]["OPENAI_API_KEY"] == "sk-secret"
        assert "OPENAI_BASE_URL" not in saved["env"]


@pytest.mark.parametrize("exit_code", [0, 1])
def test_probe_uses_guard_isolated_cwd_and_success_exit(isolated_providers, monkeypatch, exit_code):
    import subprocess
    from pathlib import Path

    from promptpilot import config
    config.save_provider("probe", "echo {prompt}")
    guarded = []
    monkeypatch.setattr(config, "build_cmd", lambda name, prompt, **kw: guarded.append(kw) or ["echo", prompt])

    def run(args, **kwargs):
        assert Path(kwargs["cwd"]).is_dir() and not list(Path(kwargs["cwd"]).iterdir())
        assert kwargs["timeout"] == 300
        return subprocess.CompletedProcess(args, exit_code, args[1], "sk-secret")

    monkeypatch.setattr("promptpilot.process_tree.run_owned", run)
    response = _request("POST", "/api/providers/probe/probe", json={})
    assert response.status_code == 200
    assert response.json()["ok"] is (exit_code == 0)
    assert guarded == [{"guard": True}]
    assert "sk-secret" not in response.text


def test_catalog_publishes_one_complete_entry_or_nothing(isolated_providers, monkeypatch):
    from promptpilot import config
    original = config._write_custom_providers
    writes = []

    def fail(entries):
        writes.append(entries)
        assert entries["goose-chosen"]["prompt_stdin"] is True
        assert entries["goose-chosen"]["catalog_id"] == "goose"
        raise OSError("simulated publication failure")

    monkeypatch.setattr(config, "_write_custom_providers", fail)
    with pytest.raises(OSError, match="publication failure"):
        _request("POST", "/api/provider-catalog/goose/create", json={
            "base": "https://example.com/v1", "model": "chosen"})
    assert len(writes) == 1
    assert "goose-chosen" not in config.load_providers()
    monkeypatch.setattr(config, "_write_custom_providers", original)
    assert _request("POST", "/api/provider-catalog/goose/create", json={
        "base": "https://example.com/v1", "model": "chosen"}).status_code == 200
    assert config.load_providers()["goose-chosen"]["prompt_stdin"] is True


@pytest.mark.parametrize("cid", [e["id"] for e in CATALOG])
def test_long_generated_names_and_collisions_remain_editable(isolated_providers, cid):
    names = []
    for _ in range(2):
        response = _request("POST", f"/api/provider-catalog/{cid}/create", json={
            "base": "https://example.com/v1", "model": "a" * 100})
        assert response.status_code == 200
        name = response.json()["name"]
        assert len(name) <= 32
        names.append(name)
        managed = _request("GET", "/api/providers/manage").json()[name]
        response = _request("POST", "/api/providers", json={
            "name": name, "source_name": name, "cmd": managed["cmd"],
            "description": "edited", "env": managed["env"], "models": managed["models"]})
        assert response.status_code == 201, response.text
    assert names[0] != names[1]


def test_new_goose_is_discovered_without_process_path_refresh(isolated_providers, monkeypatch):
    from pathlib import Path
    from promptpilot import config, provider_catalog
    exe = isolated_providers / "goose" / "goose.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"fake executable; must never run")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: isolated_providers))
    monkeypatch.setattr(provider_catalog.sys, "platform", "win32")
    monkeypatch.setattr("shutil.which", lambda executable: None)
    items = _request("GET", "/api/provider-catalog").json()["items"]
    assert next(item for item in items if item["id"] == "goose")["installed"] is True
    response = _request("POST", "/api/provider-catalog/goose/create", json={
        "base": "https://example.com/v1", "model": "chosen"})
    assert response.status_code == 200
    command = config.build_cmd(response.json()["name"], "prompt", guard=False)
    assert Path(command[0]) == exe
    assert command[-2:] == ["-i", "-"]
