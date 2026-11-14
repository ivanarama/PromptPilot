"""Telegram presentation and durable delivery for existing Codex dialogs."""
import asyncio
import hashlib
import io
import json
import os
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from . import codex_app_server as app, codex_bridge as bridge, db

QUESTION_PREFIX = "codex-app-server.question."
TURN_PREFIX = "codex-app-server.turn."


def enabled():
    return os.environ.get("PP_CODEX_APP_SERVER", "auto").lower() not in {"0", "off", "false"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


async def view(agent, host):
    dialog = await asyncio.to_thread(bridge.resolve, agent, host)
    if dialog:
        try:
            thread, turns = await asyncio.to_thread(dialog.snapshot)
            questions = await asyncio.to_thread(dialog.questions, turns)
            return dialog, thread, turns, questions
        except (app.Unavailable, app.RPCError):
            pass
    return None


def question_record(dialog, question, ref, pane, machine, host, chat_id):
    return {"thread_id": dialog.thread_id, "question": question, "ref": ref,
            "pane": pane, "machine": machine, "endpoint": list(host) if host else None,
            "chat_id": chat_id, "expires_at": time.time() + 600, "used": False}


def question_message(record, token):
    from . import bot
    question = record["question"]
    text = "❓ Вопрос Codex\n\n" + question["text"]
    rows = []
    for index, option in enumerate(question["options"]):
        text += f"\n{index + 1}. {option}"
        rows.append([InlineKeyboardButton(f"{index + 1}. {option}"[:60],
                                         callback_data=f"cx_answer:{token}:{index}")])
    rows.append([InlineKeyboardButton("✍️ Ответить текстом", callback_data=f"cx_text:{token}")])
    return bot._clip(text, 3800), InlineKeyboardMarkup(rows)


async def show_questions(update, dialog, questions, ref, pane, machine, host):
    for question in questions:
        token = uuid.uuid4().hex[:24]
        record = question_record(dialog, question, ref, pane, machine, host, update.effective_chat.id)
        db.set_setting(QUESTION_PREFIX + token, json.dumps(record, ensure_ascii=False))
        text, keyboard = question_message(record, token)
        await update.callback_query.message.reply_text(text, reply_markup=keyboard)


async def current_question(update, token):
    from . import bot
    raw = db.get_setting(QUESTION_PREFIX + token)
    record = json.loads(raw) if raw else None
    if (not record or record["chat_id"] != update.effective_chat.id or record.get("used")
            or record["expires_at"] < time.time()):
        return None
    try:
        machine, pane, host = bot._parse_hd_ref(record["ref"])
    except ValueError:
        return None
    if record["endpoint"] != (list(host) if host else None):
        return None
    result = await bot._herdr_json("agent", "get", pane, host=host)
    agent = ((result or {}).get("result") or {}).get("agent") or {}
    dialog = await asyncio.to_thread(bridge.resolve, agent, host)
    if not dialog or dialog.thread_id != record["thread_id"]:
        return None
    return record, dialog, machine, pane, host


def track(dialog, turn_id, pane, machine, host, chat_id, delivery=None, inherit_from=None):
    from . import bot
    key = TURN_PREFIX + _digest([list(host) if host else None, dialog.thread_id, turn_id, chat_id])
    previous = db.get_setting(key)
    if delivery is None and previous:
        delivery = json.loads(previous).get("delivery")
    if delivery is None and inherit_from:
        old_key = TURN_PREFIX + _digest([list(host) if host else None, dialog.thread_id, inherit_from, chat_id])
        old = json.loads(db.get_setting(old_key, "{}"))
        if (not old.get("done") or old.get("status") == "awaiting_input") and old.get("expires_at", 0) > time.time():
            delivery = old.get("delivery")
    db.save_codex_tracking(key, bridge.tracked_turn(
        dialog, turn_id, pane, machine, host, chat_id, delivery), bot._herdr_watch_source(machine, host))
    return key


async def answer(update, context, token, text=None, option=None):
    from . import bot
    current = await current_question(update, token)
    message = update.message or update.callback_query.message
    if not current:
        await message.reply_text("Эти кнопки устарели. Откройте вопрос заново через «Окна».")
        return
    record, dialog, machine, pane, host = current
    question = record["question"]
    if option is not None:
        if option < 0 or option >= len(question["options"]):
            return
        text = question["options"][option]
    if not text or not await bot._check_herdr_reply_access(update):
        return
    group = "codex-app-server.answers." + _digest([record["endpoint"], record["thread_id"],
                                                  question.get("request"), record["chat_id"]])
    answers = json.loads(db.get_setting(group, "{}")) if question["kind"] == "blocking" else {}
    is_partial = (question["kind"] == "blocking" and
                  len(set(answers) | {question["id"]}) < len(question["request"]["params"]["questions"]))
    identity = _digest([record["endpoint"], record["thread_id"],
                        question.get("request") if question["kind"] == "blocking" else question])
    if not is_partial and not db.claim_codex_answer(identity):
        await message.reply_text("Ответ на этот вопрос уже передавался. Обновите окно.")
        return
    try:
        result = await asyncio.to_thread(dialog.answer, question, text, answers)
    except app.Unconfirmed:
        await message.reply_text("Ответ мог быть передан. Обновите окно перед повторной отправкой.")
        return
    except (app.Unavailable, app.RPCError):
        if not is_partial:
            db.delete_setting("codex-app-server.answer." + identity)
        await message.reply_text("Вопрос изменился или Codex недоступен. Откройте вопрос заново.")
        return
    record["used"] = True
    db.set_setting(QUESTION_PREFIX + token, json.dumps(record, ensure_ascii=False))
    if is_partial:
        db.set_setting(group, json.dumps(result, ensure_ascii=False))
        await message.reply_text("Ответ сохранён. Ответьте на остальные вопросы в этой группе.")
    else:
        if isinstance(result, tuple):
            track(dialog, result[0], pane, machine, host, update.effective_chat.id,
                  inherit_from=question.get("turn_id"))
        await message.reply_text("Ответ передан Codex ✓")


async def output(update, token):
    raw = db.get_setting(TURN_PREFIX + token)
    record = json.loads(raw) if raw else None
    if not record or record["chat_id"] != update.effective_chat.id or not record.get("done"):
        await update.callback_query.answer("Результат недоступен.", show_alert=True)
        return
    await update.callback_query.answer()
    document = io.BytesIO(record["result"].encode("utf-8"))
    document.name = "codex_result.md"
    await update.callback_query.message.reply_document(document=document, caption="Полный ответ Codex")


async def watch_tick():
    from . import bot
    # Track exact turns, including ones that completed while the bot was stopped.
    for key, raw in db.settings_with_prefix(TURN_PREFIX, 10000).items():
        record = json.loads(raw)
        if record.get("done"):
            if record["expires_at"] < time.time():
                db.delete_setting(key)
            continue
        try:
            _, _, host = bot._parse_hd_ref(f"{record['machine']}:{record['pane']}")
        except ValueError:
            continue
        if record["endpoint"] != (list(host) if host else None):
            continue
        client = await asyncio.to_thread(app.get_client, host)
        if client is None:
            continue
        dialog = bridge.Dialog(client, record["thread_id"])
        try:
            turn = await asyncio.to_thread(dialog.read_turn, record["turn_id"])
        except (app.Unavailable, app.RPCError):
            continue
        if not turn or turn.get("status") not in {"completed", "failed", "interrupted"}:
            continue
        status = turn["status"]
        result = bridge.final_text(turn)
        if status == "completed" and not result:
            result = "Codex закончил запрос без финального ответа. Проверьте вопросы в окне."
            status = "awaiting_input"
        if not result:
            result = str(turn.get("error") or "Запрос Codex прерван.")
        text = ("✅ Codex завершил запрос" if status == "completed" else "⏸ Codex: требуется внимание")
        text += "\n\n" + result[:600]
        notes = [{"kind": "codex.done", "message": json.dumps({"text": text, "token": key[len(TURN_PREFIX):]})}]
        if status == "completed" and record.get("delivery"):
            notes.append({"kind": "result.manifest", "message": json.dumps(record["delivery"])})
        db.finish_codex_tracking(key, record["turn_id"], result, status, notes)

    recipients = list(bot.list_authorized())
    if not recipients:
        return
    for machine, host in bot._herdr_watch_targets():
        if await asyncio.to_thread(app.get_client, host) is None:
            continue
        listing = await bot._herdr_json("agent", "list", host=host)
        for agent in ((listing or {}).get("result") or {}).get("agents") or []:
            if agent.get("agent") != "codex" or not agent.get("pane_id"):
                continue
            snapshot = await view(agent, host)
            if not snapshot:
                continue
            dialog, _, _, questions = snapshot
            pane = agent["pane_id"]
            for question in questions:
                for chat_id in recipients:
                    token = _digest([machine, list(host) if host else None, pane,
                                     dialog.thread_id, question, int(chat_id)])
                    record = question_record(dialog, question, f"{machine}:{pane}",
                                             pane, machine, host, int(chat_id))
                    db.queue_codex_question(QUESTION_PREFIX + token, record)


async def watch_loop():
    from . import bot
    if not enabled():
        return
    while True:
        await asyncio.sleep(10)
        try:
            await watch_tick()
        except Exception as exc:
            bot.logger.warning("Codex dialog watch failed: %s", type(exc).__name__)
