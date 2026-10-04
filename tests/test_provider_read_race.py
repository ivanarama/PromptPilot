# -*- coding: utf-8 -*-
"""U14: гонка чтения providers.json — усечённая запись не должна выкидывать
кастомных провайдеров навсегда; чтение повторяется один раз."""

import json

import pytest

from promptpilot import config as cfg


@pytest.fixture()
def isolated_providers(tmp_path, monkeypatch):
    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path))
    yield tmp_path
    (tmp_path / "providers.json").unlink(missing_ok=True)


def test_custom_provider_survives_truncated_read(isolated_providers, monkeypatch):
    data = {"goose-zai": {"cmd": "echo hi {prompt}"}}
    (isolated_providers / "providers.json").write_text(
        json.dumps(data, ensure_ascii=True), encoding="utf-8")

    calls = {"n": 0}
    real_read = cfg._read_json_file

    def flaky_read(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("truncated mid-write")   # первая попытка видит обрывок
        return real_read(path)

    monkeypatch.setattr(cfg, "_read_json_file", flaky_read)
    providers = cfg.load_providers()
    assert providers.get("goose-zai", {}).get("cmd") == "echo hi {prompt}"
    assert calls["n"] == 2


def test_permanently_broken_file_falls_back_to_builtins(isolated_providers, monkeypatch):
    (isolated_providers / "providers.json").write_text("{broken", encoding="utf-8")
    calls = {"n": 0}
    real_read = cfg._read_json_file

    def always_broken(path):
        calls["n"] += 1
        raise ValueError("bad json")

    monkeypatch.setattr(cfg, "_read_json_file", always_broken)
    providers = cfg.load_providers()
    assert "codex" in providers          # встроенные живут
    assert "goose-zai" not in providers  # кастомных нет — но это стабильное, не мигание
    assert calls["n"] == 2               # ровно один повтор
