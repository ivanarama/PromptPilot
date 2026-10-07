"""Каталог известных CLI (визард подключения помощников): каталог цел,
подбор моделей с сервера (ключ не светится), создание помощника с
коротким именем «Инструмент · Модель», уникальность имени."""

import json

import pytest
from fastapi.testclient import TestClient

from promptpilot.api import app
from promptpilot.provider_catalog import (
    CATALOG,
    build_provider_entry,
    catalog_entry,
    pretty_model,
    slugify,
)


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

    def read(self):
        return self._p

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
    client = TestClient(app)
    r = client.get("/api/provider-catalog")
    assert r.status_code == 200
    by_id = {it["id"]: it for it in r.json()["items"]}
    assert by_id["codex"]["installed"] is True
    assert by_id["claude"]["installed"] is False


def test_models_fetch_openai_style_key_stays_server_side(isolated_providers, monkeypatch):
    captured = {}
    payload = {"data": [{"id": "astra-3.1-pro"}, {"id": "zeta"}, {"id": "astra-3.1-pro"}]}
    monkeypatch.setattr("urllib.request.build_opener",
                        lambda *a, **k: _FakeOpener(payload, captured))
    client = TestClient(app)
    r = client.post("/api/provider-catalog/codex/models",
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
    client = TestClient(app)
    r = client.post("/api/provider-catalog/claude/models",
                    json={"base": "https://api.anthropic.com", "key": "sk-ant"})
    assert r.status_code == 200
    assert r.json()["models"] == ["claude-sonnet-5"]
    assert captured["url"].endswith("/v1/models")
    assert captured["xapi"] == "sk-ant"


def test_models_fetch_empty_answer_is_error(isolated_providers, monkeypatch):
    monkeypatch.setattr("urllib.request.build_opener",
                        lambda *a, **k: _FakeOpener({"data": []}, {}))
    client = TestClient(app)
    r = client.post("/api/provider-catalog/codex/models",
                    json={"base": "https://api.example.com/v1", "key": "k"})
    assert r.status_code == 502


def test_create_provider_short_name_and_uniqueness(isolated_providers):
    client = TestClient(app)
    r = client.post("/api/provider-catalog/goose/create",
                    json={"base": "https://z.example/v1", "key": "sk-1",
                          "model": "glm-5.3"})
    assert r.status_code == 200
    first = r.json()
    assert first["name"] == "goose-glm-5-3"
    assert first["label"] == "Гусь · Glm 5.3"

    saved = json.loads((isolated_providers / "providers.json").read_text(encoding="utf-8"))
    prov = saved["goose-glm-5-3"]
    assert prov["cmd"] == "goose run -i -"
    assert prov["prompt_stdin"] is True
    assert prov["env"]["GOOSE_PROVIDER"] == "openai"
    assert prov["env"]["OPENAI_API_KEY"] == "sk-1"

    r2 = client.post("/api/provider-catalog/goose/create",
                     json={"base": "https://z.example/v1", "key": "sk-2",
                           "model": "glm-5.3"})
    assert r2.status_code == 200
    assert r2.json()["name"] == "goose-glm-5-3-2"


def test_create_requires_model(isolated_providers):
    client = TestClient(app)
    r = client.post("/api/provider-catalog/codex/create", json={"base": "b", "key": "k"})
    assert r.status_code == 400
