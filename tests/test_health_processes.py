"""«Здоровье конвейера»: список живости фоновых процессов для панели
(лестница жила без процесса двое суток — тихие смерти никто не замечал)."""
import json
import os

import pytest

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


def test_health_endpoint_reports_dead(isolated_db, tmp_path, monkeypatch):
    from promptpilot import api

    path = _write_pids(tmp_path, {
        "worker": os.getpid(),          # жив
        "cascade": 99999999,            # мёртв
        "bot_vk": 0,                    # не запускался
    })
    monkeypatch.setattr(api, "HEALTH_PIDS_PATH", path)
    isolated_db.touch_worker_heartbeat(os.getpid())
    result = api_health_processes()
    by_name = {p["name"]: p["state"] for p in result["processes"]}
    assert by_name["worker"] == "alive"
    assert by_name["cascade"] == "dead"        # pid есть, процесс мёртв
    assert "bot_vk" not in by_name  # optional and not launched
    assert result["ok"] is False


def test_http_health_uses_heartbeat_not_stale_pid(isolated_db, tmp_path, monkeypatch):
    import asyncio
    from datetime import datetime, timedelta, timezone

    import httpx

    from promptpilot import api

    monkeypatch.setattr(api, "HEALTH_PIDS_PATH", _write_pids(tmp_path, {"worker": os.getpid()}))

    def get():
        async def call():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
                                        base_url="http://127.0.0.1:8420") as c:
                return await c.get("/api/health/processes")
        response = asyncio.run(call())
        assert response.status_code == 200
        return response.json()

    data = get()
    assert data["ok"] is False
    assert data["processes"][1]["state"] == "unknown"
    assert data["processes"][1]["alive"] is None
    isolated_db.touch_worker_heartbeat(os.getpid())
    assert get()["ok"] is True
    isolated_db.touch_worker_heartbeat(os.getpid(), datetime.now(timezone.utc) - timedelta(minutes=2))
    assert get()["processes"][1]["state"] == "dead"
    assert get()["ok"] is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX process probe")
@pytest.mark.parametrize("error, expected", [
    (None, True), (ProcessLookupError(), False), (PermissionError(), None),
    (OSError(), None), (OverflowError(), None),
])
def test_posix_probe_distinguishes_alive_dead_and_unknown(monkeypatch, error, expected):
    def probe(pid, signal):
        assert pid == 12345 and signal == 0
        if error is not None:
            raise error

    monkeypatch.setattr(os, "kill", probe)
    assert _pid_alive(12345) is expected
