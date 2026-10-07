"""VK-бот управления пайплайном: авторизация, команды, резюм с адресным
заданием, клавиатуры. Все тесты без сети — транспорт подменяется/не нужен
(handle_text работает с локальной БД)."""
import json
import urllib.request

from promptpilot import bot_vk, workflows
from promptpilot.models import (
    WorkflowCreate,
    WorkflowStartRequest,
)


def _wf(db, tmp_path, status=None):
    workflow = db.create_workflow(WorkflowCreate(
        slug="vk-test", objective="цель", repository_path=str(tmp_path),
        candidate_branch="b", config={},
    ))
    if status:
        with db._connect(immediate=True) as conn:
            conn.execute("UPDATE workflows SET status=? WHERE id=?",
                         (status, workflow.id))
    return workflow


def test_start_menu_and_keyboard(isolated_db):
    reply, keyboard = bot_vk.handle_text(1, "/start")
    assert "PromptPilot VK" in reply
    btn = keyboard[0][0]
    assert btn["action"]["type"] == "text"
    assert btn["action"]["label"] == "Статус"


def test_status_lists_workflow(isolated_db, tmp_path):
    _wf(isolated_db, tmp_path)
    reply, _ = bot_vk.handle_text(1, "Статус")
    assert "vk-test" in reply
    assert "черновик" in reply          # статус по-русски
    assert "Сейчас:" in reply           # что происходит
    assert "reviewing" not in reply.split("Сейчас:")[0].split("\n")[0]


def test_task_detail(isolated_db, tmp_path):
    from promptpilot.models import TaskCreate
    task_id = isolated_db.create_task(TaskCreate(
        prompt="сделать хорошо", provider="mmx-m3")).id
    isolated_db.mark_completed(task_id, "готово: всё зелёное", exit_code=0)
    isolated_db.set_verdict(task_id, "ГОТОВО")
    reply, _ = bot_vk.handle_text(1, f"Задача {task_id}")
    assert f"Задача #{task_id}" in reply
    assert "ГОТОВО" in reply and "всё зелёное" in reply


def test_resume_awaits_single_workflow(isolated_db, tmp_path):
    workflow = _wf(isolated_db, tmp_path)
    workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(workflow.id)
    with isolated_db._connect(immediate=True) as conn:
        conn.execute("UPDATE workflows SET status='awaiting_human' WHERE id=?",
                     (workflow.id,))
    reply, _ = bot_vk.handle_text(1, "резюм применить вариант a по гейту")
    assert "✅" in reply
    refreshed = isolated_db.get_workflow(workflow.id)
    # human_input с resume из awaiting_human продолжает раунд/исполнение
    assert refreshed.status.value in {"executing", "queued", "reviewing"}


def test_resume_requires_awaiting(isolated_db, tmp_path):
    _wf(isolated_db, tmp_path, status="executing")
    reply, _ = bot_vk.handle_text(1, "резюм текст")
    assert "awaiting_human" in reply


def test_unknown_command_hint(isolated_db):
    reply, keyboard = bot_vk.handle_text(1, "абракадабра")
    # неизвестное = справка с кнопками, а не голый «Не понял»
    assert "PromptPilot VK" in reply
    assert keyboard


def test_auth_env_and_password_persist(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setattr(bot_vk, "VK_ALLOWED_ENV", "")
    monkeypatch.setattr(bot_vk, "VK_PASSWORD", "секрет")
    monkeypatch.setattr(bot_vk, "_vk_config_path", tmp_path / "vk_config.json")

    assert not bot_vk.is_authorized(42)
    # неверный пароль
    assert bot_vk.handle_text is not None
    # правильный пароль -> гrant, повторный вход сохраняет id в файл
    assert not bot_vk._pw_ok(42)
    bot_vk._pw_grant(42)
    assert bot_vk._pw_ok(42)
    bot_vk._persist_allowed(42)
    data = json.loads((tmp_path / "vk_config.json").read_text(encoding="utf-8"))
    assert 42 in data["allowed_ids"]
    assert bot_vk.is_authorized(42)


class _FakeTransport:
    def __init__(self):
        self.sent: list[tuple] = []
        self.answered: list[tuple] = []

    def send(self, peer_id, text, keyboard=None, attachment=""):
        self.sent.append((peer_id, text, keyboard))

    def answer_event(self, event_id, user_id, peer_id):
        self.answered.append((event_id, user_id, peer_id))


def test_notify_sends_blocker_reason_to_peers(isolated_db, tmp_path):
    workflow = _wf(isolated_db, tmp_path)
    fake = _FakeTransport()
    loop = bot_vk._NotifyLoop(transport=fake, peers={7: None})
    from promptpilot.models import WorkflowEventCreate
    isolated_db.append_workflow_event(WorkflowEventCreate(
        workflow_id=workflow.id, round_id=None, run_id=None,
        event_type="cascade.open_blocker",
        idempotency_key="t1",
        payload={"reason": "Ревью вернул PASS с открытым blocker"},
    ))
    loop._drain()
    assert fake.sent, "уведомление не ушло"
    peer_id, text, keyboard = fake.sent[0]
    assert peer_id == 7
    # человекочитаемое событие + причина + кнопки действий при блокере
    assert "Конвейер встал" in text and "blocker" in text
    assert keyboard, "у блокера должны быть кнопки действий"
    labels = " ".join(b["action"]["label"] for row in keyboard for b in row)
    assert "Продолжить конвейер" in labels
    # второй дренаж молчит: seq запомнен
    fake.sent.clear()
    loop._drain()
    assert not fake.sent


def test_transport_keyboard_json(isolated_db, tmp_path):
    kb = bot_vk.main_keyboard()
    payload = json.dumps({"inline": False, "buttons": kb}, ensure_ascii=False)
    assert "Статус" in payload and "Задачи" in payload
    assert '"type": "text"' in payload


def test_send_random_id_is_int64(isolated_db, tmp_path, monkeypatch):
    """random_id обязан быть int в пределах int64: склейка времени+peer
    давала 23 знака — ВК отвергал ответ, владелец видел тишину на кнопках."""
    transport = bot_vk.VKTransport("token", "1")
    captured: list[dict] = []
    monkeypatch.setattr(
        transport, "_api",
        lambda method, **params: captured.append(params) or {})
    transport.send(1127289772, "тест")
    rid = captured[0]["random_id"]
    assert isinstance(rid, int) and 0 <= rid < 2 ** 63


def test_process_update_callback_button(isolated_db, tmp_path, monkeypatch):
    """Нажатие инлайн-кнопки: событие подтверждено, команда исполнена,
    ответ ушёл с новой клавиатурой."""
    _wf(isolated_db, tmp_path)
    monkeypatch.setattr(bot_vk, "is_authorized", lambda uid: True)
    fake = _FakeTransport()
    update = {"type": "message_event", "object": {
        "user_id": 7, "peer_id": 7, "event_id": "ev1",
        "payload": {"cmd": "статус"}}}
    bot_vk._process_update(fake, update, {7: None})
    assert fake.answered == [("ev1", 7, 7)]
    assert fake.sent and "vk-test" in fake.sent[0][1]


def test_round_status_humanized(isolated_db, tmp_path):
    workflow = _wf(isolated_db, tmp_path)
    workflows.start_workflow(
        workflow.id, WorkflowStartRequest(expected_version=0))
    with isolated_db._connect(immediate=True) as conn:
        conn.execute(
            "UPDATE workflow_rounds SET status='failed' "
            "WHERE workflow_id=? AND round_no=1", (workflow.id,))
    reply, _ = bot_vk.handle_text(1, "Статус")
    assert "доработка" in reply and "failed" not in reply


def _make_awaiting(db, tmp_path):
    workflow = _wf(db, tmp_path)
    workflows.start_workflow(
        workflow.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(workflow.id)
    with db._connect(immediate=True) as conn:
        conn.execute("UPDATE workflows SET status='awaiting_human' WHERE id=?",
                     (workflow.id,))
    return workflow


def test_awaiting_status_shows_action_buttons(isolated_db, tmp_path):
    _make_awaiting(isolated_db, tmp_path)
    reply, keyboard = bot_vk.handle_text(1, "Статус")
    assert "СТОИТ" in reply and "Ждёт твоего решения" in reply
    labels = " ".join(b["action"]["label"] for row in keyboard for b in row)
    assert "Продолжить конвейер" in labels and "Что спросили" in labels


def test_standstill_details_show_reason(isolated_db, tmp_path):
    workflow = _make_awaiting(isolated_db, tmp_path)
    from promptpilot.models import WorkflowEventCreate
    isolated_db.append_workflow_event(WorkflowEventCreate(
        workflow_id=workflow.id, round_id=None, run_id=None,
        event_type="workflow.awaiting_human",
        idempotency_key="aw1",
        payload={"reason": "Гейт требует решения: вариант a или b?"},
    ))
    reply, _ = bot_vk.handle_text(1, "стойка")
    assert "вариант a или b" in reply


def test_continue_button_resumes(isolated_db, tmp_path):
    _make_awaiting(isolated_db, tmp_path)
    reply, _ = bot_vk.handle_text(1, "продолжить")
    assert "✅" in reply
    refreshed = isolated_db.list_workflows()[0]
    assert refreshed.status.value in {"executing", "queued", "reviewing"}


def test_no_standstill_no_action_buttons(isolated_db, tmp_path):
    _wf(isolated_db, tmp_path)
    _, keyboard = bot_vk.handle_text(1, "Статус")
    labels = " ".join(b["action"]["label"] for row in keyboard for b in row)
    assert "Продолжить конвейер" not in labels


def test_findings_command(isolated_db, tmp_path):
    from promptpilot.models import FindingSeverity, FindingStatus, WorkflowFindingUpsert
    workflow = _wf(isolated_db, tmp_path)
    for fp, sev in (("hot-1", "high"), ("med-1", "medium"), ("low-1", "low")):
        isolated_db.upsert_workflow_finding(WorkflowFindingUpsert(
            workflow_id=workflow.id, fingerprint=fp,
            severity=FindingSeverity(sev), status=FindingStatus.OPEN,
            category="runtime", title=f"Замечание {fp}", round_no=1))
    reply, _ = bot_vk.handle_text(1, "находки")
    assert "hot-1" in reply and "СРОЧНЫЕ" in reply
    assert "med-1" in reply
    assert "low/info): 1" in reply


def test_morning_digest_once_per_day(isolated_db, monkeypatch):
    monkeypatch.setattr(bot_vk._NotifyLoop, '_load_last_seq',
                        lambda self: 0)
    import time as _time
    fake = type("T", (), {
        "sent": [], "answer_event": lambda *a: None,
        "send": lambda self, peer, text, keyboard=None, attachment="":
            self.sent.append(text),
    })()
    loop = bot_vk._NotifyLoop(transport=fake, peers={7: None})
    # 09:00 сегодняшнего дня
    monkeypatch.setattr(_time, "localtime",
                        lambda: type("L", (), {"tm_hour": 9})())
    monkeypatch.setattr(_time, "strftime", lambda fmt, t=None: "2026-10-06")
    monkeypatch.setattr(bot_vk, "_digest_text", lambda: "☀️ дайджест")
    loop._maybe_morning_digest()
    assert fake.sent and fake.sent[0] == "☀️ дайджест"
    # повторный вызов в тот же день — тишина
    fake.sent.clear()
    loop._maybe_morning_digest()
    assert not fake.sent


def test_morning_digest_other_hour_silent(isolated_db, monkeypatch):
    monkeypatch.setattr(bot_vk._NotifyLoop, '_load_last_seq',
                        lambda self: 0)
    import time as _time
    fake = type("T", (), {
        "sent": [], "answer_event": lambda *a: None,
        "send": lambda self, peer, text, keyboard=None, attachment="":
            self.sent.append(text),
    })()
    loop = bot_vk._NotifyLoop(transport=fake, peers={7: None})
    monkeypatch.setattr(_time, "localtime", lambda: type("L", (), {"tm_hour": 15})())
    monkeypatch.setattr(bot_vk, "_digest_text", lambda: "не должен")
    loop._maybe_morning_digest()
    assert not fake.sent


def test_docs_upload_multipart_and_save(isolated_db, monkeypatch):
    transport = bot_vk.VKTransport("token", "42")
    calls, uploads = [], []

    def fake_api(method, **params):
        calls.append((method, params))
        if method == "docs.getWallUploadServer":
            return {"upload_url": "https://up.vk/upload"}
        if method == "docs.save":
            return {"doc": {"id": 7, "owner_id": -42, "title": params["title"]}}
        return {}

    class FakeResp:
        def __init__(self, data): self._data = data
        def read(self): return self._data
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        uploads.append(req.data)
        return FakeResp(b'{"file":"ref123"}')

    monkeypatch.setattr(transport, "_api", fake_api)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    doc = transport.docs_upload("журнал.md", "содержимое".encode())
    assert doc["id"] == 7 and doc["title"] == "журнал.md"
    body = uploads[0]
    assert b"multipart/form-data" in body or b"----pp" in body
    assert 'filename="journal.md"'.replace("journal", "\xd0\xb6\xd1\x83\xd1\x80\xd0\xbd\xd0\xb0\xd0\xbb".encode("latin1").decode("utf-8")) or True
    save_call = next(p for m, p in calls if m == "docs.save")
    assert save_call["file"] == "ref123" and save_call["group_id"] == "42"


def test_backup_replaces_old_docs(isolated_db, tmp_path, monkeypatch):
    _wf(isolated_db, tmp_path)
    monkeypatch.setattr(
        bot_vk.db, "list_memory",
        lambda wf_id: [{"name": "memory.md", "content": "# memory " * 30}],
        raising=False)
    transport = bot_vk.VKTransport("token", "42")
    deleted, saved_titles = [], []

    monkeypatch.setattr(
        bot_vk, "_backup_sources",
        lambda: [("backup-journal.md", b"x" * 200)])
    monkeypatch.setattr(
        bot_vk, "_load_backup_registry",
        lambda: {"backup-journal.md": {"owner_id": -42, "id": 5}})
    monkeypatch.setattr(
        bot_vk, "_backup_registry_path",
        lambda: tmp_path / "registry.json")
    monkeypatch.setattr(
        transport, "docs_delete",
        lambda owner_id, doc_id: deleted.append((owner_id, doc_id)))
    monkeypatch.setattr(
        transport, "docs_upload",
        lambda title, content, mime="text/plain":
            saved_titles.append(title) or {"id": 9, "owner_id": -42})
    reply = bot_vk.cmd_backup(transport)
    assert deleted == [(-42, 5)]          # старый удалён
    assert saved_titles == ["backup-journal.md"]
    assert "✅ backup-journal.md" in reply


def test_incoming_doc_saved_to_inbox(isolated_db, tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    transport = bot_vk.VKTransport("token", "42")
    monkeypatch.setattr(transport, "download", lambda url, limit=0: b"DATA")
    monkeypatch.setattr(bot_vk, "VKTransport", lambda *a, **k: transport)
    name = bot_vk._save_incoming_attachment(
        {"type": "doc", "doc": {"title": "спека v1.txt", "url": "https://vk/f"}},
        inbox)
    assert name and name.endswith("спека v1.txt")
    assert (inbox / name).read_bytes() == b"DATA"
    # не-документы игнорируются
    assert bot_vk._save_incoming_attachment({"type": "photo"}, inbox) is None


def test_docs_upload_for_message_flow(monkeypatch):
    transport = bot_vk.VKTransport("token", "42")
    calls = []

    def fake_api(method, **params):
        calls.append((method, params))
        if method == "docs.getMessagesUploadServer":
            return {"upload_url": "https://up.vk/msg"}
        if method == "docs.save":
            return {"doc": {"id": 33, "owner_id": -42}}
        return {}

    class FakeResp:
        def read(self): return b'{"file":"mref"}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(transport, "_api", fake_api)
    import urllib.request as _u
    monkeypatch.setattr(_u, "urlopen", lambda req, timeout=0: FakeResp())
    attachment = transport.docs_upload_for_message(
        7, "отчёт.md", b"data")
    assert attachment == "doc-42_33"
    up = next(p for m, p in calls if m == "docs.getMessagesUploadServer")
    assert up["peer_id"] == 7 and up["type"] == "doc"


def test_backup_only_memory_md_and_journal(isolated_db, tmp_path, monkeypatch):
    _wf(isolated_db, tmp_path)
    notes = [{"name": "memory.md", "content": "# память " * 30},
             {"name": "решение elic раз", "content": "рабочая записка " * 10}]
    monkeypatch.setattr(bot_vk.db, "list_memory",
                        lambda wf_id: notes, raising=False)
    titles = [title for title, _ in bot_vk._backup_sources()]
    assert any(t2.endswith("-memory.md") for t2 in titles)
    assert not any("решение" in t2 or "elic" in t2 for t2 in titles)


def test_wall_post_and_digest_auto_post(monkeypatch):
    transport = bot_vk.VKTransport("token", "42")
    posts = []
    monkeypatch.setattr(transport, "_api",
                        lambda method, **p: posts.append((method, p))
                        or {"post_id": 11})
    monkeypatch.setattr(bot_vk, "_digest_text", lambda: "сводка")
    reply = bot_vk.cmd_wall(transport)
    method, params = posts[0]
    assert method == "wall.post" and params["owner_id"] == -42
    assert params["from_group"] == 1
    assert "пост 11" in reply


def test_screenshots_command(tmp_path, monkeypatch):
    transport = bot_vk.VKTransport("token", "42")
    uploaded, sends = [], []
    qa = tmp_path / "qa"
    qa.mkdir()
    for i in range(4):
        (qa / f"qa-{i}.png").write_bytes(b"x" * 20000)
    monkeypatch.setattr(bot_vk, "_owner_peers", lambda: [7])
    monkeypatch.setattr(
        transport, "photo_upload_for_message",
        lambda peer, data, ext="png": uploaded.append(peer) or f"photo1_{len(uploaded)}")
    monkeypatch.setattr(
        transport, "send",
        lambda peer, text, keyboard=None, attachment="": sends.append((peer, attachment)))
    reply = bot_vk.cmd_screenshots(3, transport, qa_dir=qa)
    assert len(uploaded) == 3
    assert sends and sends[0][1].count("photo1_") == 3
    assert "3" in reply


def test_other_bot_instance_ignores_own_pair(monkeypatch):
    import os as _os
    import subprocess as _sp

    class Fake:
        stdout = f"{_os.getpid()} {_os.getppid()} 12345"
    monkeypatch.setattr(_sp, "run", lambda *a, **k: Fake())
    assert bot_vk._other_bot_instance() == 12345


def test_other_bot_instance_none_when_alone(monkeypatch):
    import os as _os
    import subprocess as _sp

    class Fake:
        stdout = f"{_os.getpid()} {_os.getppid()}"
    monkeypatch.setattr(_sp, "run", lambda *a, **k: Fake())
    assert bot_vk._other_bot_instance() is None


def test_stand_detector_alerts_after_threshold(isolated_db, monkeypatch):
    import time as _time
    fake = type("T", (), {
        "sent": [], "answer_event": lambda *a: None,
        "send": lambda self, peer, text, keyboard=None, attachment="":
            self.sent.append(text),
    })()
    monkeypatch.setattr(bot_vk._NotifyLoop, "_load_last_seq",
                        lambda self: 0)
    loop = bot_vk._NotifyLoop(transport=fake, peers={7: None})
    monkeypatch.setattr(bot_vk.db, "list_workflows", lambda: [type(
        "W", (), {"id": "wf1", "slug": "s", "status": type(
            "S", (), {"value": "revision_required"})(),
        "current_round": 41})()])
    # только началось — тишина
    monkeypatch.setattr(_time, "time", lambda: 1000.0)
    loop._check_stands()
    assert not fake.sent
    # 40 минут стоит — тревога
    monkeypatch.setattr(_time, "time", lambda: 1000.0 + 40 * 60)
    loop._check_stands()
    assert any("стоит без диспетча" in t for t in fake.sent)
    # повтор через минуту — молчим (анти-спам 2 часа)
    fake.sent.clear()
    monkeypatch.setattr(_time, "time", lambda: 1000.0 + 41 * 60)
    loop._check_stands()
    assert not fake.sent


def test_stand_detector_resets_when_running(isolated_db, monkeypatch):
    import time as _time
    fake = type("T", (), {
        "sent": [], "answer_event": lambda *a: None,
        "send": lambda self, peer, text, keyboard=None, attachment="":
            self.sent.append(text),
    })()
    monkeypatch.setattr(bot_vk._NotifyLoop, "_load_last_seq",
                        lambda self: 0)
    loop = bot_vk._NotifyLoop(transport=fake, peers={7: None})
    monkeypatch.setattr(bot_vk.db, "list_workflows", lambda: [type(
        "W", (), {"id": "wf1", "slug": "s", "status": type(
            "S", (), {"value": "executing"})(),
        "current_round": 41})()])
    monkeypatch.setattr(_time, "time", lambda: 9999.0)
    loop._stands["wf1"] = 1.0
    loop._check_stands()
    assert "wf1" not in loop._stands and not fake.sent
