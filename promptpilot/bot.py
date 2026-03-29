"""Telegram bot for PromptPilot.

Authorization: user sends /start → shares phone via button → phone is checked
against PP_TG_ALLOWED_PHONES env var (comma-separated) or ~/.promptpilot/tg_config.json.
After authorization all task management features are available.
"""

import logging
import os

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
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
from .config import DEFAULT_CLI, get_skills, load_providers, PROJECTS_ROOT, TASK_PASSWORD
from .models import TaskCreate, TaskStatus
from .tg_auth import authorize_user, is_authorized, load_allowed_phones

logger = logging.getLogger(__name__)

# Conversation states
ASK_PASSWORD, ASK_PROMPT, ASK_PROVIDER, ASK_PRIORITY, ASK_SKIP_PERMS, ASK_DIR, ASK_DIR_MANUAL, ASK_SCHEDULE, ASK_REPLY, ASK_SKILL_ARGS = range(10)

PAGE_SIZE = 5

STATUS_ICON = {
    "pending": "⏳",
    "running": "🔄",
    "completed": "✅",
    "failed": "❌",
    "rate_limited": "⏸",
    "cancelled": "🚫",
}


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["📋 Задачи", "➕ Добавить задачу"],
            ["📊 Статистика", "🔌 Провайдеры", "⚡ Скилы"],
        ],
        resize_keyboard=True,
    )


def _contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Поделиться контактом", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _tasks_keyboard(tasks, page: int, total: int) -> InlineKeyboardMarkup:
    keyboard = []
    for t in tasks:
        icon = STATUS_ICON.get(t.status.value, "•")
        label = t.prompt[:38].replace("\n", " ")
        keyboard.append([
            InlineKeyboardButton(
                f"{icon} #{t.id} {label}",
                callback_data=f"task:{t.id}",
            )
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Пред", callback_data=f"page:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("▶ След", callback_data=f"page:{page + 1}"))
    if nav:
        keyboard.append(nav)
    return InlineKeyboardMarkup(keyboard)


def _task_detail_keyboard(task) -> InlineKeyboardMarkup:
    rows = []
    if task.status.value in ("pending", "rate_limited"):
        rows.append([InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_task:{task.id}")])
    if task.status.value == "running":
        rows.append([InlineKeyboardButton("🔁 Сбросить (stuck)", callback_data=f"reset_task:{task.id}")])
    if task.status.value == "completed" and task.session_id:
        rows.append([InlineKeyboardButton("💬 Ответить", callback_data=f"reply_task:{task.id}")])
    rows.append([InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_task:{task.id}")])
    return InlineKeyboardMarkup(rows)


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
        await update.message.reply_text(
            "Список разрешённых номеров не настроен. "
            "Задайте PP_TG_ALLOWED_PHONES или ~/.promptpilot/tg_config.json."
        )
        return

    if phone in allowed:
        authorize_user(user_id, phone)
        await update.message.reply_text(
            "Авторизация успешна! Добро пожаловать.",
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

    page = context.user_data.get("tasks_page", 0)
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


async def cb_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    page = int(query.data.split(":")[1])
    context.user_data["tasks_page"] = page

    stats = db.get_stats()
    tasks = db.list_tasks(limit=PAGE_SIZE, offset=page * PAGE_SIZE)

    if not tasks:
        await query.edit_message_text("Задач нет.")
        return

    await query.edit_message_text(
        f"*Задачи* (стр. {page + 1}, всего {stats.total}):",
        reply_markup=_tasks_keyboard(tasks, page, stats.total),
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Task detail
# ---------------------------------------------------------------------------

async def cb_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    task_id = int(query.data.split(":")[1])
    task = db.get_task(task_id)
    if not task:
        await query.edit_message_text("Задача не найдена.")
        return

    icon = STATUS_ICON.get(task.status.value, "•")
    created = task.created_at.strftime("%d.%m.%Y %H:%M") if task.created_at else "—"
    provider_str = _esc(task.provider) if task.provider else "claude \\(по умолчанию\\)"

    text = (
        f"*Задача \\#{task.id}*\n"
        f"Статус: {icon} {_esc(task.status.value)}\n"
        f"Провайдер: {provider_str}\n"
    )
    if task.model_used:
        text += f"Модель: `{_esc(task.model_used)}`\n"
    text += (
        f"Приоритет: {task.priority}\n"
        f"Создана: {_esc(created)}\n"
        f"Retry: {task.retry_count}/{task.max_retries}"
    )
    if task.working_dir:
        text += f"\nДир: `{_esc(task.working_dir)}`"
    if task.status.value == "rate_limited" and task.next_run_at:
        reset_str = task.next_run_at.strftime("%d.%m.%Y %H:%M UTC")
        text += f"\nСброс: {_esc(reset_str)}"

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

    await query.edit_message_text(
        text,
        reply_markup=_task_detail_keyboard(task),
        parse_mode="MarkdownV2",
    )


def _esc(text: str) -> str:
    """Escape special chars for MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _esc_code(text: str) -> str:
    """Escape for text inside MarkdownV2 backtick code spans (only backtick and backslash)."""
    return text.replace("\\", "\\\\").replace("`", "\\`")


async def cb_cancel_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    task_id = int(query.data.split(":")[1])
    if db.cancel_task(task_id):
        await query.answer("Отменено.")
        await query.edit_message_text(f"Задача #{task_id} отменена.")
    else:
        await query.answer("Не удалось отменить (уже выполнена или не найдена).", show_alert=True)


async def cb_reset_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    task_id = int(query.data.split(":")[1])
    if db.reset_task(task_id):
        await query.answer("Сброшено в pending.")
        await query.edit_message_text(f"Задача #{task_id} возвращена в очередь.")
    else:
        await query.answer("Задача не в статусе running.", show_alert=True)


async def cb_delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    task_id = int(query.data.split(":")[1])
    if db.delete_task(task_id):
        await query.answer("Удалено.")
        await query.edit_message_text(f"Задача #{task_id} удалена.")
    else:
        await query.answer("Задача не найдена.", show_alert=True)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await _deny(update)
        return

    s = db.get_stats()
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
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=_main_menu())


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

async def show_providers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await _deny(update)
        return

    providers = load_providers()
    lines = ["*Провайдеры:*\n"]
    for name, info in providers.items():
        desc = info.get("description", "")
        lines.append(f"• `{name}` — {desc}")
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=_main_menu()
    )


# ---------------------------------------------------------------------------
# Add task (ConversationHandler)
# ---------------------------------------------------------------------------

async def add_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await _deny(update)
        return ConversationHandler.END

    if TASK_PASSWORD:
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


async def add_task_got_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    entered = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    if entered != TASK_PASSWORD:
        await update.message.reply_text(
            "Неверный пароль. Создание задачи отменено.",
            reply_markup=_main_menu(),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Введите промпт для задачи:\n(/cancel — отменить)",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_PROMPT


async def add_task_got_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_prompt"] = update.message.text

    providers = load_providers()
    row, buttons = [], []
    for name in providers:
        row.append(InlineKeyboardButton(name, callback_data=f"prov:{name}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⬛ По умолчанию", callback_data="prov:")])

    await update.message.reply_text(
        "Выберите провайдера:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ASK_PROVIDER


async def add_task_got_provider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["new_provider"] = query.data.split(":", 1)[1] or None

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1 ⬆ высший", callback_data="pri:1"),
            InlineKeyboardButton("3", callback_data="pri:3"),
            InlineKeyboardButton("5 норм", callback_data="pri:5"),
        ],
        [
            InlineKeyboardButton("7", callback_data="pri:7"),
            InlineKeyboardButton("10 ⬇ низший", callback_data="pri:10"),
        ],
    ])
    await query.edit_message_text("Выберите приоритет:", reply_markup=keyboard)
    return ASK_PRIORITY


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

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да (--dangerously-skip-permissions)", callback_data="skipper:yes"),
            InlineKeyboardButton("❌ Нет", callback_data="skipper:no"),
        ]
    ])
    await query.edit_message_text(
        "Запустить с `--dangerously-skip-permissions`?",
        reply_markup=keyboard,
        parse_mode="MarkdownV2",
    )
    return ASK_SKIP_PERMS


async def add_task_got_skip_perms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["new_skip_permissions"] = query.data == "skipper:yes"

    # Directory pre-filled (e.g. from project skills picker) — skip dir step
    if "new_dir" in context.user_data:
        pre_dir = context.user_data["new_dir"]
        label = pre_dir if pre_dir else "не указана"
        await query.edit_message_text(f"Директория: `{_esc(label)}`", parse_mode="MarkdownV2")
        return await _ask_schedule_from_query(query, context)

    projects = _list_projects()
    if projects:
        # Show project selector
        buttons = []
        row = []
        for proj in projects:
            row.append(InlineKeyboardButton(proj, callback_data=f"dir:{proj}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([
            InlineKeyboardButton("✏️ Ввести вручную", callback_data="dir:__manual__"),
            InlineKeyboardButton("⏭ Пропустить", callback_data="dir:__skip__"),
        ])
        await query.edit_message_text(
            "Выберите рабочую директорию:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return ASK_DIR
    else:
        await query.edit_message_text(
            "Рабочая директория для выполнения задачи:\n"
            "Введите путь или /skip чтобы пропустить."
        )
        return ASK_DIR_MANUAL


async def add_task_got_dir_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle directory selection from inline keyboard."""
    query = update.callback_query
    await query.answer()
    value = query.data.split(":", 1)[1]

    if value == "__skip__":
        context.user_data["new_dir"] = None
        await query.edit_message_text("Директория: не указана")
        return await _ask_schedule_from_query(query, context)
    elif value == "__manual__":
        await query.edit_message_text(
            "Введите путь к директории или /skip чтобы пропустить:"
        )
        return ASK_DIR_MANUAL
    else:
        full_path = os.path.join(PROJECTS_ROOT, value)
        context.user_data["new_dir"] = full_path
        await query.edit_message_text(f"Директория: `{full_path}`", parse_mode="Markdown")
        return await _ask_schedule_from_query(query, context)


async def add_task_got_dir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_dir"] = update.message.text.strip() or None
    return await _ask_schedule(update, context)


async def add_task_skip_dir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_dir"] = None
    return await _ask_schedule(update, context)


async def _ask_schedule_from_query(query, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶ Сейчас", callback_data="sched:now"),
            InlineKeyboardButton("+1ч",      callback_data="sched:+1h"),
            InlineKeyboardButton("+3ч",      callback_data="sched:+3h"),
        ],
        [
            InlineKeyboardButton("+8ч",      callback_data="sched:+8h"),
            InlineKeyboardButton("+24ч",     callback_data="sched:+24h"),
        ],
    ])
    await query.message.reply_text(
        "Когда запустить?\n"
        "Выберите или введите время вручную (формат: `2026-03-27T03:00`)",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return ASK_SCHEDULE


async def _ask_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶ Сейчас", callback_data="sched:now"),
            InlineKeyboardButton("+1ч",      callback_data="sched:+1h"),
            InlineKeyboardButton("+3ч",      callback_data="sched:+3h"),
        ],
        [
            InlineKeyboardButton("+8ч",      callback_data="sched:+8h"),
            InlineKeyboardButton("+24ч",     callback_data="sched:+24h"),
        ],
    ])
    await update.message.reply_text(
        "Когда запустить?\n"
        "Выберите или введите время вручную (формат: `2026-03-27T03:00`)",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return ASK_SCHEDULE


async def add_task_got_schedule_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from datetime import datetime, timedelta, timezone
    query = update.callback_query
    await query.answer()

    value = query.data.split(":", 1)[1]
    now = datetime.now(timezone.utc)
    offsets = {"+1h": 1, "+3h": 3, "+8h": 8, "+24h": 24}

    if value == "now":
        scheduled_at = None
    elif value in offsets:
        scheduled_at = now + timedelta(hours=offsets[value])
    else:
        scheduled_at = None

    context.user_data["new_schedule"] = scheduled_at
    await query.edit_message_text(
        "Время выбрано: " + (scheduled_at.strftime("%d.%m.%Y %H:%M UTC") if scheduled_at else "сейчас")
    )
    return await _finish_add_task_from_query(query, context)


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

    context.user_data["new_schedule"] = scheduled_at
    return await _finish_add_task(update, context)


async def _finish_add_task_from_query(query, context):
    """Called after inline button — uses query.message for reply."""
    from .models import TaskCreate
    prompt = context.user_data.pop("new_prompt", "")
    provider = context.user_data.pop("new_provider", None)
    priority = context.user_data.pop("new_priority", 5)
    working_dir = context.user_data.pop("new_dir", None)
    scheduled_at = context.user_data.pop("new_schedule", None)
    skip_permissions = context.user_data.pop("new_skip_permissions", False)

    task = db.create_task(TaskCreate(
        prompt=prompt,
        working_dir=working_dir,
        provider=provider,
        priority=priority,
        scheduled_at=scheduled_at,
        skip_permissions=skip_permissions,
    ))

    sched_str = scheduled_at.strftime("%d.%m.%Y %H:%M UTC") if scheduled_at else "сейчас"
    skip_str = " ⚠️ --dangerously-skip-permissions" if skip_permissions else ""
    await query.message.reply_text(
        f"✅ Задача #{task.id} добавлена!\n"
        f"Провайдер: {provider or 'claude (по умолчанию)'}\n"
        f"Приоритет: {priority}\n"
        f"Директория: {working_dir or 'не указана'}\n"
        f"Запуск: {sched_str}{skip_str}",
        reply_markup=_main_menu(),
    )
    return ConversationHandler.END


async def _finish_add_task(update: Update, context: ContextTypes.DEFAULT_TYPE, working_dir=None):
    prompt = context.user_data.pop("new_prompt", "")
    provider = context.user_data.pop("new_provider", None)
    priority = context.user_data.pop("new_priority", 5)
    working_dir = working_dir or context.user_data.pop("new_dir", None)
    scheduled_at = context.user_data.pop("new_schedule", None)
    skip_permissions = context.user_data.pop("new_skip_permissions", False)

    task = db.create_task(TaskCreate(
        prompt=prompt,
        working_dir=working_dir,
        provider=provider,
        priority=priority,
        scheduled_at=scheduled_at,
        skip_permissions=skip_permissions,
    ))

    sched_str = scheduled_at.strftime("%d.%m.%Y %H:%M UTC") if scheduled_at else "сейчас"
    skip_str = " ⚠️ --dangerously-skip-permissions" if skip_permissions else ""
    await update.message.reply_text(
        f"✅ Задача #{task.id} добавлена!\n"
        f"Провайдер: {provider or 'claude (по умолчанию)'}\n"
        f"Приоритет: {priority}\n"
        f"Директория: {working_dir or 'не указана'}\n"
        f"Запуск: {sched_str}{skip_str}",
        reply_markup=_main_menu(),
    )
    return ConversationHandler.END


async def add_task_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in ("new_prompt", "new_provider", "new_priority", "new_dir", "new_schedule"):
        context.user_data.pop(key, None)
    await update.message.reply_text("Отменено.", reply_markup=_main_menu())
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Reply to task (continue session)
# ---------------------------------------------------------------------------

async def cb_reply_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    task_id = int(query.data.split(":")[1])
    task = db.get_task(task_id)
    if not task or not task.session_id:
        await query.edit_message_text("Сессия не найдена.")
        return ConversationHandler.END

    context.user_data["reply_task_id"] = task_id
    context.user_data["reply_session_id"] = task.session_id
    context.user_data["reply_provider"] = task.provider
    context.user_data["reply_dir"] = task.working_dir
    context.user_data["reply_skip_permissions"] = task.skip_permissions

    await query.message.reply_text(
        f"Продолжение задачи \\#{task_id}\\.\nВведите ваш ответ:\n\\(/cancel — отменить\\)",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="MarkdownV2",
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
    ))

    await update.message.reply_text(
        f"✅ Задача #{task.id} добавлена (продолжение #{parent_id})!",
        reply_markup=_main_menu(),
    )
    return ConversationHandler.END


async def reply_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in ("reply_task_id", "reply_session_id", "reply_provider", "reply_dir", "reply_skip_permissions"):
        context.user_data.pop(key, None)
    await update.message.reply_text("Отменено.", reply_markup=_main_menu())
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Skills (/skills command + skill task creation)
# ---------------------------------------------------------------------------

def _best_claude_provider() -> str | None:
    """Return the best available Claude provider (prefers DEFAULT_CLI if it supports skills)."""
    providers = load_providers()
    claude_providers = [name for name, info in providers.items() if info.get("supports_skills", False)]
    if not claude_providers:
        return None
    return DEFAULT_CLI if DEFAULT_CLI in claude_providers else claude_providers[0]


def _priority_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1 ⬆ высший", callback_data="pri:1"),
            InlineKeyboardButton("3", callback_data="pri:3"),
            InlineKeyboardButton("5 норм", callback_data="pri:5"),
        ],
        [
            InlineKeyboardButton("7", callback_data="pri:7"),
            InlineKeyboardButton("10 ⬇ низший", callback_data="pri:10"),
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
        row.append(InlineKeyboardButton(proj, callback_data=f"skills_dir:{proj}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("◀ Назад", callback_data="skills_back")])

    await query.edit_message_text(
        "Выберите проект для загрузки его скилов:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_skills_dir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Load and show global + project-local skills for the selected project."""
    query = update.callback_query
    await query.answer()

    proj_name = query.data.split(":", 1)[1]
    workdir = os.path.join(PROJECTS_ROOT, proj_name)
    context.user_data["skills_workdir"] = workdir

    skills = get_skills(working_dir=workdir)
    if not skills:
        await query.edit_message_text(f"Скилы не найдены ни глобально, ни в `{proj_name}`.")
        return

    text, keyboard = _build_skills_message(
        skills, f"Скилы ({proj_name}):", show_proj_btn=False
    )
    rows = list(keyboard.inline_keyboard)
    rows.append([InlineKeyboardButton("◀ Назад", callback_data="skills_proj_picker")])
    await query.edit_message_text(
        text, parse_mode="MarkdownV2", reply_markup=InlineKeyboardMarkup(rows)
    )


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


async def skill_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for skill conversation — triggered when user taps a skill button."""
    query = update.callback_query
    await query.answer()

    skill_name = query.data.split(":", 1)[1]
    provider = _best_claude_provider()
    if not provider:
        await query.edit_message_text("Нет Claude-провайдера для выполнения скилов.")
        return ConversationHandler.END

    context.user_data["new_provider"] = provider
    context.user_data["new_skill_name"] = skill_name

    # If user browsed to a project's skills, pre-fill working directory and skip dir step
    workdir = context.user_data.pop("skills_workdir", None)
    if workdir:
        context.user_data["new_dir"] = workdir

    all_skills = get_skills(working_dir=workdir) if workdir else get_skills()
    skill = next((s for s in all_skills if s["name"] == skill_name), None)
    arg_hint = skill.get("argument_hint", "") if skill else ""

    if arg_hint:
        await query.edit_message_text(
            f"Скил: `/{_esc_code(skill_name)}`\n"
            f"Аргументы: _{_esc(arg_hint)}_\n\n"
            f"Введите аргументы или /skip:",
            parse_mode="MarkdownV2",
        )
        return ASK_SKILL_ARGS

    context.user_data["new_prompt"] = f"/{skill_name}"
    await query.edit_message_text(
        f"Скил: `/{_esc_code(skill_name)}`\nПровайдер: {_esc(provider)}\n\nВыберите приоритет:",
        parse_mode="MarkdownV2",
        reply_markup=_priority_keyboard(),
    )
    return ASK_PRIORITY


async def skill_got_args(update: Update, context: ContextTypes.DEFAULT_TYPE):
    skill_name = context.user_data.get("new_skill_name", "")
    args = update.message.text.strip()
    context.user_data["new_prompt"] = f"/{skill_name} {args}" if args else f"/{skill_name}"
    await update.message.reply_text("Выберите приоритет:", reply_markup=_priority_keyboard())
    return ASK_PRIORITY


async def skill_skip_args(update: Update, context: ContextTypes.DEFAULT_TYPE):
    skill_name = context.user_data.get("new_skill_name", "")
    context.user_data["new_prompt"] = f"/{skill_name}"
    await update.message.reply_text("Выберите приоритет:", reply_markup=_priority_keyboard())
    return ASK_PRIORITY


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_bot():
    token = os.environ.get("PP_TG_TOKEN")
    if not token:
        raise RuntimeError("PP_TG_TOKEN environment variable is not set")

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )

    app = Application.builder().token(token).build()

    add_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ Добавить задачу$"), add_task_start),
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
            ASK_PROVIDER: [
                CallbackQueryHandler(add_task_got_provider, pattern=r"^prov:")
            ],
            ASK_PRIORITY: [
                CallbackQueryHandler(add_task_got_priority, pattern=r"^pri:")
            ],
            ASK_SKIP_PERMS: [
                CallbackQueryHandler(add_task_got_skip_perms, pattern=r"^skipper:"),
            ],
            ASK_DIR: [
                CallbackQueryHandler(add_task_got_dir_btn, pattern=r"^dir:"),
            ],
            ASK_DIR_MANUAL: [
                CommandHandler("skip", add_task_skip_dir),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_task_got_dir),
            ],
            ASK_SCHEDULE: [
                CallbackQueryHandler(add_task_got_schedule_btn, pattern=r"^sched:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_task_got_schedule_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", add_task_cancel)],
    )

    reply_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_reply_task_start, pattern=r"^reply_task:\d+$")
        ],
        states={
            ASK_REPLY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, reply_got_text)
            ],
        },
        fallbacks=[CommandHandler("cancel", reply_cancel)],
    )

    # Group -1: skills navigation runs before ConversationHandlers (group 0).
    # ConversationHandler eats all callbacks when in an active state, so
    # skills_dir / skills_proj_picker / skills_back must be in a higher-priority group.
    app.add_handler(CallbackQueryHandler(cb_skills_proj_picker, pattern=r"^skills_proj_picker$"), group=-1)
    app.add_handler(CallbackQueryHandler(cb_skills_dir, pattern=r"^skills_dir:"), group=-1)
    app.add_handler(CallbackQueryHandler(cb_skills_back, pattern=r"^skills_back$"), group=-1)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    app.add_handler(reply_conv)
    app.add_handler(add_conv)
    app.add_handler(MessageHandler(filters.Regex("^📋 Задачи$"), show_tasks))
    app.add_handler(MessageHandler(filters.Regex("^📊 Статистика$"), show_stats))
    app.add_handler(MessageHandler(filters.Regex("^🔌 Провайдеры$"), show_providers))
    app.add_handler(MessageHandler(filters.Regex("^⚡ Скилы$"), cmd_skills))
    app.add_handler(CallbackQueryHandler(cb_task, pattern=r"^task:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_page, pattern=r"^page:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_task, pattern=r"^cancel_task:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_reset_task, pattern=r"^reset_task:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_delete_task, pattern=r"^delete_task:\d+$"))

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Unhandled exception for update %s", update, exc_info=context.error)

    app.add_error_handler(error_handler)
    logger.info("PromptPilot Telegram bot started.")
    app.run_polling(drop_pending_updates=True)
