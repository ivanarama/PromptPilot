# PromptPilot

> **Background task queue for AI CLIs** — schedule prompts, retry on rate limits, manage everything via Web UI or Telegram bot.
>
> Works with Claude Code, OpenAI Codex, Qwen Code, or any CLI that accepts a prompt argument.

---

Универсальный планировщик промптов для AI CLI — очередь, планирование и автоматический retry.

Работает с **любым** AI CLI: Claude Code, Codex, Qwen Code и другими.

## Возможности

- **Мульти-провайдер** — Claude, Codex, Qwen, или любой свой CLI
- **Очередь задач** с приоритетами (1 — высший, 10 — низший)
- **Планирование** — запуск промптов в заданное время
- **Выбор модели** — для Claude Code провайдеров: sonnet / opus / haiku (Web UI + бот)
- **Rate limit detection** — автоматическое определение лимитов API
- **Exponential backoff** — retry с нарастающей задержкой (60s → 1h)
- **Crash recovery** — при перезапуске воркера зависшие задачи возвращаются в очередь
- **CLI + Web UI** — два интерфейса на выбор
- **Telegram бот** — управление задачами через Telegram с авторизацией по номеру телефона
- **Пароль на создание задач в боте** — опциональная защита через `PP_TASK_PASSWORD`
- **`--dangerously-skip-permissions`** — флаг на задачу для запуска Claude без интерактивных подтверждений
- **Скилы Claude Code** — запуск `/skill-name` через Web UI и бота (для всех Claude Code провайдеров)
- **Продолжение сессии** — кнопка 💬 в боте после завершённой задачи для диалога в той же сессии
- **Tray-приложение** — двойной клик на `pp.exe`, иконка в трее, всё управление мышью
- **Standalone .exe** — сборка без зависимостей через PyInstaller
- **SQLite** — данные хранятся локально в `~/.promptpilot/`

## Установка

```bash
cd PromptPilot
pip install -e .
```

Требования: Python 3.10+, хотя бы один AI CLI в PATH (claude, codex, qwen и т.д.).

## Быстрый старт

```bash
# Добавить задачу
pp add "Объясни что такое рекурсия"

# Запустить воркер (выполняет задачи)
pp worker

# В другом терминале — запустить веб-интерфейс
pp server
# Откроется на http://127.0.0.1:8420
```

## Запуск: два режима

### Режим 1 — Tray (рекомендуется для .exe)

Двойной клик на `pp.exe` — иконка появляется в системном трее, worker и server стартуют автоматически.

Правый клик на иконке:

```
▶ Worker          ← кликнуть = остановить
▶ Server          ← кликнуть = остановить
■ Bot             ← кликнуть = запустить (нужен PP_TG_TOKEN в .env)
─────────────────
Запустить все
Остановить все
─────────────────
Открыть Web UI    ← открывает браузер на http://127.0.0.1:8420
─────────────────
Выход             ← останавливает все сервисы и закрывает трей
```

Цвет иконки показывает состояние: 🟢 все работают / 🟠 частично / ⚫ остановлено.

Или явно через команду:

```powershell
pp tray
```

### Режим 2 — CLI (все команды работают)

```powershell
pp worker          # запустить воркер
pp server          # запустить веб-UI
pp bot             # запустить Telegram бот
pp add "промпт"    # добавить задачу
pp list            # список задач
# и т.д.
```

Оба режима работают с одной и той же БД и настройками.

## Файл .env (настройки)

Все настройки — токен бота, путь к `claude.exe`, разрешённые номера — хранятся в `.env` файле.

Скопируй шаблон и заполни:

```powershell
copy .env.example .env
notepad .env
```

`.env` рядом с `pp.exe` (или рядом со скриптом):

```ini
PP_TG_TOKEN=7123456789:AAF...
PP_TG_ALLOWED_PHONES=+79001234567,+79007654321
PP_CLAUDE_EXE=C:\Users\YourName\.local\bin\claude.exe
PP_DEFAULT_CLI=claude
```

> **Авторизация Claude:** PromptPilot запускает `claude.exe` как обычный процесс — он наследует окружение текущего пользователя. Достаточно один раз выполнить `claude auth login` на этой машине, больше ничего настраивать не нужно.

Порядок поиска `.env`:
1. Рядом с `pp.exe` — для дистрибуции
2. Текущая рабочая директория — для разработки
3. `~/.promptpilot/.env` — постоянный пользовательский конфиг

Значения из `.env` применяются только если переменная **не задана** в окружении — то есть `$env:PP_TG_TOKEN` всегда перекрывает `.env`.

## PowerShell: запуск одной командой

Запустить воркер + сервер в фоне:

```powershell
.\start.ps1
```

Запустить всё включая Telegram бота:

```powershell
$env:PP_TG_TOKEN = "ваш-токен"
$env:PP_TG_ALLOWED_PHONES = "+79001234567"
.\start.ps1 -Bot
```

Логи пишутся в `.\logs\`. Остановить:

```powershell
.\stop.ps1
```

Скрипт автоматически использует `dist\pp.exe` если он собран, иначе `pp` из PATH.

## Сборка .exe

Сборка standalone-бинаря (не требует Python на целевой машине):

```powershell
.\build.ps1
```

На выходе: `dist\pp.exe`. Использование аналогично:

```powershell
.\dist\pp.exe worker
.\dist\pp.exe server
.\dist\pp.exe bot
.\dist\pp.exe add "промпт"
```

> **Примечание:** при первом запуске `pp.exe` может занять несколько секунд — PyInstaller распаковывает бандл во временную папку.

## CLI

```
pp add "промпт"                        # добавить задачу (дефолтный провайдер)
pp add "промпт" -c codex               # через Codex
pp add "промпт" -c qwen                # через Qwen
pp add "промпт" -c claude-z            # через кастомный алиас
pp add "промпт" -p 1                   # с приоритетом (1 = высший)
pp add "промпт" -a "2026-03-25T03:00"  # запланировать на время
pp add -f prompts.txt                  # добавить из файла (по строке)
pp add "промпт" -d /path/to/project    # задать рабочую директорию

pp list                                # все задачи
pp list -s pending                     # фильтр по статусу
pp status 1                            # детали задачи #1
pp cancel 1                            # отменить задачу
pp delete 1                            # удалить задачу
pp stats                               # статистика
pp purge --days 7                      # удалить старые завершённые задачи

pp worker                              # запустить воркер
pp server                              # запустить веб-UI
pp server -p 9000                      # на другом порту
pp bot                                 # запустить Telegram бот
```

## Telegram бот

### Настройка

1. Создай бота через [@BotFather](https://t.me/BotFather), получи токен.
2. Задай переменные окружения:

```powershell
$env:PP_TG_TOKEN = "токен-от-botfather"
$env:PP_TG_ALLOWED_PHONES = "+79001234567,+79007654321"
```

3. Запусти:

```powershell
pp bot
```

### Авторизация

При первом открытии бота пользователь видит кнопку **«Поделиться контактом»**. Бот получает номер телефона и сверяет с `PP_TG_ALLOWED_PHONES`. При совпадении — доступ открыт.

Авторизованные пользователи сохраняются в `~/.promptpilot/tg_users.json`. Повторная авторизация при перезапуске не нужна.

Альтернатива env-переменной — файл `~/.promptpilot/tg_config.json`:

```json
{
  "allowed_phones": ["+79001234567", "+79007654321"]
}
```

### Возможности бота

| Функция | Описание |
|---------|----------|
| 📋 Задачи | Список задач с пагинацией и статусами |
| ➕ Добавить задачу | Промпт → провайдер → **модель** → приоритет → skip-permissions → директория → расписание |
| 📊 Статистика | Сводка по статусам |
| 🔌 Провайдеры | Список доступных провайдеров |
| Детали задачи | Промпт, результат, ошибка, кнопки отмены / удаления / сброса |
| 💬 Ответить | Продолжить диалог с моделью в той же сессии |
| ⚡ Скилы (`/skills`) | Список Claude Code скилов; выбор запускает пошаговое создание задачи |

### Продолжение сессии (💬 Ответить)

После завершения задачи в деталях появляется кнопка **💬 Ответить** — если модель спросила что-то или ты хочешь продолжить диалог:

1. Открой детали завершённой задачи → нажми **💬 Ответить**
2. Введи ответ или следующий вопрос
3. Бот создаст новую задачу с флагом `--resume <session_id>` — Claude продолжит разговор в том же контексте

Цепочка не ограничена: каждый «ответ» тоже получает кнопку 💬. Новая задача наследует провайдера, рабочую директорию и флаги оригинальной.

### Защита паролем (PP_TASK_PASSWORD)

Если задана переменная `PP_TASK_PASSWORD`, бот запрашивает пароль перед созданием задачи. При неверном вводе создание отменяется; введённое сообщение автоматически удаляется из чата.

```ini
PP_TASK_PASSWORD=mysecretpassword
```

Просмотр задач и статистика паролем не защищены — только создание.

## Провайдеры

Встроенные провайдеры:

| Имя | Описание | Скилы | Выбор модели |
|-----|----------|-------|--------------|
| `claude` | Claude Code (Anthropic) — дефолт | ✅ | ✅ sonnet / opus / haiku |
| `claude-z` | Claude Code с альтернативным API (GLM, z.ai и др.) | ✅ | ✅ sonnet / opus / haiku |
| `codex` | OpenAI Codex | — | — |
| `qwen` | Qwen Code | — | — |

> Любой провайдер с `supports_skills=True` считается Claude Code-совместимым и получает выбор модели автоматически.

Команды управления:

```bash
pp provider                   # список всех
pp provider add <name> ...    # добавить
pp provider remove <name>     # удалить
```

Кастомные провайдеры сохраняются в `~/.promptpilot/providers.json`.

Дефолтный провайдер: переменная `PP_DEFAULT_CLI` (по умолчанию `claude`).

Путь к `claude.exe` по умолчанию: `~/.local/bin/claude.exe`. Переопределяется через `PP_CLAUDE_EXE`.

### Добавление кастомного провайдера

```bash
pp provider add myai \
  --cmd "myai run {prompt}" \
  --desc "My AI Tool"
```

С переменными окружения и поддержкой скилов:

```powershell
pp provider add claude-z `
  --cmd "C:\Users\<username>\.local\bin\claude.exe -p --verbose --output-format stream-json {prompt}" `
  --desc "Claude Code (GLM via z.ai)" `
  --env "ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic" `
  --env "ANTHROPIC_AUTH_TOKEN=your-token-here" `
  --env "ANTHROPIC_DEFAULT_SONNET_MODEL=glm-4.7" `
  --env "ANTHROPIC_DEFAULT_OPUS_MODEL=glm-4.7"
```

> **Windows:** `subprocess` не видит PowerShell-функции и алиасы — нужен полный путь к исполняемому файлу. `.cmd`/`.bat`-обёртки (npm-инструменты вроде `qwen`, `codex`) находятся автоматически через `shutil.which`.

## Скилы Claude Code

Скилы — команды (`/skill-name`) из `~/.claude/commands/`, `~/.claude/skills/` и плагинов Claude Code. Доступны для всех провайдеров с `supports_skills=True`.

### Web UI

При выборе Claude-провайдера под полем промпта появляется кнопка **⚡ Skills**. Нажми — откроется список скилов с описаниями. Выбор подставляет `/skill-name ` в промпт.

### Telegram бот

Кнопка **⚡ Скилы** в главном меню или команда `/skills`. Поддерживает глобальные скилы и скилы конкретного проекта (`📁 Скилы проекта...`).

### REST API

```
GET /api/skills                                — все доступные скилы
GET /api/skills?provider=claude                — только если провайдер поддерживает скилы
GET /api/skills?provider=claude&workdir=/path  — + локальные скилы проекта
```

## Веб-интерфейс

Минималистичный dark-theme UI на `http://127.0.0.1:8420`:

- Выбор провайдера и модели (дропдаун модели появляется автоматически для Claude Code провайдеров)
- Добавление задач с приоритетом и расписанием (Ctrl+Enter для отправки)
- Чекбокс `--dangerously-skip-permissions`
- **⚡ Skills** — раскрывает список доступных скилов
- Фильтры по статусу, раскрытие деталей задачи
- Отмена и удаление задач
- Автообновление каждые 5 секунд

## REST API

```
GET    /api/tasks              — список задач (?status=pending&limit=50)
POST   /api/tasks              — создать задачу
GET    /api/tasks/{id}         — детали задачи
PATCH  /api/tasks/{id}         — обновить (отменить, сменить приоритет)
DELETE /api/tasks/{id}         — удалить
POST   /api/tasks/{id}/reset   — сбросить зависшую задачу в pending
GET    /api/stats              — статистика по статусам
GET    /api/providers          — провайдеры (description, supports_skills, models)
GET    /api/skills             — скилы (?provider=claude&workdir=/path)
GET    /api/projects           — проекты из PP_PROJECTS_ROOT
```

## Конфигурация

| Переменная | По умолчанию | Описание |
|---|---|---|
| `PP_DATA_DIR` | `~/.promptpilot` | Директория для БД |
| `PP_POLL_INTERVAL` | `5` | Интервал опроса очереди (сек) |
| `PP_TASK_TIMEOUT` | `300` | Таймаут выполнения задачи (сек) |
| `PP_BASE_DELAY` | `60` | Начальная задержка retry (сек) |
| `PP_MAX_DELAY` | `3600` | Максимальная задержка retry (сек) |
| `PP_MAX_RETRIES` | `5` | Макс. кол-во retry по умолчанию |
| `PP_DEFAULT_CLI` | `claude` | Провайдер по умолчанию |
| `PP_HOST` | `127.0.0.1` | Хост веб-сервера |
| `PP_PORT` | `8420` | Порт веб-сервера |
| `PP_TG_TOKEN` | — | Токен Telegram бота |
| `PP_TG_ALLOWED_PHONES` | — | Разрешённые номера (через запятую) |
| `PP_TASK_PASSWORD` | — | Пароль для создания задач через бота |
| `PP_PROJECTS_ROOT` | — | Корневая папка проектов для быстрого выбора директории |
| `PP_CLAUDE_EXE` | `~/.local/bin/claude.exe` | Путь к claude.exe |

## Статусы задач

| Статус | Описание |
|---|---|
| `pending` | В очереди |
| `running` | Выполняется |
| `completed` | Успешно завершена |
| `failed` | Завершена с ошибкой |
| `rate_limited` | Ожидает retry после rate limit |
| `cancelled` | Отменена |

## Архитектура

```
promptpilot/
├── config.py       — настройки, провайдеры, скилы, build_cmd
├── models.py       — Pydantic-модели
├── db.py           — SQLite (очередь, CRUD, планирование)
├── worker.py       — воркер (subprocess → любой AI CLI)
├── cli.py          — CLI (Click)
├── api.py          — REST API (FastAPI)
├── bot.py          — Telegram бот (python-telegram-bot)
├── tg_auth.py      — авторизация по номеру телефона
└── static/
    └── index.html  — веб-интерфейс

start.ps1           — запустить все сервисы
stop.ps1            — остановить все сервисы
build.ps1           — собрать dist\pp.exe
pp.spec             — конфиг PyInstaller
```

Воркер и сервер — два отдельных процесса, работающих с одной SQLite БД. Воркер выполняет задачи последовательно (одна за раз), чтобы не упираться в rate limits.
