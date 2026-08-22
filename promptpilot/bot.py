"""Telegram bot for PromptPilot.

Authorization: user sends /start → shares phone via button → phone is checked
against PP_TG_ALLOWED_PHONES env var (comma-separated) or ~/.promptpilot/tg_config.json.
After authorization all task management features are available.
"""

import functools
import json
import logging
import os
import time
from datetime import timezone
from typing import Optional

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import db
from .config import (
    DEFAULT_CLI, HERDR_WATCH, HERDR_WATCH_INTERVAL,
    get_provider_models, get_proxy_url, get_skills, load_machines, load_providers,
    load_providers_detailed, mask_proxy_url, pickable_providers,
    PROJECTS_ROOT, TASK_PASSWORD,
)
from .models import TaskCreate
from .tg_auth import authorize_user, is_authorized, list_authorized, load_allowed_phones
from .worktree import branch_for as wt_branch

logger = logging.getLogger(__name__)

# Conversation states
(ASK_PASSWORD, ASK_PROMPT, ASK_PROVIDER, ASK_PRIORITY, ASK_DIR, ASK_DIR_MANUAL,
 ASK_SCHEDULE, ASK_REPLY, ASK_SKILL_ARGS, ASK_MODEL, ASK_RECURRENCE,
 ASK_HERDR_REPLY, ASK_HERDR_TARGET, ASK_MACHINE, ASK_CONFIRM, ASK_EXTRAS,
 ASK_LAST) = range(17)


class _MsgSend:
    """Adapter so message-based steps can reuse query-style screen renderers:
    'editing' from a plain message just sends a new one."""
    def __init__(self, message):
        self.message = message
        self.edit_message_text = message.reply_text

PAGE_SIZE = 5

STATUS_ICON = {
    "pending": "⏳",
    "running": "🔄",
    "completed": "✅",
    "failed": "❌",
    "rate_limited": "⏸",
    "cancelled": "🚫",
}

# Human names for the card — raw enum values ("rate_limited") read as jargon
STATUS_RU = {
    "pending": "в очереди",
    "running": "выполняется",
    "completed": "выполнена",
    "failed": "ошибка",
    "rate_limited": "ждёт снятия лимита",
    "cancelled": "отменена",
}

# One spelling for recurring buttons — «← Назад» vs «◀ Назад» and 💬 vs ✍️
# made the UI look assembled from different bots
BACK_LABEL = "← Назад"
REPLY_LABEL = "💬 Ответить"


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _main_menu() -> ReplyKeyboardMarkup:
    pause_label = "▶ Продолжить" if db.is_paused() else "⏸ Пауза"
    return ReplyKeyboardMarkup(
        [
            ["📋 Задачи", "➕ Добавить задачу"],
            ["📊 Статистика", "🔌 Провайдеры", "⚡ Скилы"],
            [pause_label],
        ],
        resize_keyboard=True,
    )


def _contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Поделиться контактом", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _local_str(dt, fmt="%d.%m.%Y %H:%M") -> str:
    """Convert UTC datetime to local time string."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        # naive — assume UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime(fmt)


def _project_name(working_dir):
    """Extract short project name from working_dir path."""
    if not working_dir:
        return None
    return os.path.basename(working_dir.rstrip("/\\"))


# Status filters for the task list: after a hundred finished tasks the active
# ones are impossible to find by paging through everything.
_TASK_FILTERS = {
    "all": (None, "📦 Все"),
    "act": (("pending", "running", "rate_limited"), "🔄 Активные"),
    "done": (("completed", "failed", "cancelled"), "✅ Завершённые"),
}


def _filter_total(stats, flt: str) -> int:
    if flt == "act":
        return stats.pending + stats.running + stats.rate_limited
    if flt == "done":
        return stats.completed + stats.failed + stats.cancelled
    return stats.total


def _tasks_keyboard(tasks, page: int, total: int, flt: str = "all") -> InlineKeyboardMarkup:
    keyboard = [[
        InlineKeyboardButton(("• " if key == flt else "") + label, callback_data=f"flt:{key}")
        for key, (_, label) in _TASK_FILTERS.items()
    ]]
    for t in tasks:
        icon = STATUS_ICON.get(t.status.value, "•")
        proj = _project_name(t.working_dir)
        proj_tag = f"[{proj}] " if proj else ""
        label = t.prompt[:32].replace("\n", " ")
        keyboard.append([
            InlineKeyboardButton(
                f"{icon} #{t.id} {proj_tag}{label}",
                callback_data=f"task:{t.id}",
            )
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Пред", callback_data=f"page:{page - 1}:{flt}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("▶ След", callback_data=f"page:{page + 1}:{flt}"))
    if nav:
        keyboard.append(nav)
    return InlineKeyboardMarkup(keyboard)


def _task_detail_keyboard(task) -> InlineKeyboardMarkup:
    rows = []
    status = task.status.value
    if status in ("pending", "rate_limited", "running"):
        rows.append([InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_task:{task.id}")])
    if status == "running":
        row = [InlineKeyboardButton("🔄 Обновить", callback_data=f"task:{task.id}")]
        if task.herdr_pane:
            ref = f"{task.machine or ''}:{task.herdr_pane}"
            row.append(InlineKeyboardButton("📺 Экран", callback_data=f"hd_screen:{ref}"))
        rows.append(row)
        rows.append([InlineKeyboardButton("🔁 Сбросить (stuck)", callback_data=f"reset_task:{task.id}")])
    action_row = []
    # An agent session survives a failure — "исправь и продолжи" is the most
    # common reply to a failed task, so the button must not be completed-only.
    if status in ("completed", "failed") and task.session_id:
        action_row.append(InlineKeyboardButton(REPLY_LABEL, callback_data=f"reply_task:{task.id}"))
    if status in ("completed", "failed", "cancelled"):
        action_row.append(InlineKeyboardButton("🔁 Повторить", callback_data=f"rerun:{task.id}"))
    if action_row:
        rows.append(action_row)
    if task.result and len(task.result) > 800:
        rows.append([InlineKeyboardButton("📄 Полный вывод", callback_data=f"full_result:{task.id}")])
    rows.append([InlineKeyboardButton("← К списку", callback_data="tasklist"),
                 InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_task:{task.id}")])
    return InlineKeyboardMarkup(rows)


def _after_action_keyboard(task_id: int) -> InlineKeyboardMarkup:
    """Keyboard for action confirmations so the chat never dead-ends."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔎 Открыть", callback_data=f"task:{task_id}"),
        InlineKeyboardButton("← К списку", callback_data="tasklist"),
    ]])


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _normalize_phone(phone: str) -> str:
    phone = phone.strip()
    return phone if phone.startswith("+") else "+" + phone


async def _deny(update: Update):
    await update.message.reply_text(
        "Сначала авторизуйтесь:", reply_markup=_contact_keyboard()
    )


TG_LIMIT = 4096


def _clip(text: str, limit: int = TG_LIMIT) -> str:
    """Keep a message under Telegram's 4096 limit without splitting a trailing
    MarkdownV2 escape (a lone '\\' at the cut would break parsing)."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    # don't end on an unbalanced backslash-escape
    trailing = len(cut) - len(cut.rstrip("\\"))
    if trailing % 2:
        cut = cut[:-1]
    return cut + "…"


def require_auth(func):
    """Gate a callback/entry handler on authorization AND a private chat.

    callback_data can't be trusted: a user removed from the allow-list still
    holds old inline buttons, and in a group any member could press them. Several
    callback handlers used to skip the check that message handlers already do."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        user = update.effective_user
        chat = update.effective_chat
        ok = (user is not None and is_authorized(user.id)
              and (chat is None or chat.type == "private"))
        if not ok:
            if update.callback_query:
                await update.callback_query.answer("Нет доступа.", show_alert=True)
            elif update.message:
                await update.message.reply_text("Нет доступа.")
            return ConversationHandler.END
        return await func(update, context, *a, **kw)
    return wrapper


# Password gate for task creation (PP_TASK_PASSWORD). Entering it correctly in
# the add-task flow authorizes the user for a while, so the skill/reply shortcuts
# can require it too instead of silently bypassing the password.
_pw_authed: dict = {}
_PW_TTL = 3600


def _pw_ok(user_id: int) -> bool:
    if not TASK_PASSWORD:
        return True
    exp = _pw_authed.get(user_id)
    return exp is not None and exp > time.monotonic()


def _pw_grant(user_id: int):
    _pw_authed[user_id] = time.monotonic() + _PW_TTL


_WIZARD_KEYS = ("new_prompt", "new_provider", "new_priority", "new_dir", "new_schedule",
                "new_recurrence", "new_detached", "new_keep_pane", "new_model",
                "new_skip_permissions", "new_skill_name", "new_herdr_target", "new_machine",
                "new_worktree", "pw_attempts", "model_list", "herdr_targets",
                "last_settings", "dir_base", "dir_subs", "dir_page", "dir_hist")


def _clear_wizard(context):
    """Drop leftover add-task state so an aborted wizard can't bleed a stale
    machine/provider into the next task (e.g. a skill started right after)."""
    for key in _WIZARD_KEYS:
        context.user_data.pop(key, None)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_authorized(update.effective_user.id):
        await update.message.reply_text(
            "Добро пожаловать в PromptPilot!\nВыберите действие:",
            reply_markup=_main_menu(),
        )
    else:
        await update.message.reply_text(
            "Для доступа поделитесь своим номером телефона:",
            reply_markup=_contact_keyboard(),
        )


# ---------------------------------------------------------------------------
# Contact (authorization)
# ---------------------------------------------------------------------------

async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact
    user_id = update.effective_user.id

    if contact.user_id != user_id:
        await update.message.reply_text("Можно поделиться только своим контактом.")
        return

    phone = _normalize_phone(contact.phone_number)
    allowed = {_normalize_phone(p) for p in load_allowed_phones()}

    if not allowed:
        from .config import DB_DIR
        await update.message.reply_text(
            "Список разрешённых номеров не настроен.\n"
            f"Задайте PP_TG_ALLOWED_PHONES или создайте {DB_DIR / 'tg_config.json'} вида:\n"
            '{"allowed_phones": ["+79991234567"]}'
        )
        return

    if phone in allowed:
        authorize_user(user_id, phone)
        await update.message.reply_text(
            "Авторизация успешна! Добро пожаловать.\n\n"
            "PromptPilot ставит задачи для AI-агентов (Claude Code, opencode) в очередь:\n"
            "1. «➕ Добавить задачу» — опишите задачу текстом.\n"
            "2. Мастер уточнит, где её выполнить; редкие опции — за «⚙ Дополнительно».\n"
            "3. Когда агент закончит, придёт уведомление с кнопками: открыть результат, "
            "ответить агенту, повторить задачу.\n\n"
            "Подробнее — /help",
            reply_markup=_main_menu(),
        )
    else:
        await update.message.reply_text(
            "Ваш номер не найден в списке разрешённых. Обратитесь к администратору."
        )


# ---------------------------------------------------------------------------
# Task list
# ---------------------------------------------------------------------------

async def show_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await _deny(update)
        return

    # Opening the list from the menu is always the first page of «Все» —
    # otherwise a remembered deep page/filter shows "Задач нет" confusingly.
    context.user_data["tasks_page"] = 0
    context.user_data["tasks_filter"] = "all"
    page = 0
    stats = db.get_stats()
    tasks = db.list_tasks(limit=PAGE_SIZE, offset=page * PAGE_SIZE)

    if not tasks:
        await update.message.reply_text("Задач нет.", reply_markup=_main_menu())
        return

    await update.message.reply_text(
        f"*Задачи* (стр. {page + 1}, всего {stats.total}):",
        reply_markup=_tasks_keyboard(tasks, page, stats.total),
        parse_mode="Markdown",
    )


async def _render_task_list(query, context, page: int, flt: str = None):
    if flt is None:
        flt = context.user_data.get("tasks_filter", "all")
    if flt not in _TASK_FILTERS:
        flt = "all"
    context.user_data["tasks_page"] = page
    context.user_data["tasks_filter"] = flt

    stats = db.get_stats()
    total = _filter_total(stats, flt)
    statuses = _TASK_FILTERS[flt][0]
    tasks = db.list_tasks(limit=PAGE_SIZE, offset=page * PAGE_SIZE, statuses=statuses)

    if not tasks and flt == "all":
        await query.edit_message_text("Задач нет.")
        return

    label = _TASK_FILTERS[flt][1].split(" ", 1)[1].lower()
    await query.edit_message_text(
        f"*Задачи* ({label}: {total}, стр. {page + 1}):",
        reply_markup=_tasks_keyboard(tasks, page, total, flt),
        parse_mode="Markdown",
    )


@require_auth
async def cb_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    page = int(parts[1])
    flt = parts[2] if len(parts) > 2 else None
    await _render_task_list(query, context, page, flt)


@require_auth
async def cb_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _render_task_list(query, context, 0, query.data.split(":")[1])


@require_auth
async def cb_back_to_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """«← К списку» from a task card — return to the remembered list page."""
    query = update.callback_query
    await query.answer()
    await _render_task_list(query, context, context.user_data.get("tasks_page", 0))


# ---------------------------------------------------------------------------
# Task detail
# ---------------------------------------------------------------------------

@require_auth
async def cb_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # answerCallbackQuery works only once per press — don't answer up front,
    # or the "Без изменений." toast on refresh would be silently dropped.
    query = update.callback_query

    task_id = int(query.data.split(":")[1])
    task = db.get_task(task_id)
    if not task:
        await query.answer()
        await query.edit_message_text("Задача не найдена.")
        return

    icon = STATUS_ICON.get(task.status.value, "•")
    created = _local_str(task.created_at)
    completed = _local_str(task.completed_at) if task.completed_at else None
    provider_str = _esc(task.provider) if task.provider else "claude \\(по умолчанию\\)"

    proj = _project_name(task.working_dir)
    status_ru = STATUS_RU.get(task.status.value, task.status.value)
    text = (
        f"*Задача \\#{task.id}*\n"
        f"Статус: {icon} {_esc(status_ru)}\n"
        f"Провайдер: {provider_str}\n"
    )
    if proj:
        text += f"Проект: {_esc(proj)}\n"
    if task.model_used:
        text += f"Модель: `{_esc_code(task.model_used)}`\n"
    text += (
        f"Приоритет: {task.priority}\n"
        f"Создана: {_esc(created)}\n"
        f"Выполнена: {_esc(completed or '—')}\n"
        f"Попытки: {task.retry_count} из {task.max_retries}"
    )
    if task.working_dir:
        text += f"\nДир: `{_esc_code(task.working_dir)}`"
    if task.verdict:
        icon = {"ГОТОВО": "✅", "УЖЕ СДЕЛАНО": "✅",
                "НУЖЕН ЧЕЛОВЕК": "🟡", "НЕ СМОГ": "❌"}.get(task.verdict, "•")
        text += f"\nИтог: {icon} {_esc(task.verdict)}"
    if task.note:
        text += f"\n✎ Приписка: {_esc(task.note[:200])}"
    if task.worktree:
        branch = task.worktree_branch or wt_branch(task.id)
        text += f"\n🌿 Worktree: `{_esc_code(task.worktree_path or 'ещё не создан')}`\nВетка: `{_esc_code(branch)}`"
    if task.status.value == "rate_limited" and task.next_run_at:
        # Local time like every other date on the card — not bare UTC
        text += f"\nСброс: {_esc(_local_str(task.next_run_at))}"

    text += f"\n\n*Промпт:*\n{_esc(task.prompt[:500])}"

    if task.result:
        result_text = task.result.split("\n--- Meta ---")[0].strip()
        if result_text:
            text += f"\n\n*Результат:*\n{_esc(result_text[:800])}"
        if "--- Meta ---" in task.result:
            meta_block = task.result[task.result.find("--- Meta ---"):]
            for line in meta_block.splitlines():
                line = line.strip()
                if line.startswith(("Model:", "Cost:", "Time:", "Tokens:", "Rate limit resets:")):
                    text += f"\n{_esc(line)}"
    if task.error:
        text += f"\n\n*Ошибка:*\n{_esc(task.error[:300])}"

    kb = _task_detail_keyboard(task)
    try:
        await query.edit_message_text(_clip(text), reply_markup=kb, parse_mode="MarkdownV2")
    except BadRequest as e:
        if "not modified" in str(e).lower():
            # "🔄 Обновить" pressed while nothing changed
            await query.answer("Без изменений.")
            return
        # Too long after escaping, or a truncation landed inside markdown —
        # fall back to plain text so the card is never unopenable.
        plain = (f"Задача #{task.id} [{task.status.value}]\n"
                 f"Промпт:\n{task.prompt[:500]}\n\n"
                 f"Результат:\n{(task.result or task.error or '')[:1500]}")
        try:
            await query.edit_message_text(_clip(plain), reply_markup=kb)
        except BadRequest as e2:
            if "not modified" not in str(e2).lower():
                raise
    await query.answer()


def _esc(text: str) -> str:
    """Escape special chars for MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _esc_code(text: str) -> str:
    """Escape for text inside MarkdownV2 backtick code spans (only backtick and backslash)."""
    return text.replace("\\", "\\\\").replace("`", "\\`")


def _mask_secret(name: str, value: str) -> str:
    """Mask provider env values for display (masks by default, no suffix leak)."""
    from .config import mask_secret_value
    return mask_secret_value(name, value)


@require_auth
async def cb_cancel_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    task_id = int(query.data.split(":")[1])
    task = db.get_task(task_id)
    if task and task.status.value == "running":
        # running: worker убьёт процесс в течение пары секунд
        if db.request_cancel(task_id):
            await query.answer("Останавливаю…")
            await query.edit_message_text(f"Задача #{task_id} останавливается (процесс будет убит).",
                                          reply_markup=_after_action_keyboard(task_id))
        else:
            await query.answer("Задача уже не выполняется.", show_alert=True)
    elif db.cancel_task(task_id):
        await query.answer("Отменено.")
        await query.edit_message_text(f"Задача #{task_id} отменена.",
                                      reply_markup=_after_action_keyboard(task_id))
    else:
        await query.answer("Не удалось отменить (уже выполнена или не найдена).", show_alert=True)


@require_auth
async def cb_reset_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    task_id = int(query.data.split(":")[1])
    if db.reset_task(task_id):
        await query.answer("Сброшено в pending.")
        await query.edit_message_text(f"Задача #{task_id} возвращена в очередь.",
                                      reply_markup=_after_action_keyboard(task_id))
    else:
        await query.answer("Задача не в статусе running.", show_alert=True)


@require_auth
async def cb_delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """First tap on 🗑 — ask for confirmation instead of deleting right away:
    the button sits next to the frequently-used ones and a stray tap would
    destroy the task together with its result."""
    query = update.callback_query
    await query.answer()
    task_id = int(query.data.split(":")[1])
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Точно удалить", callback_data=f"del_yes:{task_id}"),
        InlineKeyboardButton("↩ Отмена", callback_data=f"task:{task_id}"),
    ]]))


@require_auth
async def cb_delete_task_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    task_id = int(query.data.split(":")[1])
    if db.delete_task(task_id):
        await query.answer("Удалено.")
        await query.edit_message_text(
            f"Задача #{task_id} удалена.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("← К списку", callback_data="tasklist")]]))
    else:
        await query.answer("Задача не найдена.", show_alert=True)


@require_auth
async def cb_rerun_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """«🔁 Повторить» — clone a finished task without walking the wizard again."""
    query = update.callback_query
    task_id = int(query.data.split(":")[1])
    task = db.get_task(task_id)
    if not task:
        await query.answer("Задача не найдена.", show_alert=True)
        return
    if not _pw_ok(query.from_user.id):
        await query.answer("Сначала введите пароль через ➕ Добавить задачу.", show_alert=True)
        return
    new = db.create_task(TaskCreate(
        prompt=task.prompt,
        working_dir=task.working_dir,
        provider=task.provider,
        priority=task.priority,
        skip_permissions=task.skip_permissions,
        model=task.model,
        tg_chat_id=query.message.chat_id,
        detached=task.detached,
        keep_pane=task.keep_pane,
        machine=task.machine,
        worktree=task.worktree,
    ))
    await query.answer(f"Задача #{new.id} добавлена.")
    await query.message.reply_text(
        f"🔁 Задача #{new.id} добавлена (повтор #{task_id}).",
        reply_markup=_after_action_keyboard(new.id),
    )


@require_auth
async def cb_full_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """«📄 Полный вывод» — send the untruncated result as a document; the card
    itself clips it to 800 chars and Telegram messages cap at 4096."""
    import io
    query = update.callback_query
    task_id = int(query.data.split(":")[1])
    task = db.get_task(task_id)
    if not task or not (task.result or task.error):
        await query.answer("Вывода нет.", show_alert=True)
        return
    await query.answer()
    content = task.result or task.error
    buf = io.BytesIO(content.encode("utf-8"))
    buf.name = f"task_{task_id}.md"
    await query.message.reply_document(
        document=buf,
        caption=f"Полный вывод задачи #{task_id} ({len(content)} символов)",
    )


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await _deny(update)
        return

    s = db.get_stats()
    costs = db.get_cost_stats()
    text = (
        "*Статистика PromptPilot*\n\n"
        f"⏳ Ожидают:      {s.pending}\n"
        f"🔄 Выполняются:  {s.running}\n"
        f"⏸ Rate limited: {s.rate_limited}\n"
        f"✅ Выполнены:    {s.completed}\n"
        f"❌ Ошибки:       {s.failed}\n"
        f"🚫 Отменены:     {s.cancelled}\n"
        f"📦 Всего:        {s.total}"
    )
    if costs["total"] > 0:
        text += (
            f"\n\n💰 Сегодня:  ${costs['today']:.4f}\n"
            f"💰 За неделю: ${costs['week']:.4f}\n"
            f"💰 Всего:     ${costs['total']:.4f}"
        )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=_main_menu())


async def toggle_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await _deny(update)
        return
    if db.is_paused():
        db.set_setting("worker_paused", "0")
        await update.message.reply_text("▶ Воркер возобновлён.", reply_markup=_main_menu())
    else:
        db.set_setting("worker_paused", "1")
        await update.message.reply_text("⏸ Воркер на паузе. Текущие задачи завершатся, новые не запустятся.", reply_markup=_main_menu())


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

async def show_providers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await _deny(update)
        return

    providers = load_providers_detailed()
    buttons = []
    for name, info in providers.items():
        desc = info.get("description", "")
        label = f"{name} — {desc}" if desc else name
        buttons.append([InlineKeyboardButton(label, callback_data=f"prov_detail:{name}")])

    await update.message.reply_text(
        "*Провайдеры:*\nНажмите для просмотра настроек",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_provider_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_authorized(query.from_user.id):
        await query.answer("Нет доступа.", show_alert=True)
        return

    name = query.data.split(":", 1)[1]
    providers = load_providers_detailed()
    info = providers.get(name)
    if not info:
        await query.answer("Провайдер не найден.", show_alert=True)
        return

    lines = [f"*{_esc(name)}* — {_esc(info.get('description', ''))}\n"]

    cmd = info.get("cmd", "")
    if cmd:
        lines.append(f"🔧 Команда:\n`{_esc_code(cmd)}`\n")
    elif info.get("executor"):
        lines.append(f"🔧 Исполнитель: {_esc(info['executor'])} \\(kind: {_esc(info.get('kind', 'claude'))}\\)\n")

    import asyncio
    models = await asyncio.to_thread(get_provider_models, name)
    if models:
        shown = models[:20]
        more = "" if len(models) <= 20 else f" \\(\\+{len(models) - 20}\\)"
        lines.append("🏷 Модели: " + ", ".join(f"`{_esc_code(m)}`" for m in shown) + more)
    else:
        lines.append("🏷 Модели: по умолчанию \\(sonnet, opus, haiku\\)")
    lines.append("")

    env = info.get("env")
    if env:
        lines.append("🔐 Переменные:")
        for k, v in env.items():
            masked = _mask_secret(k, str(v))
            lines.append(f"  {_esc(k)}: `{_esc_code(masked)}`")
        lines.append("")

    source = info.get("_source", "builtin")
    lines.append(f"📁 Источник: {_esc(source)}")
    source_path = info.get("_source_path")
    if source_path:
        lines.append(f"   `{_esc_code(source_path)}`")

    buttons = [[InlineKeyboardButton(BACK_LABEL, callback_data="prov_list")]]
    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_provider_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_authorized(query.from_user.id):
        await query.answer("Нет доступа.", show_alert=True)
        return

    providers = load_providers_detailed()
    buttons = []
    for pname, info in providers.items():
        desc = info.get("description", "")
        label = f"{pname} — {desc}" if desc else pname
        buttons.append([InlineKeyboardButton(label, callback_data=f"prov_detail:{pname}")])

    await query.edit_message_text(
        "*Провайдеры:*\nНажмите для просмотра настроек",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ---------------------------------------------------------------------------
# Add task (ConversationHandler)
# ---------------------------------------------------------------------------

async def add_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await _deny(update)
        return ConversationHandler.END

    _clear_wizard(context)
    # _pw_grant already gave this user an hour of access (reply/skill flows
    # honour it) — asking again for every task just retypes the same password.
    if TASK_PASSWORD and not _pw_ok(update.effective_user.id):
        await update.message.reply_text(
            "Введите пароль для создания задачи:\n(/cancel — отменить)",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ASK_PASSWORD

    await update.message.reply_text(
        "Введите промпт для задачи:\n(/cancel — отменить)",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_PROMPT


_PW_ATTEMPTS = 3


async def add_task_got_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    entered = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    import hmac
    if not hmac.compare_digest(entered, TASK_PASSWORD):
        # A typo must not kill the wizard (and a typed-out prompt with it)
        attempts = context.user_data.get("pw_attempts", 0) + 1
        context.user_data["pw_attempts"] = attempts
        if attempts >= _PW_ATTEMPTS:
            context.user_data.pop("pw_attempts", None)
            _clear_wizard(context)
            await update.message.reply_text(
                f"Неверный пароль ({_PW_ATTEMPTS} попытки). Создание задачи отменено.",
                reply_markup=_main_menu(),
            )
            return ConversationHandler.END
        await update.message.reply_text(
            f"Неверный пароль, попробуйте ещё раз ({attempts}/{_PW_ATTEMPTS}):\n(/cancel — отменить)"
        )
        return ASK_PASSWORD

    context.user_data.pop("pw_attempts", None)
    _pw_grant(update.effective_user.id)
    # The skill flow enters the wizard at the password step too — continue it
    # from where it left off instead of dumping the user into a plain prompt.
    if context.user_data.get("new_skill_name"):
        return await _ask_skill_args(update.message.reply_text, context)
    await update.message.reply_text(
        "Введите промпт для задачи:\n(/cancel — отменить)",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_PROMPT


def _provider_buttons(machine: str = None):
    if machine:
        # Availability is decided by the machine's own probe — a provider may
        # be installed there and missing here (and vice versa).
        allowed = set(load_machines().get(machine, {}).get("providers") or [])
        providers = {n: i for n, i in load_providers().items()
                     if n in allowed and not i.get("hidden")}
    else:
        providers = pickable_providers()
    row, buttons = [], []
    for name in providers:
        row.append(InlineKeyboardButton(name, callback_data=f"pickprov:{name}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return buttons


# Which wizard choices are worth remembering between tasks. Schedule and
# recurrence are task-specific; a herdr target names a live session.
_LAST_FIELDS = ("new_machine", "new_provider", "new_model", "new_dir", "new_priority",
                "new_skip_permissions", "new_keep_pane", "new_worktree", "new_detached")


def _save_last_settings(chat_id: int, context):
    ud = context.user_data
    if ud.get("new_herdr_target"):
        return
    try:
        db.set_setting(f"tg_last_wizard:{chat_id}",
                       json.dumps({k: ud.get(k) for k in _LAST_FIELDS}))
    except Exception as e:
        logger.warning("не смог сохранить настройки мастера: %s", e)


def _load_last_settings(chat_id: int) -> Optional[dict]:
    try:
        raw = db.get_setting(f"tg_last_wizard:{chat_id}")
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _last_settings_line(last: dict) -> str:
    parts = [last.get("new_provider") or f"{DEFAULT_CLI} (по умолчанию)"]
    if last.get("new_model"):
        parts.append(last["new_model"])
    parts.append(last.get("new_machine") or "локально")
    parts.append(_short_dir(last["new_dir"]) if last.get("new_dir") else "без директории")
    return " · ".join(parts)


async def _ask_machine_or_provider(send, context):
    machines = load_machines()
    if machines:
        buttons = [[InlineKeyboardButton("💻 Локально", callback_data="machine:")]]
        buttons += [[InlineKeyboardButton(f"🖥 {n} ({m.get('host')})", callback_data=f"machine:{n}")]
                    for n, m in machines.items()]
        await send("Где выполнить задачу?", reply_markup=InlineKeyboardMarkup(buttons))
        return ASK_MACHINE

    buttons = _provider_buttons()
    buttons.append([InlineKeyboardButton(f"⬛ По умолчанию ({DEFAULT_CLI})", callback_data="pickprov:")])
    await send("Выберите провайдера:", reply_markup=InlineKeyboardMarkup(buttons))
    return ASK_PROVIDER


async def add_task_got_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_prompt"] = update.message.text

    # The user almost always runs the next task with the same provider and
    # folder — offer last time's settings before walking every step again.
    last = _load_last_settings(update.effective_chat.id)
    if last and not load_providers().get(last.get("new_provider") or DEFAULT_CLI, {}).get("session_target"):
        context.user_data["last_settings"] = last
        await update.message.reply_text(
            "Как в прошлый раз?\n" + _last_settings_line(last),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↩ Да, с этими настройками", callback_data="last_use")],
                [InlineKeyboardButton("✏️ Настроить заново", callback_data="last_edit"),
                 InlineKeyboardButton("✖ Отмена", callback_data="wiz_cancel")],
            ]),
        )
        return ASK_LAST

    return await _ask_machine_or_provider(update.message.reply_text, context)


async def cb_last_use(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    last = context.user_data.pop("last_settings", None) or {}
    for key in _LAST_FIELDS:
        if key in last:
            context.user_data[key] = last[key]
    # The saved provider may be gone from providers.json by now
    if context.user_data.get("new_provider") and \
            context.user_data["new_provider"] not in load_providers():
        for key in _LAST_FIELDS:
            context.user_data.pop(key, None)
        return await _ask_machine_or_provider(query.edit_message_text, context)
    if context.user_data.get("new_worktree") and not _worktree_available(context):
        context.user_data["new_worktree"] = False
    return await _show_confirm(query.edit_message_text, context)


async def cb_last_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("last_settings", None)
    return await _ask_machine_or_provider(query.edit_message_text, context)


_MODELS_PER_PAGE = 12


def _model_labels(models):
    """Readable button labels: drop the 'vendor/' prefix — the picker already
    scopes the vendor, and full ids in 3 columns truncate to 'opencode/…' on a
    phone. Models that collide after stripping keep the full id."""
    from collections import Counter
    short = [m.split("/", 1)[1] if "/" in m else m for m in models]
    counts = Counter(short)
    return [m if counts[s] > 1 else s for s, m in zip(short, models)]


def _model_keyboard(models, page: int = 0) -> InlineKeyboardMarkup:
    """Model picker over the full list, selected by index — no 64-byte
    callback_data limit and nothing silently dropped — paginated past 12.

    The list is fetched by the caller via asyncio.to_thread (get_provider_models
    may do a blocking network call for dynamic discovery) and cached in
    user_data["model_list"] for the index/page callbacks.
    """
    labels = _model_labels(models)
    last_page = max(0, (len(models) - 1) // _MODELS_PER_PAGE)
    page = max(0, min(page, last_page))
    start = page * _MODELS_PER_PAGE
    idxs = range(start, min(start + _MODELS_PER_PAGE, len(models)))

    # Column count follows the longest visible label so nothing truncates
    longest = max((len(labels[i]) for i in idxs), default=0)
    cols = 3 if longest <= 8 else 2 if longest <= 16 else 1

    buttons = [[InlineKeyboardButton("⚙️ По умолчанию (авто)", callback_data="model:")]]
    row = []
    for i in idxs:
        row.append(InlineKeyboardButton(labels[i], callback_data=f"model_i:{i}"))
        if len(row) == cols:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"model_pg:{page - 1}"))
    if page < last_page:
        rest = len(models) - start - _MODELS_PER_PAGE
        nav.append(InlineKeyboardButton(f"▶ ещё {rest}", callback_data=f"model_pg:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("✏️ Ввести id вручную", callback_data="model_manual")])
    return InlineKeyboardMarkup(buttons)


async def add_task_got_machine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    machine = query.data.split(":", 1)[1] or None
    context.user_data["new_machine"] = machine
    buttons = _provider_buttons(machine)
    if not buttons:
        # Not a dead end: ending the conversation here would throw away the
        # typed-out prompt. Re-show the machine list with an explanation.
        context.user_data.pop("new_machine", None)
        machines = load_machines()
        mb = [[InlineKeyboardButton("💻 Локально", callback_data="machine:")]]
        mb += [[InlineKeyboardButton(f"🖥 {n} ({m.get('host')})", callback_data=f"machine:{n}")]
               for n, m in machines.items()]
        await query.edit_message_text(
            f"На машине {machine} не найдено провайдеров.\n"
            "Обнови её пробу в веб-панели PromptPilot (раздел Machines) "
            "или выбери другую машину:",
            reply_markup=InlineKeyboardMarkup(mb),
        )
        return ASK_MACHINE
    await query.edit_message_text("Выберите провайдера:", reply_markup=InlineKeyboardMarkup(buttons))
    return ASK_PROVIDER


async def add_task_got_provider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    provider = query.data.split(":", 1)[1] or None
    context.user_data["new_provider"] = provider

    if load_providers().get(provider or DEFAULT_CLI, {}).get("session_target"):
        machine = context.user_data.get("new_machine")
        host = _machine_remote(machine)
        data = await _herdr_json("agent", "list", host=host)
        agents = ((data or {}).get("result") or {}).get("agents") or []
        agents = [a for a in agents if a.get("pane_id")]
        if not agents:
            # Keep the wizard (and the typed prompt) alive: let the user open
            # a pane and tap the provider again, or pick another one.
            where = f" на машине {machine}" if machine else ""
            buttons = _provider_buttons(machine)
            await query.edit_message_text(
                f"Нет открытых herdr-сессий{where}.\n"
                "Открой панель с агентом и выбери провайдера ещё раз:",
                reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
            )
            return ASK_PROVIDER
        ws = await _herdr_json("workspace", "list", host=host)
        ws_labels = {w.get("workspace_id"): w.get("label") or w.get("workspace_id")
                     for w in ((ws or {}).get("result") or {}).get("workspaces") or []}
        multi_ws = len(set(a.get("workspace_id") for a in agents)) > 1
        status_icon = {"blocked": "⏸", "done": "✅", "working": "🔄"}

        def _label(a):
            icon = status_icon.get(a.get("agent_status"), "•")
            what = (a.get("terminal_title_stripped") or a.get("name") or a.get("pane_id") or "")[:35]
            wsl = ws_labels.get(a.get("workspace_id"), "")
            return f"{icon} {wsl}: {what}" if multi_ws and wsl else f"{icon} {what}"

        # Selection by index into a cached list: an agent name in callback_data
        # can pass 64 bytes, and Telegram then rejects the WHOLE keyboard.
        targets = [a.get("name") or a.get("pane_id") for a in agents]
        context.user_data["herdr_targets"] = targets
        buttons = [[InlineKeyboardButton(_label(a), callback_data=f"hst_i:{i}")]
                   for i, a in enumerate(agents[:20])]
        if len(agents) > 20:
            buttons.append([InlineKeyboardButton(
                f"… ещё {len(agents) - 20} — сузь список, закрыв лишние панели",
                callback_data="hst_more")])
        await query.edit_message_text("В какую сессию отправить промпт?",
                                      reply_markup=InlineKeyboardMarkup(buttons))
        return ASK_HERDR_TARGET

    import asyncio
    models = await asyncio.to_thread(get_provider_models, provider or DEFAULT_CLI)
    if models:
        context.user_data["model_list"] = models
        await query.edit_message_text("Выберите модель:", reply_markup=_model_keyboard(models))
        return ASK_MODEL

    return await _ask_dir(query, context)


async def add_task_got_herdr_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "hst_more":
        await query.answer("Показаны первые 20 сессий.", show_alert=True)
        return ASK_HERDR_TARGET
    targets = context.user_data.get("herdr_targets") or []
    idx = int(query.data.split(":")[1])
    if idx >= len(targets):
        await query.answer("Список устарел — выберите провайдера заново.", show_alert=True)
        return ASK_HERDR_TARGET
    await query.answer()
    context.user_data["new_herdr_target"] = targets[idx]
    return await _show_confirm(query.edit_message_text, context)


async def add_task_got_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """«По умолчанию» (and legacy model:<id> buttons from old messages)."""
    query = update.callback_query
    await query.answer()
    model = query.data.split(":", 1)[1] or None
    context.user_data["new_model"] = model
    return await _ask_dir(query, context)


async def add_task_got_model_idx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    models = context.user_data.get("model_list") or []
    idx = int(query.data.split(":")[1])
    if idx >= len(models):
        await query.answer("Список устарел — выберите модель заново.", show_alert=True)
        return ASK_MODEL
    await query.answer()
    context.user_data["new_model"] = models[idx]
    return await _ask_dir(query, context)


async def cb_model_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    models = context.user_data.get("model_list") or []
    if models:
        page = int(query.data.split(":")[1])
        await query.edit_message_reply_markup(reply_markup=_model_keyboard(models, page))
    return ASK_MODEL


async def cb_model_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Введите id модели текстом (например `opencode/big-pickle`)\n"
        "или /skip — модель по умолчанию:",
        parse_mode="Markdown",
    )
    return ASK_MODEL


async def add_task_got_model_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_model"] = update.message.text.strip() or None
    return await _ask_dir(_MsgSend(update.message), context)


async def add_task_skip_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_model"] = None
    return await _ask_dir(_MsgSend(update.message), context)


def _list_projects():
    """Return sorted list of immediate subdirectories under PROJECTS_ROOT."""
    if not PROJECTS_ROOT:
        return []
    try:
        return sorted(
            d for d in os.listdir(PROJECTS_ROOT)
            if os.path.isdir(os.path.join(PROJECTS_ROOT, d)) and not d.startswith(".")
        )
    except OSError:
        return []


def _list_subdirs(parent_path):
    """Return sorted list of immediate subdirectories inside *parent_path*."""
    try:
        return sorted(
            d for d in os.listdir(parent_path)
            if os.path.isdir(os.path.join(parent_path, d)) and not d.startswith(".")
        )
    except OSError:
        return []


def _list_projects_with_skills():
    """Return projects that have local skill files in .claude/commands/ or .claude/skills/.

    Supports both layouts:
    - Flat:   .claude/skills/*.md
    - Subdir: .claude/skills/<skill-name>/*.md
    """
    from pathlib import Path
    result = []
    for proj in _list_projects():
        full = Path(PROJECTS_ROOT) / proj
        for sub in ("commands", "skills"):
            skill_dir = full / ".claude" / sub
            if not skill_dir.is_dir():
                continue
            # Flat .md files
            if any(f for f in skill_dir.glob("*.md") if f.name.lower() != "readme.md"):
                result.append(proj)
                break
            # Subdir-style: subdirectory containing at least one .md file
            if any(d for d in skill_dir.iterdir() if d.is_dir() and any(d.glob("*.md"))):
                result.append(proj)
                break
    return result


async def add_task_got_priority(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["new_priority"] = int(query.data.split(":")[1])
    return await _show_extras(query.edit_message_text, context)


async def cb_priority_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await _show_extras(query.edit_message_text, context)


_DIRS_PER_PAGE = 12


def _short_dir(path: str) -> str:
    """Last two path segments — a full absolute path won't fit on a button."""
    parts = [p for p in path.replace("\\", "/").rstrip("/").split("/") if p]
    return "…/" + "/".join(parts[-2:]) if len(parts) > 2 else path


def _manual_dir_prompt() -> str:
    text = ("Рабочая директория для выполнения задачи:\n"
            "Введите путь или /skip чтобы пропустить.")
    if not PROJECTS_ROOT:
        text += ("\n\nСовет: задайте PP_PROJECTS_ROOT в .env — "
                 "и папки можно будет выбирать кнопками.")
    return text


def _clear_dir_nav(context):
    for k in ("dir_base", "dir_subs", "dir_page", "dir_hist"):
        context.user_data.pop(k, None)


def _dir_screen(context):
    """(text, keyboard) for the catalog picker at user_data['dir_base'].

    Navigation state lives in user_data and buttons carry indexes into
    dir_subs: a path in callback_data would hit Telegram's 64-byte limit on
    any deep tree. Depth is unlimited (the old picker stopped at 2 levels)."""
    base = context.user_data.get("dir_base") or PROJECTS_ROOT
    subs = _list_subdirs(base)
    context.user_data["dir_base"] = base
    context.user_data["dir_subs"] = subs

    last_page = max(0, (len(subs) - 1) // _DIRS_PER_PAGE)
    page = max(0, min(context.user_data.get("dir_page", 0), last_page))
    context.user_data["dir_page"] = page
    start = page * _DIRS_PER_PAGE

    at_root = os.path.abspath(base) == os.path.abspath(PROJECTS_ROOT)
    buttons = []
    if not at_root:
        buttons.append([InlineKeyboardButton("✅ Выбрать эту папку", callback_data="dir_here")])
    row = []
    for i in range(start, min(start + _DIRS_PER_PAGE, len(subs))):
        name = subs[i]
        if _list_subdirs(os.path.join(base, name)):
            # has children → opens; a leaf is picked with a single tap
            row.append(InlineKeyboardButton(f"{name} 📁", callback_data=f"dirn:{i}"))
        else:
            row.append(InlineKeyboardButton(name, callback_data=f"dirp:{i}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"dir_pg:{page - 1}"))
    if page < last_page:
        rest = len(subs) - start - _DIRS_PER_PAGE
        nav.append(InlineKeyboardButton(f"▶ ещё {rest}", callback_data=f"dir_pg:{page + 1}"))
    if nav:
        buttons.append(nav)
    if not at_root:
        buttons.append([InlineKeyboardButton(BACK_LABEL, callback_data="dir_up")])
    buttons.append([
        InlineKeyboardButton("✏️ Ввести вручную", callback_data="dir:__manual__"),
        InlineKeyboardButton("⏭ Пропустить", callback_data="dir:__skip__"),
    ])

    if at_root:
        text = "Выберите рабочую директорию:"
    else:
        text = f"📂 {os.path.relpath(base, PROJECTS_ROOT)}:"
    return text, InlineKeyboardMarkup(buttons)


async def _ask_dir(query, context):
    # Directory pre-filled (e.g. from project skills picker) — skip dir step
    if "new_dir" in context.user_data:
        pre_dir = context.user_data["new_dir"]
        label = pre_dir if pre_dir else "не указана"
        await query.edit_message_text(f"Директория: `{_esc_code(label)}`", parse_mode="MarkdownV2")
        return await _after_dir_from_query(query, context)

    if _list_projects():
        context.user_data["dir_base"] = PROJECTS_ROOT
        context.user_data["dir_page"] = 0
        text, kb = _dir_screen(context)
        await query.edit_message_text(text, reply_markup=kb)
        return ASK_DIR

    # No PP_PROJECTS_ROOT (or it is empty/unreadable): the working_dirs of past
    # tasks are already in the DB — offer those before falling back to typing
    # an absolute path on a phone keyboard.
    machine = context.user_data.get("new_machine")
    try:
        hist = db.recent_working_dirs(limit=8, machine=machine)
    except Exception:
        hist = []
    if hist:
        context.user_data["dir_hist"] = hist
        buttons = [[InlineKeyboardButton(_short_dir(d), callback_data=f"dirh:{i}")]
                   for i, d in enumerate(hist)]
        buttons.append([
            InlineKeyboardButton("✏️ Ввести вручную", callback_data="dir:__manual__"),
            InlineKeyboardButton("⏭ Пропустить", callback_data="dir:__skip__"),
        ])
        await query.edit_message_text(
            "Рабочая директория (недавние):",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return ASK_DIR

    await query.edit_message_text(_manual_dir_prompt())
    return ASK_DIR_MANUAL


async def _pick_dir(query, context, full_path):
    context.user_data["new_dir"] = full_path
    _clear_dir_nav(context)
    await query.edit_message_text(f"Директория: `{_esc_code(full_path)}`", parse_mode="MarkdownV2")
    return await _after_dir_from_query(query, context)


async def cb_dir_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open a subdirectory (dirn:<idx> into dir_subs)."""
    query = update.callback_query
    await query.answer()
    subs = context.user_data.get("dir_subs") or []
    idx = int(query.data.split(":")[1])
    if idx >= len(subs):
        text, kb = _dir_screen(context)  # stale buttons — re-render
        await query.edit_message_text(text, reply_markup=kb)
        return ASK_DIR
    base = context.user_data.get("dir_base") or PROJECTS_ROOT
    context.user_data["dir_base"] = os.path.join(base, subs[idx])
    context.user_data["dir_page"] = 0
    text, kb = _dir_screen(context)
    await query.edit_message_text(text, reply_markup=kb)
    return ASK_DIR


async def cb_dir_pick_idx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pick a leaf subdirectory (dirp:<idx>)."""
    query = update.callback_query
    subs = context.user_data.get("dir_subs") or []
    idx = int(query.data.split(":")[1])
    if idx >= len(subs):
        await query.answer("Список устарел — выберите заново.", show_alert=True)
        return ASK_DIR
    await query.answer()
    base = context.user_data.get("dir_base") or PROJECTS_ROOT
    return await _pick_dir(query, context, os.path.join(base, subs[idx]))


async def cb_dir_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    base = context.user_data.get("dir_base") or PROJECTS_ROOT
    return await _pick_dir(query, context, base)


async def cb_dir_up(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    base = context.user_data.get("dir_base") or PROJECTS_ROOT
    if os.path.abspath(base) != os.path.abspath(PROJECTS_ROOT):
        context.user_data["dir_base"] = os.path.dirname(base)
        context.user_data["dir_page"] = 0
    text, kb = _dir_screen(context)
    await query.edit_message_text(text, reply_markup=kb)
    return ASK_DIR


async def cb_dir_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["dir_page"] = int(query.data.split(":")[1])
    text, kb = _dir_screen(context)
    await query.edit_message_text(text, reply_markup=kb)
    return ASK_DIR


async def cb_dir_hist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pick a directory from task history (dirh:<idx> into dir_hist)."""
    query = update.callback_query
    hist = context.user_data.get("dir_hist") or []
    idx = int(query.data.split(":")[1])
    if idx >= len(hist):
        await query.answer("Список устарел — выберите заново.", show_alert=True)
        return ASK_DIR
    await query.answer()
    return await _pick_dir(query, context, hist[idx])


async def add_task_got_dir_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """__skip__ / __manual__ (and legacy dir:<name> buttons in old messages)."""
    query = update.callback_query
    await query.answer()
    value = query.data.split(":", 1)[1]

    if value == "__skip__":
        context.user_data["new_dir"] = None
        _clear_dir_nav(context)
        await query.edit_message_text("Директория: не указана")
        return await _after_dir_from_query(query, context)
    if value == "__manual__":
        await query.edit_message_text(_manual_dir_prompt())
        return ASK_DIR_MANUAL
    return await _pick_dir(query, context, os.path.join(PROJECTS_ROOT, value))


async def add_task_got_dir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = os.path.expanduser(update.message.text.strip())
    # A local path can be checked right now — a typo caught here beats a task
    # that fails at start. A remote machine's filesystem is not ours to ask.
    if path and not context.user_data.get("new_machine") and not os.path.isdir(path):
        await update.message.reply_text(
            f"Каталог не найден: {path}\nВведите ещё раз или /skip:")
        return ASK_DIR_MANUAL
    context.user_data["new_dir"] = path or None
    return await _after_dir(update, context)


async def add_task_skip_dir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_dir"] = None
    return await _after_dir(update, context)


def _worktree_available(context) -> bool:
    """Worktree is a real choice only for a local git repo, or for a herdr
    executor on a remote machine (the plain ssh runner never enters the
    checkout; whether the remote path is a repo is herdr's answer to give)."""
    from .worktree import is_git_repo

    ud = context.user_data
    working_dir = ud.get("new_dir")
    if ud.get("new_herdr_target") or not working_dir:
        return False
    if ud.get("new_machine"):
        provider = ud.get("new_provider") or DEFAULT_CLI
        return load_providers().get(provider, {}).get("executor") == "herdr"
    return is_git_repo(working_dir)


async def _after_dir_from_query(query, context: ContextTypes.DEFAULT_TYPE):
    return await _show_confirm(query.message.reply_text, context)


async def _after_dir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _show_confirm(update.message.reply_text, context)


# ---------------------------------------------------------------------------
# Confirm screen + extras (⚙ Дополнительно)
# ---------------------------------------------------------------------------

def _wizard_summary(context) -> str:
    ud = context.user_data
    prompt = ud.get("new_prompt", "")
    if len(prompt) > 200:
        prompt = prompt[:200] + "…"
    prov = ud.get("new_provider") or f"{DEFAULT_CLI} (по умолчанию)"
    model = f" · {ud['new_model']}" if ud.get("new_model") else ""
    lines = [
        "Проверьте задачу:",
        "",
        f"📝 {prompt}",
        "",
        f"🔌 {prov}{model} · 💻 {ud.get('new_machine') or 'локально'}",
    ]
    if ud.get("new_herdr_target"):
        lines.append(f"🖥 Сессия: {ud['new_herdr_target']}")
    else:
        lines.append(f"📂 {ud.get('new_dir') or 'директория не указана'}")
    sched = ud.get("new_schedule")
    when = _local_str(sched) if sched else "сейчас"
    rec = f" · повтор {ud['new_recurrence']}" if ud.get("new_recurrence") else ""
    lines.append(f"⏰ Запуск: {when}{rec}")
    flags = []
    if ud.get("new_priority", 5) != 5:
        flags.append(f"приоритет {ud['new_priority']}")
    if ud.get("new_skip_permissions"):
        flags.append("⚠️ без подтверждений")
    if ud.get("new_worktree"):
        flags.append("🌿 worktree")
    if ud.get("new_detached"):
        flags.append("фоновый")
    if ud.get("new_keep_pane") is False:
        flags.append("закрыть панель после")
    if flags:
        lines.append("⚙ " + ", ".join(flags))
    return "\n".join(lines)


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Запустить", callback_data="wiz_run")],
        [InlineKeyboardButton("⏰ Отложить", callback_data="wiz_sched"),
         InlineKeyboardButton("⚙ Дополнительно", callback_data="wiz_extras")],
        [InlineKeyboardButton(BACK_LABEL, callback_data="wiz_back"),
         InlineKeyboardButton("✖ Отмена", callback_data="wiz_cancel")],
    ])


async def _show_confirm(send, context):
    """Final screen: the whole task on one card, defaults applied, the rare
    options behind ⚙ — the old wizard asked priority/permissions/timing/
    recurrence/detached one by one on every single task."""
    for key, default in (("new_priority", 5), ("new_skip_permissions", False),
                         ("new_keep_pane", True), ("new_worktree", False),
                         ("new_detached", False), ("new_schedule", None),
                         ("new_recurrence", None)):
        context.user_data.setdefault(key, default)
    await send(_wizard_summary(context), reply_markup=_confirm_keyboard())
    return ASK_CONFIRM


async def cb_wiz_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass
    return await _finish_add_task_from_query(query, context)


async def cb_wiz_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """«← Назад» on the confirm screen — return to the step before it."""
    query = update.callback_query
    await query.answer()
    if context.user_data.pop("new_herdr_target", None):
        buttons = _provider_buttons(context.user_data.get("new_machine"))
        if buttons:
            await query.edit_message_text("Выберите провайдера:",
                                          reply_markup=InlineKeyboardMarkup(buttons))
            return ASK_PROVIDER
        return await _ask_machine_or_provider(query.edit_message_text, context)
    context.user_data.pop("new_dir", None)
    _clear_dir_nav(context)
    return await _ask_dir(query, context)


async def cb_wiz_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _clear_wizard(context)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass
    await query.message.reply_text("Отменено.", reply_markup=_main_menu())
    return ConversationHandler.END


_EXTRA_TOGGLES = {
    "ex_perms": "new_skip_permissions",
    "ex_wt": "new_worktree",
    "ex_det": "new_detached",
    "ex_keep": "new_keep_pane",
}

_EXTRAS_TEXT = (
    "Дополнительно — нажмите, чтобы изменить:\n"
    "• Права «не спрашивать» = --dangerously-skip-permissions: агент действует без подтверждений\n"
    "• Worktree: отдельный git-чекаут на ветке pp/t<id>, рабочее дерево не трогается\n"
    "• Фоновый запуск: для серверов/ботов — задача завершается сразу после старта"
)


def _extras_keyboard(context) -> InlineKeyboardMarkup:
    ud = context.user_data
    provider = ud.get("new_provider") or DEFAULT_CLI
    rows = [
        [InlineKeyboardButton(f"Приоритет: {ud.get('new_priority', 5)}", callback_data="ex_pri")],
        [InlineKeyboardButton(
            "⚠️ Права: не спрашивать" if ud.get("new_skip_permissions") else "Права: спрашивать",
            callback_data="ex_perms")],
        [InlineKeyboardButton(f"Повтор: {ud.get('new_recurrence') or 'нет'}", callback_data="ex_rec")],
        [InlineKeyboardButton(
            "Запуск: фоновый (не ждать)" if ud.get("new_detached") else "Запуск: ждать результата",
            callback_data="ex_det")],
    ]
    if _worktree_available(context):
        rows.append([InlineKeyboardButton(
            "🌿 Worktree: да" if ud.get("new_worktree") else "🌿 Worktree: нет",
            callback_data="ex_wt")])
    if not ud.get("new_herdr_target") and \
            load_providers().get(provider, {}).get("executor") == "herdr":
        rows.append([InlineKeyboardButton(
            "Панель: оставить открытой" if ud.get("new_keep_pane", True) else "Панель: закрыть после",
            callback_data="ex_keep")])
    rows.append([InlineKeyboardButton("✅ Готово", callback_data="ex_done")])
    return InlineKeyboardMarkup(rows)


async def _show_extras(send, context):
    await send(_EXTRAS_TEXT, reply_markup=_extras_keyboard(context))
    return ASK_EXTRAS


async def cb_extras_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await _show_extras(query.edit_message_text, context)


async def cb_extras_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = _EXTRA_TOGGLES[query.data]
    default = key == "new_keep_pane"
    context.user_data[key] = not context.user_data.get(key, default)
    await query.edit_message_reply_markup(reply_markup=_extras_keyboard(context))
    return ASK_EXTRAS


async def cb_extras_priority(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Приоритет задачи (1 — вперёд очереди, 10 — в конец):",
        reply_markup=_priority_keyboard())
    return ASK_PRIORITY


async def cb_extras_recurrence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Повторять задачу?", reply_markup=_recurrence_keyboard())
    return ASK_RECURRENCE


async def cb_extras_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await _show_confirm(query.edit_message_text, context)


# ---------------------------------------------------------------------------
# Schedule + recurrence steps (reached from the confirm screen)
# ---------------------------------------------------------------------------

def _schedule_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶ Сейчас", callback_data="sched:now"),
            InlineKeyboardButton("+1ч",      callback_data="sched:+1h"),
            InlineKeyboardButton("+3ч",      callback_data="sched:+3h"),
        ],
        [
            InlineKeyboardButton("+8ч",      callback_data="sched:+8h"),
            InlineKeyboardButton("+24ч",     callback_data="sched:+24h"),
        ],
        [InlineKeyboardButton(BACK_LABEL, callback_data="sched_back")],
    ])


async def cb_wiz_sched(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Когда запустить?\n"
        "Выберите или введите время вручную "
        "(формат: `2026-03-27T03:00`, время сервера)",
        reply_markup=_schedule_keyboard(),
        parse_mode="Markdown",
    )
    return ASK_SCHEDULE


async def cb_sched_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await _show_confirm(query.edit_message_text, context)


async def add_task_got_schedule_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from datetime import datetime, timedelta, timezone
    query = update.callback_query
    await query.answer()

    value = query.data.split(":", 1)[1]
    offsets = {"+1h": 1, "+3h": 3, "+8h": 8, "+24h": 24}
    if value in offsets:
        context.user_data["new_schedule"] = \
            datetime.now(timezone.utc) + timedelta(hours=offsets[value])
    else:
        context.user_data["new_schedule"] = None
    return await _show_confirm(query.edit_message_text, context)


async def add_task_got_schedule_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from datetime import datetime
    text = update.message.text.strip()
    try:
        scheduled_at = datetime.fromisoformat(text)
    except ValueError:
        await update.message.reply_text(
            "Не удалось распознать время. Используй формат `2026-03-27T03:00` или выбери кнопку.",
            parse_mode="Markdown",
        )
        return ASK_SCHEDULE

    if scheduled_at.tzinfo is None:
        # Typed times are read as server-local (matching the DB convention) —
        # pin the zone now so the confirm card shows the same instant.
        scheduled_at = scheduled_at.astimezone()
    context.user_data["new_schedule"] = scheduled_at
    return await _show_confirm(update.message.reply_text, context)


def _recurrence_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Каждый час", callback_data="rec:1h"),
            InlineKeyboardButton("Каждые 6ч", callback_data="rec:6h"),
        ],
        [
            InlineKeyboardButton("Ежедневно в 09:00", callback_data="rec:daily@09:00"),
            InlineKeyboardButton("Не повторять", callback_data="rec:"),
        ],
        [InlineKeyboardButton("✏️ Свой формат", callback_data="rec_manual")],
    ])


async def cb_recurrence_preset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["new_recurrence"] = query.data.split(":", 1)[1] or None
    return await _show_extras(query.edit_message_text, context)


async def cb_recurrence_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Введите формат повторения: `1h`, `6h`, `24h` или `daily@09:00`",
        parse_mode="Markdown",
    )
    return ASK_RECURRENCE


async def add_task_got_recurrence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if db.parse_recurrence(text) is None:
        await update.message.reply_text(
            "Не понял формат. Примеры: `1h`, `6h`, `daily@09:00`.",
            parse_mode="Markdown",
        )
        return ASK_RECURRENCE
    context.user_data["new_recurrence"] = text
    return await _show_extras(update.message.reply_text, context)


async def _finish_add_task_from_query(query, context):
    """Called after inline button — uses query.message for reply."""
    from .models import TaskCreate
    _save_last_settings(query.message.chat_id, context)
    prompt = context.user_data.pop("new_prompt", "")
    provider = context.user_data.pop("new_provider", None)
    priority = context.user_data.pop("new_priority", 5)
    working_dir = context.user_data.pop("new_dir", None)
    scheduled_at = context.user_data.pop("new_schedule", None)
    skip_permissions = context.user_data.pop("new_skip_permissions", False)
    model = context.user_data.pop("new_model", None)
    recurrence = context.user_data.pop("new_recurrence", None)
    detached = context.user_data.pop("new_detached", False)
    keep_pane = context.user_data.pop("new_keep_pane", True)
    herdr_target = context.user_data.pop("new_herdr_target", None)
    machine = context.user_data.pop("new_machine", None)
    use_worktree = context.user_data.pop("new_worktree", False)

    task = db.create_task(TaskCreate(
        prompt=prompt,
        working_dir=working_dir,
        provider=provider,
        priority=priority,
        scheduled_at=scheduled_at,
        skip_permissions=skip_permissions,
        model=model,
        tg_chat_id=query.message.chat_id,
        recurrence=recurrence,
        detached=detached,
        keep_pane=keep_pane,
        herdr_target=herdr_target,
        machine=machine,
        worktree=use_worktree,
    ))

    sched_str = _local_str(scheduled_at) if scheduled_at else "сейчас"
    skip_str = " ⚠️ без подтверждений" if skip_permissions else ""
    detached_str = " 🔁 фоновый" if detached else ""
    wt_str = f"\nWorktree: ветка {wt_branch(task.id)}" if use_worktree else ""
    await query.message.reply_text(
        f"✅ Задача #{task.id} добавлена!\n"
        f"Провайдер: {provider or f'{DEFAULT_CLI} (по умолчанию)'}\n"
        f"Директория: {working_dir or 'не указана'}{wt_str}\n"
        f"Запуск: {sched_str}{skip_str}{detached_str}",
        reply_markup=_main_menu(),
    )
    return ConversationHandler.END


async def add_task_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_wizard(context)
    await update.message.reply_text("Отменено.", reply_markup=_main_menu())
    return ConversationHandler.END


async def wizard_unexpected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A message the current step can't use — hint instead of dead silence.
    Returning None keeps the conversation in its current state."""
    if update.message:
        await update.message.reply_text(
            "Мастер ждёт ответа на вопрос выше (кнопка или текст). /cancel — отменить.")


async def wizard_unexpected_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A button from another step (or a stale message) pressed mid-wizard."""
    try:
        await update.callback_query.answer(
            "Эта кнопка не относится к текущему шагу мастера.")
    except BadRequest:
        pass


async def wizard_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """An abandoned wizard must not swallow a message sent a day later as if
    it were a prompt/path/interval."""
    _clear_wizard(context)
    msg = update.effective_message
    if msg:
        await msg.reply_text("⌛ Мастер создания задачи отменён по неактивности.",
                             reply_markup=_main_menu())
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Reply to task (continue session)
# ---------------------------------------------------------------------------

@require_auth
async def cb_reply_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    task_id = int(query.data.split(":")[1])
    task = db.get_task(task_id)
    if not task or not task.session_id:
        await query.answer("Сессия не найдена.", show_alert=True)
        return ConversationHandler.END
    await query.answer()

    context.user_data["reply_task_id"] = task_id
    context.user_data["reply_session_id"] = task.session_id
    context.user_data["reply_provider"] = task.provider
    # Continue where the parent worked: for a worktree task that is its
    # checkout, not the repository it forked from.
    context.user_data["reply_dir"] = task.worktree_path or task.working_dir
    context.user_data["reply_skip_permissions"] = task.skip_permissions

    # Expired password TTL: ask here, then continue with the reply — an alert
    # sending the user through the add-task wizard was six taps for nothing.
    if not _pw_ok(query.from_user.id):
        await query.message.reply_text(
            "Введите пароль:\n(/cancel — отменить)",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ASK_PASSWORD

    await query.message.reply_text(
        f"Продолжение задачи \\#{task_id}\\.\nВведите ваш ответ:\n\\(/cancel — отменить\\)",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="MarkdownV2",
    )
    return ASK_REPLY


async def reply_got_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    entered = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    import hmac
    if not hmac.compare_digest(entered, TASK_PASSWORD):
        attempts = context.user_data.get("pw_attempts", 0) + 1
        context.user_data["pw_attempts"] = attempts
        if attempts >= _PW_ATTEMPTS:
            context.user_data.pop("pw_attempts", None)
            return await reply_cancel(update, context)
        await update.message.reply_text(
            f"Неверный пароль, попробуйте ещё раз ({attempts}/{_PW_ATTEMPTS}):\n(/cancel — отменить)"
        )
        return ASK_PASSWORD

    context.user_data.pop("pw_attempts", None)
    _pw_grant(update.effective_user.id)
    task_id = context.user_data.get("reply_task_id")
    await update.message.reply_text(
        f"Продолжение задачи #{task_id}.\nВведите ваш ответ:\n(/cancel — отменить)"
    )
    return ASK_REPLY


async def reply_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = update.message.text
    parent_id = context.user_data.pop("reply_task_id", None)
    session_id = context.user_data.pop("reply_session_id", None)
    provider = context.user_data.pop("reply_provider", None)
    working_dir = context.user_data.pop("reply_dir", None)
    skip_permissions = context.user_data.pop("reply_skip_permissions", False)

    task = db.create_task(TaskCreate(
        prompt=prompt,
        working_dir=working_dir,
        provider=provider,
        priority=5,
        session_id=session_id,
        parent_task_id=parent_id,
        skip_permissions=skip_permissions,
        tg_chat_id=update.effective_chat.id,
    ))

    await update.message.reply_text(
        f"✅ Задача #{task.id} добавлена (продолжение #{parent_id})!",
        reply_markup=_main_menu(),
    )
    return ConversationHandler.END


async def reply_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in ("reply_task_id", "reply_session_id", "reply_provider", "reply_dir",
                "reply_skip_permissions", "pw_attempts"):
        context.user_data.pop(key, None)
    await update.message.reply_text("Отменено.", reply_markup=_main_menu())
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Skills (/skills command + skill task creation)
# ---------------------------------------------------------------------------

def _best_claude_provider() -> Optional[str]:
    """Return the best available Claude provider (prefers DEFAULT_CLI if it supports skills)."""
    providers = pickable_providers()
    claude_providers = [name for name, info in providers.items() if info.get("supports_skills", False)]
    if not claude_providers:
        return None
    return DEFAULT_CLI if DEFAULT_CLI in claude_providers else claude_providers[0]


def _priority_keyboard():
    # 1 is the top of the queue, 10 the bottom — the digits alone read the
    # opposite way, so every button says what it means.
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔥 Срочно (1)", callback_data="pri:1"),
            InlineKeyboardButton("Выше обычного (3)", callback_data="pri:3"),
        ],
        [
            InlineKeyboardButton("Обычный (5)", callback_data="pri:5"),
            InlineKeyboardButton("Ниже (7)", callback_data="pri:7"),
        ],
        [
            InlineKeyboardButton("🐢 Фоновый (10)", callback_data="pri:10"),
            InlineKeyboardButton(BACK_LABEL, callback_data="pri_back"),
        ],
    ])


def _build_skills_message(skills: list, title: str, show_proj_btn: bool = False):
    """Return (text, InlineKeyboardMarkup) for a skills list."""
    lines = [f"*{_esc(title)}*\n"]
    for s in skills:
        local_mark = " 📁" if s.get("source") == "local" else ""
        hint = f" `[{_esc_code(s['argument_hint'])}]`" if s.get("argument_hint") else ""
        desc = f" — {_esc(s['description'])}" if s.get("description") else ""
        lines.append(f"`/{_esc_code(s['name'])}`{local_mark}{hint}{desc}")

    buttons = []
    row = []
    for s in skills:
        label = ("📁 " if s.get("source") == "local" else "") + f"/{s['name']}"
        row.append(InlineKeyboardButton(label, callback_data=f"skill_pick:{s['name']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    if show_proj_btn and _list_projects_with_skills():
        buttons.append([InlineKeyboardButton("📁 Скилы проекта...", callback_data="skills_proj_picker")])

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await _deny(update)
        return

    if not _best_claude_provider():
        await update.message.reply_text(
            "Нет Claude-провайдера с поддержкой скилов.",
            reply_markup=_main_menu(),
        )
        return

    skills = get_skills()
    if not skills:
        await update.message.reply_text(
            "Скилы не найдены\\. Добавьте команды в `~/\\.claude/commands/` "
            "или установите плагины через Claude Code\\.",
            parse_mode="MarkdownV2",
            reply_markup=_main_menu(),
        )
        return

    text, keyboard = _build_skills_message(skills, "Доступные скилы:", show_proj_btn=True)
    await update.message.reply_text(text, parse_mode="MarkdownV2", reply_markup=keyboard)


@require_auth
async def cb_skills_proj_picker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show project selector so user can load project-local skills."""
    query = update.callback_query
    await query.answer()

    projects = _list_projects_with_skills()
    if not projects:
        await query.edit_message_text(
            "Нет проектов с локальными скилами\\.\n"
            "Добавьте `.md` файлы в `<project>/.claude/commands/` или `<project>/.claude/skills/`",
            parse_mode="MarkdownV2",
        )
        return

    buttons = []
    row = []
    for proj in projects:
        full = os.path.join(PROJECTS_ROOT, proj)
        if _list_subdirs(full):
            row.append(InlineKeyboardButton(f"{proj} 📁", callback_data=f"skills_dir_open:{proj}"))
        else:
            row.append(InlineKeyboardButton(proj, callback_data=f"skills_dir:{proj}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(BACK_LABEL, callback_data="skills_back")])

    await query.edit_message_text(
        "Выберите проект для загрузки его скилов:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@require_auth
async def cb_skills_dir_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Expand a folder in skills project picker showing its subdirectories."""
    query = update.callback_query
    await query.answer()
    proj_name = query.data.split(":", 1)[1]
    full = os.path.join(PROJECTS_ROOT, proj_name)

    buttons = [[InlineKeyboardButton(f"📁 {proj_name} (эта папка)", callback_data=f"skills_dir:{proj_name}")]]
    row = []
    for sub in _list_subdirs(full):
        row.append(InlineKeyboardButton(sub, callback_data=f"skills_dir_sub:{proj_name}/{sub}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(BACK_LABEL, callback_data="skills_proj_picker")])

    await query.edit_message_text(
        f"📂 {proj_name}:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@require_auth
async def cb_skills_dir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Load and show global + project-local skills for the selected project."""
    query = update.callback_query
    await query.answer()

    data = query.data

    # Subdirectory selected
    if data.startswith("skills_dir_sub:"):
        rel = data.split(":", 1)[1]
        workdir = os.path.join(PROJECTS_ROOT, rel.replace("/", os.sep))
    else:
        proj_name = data.split(":", 1)[1]
        workdir = os.path.join(PROJECTS_ROOT, proj_name)

    context.user_data["skills_workdir"] = workdir
    short = os.path.relpath(workdir, PROJECTS_ROOT)

    skills = get_skills(working_dir=workdir)
    if not skills:
        await query.edit_message_text(
            f"Скилы не найдены ни глобально, ни в `{_esc_code(short)}`\\.",
            parse_mode="MarkdownV2",
        )
        return

    text, keyboard = _build_skills_message(
        skills, f"Скилы ({short}):", show_proj_btn=False
    )
    rows = list(keyboard.inline_keyboard)
    rows.append([InlineKeyboardButton(BACK_LABEL, callback_data="skills_proj_picker")])
    await query.edit_message_text(
        text, parse_mode="MarkdownV2", reply_markup=InlineKeyboardMarkup(rows)
    )


@require_auth
async def cb_skills_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Return to global skills list."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("skills_workdir", None)

    skills = get_skills()
    if not skills:
        await query.edit_message_text("Скилы не найдены.")
        return

    text, keyboard = _build_skills_message(skills, "Доступные скилы:", show_proj_btn=True)
    await query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=keyboard)


async def _ask_skill_args(send, context: ContextTypes.DEFAULT_TYPE):
    """Show the skill-arguments prompt via `send` (edit_message_text or reply_text)."""
    skill_name = context.user_data.get("new_skill_name", "")
    workdir = context.user_data.get("new_dir")
    all_skills = get_skills(working_dir=workdir) if workdir else get_skills()
    skill = next((s for s in all_skills if s["name"] == skill_name), None)
    arg_hint = skill.get("argument_hint", "") if skill else ""

    hint_line = f"\nАргументы: _{_esc(arg_hint)}_" if arg_hint else ""
    await send(
        f"Скил: `/{_esc_code(skill_name)}`{hint_line}\n\n"
        f"Введите текст для скила или /skip:",
        parse_mode="MarkdownV2",
    )
    return ASK_SKILL_ARGS


@require_auth
async def skill_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for skill conversation — triggered when user taps a skill button."""
    query = update.callback_query
    await query.answer()

    _clear_wizard(context)
    skill_name = query.data.split(":", 1)[1]
    if not _best_claude_provider():
        await query.edit_message_text("Нет Claude-провайдера для выполнения скилов.")
        await query.message.reply_text("Выберите действие:", reply_markup=_main_menu())
        return ConversationHandler.END

    context.user_data["new_skill_name"] = skill_name

    # If user browsed to a project's skills, pre-fill working directory and skip dir step
    workdir = context.user_data.pop("skills_workdir", None)
    if workdir:
        context.user_data["new_dir"] = workdir

    # Expired password TTL: ask for it right here and continue the skill flow
    # afterwards — no detour through «➕ Добавить задачу».
    if not _pw_ok(query.from_user.id):
        await query.message.reply_text(
            "Введите пароль:\n(/cancel — отменить)",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ASK_PASSWORD

    return await _ask_skill_args(query.edit_message_text, context)


def _skill_provider_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard with only skill-capable (Claude) providers."""
    providers = pickable_providers()
    skill_providers = {n: i for n, i in providers.items() if i.get("supports_skills", False)}
    row, buttons = [], []
    for name in skill_providers:
        row.append(InlineKeyboardButton(name, callback_data=f"pickprov:{name}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


async def skill_got_args(update: Update, context: ContextTypes.DEFAULT_TYPE):
    skill_name = context.user_data.get("new_skill_name", "")
    args = update.message.text.strip()
    context.user_data["new_prompt"] = f"/{skill_name} {args}" if args else f"/{skill_name}"
    await update.message.reply_text("Выберите провайдера:", reply_markup=_skill_provider_keyboard())
    return ASK_PROVIDER


async def skill_skip_args(update: Update, context: ContextTypes.DEFAULT_TYPE):
    skill_name = context.user_data.get("new_skill_name", "")
    context.user_data["new_prompt"] = f"/{skill_name}"
    await update.message.reply_text("Выберите провайдера:", reply_markup=_skill_provider_keyboard())
    return ASK_PROVIDER


# ---------------------------------------------------------------------------
# herdr → Telegram bridge
# ---------------------------------------------------------------------------
# The bot polls `herdr agent list` and notifies authorized users when ANY
# herdr agent (not only PromptPilot tasks) gets blocked on a dialog or
# finishes unseen (herdr's `done` = idle after unseen background work).
# The poll interval doubles as a debounce: dialogs the user resolves within
# seconds while working in herdr never surface here.

def _machine_remote(machine: str):
    """Remote() of a registered machine ('' / unknown → local)."""
    from .config import machine_remote
    m = load_machines().get(machine or "")
    return machine_remote(m) if m and m.get("host") else None


async def _herdr_cli(*args, host=None, timeout=15):
    """Run a herdr CLI command (locally or on `host`). Raw stdout or None."""
    import asyncio
    from .herdr_exec import herdr_argv
    argv = herdr_argv(args, host)
    if host:
        timeout = max(timeout, 30)  # ssh round-trip on top of the herdr call
    try:
        proc = await asyncio.create_subprocess_exec(
            argv[0], *argv[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (FileNotFoundError, OSError, asyncio.TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    return (out or err or b"").decode("utf-8", "replace")


async def _herdr_json(*args, host=None, timeout=15):
    raw = await _herdr_cli(*args, host=host, timeout=timeout)
    if raw is None:
        return None
    try:
        return json.loads(raw.strip())
    except ValueError:
        return None


def _herdr_agent_label(a: dict) -> str:
    kind = a.get("display_agent") or a.get("agent") or "агент"
    name = a.get("name")
    return f"{kind} ({name})" if name else kind


def _herdr_blocked_keyboard(ref: str) -> list:
    """Buttons for a blocked herdr dialog. Claude Code permission dialogs are
    multi-choice — Enter alone only accepts the default, so 2/3/Esc are a tap
    away too (hd_key:<key>:<machine>:<pane>; the key goes first because pane
    ids contain ':')."""
    return [
        [InlineKeyboardButton("✅ Подтвердить (Enter)", callback_data=f"hd_enter:{ref}")],
        [InlineKeyboardButton("2", callback_data=f"hd_key:2:{ref}"),
         InlineKeyboardButton("3", callback_data=f"hd_key:3:{ref}"),
         InlineKeyboardButton("✖ Esc", callback_data=f"hd_key:esc:{ref}"),
         InlineKeyboardButton("📺", callback_data=f"hd_screen:{ref}")],
        [InlineKeyboardButton(REPLY_LABEL, callback_data=f"hd_reply:{ref}")],
    ]


async def _herdr_screen_tail(pane_id: str, machine: str = "", lines_n: int = 10) -> str:
    """Last lines of the pane — so a blocked notification SHOWS the question
    instead of making the user tap 📺 first."""
    raw = await _herdr_cli("agent", "read", pane_id, "--source", "visible",
                           "--format", "text", host=_machine_remote(machine))
    if not raw:
        return ""
    lines = [l.rstrip() for l in raw.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines[-lines_n:])[-1000:]


async def _herdr_notify(bot, pane_id: str, status: str, agent: dict, machine: str = ""):
    label = _herdr_agent_label(agent)
    # callback_data: hd_<action>:<machine>:<pane> — machine names carry no ':',
    # pane ids do ("w9:p2"), so the split is bounded to 2.
    ref = f"{machine}:{pane_id}"
    where = f" на {machine}" if machine else ""
    if status == "blocked":
        text = f"⏸ herdr{where}: {label} в панели {pane_id} ждёт подтверждения"
        buttons = _herdr_blocked_keyboard(ref)
        tail = await _herdr_screen_tail(pane_id, machine)
        if tail:
            text += f"\n\n{tail}"
    else:
        text = f"✅ herdr{where}: {label} в панели {pane_id} завершил работу"
        buttons = [
            [InlineKeyboardButton("📺 Экран", callback_data=f"hd_screen:{ref}"),
             InlineKeyboardButton(REPLY_LABEL, callback_data=f"hd_reply:{ref}")],
        ]
    title = (agent.get("terminal_title_stripped") or "").strip()
    if title and status != "blocked":
        text += f"\n{title[:120]}"
    for chat_id in list_authorized():
        try:
            await bot.send_message(chat_id=int(chat_id), text=_clip(text),
                                   reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as e:
            logger.warning("herdr notify to %s failed: %s", chat_id, e)


def _herdr_watch_targets():
    """Machines to poll: this one plus every registered machine with herdr.
    Returns [(machine_name, remote)] — machine_name '' means local."""
    from .config import machine_has_herdr, machine_remote
    targets = [("", None)]
    for name, m in load_machines().items():
        if m.get("host") and machine_has_herdr(m):
            targets.append((name, machine_remote(m)))
    return targets


async def _herdr_watch_loop(bot):
    import asyncio
    notified = {}  # (machine, pane_id) -> status already notified about
    while True:
        await asyncio.sleep(HERDR_WATCH_INTERVAL)
        try:
            await _herdr_watch_tick(bot, notified)
        except Exception as e:  # one bad poll must not stop the whole watcher
            logger.warning("herdr watch tick упал: %s", e)


async def _herdr_watch_tick(bot, notified):
        seen = set()
        for machine, host in _herdr_watch_targets():
            data = await _herdr_json("agent", "list", host=host)
            if data is None:
                continue  # herdr not installed / server down / ssh — stay quiet
            agents = ((data.get("result") or {}).get("agents")) or []
            current = {}
            for a in agents:
                pane = a.get("pane_id")
                name = a.get("name") or ""
                if not pane or name.startswith("pp-t"):
                    continue  # PromptPilot's own tasks notify via the worker
                current[pane] = a
            seen.update((machine, pane) for pane in current)
            for pane, a in current.items():
                key = (machine, pane)
                status = a.get("agent_status")
                if status in ("blocked", "done"):
                    if notified.get(key) != status:
                        await _herdr_notify(bot, pane, status, a, machine)
                        notified[key] = status
                else:
                    notified.pop(key, None)
            # A machine that failed to answer keeps its remembered state: only
            # panes on machines we DID reach may be forgotten.
            for key in [k for k in notified if k[0] == machine and k not in seen]:
                notified.pop(key)


def _parse_hd_ref(data: str):
    """'hd_enter:<machine>:<pane>' → (machine, pane, host). Older buttons
    without the machine part ('hd_enter:w9:p2') are read as local."""
    parts = data.split(":", 2)
    if len(parts) == 3 and (parts[1] == "" or parts[1] in load_machines()):
        machine, pane = parts[1], parts[2]
    else:
        machine, pane = "", data.split(":", 1)[1]
    return machine, pane, _machine_remote(machine)


async def cb_herdr_enter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_authorized(query.from_user.id):
        await query.answer("Нет доступа.", show_alert=True)
        return
    _, pane, host = _parse_hd_ref(query.data)
    res = await _herdr_json("agent", "send-keys", pane, "enter", host=host)
    if res and res.get("result"):
        await query.answer("Enter отправлен ✓")
    else:
        await query.answer("Не удалось — агент ещё существует?", show_alert=True)


async def cb_herdr_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """hd_key:<key>:<machine>:<pane> — send a named key (2/3/esc/…) into the
    blocked dialog; Enter has its own button/handler."""
    query = update.callback_query
    if not is_authorized(query.from_user.id):
        await query.answer("Нет доступа.", show_alert=True)
        return
    _, key, machine, pane = query.data.split(":", 3)
    host = _machine_remote(machine)
    res = await _herdr_json("agent", "send-keys", pane, key, host=host)
    if res and res.get("result"):
        await query.answer(f"Отправлено: {key} ✓")
    else:
        await query.answer("Не удалось — агент ещё существует?", show_alert=True)


async def cb_herdr_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_authorized(query.from_user.id):
        await query.answer("Нет доступа.", show_alert=True)
        return
    machine, pane, host = _parse_hd_ref(query.data)
    raw = await _herdr_cli("agent", "read", pane, "--source", "visible", "--format", "text",
                           host=host)
    if raw is None:
        await query.answer("Не удалось прочитать экран.", show_alert=True)
        return
    await query.answer()
    lines = [l.rstrip() for l in raw.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    tail = "\n".join(lines[-25:])[-3500:] or "(пусто)"
    await query.message.reply_text(f"📺 {pane}{f' ({machine})' if machine else ''}:\n{tail}")


async def cb_herdr_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_authorized(query.from_user.id):
        await query.answer("Нет доступа.", show_alert=True)
        return ConversationHandler.END
    machine, pane, host = _parse_hd_ref(query.data)
    context.user_data["herdr_reply_pane"] = pane
    context.user_data["herdr_reply_host"] = host
    await query.answer()
    where = f" на {machine}" if machine else ""
    await query.message.reply_text(f"Текст для агента в панели {pane}{where} (или /cancel):")
    return ASK_HERDR_REPLY


async def herdr_reply_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pane = context.user_data.pop("herdr_reply_pane", None)
    host = context.user_data.pop("herdr_reply_host", None)
    if not pane:
        return ConversationHandler.END
    res = await _herdr_json("agent", "prompt", pane, update.message.text, host=host)
    if res and res.get("result"):
        await update.message.reply_text("Отправлено ✓")
    else:
        await update.message.reply_text("Не удалось отправить — агент ещё существует?")
    return ConversationHandler.END


async def herdr_reply_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("herdr_reply_pane", None)
    context.user_data.pop("herdr_reply_host", None)
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _notify_loop(bot):
    """Background loop: send notifications for completed/failed tasks every 10s.

    The DB reads sit inside try/except: this bot shares SQLite with a live
    worker, and one transient 'database is locked' used to kill this coroutine
    silently — notifications then just stopped until a bot restart. Permanently
    undeliverable messages (user blocked the bot, chat gone) are marked done so
    they don't retry every 10s forever.
    """
    import asyncio
    from telegram.error import BadRequest, Forbidden
    while True:
        await asyncio.sleep(10)
        # Free-form queued notifications (e.g. herdr agent blocked mid-task)
        try:
            notes = db.get_unsent_notifications()
        except Exception as e:
            logger.warning("notify: чтение очереди уведомлений упало: %s", e)
            continue
        for note in notes:
            try:
                kb = None
                if note.get("pane_id"):
                    # A blocked own task: same confirm/screen/reply buttons the
                    # foreign-agent watcher gets — not just an ssh suggestion.
                    ref = f"{note.get('machine') or ''}:{note['pane_id']}"
                    kb = InlineKeyboardMarkup(_herdr_blocked_keyboard(ref))
                await bot.send_message(chat_id=note["tg_chat_id"], text=_clip(note["message"]),
                                       reply_markup=kb)
                db.mark_notification_sent(note["id"])
            except (Forbidden, BadRequest) as e:
                logger.warning("notify %s: постоянная ошибка (%s) — помечаю отправленным", note["id"], e)
                try:
                    db.mark_notification_sent(note["id"])
                except Exception:
                    pass
            except Exception as e:
                logger.warning("Failed to send notification %s: %s", note["id"], e)
        try:
            pending = db.get_pending_notifications()
        except Exception as e:
            logger.warning("notify: чтение задач для уведомления упало: %s", e)
            continue
        for task in pending:
            try:
                if task.status.value == "completed":
                    icon, status_word = "✅", "выполнена"
                    body = ""
                    if task.result:
                        preview = task.result.split("\n--- Meta ---")[0].strip()
                        if preview:
                            body = f"\n\n{preview[:600]}"
                        meta_lines = []
                        if "--- Meta ---" in task.result:
                            for line in task.result[task.result.find("--- Meta ---"):].splitlines():
                                line = line.strip()
                                if line.startswith(("Model:", "Cost:", "Time:")):
                                    meta_lines.append(line)
                        if meta_lines:
                            body += "\n" + " · ".join(meta_lines)
                else:
                    icon, status_word = "❌", "завершилась с ошибкой"
                    body = f"\n\n{task.error[:300]}" if task.error else ""

                proj = _project_name(task.working_dir)
                proj_str = f" [{proj}]" if proj else ""
                text = _clip(f"{icon} Задача #{task.id}{proj_str} {status_word}{body}")
                # The completion notification is the entry point to the most
                # common flow (read result → reply) — give it buttons instead
                # of sending the user back through the menu and the task list.
                note_rows = [[InlineKeyboardButton("🔎 Открыть", callback_data=f"task:{task.id}")]]
                if task.session_id:
                    note_rows[0].append(InlineKeyboardButton(REPLY_LABEL, callback_data=f"reply_task:{task.id}"))
                note_rows.append([InlineKeyboardButton("🔁 Повторить", callback_data=f"rerun:{task.id}")])
                note_kb = InlineKeyboardMarkup(note_rows)
                # Agents answer in Markdown; raw «**жирный**» in the preview
                # reads as noise. Try to render it, fall back to plain text on
                # any parse error (unclosed entity etc.).
                try:
                    await bot.send_message(chat_id=task.tg_chat_id, text=text,
                                           reply_markup=note_kb, parse_mode="Markdown")
                except BadRequest:
                    await bot.send_message(chat_id=task.tg_chat_id, text=text,
                                           reply_markup=note_kb)
                db.mark_notified(task.id)
            except (Forbidden, BadRequest) as e:
                logger.warning("notify task %s: постоянная ошибка (%s) — помечаю уведомлённым", task.id, e)
                try:
                    db.mark_notified(task.id)
                except Exception:
                    pass
            except Exception as e:
                logger.warning("Failed to notify task %s: %s", task.id, e)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await _deny(update)
        return
    await update.message.reply_text(
        "PromptPilot — очередь задач для AI CLI (Claude Code, opencode и др.).\n\n"
        "Как это работает: вы описываете задачу, воркер запускает агента, по "
        "завершении приходит уведомление с кнопками (открыть / ответить / повторить).\n\n"
        "Меню:\n"
        "📋 Задачи — список с фильтрами; в карточке: отмена, сброс, повтор, полный вывод\n"
        "➕ Добавить задачу — мастер: промпт → где выполнить → папка → 🚀 Запустить\n"
        "   (приоритет, права, worktree, повтор, отложенный запуск — «⚙ Дополнительно»)\n"
        "📊 Статистика — сводка по статусам и расходы\n"
        "🔌 Провайдеры — список и настройки\n"
        "⚡ Скилы — запустить /skill Claude Code\n"
        "⏸ Пауза — приостановить воркер без потери задач\n\n"
        "Команды: /tasks, /add, /stats, /providers, /pause, /skills, /help\n"
        "/cancel — прервать текущий мастер или диалог\n\n"
        "Совет: задайте PP_PROJECTS_ROOT в .env — рабочую папку можно будет "
        "выбирать кнопками, а не печатать путь.",
        reply_markup=_main_menu(),
    )


def run_bot():
    token = os.environ.get("PP_TG_TOKEN")
    if not token:
        raise RuntimeError("PP_TG_TOKEN environment variable is not set")

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )

    async def post_init(application):
        import asyncio
        try:
            await application.bot.set_my_commands([
                BotCommand("tasks", "Список задач"),
                BotCommand("add", "Новая задача"),
                BotCommand("stats", "Статистика"),
                BotCommand("providers", "Провайдеры"),
                BotCommand("pause", "Пауза / продолжить воркер"),
                BotCommand("skills", "Скилы Claude Code"),
                BotCommand("help", "Помощь"),
                BotCommand("cancel", "Отменить текущий диалог"),
                BotCommand("start", "Меню / авторизация"),
            ])
        except Exception as e:
            logger.warning("set_my_commands failed: %s", e)
        asyncio.create_task(_notify_loop(application.bot))
        if HERDR_WATCH:
            asyncio.create_task(_herdr_watch_loop(application.bot))

    # The wizard survives a bot restart: without persistence every inline
    # button of a half-finished wizard died silently after redeploy.
    from telegram.ext import PicklePersistence
    from .config import DB_DIR
    persistence = PicklePersistence(filepath=str(DB_DIR / "bot_state.pickle"))

    builder = Application.builder().token(token).post_init(post_init).persistence(persistence)
    proxy = get_proxy_url()
    if proxy:
        # python-telegram-bot 21.x dropped the .proxy_url() builder helper; pass
        # an HTTPXRequest with a proxy for BOTH the bot channel and long-polling.
        from telegram.request import HTTPXRequest
        builder = builder.request(HTTPXRequest(proxy=proxy))
        builder = builder.get_updates_request(HTTPXRequest(proxy=proxy))
        logger.info("Telegram через прокси: %s", mask_proxy_url(proxy))
    app = builder.build()

    add_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ Добавить задачу$"), add_task_start),
            CommandHandler("add", add_task_start),
            CallbackQueryHandler(skill_selected, pattern=r"^skill_pick:"),
        ],
        states={
            ASK_SKILL_ARGS: [
                CommandHandler("skip", skill_skip_args),
                MessageHandler(filters.TEXT & ~filters.COMMAND, skill_got_args),
            ],
            ASK_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_task_got_password)
            ],
            ASK_PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_task_got_prompt)
            ],
            ASK_LAST: [
                CallbackQueryHandler(cb_last_use, pattern=r"^last_use$"),
                CallbackQueryHandler(cb_last_edit, pattern=r"^last_edit$"),
            ],
            ASK_PROVIDER: [
                CallbackQueryHandler(add_task_got_provider, pattern=r"^pickprov:")
            ],
            ASK_MACHINE: [
                CallbackQueryHandler(add_task_got_machine, pattern=r"^machine:"),
            ],
            ASK_HERDR_TARGET: [
                CallbackQueryHandler(add_task_got_herdr_target,
                                     pattern=r"^(hst_i:\d+|hst_more)$"),
            ],
            ASK_MODEL: [
                CallbackQueryHandler(add_task_got_model_idx, pattern=r"^model_i:\d+$"),
                CallbackQueryHandler(cb_model_page, pattern=r"^model_pg:\d+$"),
                CallbackQueryHandler(cb_model_manual, pattern=r"^model_manual$"),
                CallbackQueryHandler(add_task_got_model, pattern=r"^model:"),
                CommandHandler("skip", add_task_skip_model),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_task_got_model_text),
            ],
            ASK_DIR: [
                CallbackQueryHandler(cb_dir_nav, pattern=r"^dirn:\d+$"),
                CallbackQueryHandler(cb_dir_pick_idx, pattern=r"^dirp:\d+$"),
                CallbackQueryHandler(cb_dir_here, pattern=r"^dir_here$"),
                CallbackQueryHandler(cb_dir_up, pattern=r"^dir_up$"),
                CallbackQueryHandler(cb_dir_page, pattern=r"^dir_pg:\d+$"),
                CallbackQueryHandler(cb_dir_hist, pattern=r"^dirh:\d+$"),
                CallbackQueryHandler(add_task_got_dir_btn, pattern=r"^dir:"),
            ],
            ASK_DIR_MANUAL: [
                CommandHandler("skip", add_task_skip_dir),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_task_got_dir),
            ],
            ASK_CONFIRM: [
                CallbackQueryHandler(cb_wiz_run, pattern=r"^wiz_run$"),
                CallbackQueryHandler(cb_wiz_sched, pattern=r"^wiz_sched$"),
                CallbackQueryHandler(cb_extras_open, pattern=r"^wiz_extras$"),
                CallbackQueryHandler(cb_wiz_back, pattern=r"^wiz_back$"),
            ],
            ASK_EXTRAS: [
                CallbackQueryHandler(cb_extras_priority, pattern=r"^ex_pri$"),
                CallbackQueryHandler(cb_extras_toggle, pattern=r"^ex_(perms|wt|det|keep)$"),
                CallbackQueryHandler(cb_extras_recurrence, pattern=r"^ex_rec$"),
                CallbackQueryHandler(cb_extras_done, pattern=r"^ex_done$"),
            ],
            ASK_PRIORITY: [
                CallbackQueryHandler(add_task_got_priority, pattern=r"^pri:"),
                CallbackQueryHandler(cb_priority_back, pattern=r"^pri_back$"),
            ],
            ASK_SCHEDULE: [
                CallbackQueryHandler(add_task_got_schedule_btn, pattern=r"^sched:"),
                CallbackQueryHandler(cb_sched_back, pattern=r"^sched_back$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_task_got_schedule_text),
            ],
            ASK_RECURRENCE: [
                CallbackQueryHandler(cb_recurrence_preset, pattern=r"^rec:"),
                CallbackQueryHandler(cb_recurrence_manual, pattern=r"^rec_manual$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_task_got_recurrence),
            ],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, wizard_timeout),
                CallbackQueryHandler(wizard_timeout),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", add_task_cancel),
            CallbackQueryHandler(cb_wiz_cancel, pattern=r"^wiz_cancel$"),
            # skills_* / hd_* are handled in group -1 — don't toast over them
            CallbackQueryHandler(wizard_unexpected_cb, pattern=r"^(?!skills_|hd_)"),
            MessageHandler(filters.ALL, wizard_unexpected),
        ],
        allow_reentry=True,
        conversation_timeout=1800,
        name="pp_add_task",
        persistent=True,
    )

    reply_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_reply_task_start, pattern=r"^reply_task:\d+$")
        ],
        states={
            ASK_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, reply_got_password)
            ],
            ASK_REPLY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, reply_got_text)
            ],
        },
        fallbacks=[CommandHandler("cancel", reply_cancel)],
        allow_reentry=True,
        conversation_timeout=1800,
        name="pp_reply_task",
        persistent=True,
    )

    herdr_reply_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_herdr_reply_start, pattern=r"^hd_reply:")
        ],
        states={
            ASK_HERDR_REPLY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, herdr_reply_got_text)
            ],
        },
        fallbacks=[CommandHandler("cancel", herdr_reply_cancel)],
        allow_reentry=True,
        conversation_timeout=1800,
        name="pp_herdr_reply",
        persistent=True,
    )

    # Group -1: skills navigation runs before ConversationHandlers (group 0).
    # ConversationHandler eats all callbacks when in an active state, so
    # skills_dir / skills_proj_picker / skills_back must be in a higher-priority group.
    app.add_handler(CallbackQueryHandler(cb_skills_proj_picker, pattern=r"^skills_proj_picker$"), group=-1)
    app.add_handler(CallbackQueryHandler(cb_skills_dir_open, pattern=r"^skills_dir_open:"), group=-1)
    app.add_handler(CallbackQueryHandler(cb_skills_dir, pattern=r"^skills_dir_sub:"), group=-1)
    app.add_handler(CallbackQueryHandler(cb_skills_dir, pattern=r"^skills_dir:"), group=-1)
    app.add_handler(CallbackQueryHandler(cb_skills_back, pattern=r"^skills_back$"), group=-1)

    # herdr bridge buttons must outrank active ConversationHandlers (group 0)
    app.add_handler(CallbackQueryHandler(cb_herdr_enter, pattern=r"^hd_enter:"), group=-1)
    app.add_handler(CallbackQueryHandler(cb_herdr_screen, pattern=r"^hd_screen:"), group=-1)
    app.add_handler(CallbackQueryHandler(cb_herdr_key, pattern=r"^hd_key:"), group=-1)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("skills", cmd_skills))
    # Command aliases for every menu button: on desktop the reply keyboard is
    # often collapsed, and the Telegram command menu is all the user sees.
    app.add_handler(CommandHandler("tasks", show_tasks))
    app.add_handler(CommandHandler("stats", show_stats))
    app.add_handler(CommandHandler("providers", show_providers))
    app.add_handler(CommandHandler("pause", toggle_pause))
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    app.add_handler(herdr_reply_conv)
    app.add_handler(reply_conv)
    app.add_handler(add_conv)
    app.add_handler(MessageHandler(filters.Regex("^📋 Задачи$"), show_tasks))
    app.add_handler(MessageHandler(filters.Regex("^📊 Статистика$"), show_stats))
    app.add_handler(MessageHandler(filters.Regex("^🔌 Провайдеры$"), show_providers))
    app.add_handler(MessageHandler(filters.Regex("^⚡ Скилы$"), cmd_skills))
    app.add_handler(MessageHandler(filters.Regex(r"^(⏸ Пауза|▶ Продолжить)$"), toggle_pause))
    app.add_handler(CallbackQueryHandler(cb_task, pattern=r"^task:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_page, pattern=r"^page:\d+(:\w+)?$"))
    app.add_handler(CallbackQueryHandler(cb_filter, pattern=r"^flt:\w+$"))
    app.add_handler(CallbackQueryHandler(cb_back_to_list, pattern=r"^tasklist$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_task, pattern=r"^cancel_task:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_reset_task, pattern=r"^reset_task:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_delete_task, pattern=r"^delete_task:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_delete_task_confirm, pattern=r"^del_yes:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_rerun_task, pattern=r"^rerun:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_full_result, pattern=r"^full_result:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_provider_detail, pattern=r"^prov_detail:"))
    app.add_handler(CallbackQueryHandler(cb_provider_list, pattern=r"^prov_list$"))

    # --- last in group 0: catch-alls, reached only when nothing above claimed
    # the update. Free text used to get dead silence (especially bad after the
    # wizard removed the reply keyboard and a restart wiped the conversation).
    async def cancel_global(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update.effective_user.id):
            await _deny(update)
            return
        await update.message.reply_text("Нет активного диалога.", reply_markup=_main_menu())

    async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update.effective_user.id):
            await _deny(update)
            return
        await update.message.reply_text(
            "Не понял. Выберите действие в меню или /help.",
            reply_markup=_main_menu(),
        )

    async def unknown_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # A button whose handler no longer exists (very old message) — answer
        # it so the user doesn't stare at an endless spinner.
        try:
            await update.callback_query.answer(
                "Кнопка устарела. Откройте меню заново — /start.", show_alert=True)
        except BadRequest:
            pass

    app.add_handler(CommandHandler("cancel", cancel_global))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text))
    app.add_handler(CallbackQueryHandler(unknown_callback))

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Unhandled exception for update %s", update, exc_info=context.error)

    app.add_error_handler(error_handler)

    logger.info("PromptPilot Telegram bot started.")
    app.run_polling(drop_pending_updates=True)
