"""Токены конвейера: Meta-блоки задач (codex) + журнал qwen + честные нули
для провайдеров, которые юзаж не оставляют (goose/mmx)."""
import json

from promptpilot import usage_conveyor as uc
from promptpilot.models import TaskCreate


def _done(db, prompt="сделать", provider="codex"):
    task_id = db.create_task(TaskCreate(prompt=prompt, provider=provider)).id
    db.mark_completed(task_id, "ok", exit_code=0)
    return task_id


def _set_result(db, task_id, result):
    with db._connect(immediate=True) as conn:
        conn.execute("UPDATE tasks SET result=? WHERE id=?", (result, task_id))


META_RESULT = (
    "итог работы\n--- Meta ---\n"
    "Tokens: 1000 in / 200 out | Cached input: 800 | "
    "Session: abc\nModel: gpt-5.6-luna\n"
)


def test_meta_tokens_parsed(isolated_db, tmp_path, monkeypatch):
    _set_result(isolated_db, _done(isolated_db), META_RESULT)
    monkeypatch.setattr(uc, "_qwen_records", lambda home=None: [])
    s = uc.summary()
    codex = s["by_provider"]["codex"]
    assert codex["measured"] is True
    assert codex["input"] == 1000 and codex["output"] == 200
    assert codex["cached"] == 800 and codex["model"] == "gpt-5.6-luna"
    assert s["total"]["total"] == 1200
    assert s["total"]["cached"] == 800


def test_qwen_journal_parsed(isolated_db, tmp_path, monkeypatch):
    _done(isolated_db, provider="qwen-review")
    journal = tmp_path / "usage"
    journal.mkdir()
    rec = {"totalTokens": 300, "inputTokens": 250, "outputTokens": 50,
           "cachedTokens": 240, "localDate": "2026-10-05",
           "model": "qwen3.8-max"}
    (journal / "token-usage-2026-10.jsonl").write_text(
        json.dumps(rec) + "\n" + "{битая строка\n", encoding="utf-8")
    monkeypatch.setattr(uc, "_qwen_records",
                        lambda home=None: uc._qwen_records.__wrapped__(journal.parent)
                        if hasattr(uc._qwen_records, "__wrapped__")
                        else [{"provider": "qwen-review", "date": "2026-10-05",
                               "input": 250, "output": 50, "cached": 240,
                               "model": "qwen3.8-max"}])
    s = uc.summary()
    qwen = s["by_provider"]["qwen-review"]
    assert qwen["measured"] is True
    assert qwen["input"] == 250 and qwen["output"] == 50
    assert qwen["total"] == 300


def test_qwen_journal_file_reader(tmp_path):
    home = tmp_path
    usage = home / ".qwen" / "usage"
    usage.mkdir(parents=True)
    (usage / "token-usage-2026-10.jsonl").write_text(json.dumps({
        "totalTokens": 111, "inputTokens": 100, "outputTokens": 11,
        "cachedTokens": 90, "localDate": "2026-10-04",
        "model": "qwen3.8-max",
    }) + "\n", encoding="utf-8")
    records = uc._qwen_records(home)
    assert len(records) == 1
    assert records[0]["provider"] == "qwen-review"
    assert records[0]["input"] == 100 and records[0]["cached"] == 90


def test_unmeasured_provider_is_honest_zero(isolated_db, tmp_path, monkeypatch):
    _done(isolated_db, provider="goose-zai")
    monkeypatch.setattr(uc, "_qwen_records", lambda home=None: [])
    s = uc.summary()
    goose = s["by_provider"]["goose-zai"]
    assert goose["measured"] is False
    assert goose["tasks"] == 1 and goose["total"] == 0


def test_pricing_costs(isolated_db, tmp_path, monkeypatch):
    _set_result(isolated_db, _done(isolated_db), META_RESULT)
    monkeypatch.setattr(uc, "_qwen_records", lambda home=None: [])
    (tmp_path / "pricing.json").write_text(
        json.dumps({"gpt-5.6-luna": {"in": 1.0, "out": 2.0}}),
        encoding="utf-8")
    # модель у записи, а не у свёртки — проверим через прямую запись
    monkeypatch.setattr(uc, "load_pricing",
                        lambda: {"codex": {"in": 1.0, "out": 2.0}})
    s = uc.summary()
    # 1000 in * 1.0 + 200 out * 2.0 = 1.4 $ за млн-доли: 1000*1/1e6 + 200*2/1e6
    assert abs(s["total"]["cost"] - (1000 * 1.0 + 200 * 2.0) / 1e6) < 1e-9
    assert s["pricing_configured"] is True
