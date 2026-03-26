# PromptPilot

Универсальный планировщик промптов для AI CLI — очередь, планирование и автоматический retry.

Работает с **любым** AI CLI: Claude Code, Codex, Qwen Code и другими.

## Возможности

- **Мульти-провайдер** — Claude, Codex, Qwen, или любой свой CLI
- **Очередь задач** с приоритетами (1 — высший, 10 — низший)
- **Планирование** — запуск промптов в заданное время
- **Rate limit detection** — автоматическое определение лимитов API
- **Exponential backoff** — retry с нарастающей задержкой (60s → 1h)
- **Crash recovery** — при перезапуске воркера зависшие задачи возвращаются в очередь
- **CLI + Web UI** — два интерфейса на выбор
- **Telegram бот** — управление задачами через Telegram с авторизацией по номеру телефона
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
| ➕ Добавить задачу | Пошаговое создание: промпт → провайдер → приоритет → директория |
| 📊 Статистика | Сводка по статусам |
| 🔌 Провайдеры | Список доступных провайдеров |
| Детали задачи | Промпт, результат, ошибка, кнопки отмены / удаления / сброса |

## Провайдеры

Встроенные провайдеры:

| Имя | Описание |
|---|---|
| `claude` | Claude Code (Anthropic) — дефолт |
| `claude-z` | Claude Code с альтернативным API (GLM, z.ai и др.) |
| `codex` | OpenAI Codex |
| `qwen` | Qwen Code |

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
python -m promptpilot provider add myai \
  --cmd "myai run {prompt}" \
  --desc "My AI Tool"
```

Или с переменными окружения (`--env` можно повторять):

```bash
python -m promptpilot provider add myai \
  --cmd "myai run {prompt}" \
  --desc "My AI Tool" \
  --env "API_KEY=your-key-here" \
  --env "API_URL=https://api.example.com"
```

### PowerShell-алиасы и Windows

Если твой провайдер определён как PowerShell-функция (например `claude-z`), он **не доступен** напрямую через `subprocess`. В этом случае нужно указать путь к реальному исполняемому файлу и передать нужные переменные окружения явно.

```powershell
python -m promptpilot provider add claude-z `
  --cmd "C:\Users\<username>\.local\bin\claude.exe -p --verbose --output-format stream-json {prompt}" `
  --desc "Claude Code (GLM via z.ai)" `
  --env "ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic" `
  --env "ANTHROPIC_AUTH_TOKEN=your-token-here" `
  --env "ANTHROPIC_DEFAULT_SONNET_MODEL=glm-4.7" `
  --env "ANTHROPIC_DEFAULT_OPUS_MODEL=glm-4.7"
```

## Веб-интерфейс

Минималистичный dark-theme UI на `http://127.0.0.1:8420`:

- Выбор провайдера (Claude, Codex, Qwen и др.)
- Добавление задач с приоритетом и расписанием (Ctrl+Enter для отправки)
- Фильтры по статусу
- Раскрытие задачи — полный промпт, результат, ошибки
- Отмена и удаление задач
- Автообновление каждые 5 секунд
- Бейджи со счётчиками по статусам

## REST API

```
GET    /api/tasks              — список задач (?status=pending&limit=50)
POST   /api/tasks              — создать задачу
GET    /api/tasks/{id}         — детали задачи
PATCH  /api/tasks/{id}         — обновить (отменить, сменить приоритет)
DELETE /api/tasks/{id}         — удалить
POST   /api/tasks/{id}/reset   — сбросить зависшую задачу в pending
GET    /api/stats              — статистика по статусам
GET    /api/providers          — список провайдеров
```

## Конфигурация

Через переменные окружения:

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

## Статусы задач

| Статус | Описание |
|---|---|
| `pending` | В очереди, ожидает выполнения |
| `running` | Выполняется прямо сейчас |
| `completed` | Успешно завершена |
| `failed` | Завершена с ошибкой |
| `rate_limited` | Ожидает retry после rate limit |
| `cancelled` | Отменена пользователем |

## Архитектура

```
promptpilot/
├── config.py       — настройки и провайдеры
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

Воркер и сервер — два отдельных процесса, работающих с одной SQLite БД. Воркер последовательно выполняет задачи (одна за раз), чтобы не упираться в rate limits.
