import json


def test_custom_provider_human_labels_are_read_as_utf8(tmp_path, monkeypatch):
    """Windows' cp1251 locale must not corrupt UTF-8 provider labels."""
    from promptpilot import config

    providers_file = tmp_path / "providers.json"
    providers_file.write_text(
        json.dumps(
            {
                "utf8-label-test": {
                    "cmd": "echo {prompt}",
                    "human": {
                        "label": "🤖 МиниМакс",
                        "role": "writer",
                        "desc": "Пишет код",
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "DB_DIR", tmp_path)

    provider = config.load_providers()["utf8-label-test"]

    assert provider["human"]["label"] == "🤖 МиниМакс"
    assert provider["human"]["desc"] == "Пишет код"


def _code_page(monkeypatch, name):
    import locale

    monkeypatch.setattr(locale, "getpreferredencoding", lambda do_setlocale=True: name)


def test_provider_saved_from_the_form_round_trips(tmp_path, monkeypatch):
    """«И» is 0xD0 0x98 in UTF-8; 0x98 is undefined in cp1251 and used to
    raise UnicodeDecodeError out of load_providers()."""
    from promptpilot import config

    monkeypatch.setattr(config, "DB_DIR", tmp_path)
    _code_page(monkeypatch, "cp1251")

    config.save_provider("reviewer", cmd="echo {prompt}", description="Исполнитель ревью")

    assert config.load_providers()["reviewer"]["description"] == "Исполнитель ревью"
    assert config.load_providers_detailed()["reviewer"]["_source"] == "providers.json"
    assert config.remove_provider("reviewer") is True


def test_notepad_bom_is_accepted(tmp_path, monkeypatch):
    from promptpilot import config

    monkeypatch.setattr(config, "DB_DIR", tmp_path)
    body = json.dumps({"bom": {"cmd": "echo {prompt}", "description": "С BOM"}},
                      ensure_ascii=False)
    (tmp_path / "providers.json").write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))

    assert config.load_providers()["bom"]["description"] == "С BOM"


def test_file_saved_in_the_code_page_still_reads(tmp_path, monkeypatch):
    from promptpilot import config

    monkeypatch.setattr(config, "DB_DIR", tmp_path)
    _code_page(monkeypatch, "cp1251")
    body = json.dumps({"legacy": {"cmd": "echo {prompt}", "description": "Руками"}},
                      ensure_ascii=False)
    (tmp_path / "providers.json").write_bytes(body.encode("cp1251"))

    assert config.load_providers()["legacy"]["description"] == "Руками"


def test_unreadable_file_leaves_builtin_providers_working(tmp_path, monkeypatch):
    from promptpilot import config

    monkeypatch.setattr(config, "DB_DIR", tmp_path)
    _code_page(monkeypatch, "utf-8")
    (tmp_path / "providers.json").write_bytes(b"\xff\xfe\x98 not json")

    providers = config.load_providers()

    assert "claude" in providers
    assert config.load_providers_detailed()["claude"]["_source"] == "builtin"


def test_machines_and_telegram_config_are_read_as_utf8(tmp_path, monkeypatch):
    from promptpilot import config, tg_auth

    monkeypatch.setattr(config, "DB_DIR", tmp_path)
    monkeypatch.setattr(tg_auth, "DB_DIR", tmp_path)
    monkeypatch.delenv("PP_TG_ALLOWED_PHONES", raising=False)
    _code_page(monkeypatch, "cp1251")
    config._atomic_write_json(tmp_path / "machines.json",
                              {"build": {"host": "b1", "providers": [], "note": "Иркутск"}})
    config._atomic_write_json(tmp_path / "tg_config.json", {
        "allowed_phones": ["+79001234567"],
        "1c": {"bases_root": r"C:\Базы\ИБ"},
    })

    assert config.load_machines()["build"]["note"] == "Иркутск"
    assert tg_auth.load_allowed_phones() == ["+79001234567"]

