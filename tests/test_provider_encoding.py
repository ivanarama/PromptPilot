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

