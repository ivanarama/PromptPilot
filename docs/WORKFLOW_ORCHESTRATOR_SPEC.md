# Workflow Orchestrator: автономный цикл «исполнитель → проверка → аудитор»

- Статус: **W2 MVP implemented / Draft 0.3**
- Дата: 2026-08-25
- Целевой пилот: **УТ10 → БП3, этап U3-FIX-2**

Состояние реализации:

- **W0 реализован:** модели, versioned SQLite schema, append-only event log,
  repository API, REST API и CLI наблюдаемости;
- **W1 реализован:** атомарная state machine, ручной dispatch executor/reviewer,
  связь run с task, восстановление после crash-gap, max-rounds stop и импорт
  исторических раундов с provenance-статусами;
- **W2 MVP реализован:** versioned config, UI настройки ролей/этапа/gate,
  автоматические handoff, разбор структурированного вердикта аудитора,
  восстановление worker и экспорт статистики. Ограничения и инструкция описаны
  в `docs/WORKFLOW_AUTOMATION_GUIDE.md`.
- **W3–W5 не завершены:** постоянный workflow-worktree, физически read-only
  checkout аудитора, расширенные бюджеты/стагнация и release/archive policy
  остаются следующими этапами.

## 1. Назначение

PromptPilot уже умеет ставить задачи в очередь, запускать разные AI CLI,
возобновлять сессии, создавать отдельные Git worktree, повторять задачи после
сбоев среды и собирать часть usage-статистики. Не хватает надёжного уровня
оркестрации, который связывает отдельные задачи в контролируемый инженерный
цикл.

Цель расширения — дать пользователю возможность один раз определить задачу,
роли и критерии приёмки, после чего PromptPilot самостоятельно выполняет цикл:

```text
Исполнитель → детерминированные гейты → независимый аудитор
     ↑                                      │
     └──────── структурированные замечания ─┘
```

Цикл завершается только при формально подтверждённой готовности либо переходит
в `awaiting_human`, если требуется решение человека, исчерпан бюджет или
прогресс остановился.

## 2. Основные принципы

1. **Оркестрация задаётся кодом, а не договорённостью в промпте.** Переходы
   состояний, лимиты и критерии завершения проверяет PromptPilot.
2. **Агент не принимает собственную работу.** Исполнитель и аудитор имеют
   разные сессии, инструкции и права.
3. **Детерминированные проверки первичны.** Сборка, тесты, Git-инварианты,
   схемы и хэши проверяются до LLM-аудита.
4. **Проверяется точный commit SHA.** «Текущее состояние каталога» не является
   идентичностью кандидата.
5. **Передача между ролями структурирована.** Оркестратор передаёт JSON,
   commit SHA и артефакты, а не скопированный человеком пересказ.
6. **История append-only.** Исходные ответы, события, результаты гейтов и
   решения не перезаписываются следующей попыткой.
7. **Автономность ограничена.** У каждого workflow есть лимиты раундов,
   времени, стоимости и повторяющихся дефектов.
8. **Человек вмешивается по исключению.** Изменение требований, доступы,
   разрушительные действия и неоднозначные продуктовые решения не
   предполагаются автоматически.
9. **Данные для статьи отделены от утверждений агентов.** Каждый факт имеет
   происхождение и уровень подтверждения.

## 3. Границы первой версии

### 3.1. Входит в MVP

- последовательный цикл из одного исполнителя и одного аудитора;
- автоматические детерминированные гейты между ними;
- работа с одним Git-репозиторием на workflow;
- долгоживущая ветка исполнителя;
- read-only проверка точного commit SHA;
- структурированные отчёты исполнителя и аудитора;
- автоматическое создание следующего раунда по замечаниям;
- лимиты и эскалация человеку;
- append-only история и базовая статистика;
- экспорт истории в JSON, CSV и Markdown для последующего анализа/статьи;
- поддержка существующих provider-адаптеров PromptPilot.

### 3.2. Не входит в MVP

- произвольный граф из десятков взаимодействующих ролей;
- автоматическое разрешение продуктовых противоречий;
- автоматический merge в защищённую release-ветку;
- публикация статьи;
- распределённый scheduler с несколькими активными PromptPilot-серверами;
- замена существующей очереди задач;
- обязательная зависимость от OpenAI Agents SDK.

## 4. Термины и роли

### 4.1. Workflow

Долгоживущий процесс достижения одного проверяемого результата. Содержит
задачу, критерии приёмки, роли, лимиты, Git-политику и гейты.

### 4.2. Round

Одна попытка цикла: работа исполнителя, детерминированная проверка и аудит.
Новый round создаётся только после формального `REVISION_REQUIRED`.

### 4.3. Run

Один запуск конкретной роли или гейта внутри round. Повтор после сбоя среды —
новый run/attempt, но не новый round.

### 4.4. Роли

| Роль | Исполнитель | Права | Результат |
|---|---|---|---|
| `executor` | AI-агент | `workspace-write` в worktree кандидата | Commit + `executor-report.json` |
| `gate` | Код PromptPilot | Только необходимые команды | `gate-result.json` |
| `reviewer` | AI-агент | `read-only` | `review-report.json` |
| `archiver` | Код PromptPilot | Только хранилище workflow и разрешённые audit-пути | История, Markdown, evidence |

`gate` и `archiver` не являются LLM-агентами. Это уменьшает стоимость и не
позволяет модели объявлять успешными проверки, которые не выполнялись.

## 5. Конечный автомат

### 5.1. Состояния workflow

```text
draft
  ↓
queued
  ↓
executing
  ↓
gating ────────────────┐
  ↓ gates_passed       │ gates_failed
reviewing              │
  ├─ PASS → completed  │
  ├─ REVISION_REQUIRED ┴→ revision_required → executing
  ├─ HUMAN_REQUIRED → awaiting_human
  └─ invalid output / environment failure → retry или failed
```

Дополнительные терминальные состояния: `failed`, `cancelled`.

### 5.2. Разрешённые переходы

| Текущее состояние | Следующее | Условие |
|---|---|---|
| `draft` | `queued` | Конфигурация прошла schema validation |
| `queued` | `executing` | Получен worker slot и Git lock |
| `executing` | `gating` | Исполнитель вернул валидный отчёт и committed candidate |
| `executing` | `awaiting_human` | `HUMAN_REQUIRED` |
| `gating` | `reviewing` | Все обязательные гейты успешны |
| `gating` | `revision_required` | Исправимый gate failure |
| `gating` | `awaiting_human` | Небезопасный или неоднозначный failure |
| `reviewing` | `completed` | `PASS`, нет blocker/high findings, все гейты актуальны |
| `reviewing` | `revision_required` | `REVISION_REQUIRED` и есть actionable findings |
| `reviewing` | `awaiting_human` | `HUMAN_REQUIRED`, стагнация или лимит |
| `revision_required` | `executing` | Создан следующий round |
| `awaiting_human` | `queued` | Человек дал решение/доступ и возобновил workflow |
| любое нетерминальное | `cancelled` | Явная отмена |

Переход выполняется транзакционно с optimistic version check. Повтор события
с тем же `idempotency_key` не создаёт второй round или второй task.

## 6. Условия завершения и остановки

### 6.1. `completed`

Workflow может стать `completed`, только если одновременно выполнены условия:

- все обязательные гейты текущего round имеют `PASS`;
- reviewer проверил `candidate_sha`, совпадающий с SHA после гейтов;
- reviewer вернул `PASS` по JSON Schema;
- открытых findings уровня `blocker` и `high` нет;
- manifest/evidence относятся к тому же кандидату;
- рабочее дерево кандидата чистое;
- отсутствуют незакоммиченные обязательные evidence-артефакты;
- лимиты не превышены.

Строка «готово» в свободном тексте не может завершить workflow.

### 6.2. `awaiting_human`

Workflow останавливается для человека, если произошло хотя бы одно событие:

- достигнут `max_rounds`;
- исчерпан лимит времени, токенов или стоимости;
- один fingerprint уровня `blocker` повторился без прогресса два round подряд;
- reviewer и executor расходятся по требованию, отсутствующему в acceptance
  criteria;
- требуется credential, лицензия, интерактивное подтверждение или внешний
  доступ;
- требуется удаление, reset, force push, merge в защищённую ветку или иное
  разрушительное действие;
- изменился scope задачи;
- зафиксирована невозможность воспроизводимой проверки.

### 6.3. Защита от бесконечного пинг-понга

Рекомендуемые defaults:

```yaml
max_rounds: 6
max_same_blocker_rounds: 2
max_environment_retries_per_run: 3
max_wall_time_hours: 24
max_cost_usd: null
max_tokens: null
```

Лимиты настраиваются отдельно для workflow. Retry после rate limit или сбоя
среды не расходует номер round, но учитывается в статистике attempts.

## 7. Модель данных

Существующая таблица `tasks` сохраняется. Workflow использует её как низкий
уровень выполнения и не дублирует очередь.

### 7.1. `workflows`

```sql
CREATE TABLE workflows (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    objective TEXT NOT NULL,
    repository_path TEXT NOT NULL,
    candidate_branch TEXT NOT NULL,
    status TEXT NOT NULL,
    current_round INTEGER NOT NULL DEFAULT 0,
    state_version INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
```

### 7.2. `workflow_rounds`

```sql
CREATE TABLE workflow_rounds (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    status TEXT NOT NULL,
    base_sha TEXT,
    candidate_sha TEXT,
    audit_sha TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    summary_json TEXT,
    UNIQUE(workflow_id, round_no)
);
```

### 7.3. `workflow_runs`

```sql
CREATE TABLE workflow_runs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    role TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    task_id INTEGER,
    status TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    output_sha256 TEXT,
    output_json TEXT,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(round_id, role, attempt_no)
);
```

### 7.4. `workflow_findings`

```sql
CREATE TABLE workflow_findings (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    first_seen_round INTEGER NOT NULL,
    last_seen_round INTEGER NOT NULL,
    reopen_count INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    UNIQUE(workflow_id, fingerprint)
);
```

Fingerprint вычисляется из нормализованных `category`, `affected_component`,
`invariant` и устойчивой части evidence. Номер строки и случайный текст ошибки
не должны быть единственной частью fingerprint.

### 7.5. `workflow_artifacts`

```sql
CREATE TABLE workflow_artifacts (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    run_id TEXT,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    metadata_json TEXT,
    UNIQUE(round_id, kind, sha256)
);
```

### 7.6. `workflow_events`

```sql
CREATE TABLE workflow_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    round_id TEXT,
    run_id TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
```

События не обновляются и не удаляются обычными API. Исправление ошибочного
события оформляется новым компенсирующим событием.

### 7.7. Метрики

Метрики можно хранить отдельной таблицей либо типизированными событиями:

- wall time round/run;
- queue wait;
- provider/model/reasoning effort;
- input/output/cached/reasoning tokens;
- стоимость;
- attempts и environment retries;
- число изменённых файлов и строк;
- число commits;
- tests passed/failed/skipped;
- длительность сборки и runtime gates;
- findings по severity/category;
- resolved/reopened/repeated findings;
- число human interventions;
- результат round.

## 8. Контракты результатов

### 8.1. Отчёт исполнителя

Исполнитель обязан вернуть JSON, валидный по версии схемы. Минимальная форма:

```json
{
  "schema_version": "1.0",
  "workflow_id": "ut10-bp3-u3",
  "round": 4,
  "verdict": "READY_FOR_REVIEW",
  "base_sha": "f7919b3...",
  "candidate_sha": "b13cfa7...",
  "commits": ["ac93b68...", "03e823b..."],
  "resolved_findings": ["U3R1-001", "U3R1-002"],
  "changed_files": [
    {"path": "scripts/inject_loader_object_module.py", "class": "production"}
  ],
  "checks": [
    {"id": "pytest", "status": "pass", "exit_code": 0, "evidence": "..."}
  ],
  "artifacts": [
    {"kind": "loader_epf", "path": "...", "sha256": "...", "size": 617484}
  ],
  "known_limitations": [],
  "requested_review_scope": ["U3-0", "U3-1"]
}
```

Разрешённые verdict:

- `READY_FOR_REVIEW`;
- `HUMAN_REQUIRED`;
- `FAILED`.

Оркестратор сам пересчитывает Git SHA, размеры, хэши и exit codes. Поля отчёта
исполнителя считаются claims до независимой проверки.

### 8.2. Отчёт аудитора

```json
{
  "schema_version": "1.0",
  "workflow_id": "ut10-bp3-u3",
  "round": 4,
  "checked_sha": "b13cfa7...",
  "verdict": "REVISION_REQUIRED",
  "summary": "Технический dispatch работает, семантика операций неполна",
  "facts": [
    {
      "claim": "Полный дамп обработан без ошибок",
      "status": "DISPROVED",
      "evidence": ["operation_probe.json"]
    }
  ],
  "findings": [
    {
      "fingerprint": "bank-cash-operation-map-incomplete",
      "severity": "blocker",
      "category": "semantic_mapping",
      "title": "СБДС создаётся как ПБДС",
      "affected_component": "bank_loader",
      "invariant": "canonical document type must be preserved",
      "evidence": ["operation_probe.json#TK000000016"],
      "reproduction": "python scripts/...",
      "expected_fix": "Покрыть canonical СБДС и запретить generic fallback"
    }
  ]
}
```

Разрешённые verdict:

- `PASS`;
- `REVISION_REQUIRED`;
- `HUMAN_REQUIRED`;
- `REVIEW_ERROR`.

`PASS` с finding уровня `blocker` или `high` отклоняется схемой/валидатором.

### 8.3. Статусы фактов

Для последующего анализа и статьи используются значения:

- `CLAIMED` — утверждение исполнителя, ещё не проверено;
- `VERIFIED` — независимо воспроизведено;
- `DISPROVED` — независимо опровергнуто;
- `PARTIAL` — подтверждена только часть утверждения;
- `INFERRED` — восстановлено из косвенных данных;
- `UNKNOWN` — доказательств недостаточно.

Публичный экспорт по умолчанию включает только `VERIFIED`, а остальные статусы
показывает с явной маркировкой.

## 9. Git и worktree

### 9.1. Ветка исполнителя

Для workflow создаётся долгоживущая ветка:

```text
wf/<workflow-slug>/candidate
```

Исполнитель продолжает работу в одном выделенном worktree. Это сохраняет
контекст и накопленные исправления между round.

### 9.2. Проверка аудитора

После успешных гейтов создаётся временный checkout точного `candidate_sha`.
Reviewer получает `read-only` sandbox. Он не коммитит и не меняет production,
evidence или журнал.

### 9.3. Архивирование аудита

PromptPilot формирует Markdown и JSON из валидного reviewer report. Если проект
требует хранить аудит в Git, controlled archiver может создать commit, в котором
разрешены только пути из allowlist, например:

```yaml
audit_write_allowlist:
  - docs/audit/**
```

Перед commit проверяется `git diff --name-only`. Нарушение allowlist переводит
workflow в `awaiting_human`.

### 9.4. Обязательные Git-гейты

- repository и branch совпадают с конфигурацией;
- base/candidate SHA существуют;
- после executor run нет незакоммиченных обязательных изменений;
- candidate не содержит merge/rebase conflict;
- изменённые пути не нарушают denylist;
- baseline/rollback-артефакты не изменены без явного разрешения;
- reviewer проверяет SHA, прошедший гейты;
- после review candidate SHA не изменился.

## 10. Детерминированные гейты

Гейты задаются декларативно:

```yaml
gates:
  - id: git_clean
    kind: builtin.git_clean
    required: true
  - id: pytest
    kind: command
    command: python -m pytest -q
    timeout_seconds: 900
    required: true
  - id: manifest_schema
    kind: json_schema
    input: evidence/current/manifest.json
    schema: evidence/schema.json
    required: true
  - id: artifact_hashes
    kind: builtin.artifact_hashes
    required: true
```

Для каждого запуска сохраняются:

- точная команда и cwd;
- безопасный снимок окружения без секретов;
- start/end timestamps;
- exit code;
- stdout/stderr как отдельные артефакты;
- SHA входов и выходов;
- timeout/cancel reason.

Логи должны иметь лимит размера и отдельный полный artifact. В UI показывается
сокращённое представление.

## 11. Формирование промптов ролей

Промпт генерируется из версионируемых шаблонов:

```text
role template
+ workflow objective
+ acceptance criteria
+ repository policy
+ current candidate identity
+ unresolved findings
+ required output schema
```

В следующий round исполнителю передаются только открытые findings, предыдущий
candidate SHA и необходимые evidence. Полные многомегабайтные ответы прошлых
агентов не копируются в контекст.

Шаблоны имеют `template_version` и SHA. Это позволяет в статье показать, какой
именно набор инструкций использовался в каждом round.

## 12. Интеграция с provider-адаптерами

### 12.1. MVP

Существующий worker остаётся транспортом. Adapter должен уметь:

- получить role prompt и cwd;
- установить sandbox/permissions;
- вернуть raw event stream и final output;
- извлечь session/thread id;
- вернуть provider usage;
- отличать ошибку среды от результата инженерной работы.

Для Codex рекомендуется использовать:

```text
codex exec --json --output-schema <schema> -o <final.json> <prompt>
```

JSONL сохраняется целиком как run artifact. Событие `turn.completed` даёт
usage, а `final.json` проходит независимую schema validation.

Провайдер без native structured output обязан пройти внешний JSON validator.
Невалидный JSON является `run_protocol_error`, а не finding проекта.

### 12.2. Следующий этап

Для более тесной интеграции Codex можно добавить adapter на Python Codex SDK:

- start/resume thread;
- structured events без парсинга терминального текста;
- sandbox per role;
- устойчивое продолжение сессий;
- единый учёт usage.

OpenAI Agents SDK рассматривается позже, когда понадобится сложный граф
handoff между разными специалистами. Критический state machine всё равно
остаётся в PromptPilot, а не делегируется модели.

## 13. API

### 13.1. Workflow

```text
POST   /api/workflows
GET    /api/workflows
GET    /api/workflows/{id}
PATCH  /api/workflows/{id}
POST   /api/workflows/{id}/start
POST   /api/workflows/{id}/dispatch
POST   /api/workflows/{id}/gate
POST   /api/workflows/{id}/review
POST   /api/workflows/{id}/cancel
POST   /api/workflows/{id}/human-input
POST   /api/workflows/{id}/sync
POST   /api/workflows/{id}/history/import
GET    /api/workflows/{id}/rounds
GET    /api/workflows/{id}/rounds/{round_id}/runs
GET    /api/workflows/{id}/events
GET    /api/workflows/{id}/findings
GET    /api/workflows/{id}/artifacts
```

State-changing endpoints защищены существующей API-аутентификацией и
optimistic concurrency через `expected_version`. Импорт истории имеет отдельный
стабильный idempotency key; `sync` по определению повторяем. Универсальные
idempotency keys для остальных команд относятся к W2.

Планируемые после W1 endpoints: `pause`, `resume`, `metrics`, `export`.

### 13.2. CLI

```text
pp workflow create workflow.json
pp workflow import-history <id> history.json
pp workflow start <id> [--base-sha SHA]
pp workflow dispatch <id> executor|reviewer --file prompt.md
pp workflow sync <id>
pp workflow gate <id> PASS|FAIL|HUMAN_REQUIRED
pp workflow review <id> PASS|REVISION_REQUIRED|HUMAN_REQUIRED
pp workflow input <id> "решение" [--resume]
pp workflow cancel <id>
pp workflow show|rounds|events|findings <id>
```

`pause`, длительное `--follow` и article export остаются командами следующих
фаз.

### 13.3. UI и Telegram

Минимальная карточка workflow показывает:

- состояние и текущий round;
- активную роль/task;
- candidate SHA;
- гейты;
- открытые blockers;
- время, токены и стоимость;
- причины остановки;
- кнопки Pause, Cancel, Resume и Human input.

Telegram уведомляет только о `completed`, `awaiting_human`, `failed` и
превышении бюджета. Успешные промежуточные runs не создают уведомления по
умолчанию.

## 14. История и данные для статьи

### 14.1. Артефакты экспорта

```text
workflow-summary.json
rounds.csv
findings.csv
facts.json
timeline.md
metrics.json
article-source.md
```

`article-source.md` генерируется только из записей базы и проверенных
артефактов. Он не должен вводить причинно-следственные связи, которых нет в
evidence.

### 14.2. Полезные показатели

- общее число round и attempts;
- время от первого finding до закрытия;
- число findings, обнаруженных только независимым аудитом;
- доля `CLAIMED`, ставших `VERIFIED` или `DISPROVED`;
- reopen rate;
- blockers per round;
- tests и runtime scenarios per round;
- размер diff и число commits;
- стоимость и токены по ролям;
- число ручных вмешательств;
- время агента и время ожидания очереди;
- число сбоев среды отдельно от дефектов продукта.

### 14.3. Обезличивание

Перед публичным экспортом применяются правила:

- пути пользователей заменяются на `<USER_PATH>`;
- пароли, токены, connection strings и локальные адреса удаляются;
- ИНН, счета, имена и названия клиентов маскируются;
- клиентские дампы не включаются;
- короткие SHA можно сохранить, если репозиторий не публикуется;
- факты без достаточного evidence получают явную маркировку.

Исходная приватная история остаётся неизменной; публичный экспорт создаётся
отдельно и имеет собственный SHA.

## 15. Надёжность и восстановление

- Scheduler выбирает workflow/run атомарно, как существующий task claim.
- У workflow не больше одного активного перехода состояния.
- На старте worker восстанавливает runs, не имеющие heartbeat.
- Завершённый provider task можно повторно связать с run без повторного запуска.
- Каждая команда и callback имеют idempotency key.
- Создание следующего round выполняется в одной DB transaction.
- Потеря Telegram/Web UI не влияет на выполнение.
- После рестарта PromptPilot продолжает workflow с последнего committed event.
- Неопределённый результат provider не превращается в `PASS`.
- Отмена завершает дочерний task/process и фиксирует причину.

## 16. Безопасность

1. По умолчанию reviewer использует `read-only`.
2. Executor получает запись только в выделенный worktree.
3. `danger-full-access` разрешается только явной политикой workflow и в
   изолированной среде.
4. Секреты не записываются в prompts, events и stdout artifacts.
5. Destructive command guard действует независимо от provider.
6. Репозиторий, разрешённые cwd и writable roots задаются allowlist.
7. Команды гейтов задаются доверенной конфигурацией, а не JSON-ответом агента.
8. Агент не может изменить `max_rounds`, budgets или acceptance criteria.
9. Reviewer report считается недоверенным вводом и проходит schema validation.
10. Controlled archiver проверяет path allowlist перед любой записью/commit.

## 17. Пилотный workflow УТ10 → БП3

Пример конфигурации:

```yaml
schema_version: "1.0"
slug: ut10-bp3-u3-fix2
objective: >
  Закрыть блокирующие замечания U3-FIX-2 и получить независимо
  подтверждённую готовность этапов U3-0/U3-1 без повышения формального
  readiness сверх evidence.

repository:
  path: D:\Projects\exchange
  candidate_branch: checkpoint/u2-wip-20260824
  executor_worktree: persistent
  reviewer_checkout: ephemeral_read_only
  protected_paths:
    - build/UT10-BP3_v49.epf
    - docs/audit/UT10-BP3_AUDIT_LOG.md
  executor_denylist:
    - docs/audit/**
  audit_write_allowlist:
    - docs/audit/**

roles:
  executor:
    provider: codex
    sandbox: workspace-write
    prompt_template: prompts/ut10-executor.md
    output_schema: schemas/executor-report.schema.json
  reviewer:
    provider: codex
    sandbox: read-only
    prompt_template: prompts/ut10-reviewer.md
    output_schema: schemas/review-report.schema.json

limits:
  max_rounds: 6
  max_same_blocker_rounds: 2
  max_environment_retries_per_run: 3
  max_wall_time_hours: 24

gates:
  - id: git_clean
    kind: builtin.git_clean
    required: true
  - id: forbidden_paths
    kind: builtin.path_policy
    required: true
  - id: pytest
    kind: command
    command: python -m pytest -q
    timeout_seconds: 1200
    required: true
  - id: fleet_status
    kind: command
    command: python scripts/fleet_status.py --format markdown
    required: true
  - id: manifest_schema
    kind: json_schema
    schema: evidence/schema.json
    input_glob: evidence/ut10-bp3/*/manifest.json
    required: true
  - id: artifact_identity
    kind: builtin.artifact_hashes
    required: true

acceptance:
  required_review_scope:
    - single production core
    - canonical dump columns/rows
    - bank and cash semantic mapping
    - real dump full dispatch
    - settings and preview path
    - clean reproducible evidence cycle
  blocker_policy: zero_open
  high_policy: zero_open
  formal_readiness_max: R0
```

Точные команды сборки и runtime gates должны быть взяты из репозитория
`exchange` и закреплены в конфигурации после отдельной проверки. Агент не
должен конструировать их заново в каждом round.

## 18. План реализации

### Фаза W0 — схемы и event log

- Pydantic-модели workflow/round/run/finding/artifact/event;
- SQLite migrations;
- CRUD API и CLI read-only команды;
- append-only event writer;
- unit tests миграции и идемпотентности.

### Фаза W1 — state machine и ручной pilot

- **Реализовано 2026-08-25.**
- переходы состояний;
- связь workflow run с существующей task;
- ручной запуск executor/reviewer через API;
- limits и `awaiting_human`;
- crash recovery tests.

Дополнительно реализован импорт дооркестраторной истории. Он принимает только
терминальные раунды, требует стабильный idempotency key и сохраняет каждый факт
с явным статусом `CLAIMED`, `VERIFIED`, `DISPROVED`, `PARTIAL`, `INFERRED` или
`UNKNOWN`. Исторические раунды не расходуют бюджет новых автоматизированных
раундов. Формат описан в `docs/HISTORY_IMPORT_GUIDE.md`.

### Фаза W2 — автоматический executor/gate/reviewer loop

- генерация role prompts;
- JSON Schema outputs;
- Git/worktree policy;
- command и builtin gates;
- автоматическое создание следующего round;
- stagnation detection.

### Фаза W3 — UI, Telegram и статистика

- карточка workflow;
- timeline и findings;
- уведомления по терминальным/человеческим состояниям;
- token/cost/duration metrics.

### Фаза W4 — экспорт для анализа и статьи

- JSON/CSV/Markdown export;
- fact provenance;
- anonymization profile;
- воспроизводимый `article-source.md`.

### Фаза W5 — расширенные adapters

- Python Codex SDK adapter;
- разные модели/провайдеры по ролям;
- дополнительные специализированные reviewers;
- при необходимости Agents SDK handoffs/traces.

## 19. Критерии приёмки оркестратора

MVP считается готовым после следующих проверок:

1. Unit tests покрывают каждый разрешённый и запрещённый переход состояния.
2. Повтор одного completion event не создаёт второй round.
3. Рестарт worker между executor и reviewer продолжает тот же workflow.
4. Невалидный executor/reviewer JSON не считается инженерным verdict.
5. Reviewer физически не может изменить candidate checkout.
6. `PASS` на другом SHA отклоняется.
7. Failed required gate не позволяет запустить reviewer как успешную проверку.
8. Два одинаковых blocker без прогресса переводят workflow в
   `awaiting_human`.
9. Достижение `max_rounds` останавливает цикл.
10. Cancel завершает активный task и не создаёт следующий round.
11. Тестовый репозиторий проходит сценарий `FAIL → FIX → PASS` без действий
    человека.
12. История после сценария полностью восстанавливает prompts, SHA, результаты
    гейтов, findings, timestamps и usage.
13. Article export воспроизводим: одинаковая история даёт одинаковый контент
    после нормализации времени генерации.
14. Существующие одиночные PromptPilot tasks работают без изменений.
15. Пилот УТ10→БП3 проходит хотя бы один реальный автоматический цикл
    executor → reviewer → revision → reviewer.

## 20. Нерешённые решения перед реализацией

Следующие вопросы не блокируют W0, но должны быть зафиксированы до W2:

1. Хранить generated audit commits в candidate branch или в отдельной
   `wf/<slug>/audit` ветке.
2. Использовать ли один thread исполнителя между round или создавать новый при
   превышении порога контекста.
3. Как получать точный token/cost usage для каждого поддерживаемого provider.
4. Какие команды UT10 runtime gate безопасно выполнять автономно и какие базы
   должны быть только disposable.
5. Нужна ли подпись workflow exports и manifest отдельным ключом.
6. Какой anonymization profile применять для будущей публичной статьи.

Рекомендуемый выбор для пилота: долгоживущий executor thread, новый reviewer
thread на каждый round, audit в отдельном хранилище PromptPilot с опциональным
controlled commit в `docs/audit/**`, runtime только на disposable базах.
