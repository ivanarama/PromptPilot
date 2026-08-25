# Автономный workflow: настройка и эксплуатация

Статус: W2 MVP, schema version 1.

PromptPilot поддерживает два режима. Старые workflow остаются ручными. Для нового
механизма оператор явно включает `automation.enabled` в UI или API; после этого
оркестратор самостоятельно передаёт управление по цепочке:

```text
executor → deterministic gate → independent reviewer
    ↑                                  │
    └──── REVISION_REQUIRED ───────────┘
                         PASS → completed
```

## Настройка в UI

Откройте `Workflows`, выберите процесс и раскройте блок
`Настройки автономного workflow`. Доступны:

- код, название и проверяемая цель текущего бизнес-этапа;
- лимит новых раундов;
- отдельные provider/model/effort/timeout и prompt template для исполнителя и
  аудитора;
- отдельный worktree и `skip_permissions` для исполнителя;
- команды deterministic gate, выполняемые по одной строке;
- независимые переключатели автоматического запуска ролей, gate, применения
  вердикта и создания следующего раунда.

Сохранение настроек активного, но не терминального workflow разрешено. Изменение
цели, репозитория и ветки после старта запрещено, чтобы не менять scope незаметно
для уже созданной истории.

Для `herdr-session` необходимо задать устойчивый `herdr_target`. Для полностью
автономной работы предпочтительнее headless-provider (`agy`, `codex`, `claude` и
т. п.), которому не нужна уже открытая пользовательская панель.

## Контракт аудитора

Встроенный prompt требует две строки:

```text
AUDIT_FINDINGS_JSON: []
AUDIT_VERDICT: PASS
```

Допустимые вердикты: `PASS`, `REVISION_REQUIRED`, `HUMAN_REQUIRED`. Findings —
JSON-массив объектов с `fingerprint`, `severity`, `category`, `title`, `status`
и `payload`. Если при `REVISION_REQUIRED` массив пуст, PromptPilot сохраняет
одно агрегированное замечание уровня `medium`, а полный отчёт остаётся в run.

Невалидный контракт не угадывается по свободному тексту: workflow переходит к
человеку с событием `automation.paused`.

## Deterministic gate

Команды запускаются кодом PromptPilot в `repository_path`, а не LLM-аудитором.
На Windows используется Windows PowerShell, на POSIX — `/bin/sh`. Ненулевой exit
code или timeout дают `FAIL`; ошибка запуска среды даёт `HUMAN_REQUIRED`.
Последние 4000 символов вывода каждой команды сохраняются в append-only event.

Если список команд пуст, transport gate проходит с честной пометкой
`Настроенных deterministic-команд нет`. Это не является доказательством
функциональной готовности — нужные команды должен задать владелец workflow.

## Автоматические остановки

Человек получает мяч, когда:

- аудитор вернул `HUMAN_REQUIRED` или нарушил машинный контракт;
- исчерпан `max_rounds`;
- provider/session настроены некорректно;
- semantic conflict не позволяет принять PASS (например, остался открытый
  blocker/high finding);
- команда gate не может быть запущена из-за среды;
- превышен защитный лимит внутренних переходов state machine.

После перезапуска worker выполняет sync незавершённых workflow и продолжает
разрешённый автоматический переход. Все переходы используют optimistic version
и idempotent task/run projection.

## Экспорт для анализа и статьи

В карточке есть кнопки `Экспорт JSON` и `Экспорт Markdown`. Endpoint:

```text
GET /api/workflows/{id}/report?format=json
GET /api/workflows/{id}/report?format=markdown
```

JSON содержит конфигурацию, раунды, attempts, findings, artifacts, append-only
timeline и вычисленные показатели: количество ручных/PromptPilot-раундов,
попыток ролей, PASS/FAIL gate, revision/PASS аудитов, пауз, провайдеров, моделей,
время агентов и полное elapsed time. Это факты из SQLite; утверждения старых
агентских отчётов остаются отдельно в raw run output/events.

## Текущие границы W2 MVP

- физический read-only checkout аудитора ещё не обеспечен; запрет записи задан
  ролью и отсутствием `skip_permissions`;
- список следующих бизнес-этапов пока не является графом: UI настраивает текущий
  этап, а его смена остаётся решением владельца;
- автоматический merge/release не выполняется;
- gate-команды выполняются синхронно в worker-потоке;
- токены и стоимость берутся общей подсистемой PromptPilot и пока не связываются
  с workflow report как отдельная бухгалтерия.

