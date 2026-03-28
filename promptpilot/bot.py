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
from .config import load_providers
from .models import TaskCreate, TaskStatus
from .tg_auth import authorize_user, is_authorized, load_allowed_phones

logger = logging.getLogger(__name__)

# Conversation states
ASK_PROMPT, ASK_PROVIDER, ASK_PRIORITY, ASK_DIR, ASK_SCHEDULE = range(5)

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
            ["📊 Статистика", "🔌 Провайдеры"],
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
    provider_str = task.provider or "claude \\(по умолчанию\\)"

    text = (
        f"*Задача \\#{task.id}*\n"
        f"Статус: {icon} {task.status.value}\n"
        f"Провайдер: {provider_str}\n"
        f"Приоритет: {task.priority}\n"
        f"Создана: {created}\n"
        f"Retry: {task.retry_count}/{task.max_retries}"
    )
    if task.working_dir:
        text += f"\nДир: `{task.working_dir}`"

    text += f"\n\n*Промпт:*\n{_esc(task.prompt[:500])}"

    if task.result:
        text += f"\n\n*Результат:*\n{_esc(task.result[:800])}"
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


async def add_task_got_priority(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["new_priority"] = int(query.data.split(":")[1])

    await query.edit_message_text(
        "Рабочая директория для выполнения задачи:\n"
        "Введите путь или /skip чтобы пропустить."
    )
    return ASK_DIR


async def add_task_got_dir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_dir"] = update.message.text.strip() or None
    return await _ask_schedule(update, context)


async def add_task_skip_dir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_dir"] = None
    return await _ask_schedule(update, context)


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

    task = db.create_task(TaskCreate(
        prompt=prompt,
        working_dir=working_dir,
        provider=provider,
        priority=priority,
        scheduled_at=scheduled_at,
    ))

    sched_str = scheduled_at.strftime("%d.%m.%Y %H:%M UTC") if scheduled_at else "сейчас"
    await query.message.reply_text(
        f"✅ Задача #{task.id} добавлена!\n"
        f"Провайдер: {provider or 'claude (по умолчанию)'}\n"
        f"Приоритет: {priority}\n"
        f"Директория: {working_dir or 'не указана'}\n"
        f"Запуск: {sched_str}",
        reply_markup=_main_menu(),
    )
    return ConversationHandler.END


async def _finish_add_task(update: Update, context: ContextTypes.DEFAULT_TYPE, working_dir=None):
    prompt = context.user_data.pop("new_prompt", "")
    provider = context.user_data.pop("new_provider", None)
    priority = context.user_data.pop("new_priority", 5)
    working_dir = working_dir or context.user_data.pop("new_dir", None)
    scheduled_at = context.user_data.pop("new_schedule", None)

    task = db.create_task(TaskCreate(
        prompt=prompt,
        working_dir=working_dir,
        provider=provider,
        priority=priority,
        scheduled_at=scheduled_at,
    ))

    sched_str = scheduled_at.strftime("%d.%m.%Y %H:%M UTC") if scheduled_at else "сейчас"
    await update.message.reply_text(
        f"✅ Задача #{task.id} добавлена!\n"
        f"Провайдер: {provider or 'claude (по умолчанию)'}\n"
        f"Приоритет: {priority}\n"
        f"Директория: {working_dir or 'не указана'}\n"
        f"Запуск: {sched_str}",
        reply_markup=_main_menu(),
    )
    return ConversationHandler.END


async def add_task_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in ("new_prompt", "new_provider", "new_priority", "new_dir", "new_schedule"):
        context.user_data.pop(key, None)
    await update.message.reply_text("Отменено.", reply_markup=_main_menu())
    return ConversationHandler.END


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
            MessageHandler(filters.Regex("^➕ Добавить задачу$"), add_task_start)
        ],
        states={
            ASK_PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_task_got_prompt)
            ],
            ASK_PROVIDER: [
                CallbackQueryHandler(add_task_got_provider, pattern=r"^prov:")
            ],
            ASK_PRIORITY: [
                CallbackQueryHandler(add_task_got_priority, pattern=r"^pri:")
            ],
            ASK_DIR: [
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

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    app.add_handler(add_conv)
    app.add_handler(MessageHandler(filters.Regex("^📋 Задачи$"), show_tasks))
    app.add_handler(MessageHandler(filters.Regex("^📊 Статистика$"), show_stats))
    app.add_handler(MessageHandler(filters.Regex("^🔌 Провайдеры$"), show_providers))
    app.add_handler(CallbackQueryHandler(cb_task, pattern=r"^task:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_page, pattern=r"^page:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_task, pattern=r"^cancel_task:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_reset_task, pattern=r"^reset_task:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_delete_task, pattern=r"^delete_task:\d+$"))

    logger.info("PromptPilot Telegram bot started.")
    app.run_polling(drop_pending_updates=True)
