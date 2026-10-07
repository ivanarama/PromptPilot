"""«Здоровье конвейера»: список живости фоновых процессов для панели
(лестница жила без процесса двое суток — тихие смерти никто не замечал)."""
import json
import os

from promptpilot.api import _pid_alive, api_health_processes


def _write_pids(tmp_path, data):
    pids = tmp_path / ".pp-pids.json"
    pids.write_text(json.dumps(data), encoding="utf-8")
    return pids


def test_pid_alive_current_and_dead():
    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(0) is False
    assert _pid_alive(-5) is False
    assert _pid_alive(99999999) is False


def test_health_endpoint_reports_dead(tmp_path, monkeypatch):
    from promptpilot import api

    path = _write_pids(tmp_path, {
        "worker": os.getpid(),          # жив
        "cascade": 99999999,            # мёртв
        "bot_vk": 0,                    # не запускался
    })
    monkeypatch.setattr(api, "HEALTH_PIDS_PATH", path)
    result = api_health_processes()
    by_name = {p["name"]: p["state"] for p in result["processes"]}
    assert by_name["worker"] == "alive"
    assert by_name["cascade"] == "dead"        # pid есть, процесс мёртв
    assert by_name["bot_vk"] == "off"          # pid=0 — не запущен, не тревога
    assert result["ok"] is False
