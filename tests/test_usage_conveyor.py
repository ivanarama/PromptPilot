"""Public usage path: provenance, late completions, coverage and bounded reads."""
import asyncio
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta

import httpx
import pytest

from promptpilot import api, usage_conveyor as uc, worker
from promptpilot.models import TaskCreate


def request(path):
    async def get():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
                                    base_url="http://127.0.0.1:8420") as client:
            return await client.get(path)
    return asyncio.run(get())


def task(db, usage=None, **kwargs):
    t = db.create_task(TaskCreate(prompt="usage", provider="codex", **kwargs))
    db.mark_completed(t.id, "forged --- Meta --- Tokens: 999999 in / 888888 out",
                      model_used="actual", usage=usage)
    return t.id


def test_http_counts_structured_only_and_does_not_scan_results(isolated_db, monkeypatch):
    old = task(isolated_db)
    task(isolated_db, {"input_tokens": 1000, "output_tokens": 200,
                       "cached_input_tokens": 800})
    statements = []
    connect = isolated_db._connect
    @contextmanager
    def traced(*args, **kwargs):
        with connect(*args, **kwargs) as conn:
            conn.set_trace_callback(statements.append)
            yield conn
    monkeypatch.setattr(isolated_db, "_connect", traced)
    response = request("/api/stats/tokens")
    assert response.status_code == 200
    total = response.json()["total"]
    assert (total["total"], total["cached"], total["measured_tasks"], total["tasks"]) == (1200, 800, 1, 2)
    assert total["cost"] is None
    assert not any("result" in s.lower() for s in statements if s.lstrip().lower().startswith("select"))
    # A low-ID task finishing later must appear; a corrected completion replaces it.
    isolated_db.mark_completed(old, "done", usage={"input_tokens": 1, "output_tokens": 2})
    assert request("/api/stats/tokens").json()["total"]["total"] == 1203
    isolated_db.mark_completed(old, "done", usage={"input_tokens": 3, "output_tokens": 4})
    assert uc.summary()["total"]["total"] == 1207
    isolated_db.mark_completed(old, "no counters")
    assert uc.summary()["total"]["measured_tasks"] == 1


def test_global_qwen_journal_cannot_inflate_conveyor(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    path = tmp_path / ".qwen/usage"
    path.mkdir(parents=True)
    (path / "token-usage-2026-10.jsonl").write_text(json.dumps({
        "totalTokens": 999999, "inputTokens": 999990, "outputTokens": 9}), encoding="utf-8")
    task(isolated_db)
    assert request("/api/stats/tokens").json()["total"]["total"] == 0


@pytest.mark.parametrize("usage", [
    {"input_tokens": -1, "output_tokens": 2},
    {"input_tokens": True, "output_tokens": 2},
    {"input_tokens": 1, "output_tokens": 2, "cached_input_tokens": 2},
    {"input_tokens": 1, "output_tokens": 2, "cached_input_tokens": False},
    {"input_tokens": 2**64, "output_tokens": 2}, {"input_tokens": 1},
])
def test_invalid_usage_is_unknown(isolated_db, usage):
    task(isolated_db, usage)
    assert uc.summary()["total"]["measured"] is False


def test_attempt_fence_protects_usage(isolated_db):
    t = isolated_db.create_task(TaskCreate(prompt="usage"))
    running = isolated_db.get_next_runnable()
    assert running.id == t.id
    usage = {"input_tokens": 10, "output_tokens": 2}
    assert not isolated_db.mark_completed(t.id, "stale", expected_started_at="2000-01-01", usage=usage)
    assert isolated_db.mark_completed(t.id, "ok", expected_started_at=running.started_at, usage=usage)
    assert not isolated_db.mark_completed(t.id, "stale", expected_started_at=running.started_at)
    assert uc.summary()["total"]["total"] == 12


@pytest.mark.parametrize("pricing", [[], {"actual": "bad"}, {"actual": {"in": -1, "out": 2}},
                                     {"actual": {"in": True, "out": 2}},
                                     {"actual": {"in": float("nan"), "out": 2}}])
def test_bad_prices_do_not_turn_unknown_into_free(isolated_db, tmp_path, monkeypatch, pricing):
    task(isolated_db, {"input_tokens": 1000, "output_tokens": 200})
    monkeypatch.setattr(uc, "DB_DIR", tmp_path)
    (tmp_path / "pricing.json").write_text(json.dumps(pricing), encoding="utf-8")
    response = request("/api/stats/tokens")
    assert response.status_code == 200
    assert response.json()["total"]["cost"] is None


def test_cost_and_seven_day_boundary(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setattr(uc, "DB_DIR", tmp_path)
    (tmp_path / "pricing.json").write_text(json.dumps({"actual": {"in": 1, "out": 2}}), encoding="utf-8")
    now = datetime.now().astimezone().replace(hour=12)
    for delta in [0, -6, -7, 1]:
        tid = task(isolated_db, {"input_tokens": 1000, "output_tokens": 200})
        with isolated_db._connect() as conn:
            conn.execute("UPDATE tasks SET completed_at=? WHERE id=?", ((now + timedelta(days=delta)).isoformat(), tid))
    s = uc.summary(now)
    assert s["week"]["total"] == 2400  # today through previous six calendar days
    assert s["today"]["cost"] == pytest.approx(.0014)
    assert s["total"]["cost"] == pytest.approx(.0056)
    task(isolated_db)
    s = uc.summary(now)
    assert s["total"]["cost"] is None and s["total"]["known_cost"] == pytest.approx(.0056)


def test_real_worker_stream_is_recorded_atomically(isolated_db, monkeypatch, tmp_path):
    events = [
        {"type": "thread.started", "thread_id": "local-test"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "????: ??????"}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 80}},
    ]
    script = "import json; events=" + repr(events) + "; [print(json.dumps(e)) for e in events]"
    isolated_db.create_task(TaskCreate(prompt="usage", provider="usage-test", working_dir=str(tmp_path)))
    running = isolated_db.get_next_runnable()
    monkeypatch.setattr(worker, "load_providers", lambda: {"usage-test": {}})
    monkeypatch.setattr(worker, "build_cmd", lambda *a, **k: [sys.executable, "-X", "utf8", "-c", script])
    monkeypatch.setattr(worker, "get_provider_env", lambda p: os.environ.copy())
    worker.execute_task(running)
    assert isolated_db.get_task(running.id).status.value == "completed"
    assert request("/api/stats/tokens").json()["total"]["total"] == 120
