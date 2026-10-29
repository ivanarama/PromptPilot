# Проверка отчётов REVIEW: подготовительный срез A1 плана 195

Это первая часть PromptPilot #151, связанная с OneBase #1908. Она предлагает
общий формат типизированного отчёта и проверяет его локально. Она не завершает
срез A принятого плана и не включает техническую передачу REVIEW → FIX.
Установленный выпуск, scheduler и данные пользователя не меняются.

## Инвентаризация существующего маршрута

Основание — main `5b31b8b9d469155ded04249b1e4f30a99a51416a`, включающий
независимо принятую в PR #144 парковку одной цели и подписанный FIX-маршрут
PR #141. Ни один из них не включает техническую передачу по новому отчёту.

| Точка входа | Текущий контракт и ограничение для следующего среза |
|---|---|
| `project_pipeline.run` | `next` принимает review/merge/fix, `complete` — review/merge/merge-cleanup. Подписанный `next fix` из PR #141 требует явного opt-in и совместимого проектного consumer. Он не читает новый технический outcome; отдельную передачу REVIEW → FIX ещё нужно согласовать и реализовать. |
| `project_pipeline.next_review` | При depth >= 2 возвращает fallback с причиной third review round requires human-escalation rules; технический outcome не выдаёт. |
| `project_pipeline.format_review` / `complete_review` | Legacy blockers — строки. Completion проверяет lease, identity, target/global gate и публикует review/claim/label/completion; новый report должен получить отдельный совместимый путь и привязку hash. |
| `project_pipeline.content_review_target_gate` / `review_gate` | Текущие HEAD/epoch/routing и allowlist остаются mutation gates, их не заменяет offline validator. |
| `fallback_handoff.gate` | HMAC/expiry и свежий допуск точной цели должны сохраняться для fallback; новый outcome сам по себе полномочий не добавляет. |
| `pipeline_insights.execution_route` | Выбирает инструмент/полный проектный prompt; для opt-in FIX привязывает точную цель и TaskAttempt к подписанному handoff. Техническую передачу по новому отчёту нельзя добавить лишь новым label в health, без совместимого публичного dispatch. |
| `pipeline_item_holds.register_review_target`, `prepare`, `record` | Уже сохраняют точную цель REVIEW и локальную парковку одной работы. Использовать существующий механизм, не создавать вторую парковку для #144. |

Эта инвентаризация не доказывает готовность durable technical intent/recovery:
они ещё не реализованы. Merge integration owner не становится владельцем
технической доработки, а локальная парковка не заменяет общую GitHub chain.

## Публичная команда

```console
pp pipelinectl validate-routing-report --report report.json --head aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

Команда не требует `pipelinectl.json`, не обращается к GitHub и не запускает
health. Она проверяет формат, совпадение с явно указанным SHA и выводит
категорию, канонический JSON record и его SHA-256. Ответ всегда содержит
`authority: unverified-input`, `mutation_authorized: false`,
`handoff_enabled: false`. SHA аргумента и текст evidence предоставлены автором
файла: команда не доказывает их происхождение или успешное воспроизведение.
Классификация не является allowlist для FIX или основанием для публикации proof.

Новый формат явно отклоняется существующим `complete review` до обращения
к GitHub. Обычный отчёт без `routing_version` сохраняет прежний контракт.
Новый capability, feature flag и label outcome пока не публикуются.

## Предлагаемый формат для парной приёмки

Полный набор верхних полей: `routing_version: review-routing-v1`, `head`,
`change`, непустой массив `checks`, массивы `blocking` и `tail`.
Каждый blocker содержит ровно `id`, `kind`, `summary`, `evidence`.
`id` обозначает нарушенный инвариант, а не hash формулировки замечания;
он уникален в отчёте, соответствует `[a-z][a-z0-9._-]{0,63}`. Будущий reader
должен сохранять идентичность серии при смене SHA и формулировки.

| kind | Поля evidence |
|---|---|
| technical | entrypoint, environment, command, expected, actual, result, invariant |
| product | options: минимум два объекта number/behavior/consequences; recommendation: number/reason |
| safety_exception | boundary, requested_exception |
| insufficient_data | missing, verification |

Текстовые поля непустые; номера продуктовых вариантов уникальны, целые,
от 1 до 2147483647. Рекомендация выбирает существующий вариант. Поля `tail`
повторяют минимальный существующий формат: `kind: issue|discard`, `text`,
обязательный для issue `title`. Неизвестные поля, версии и категории, в том
числе `tool_error`, отклоняются. Operational error не является завершённым
содержательным REVIEW. Категория никогда не выводится из прозы.

Одна явная категория даёт одноимённую описательную классификацию; несколько
дают `mixed_blockers`, пустой список — `no_blockers`. Даже чистый technical
в этом срезе не разрешает назначение или исполнение. Смешанный отчёт никогда
не сворачивается в technical.

Общие примеры находятся в
[`tests/fixtures/review-routing-v1.json`](../tests/fixtures/review-routing-v1.json).
Они включают кириллицу, все четыре категории, смешанный и пустой отчёт.
Формат предлагается для последующего OneBase consumer; его поддержка там
этим PR не заявляется. Данные примеров синтетические, не являются review proof
или подтверждением актуального дефекта упомянутой функции.

Каноническая запись — JSON с лексикографически отсортированными ключами,
`ensure_ascii=false`, разделителями `,` и `:`, без пробелов, с финальным LF.
Порядок массивов и точный Unicode-текст сохраняются без нормализации.
Кодировка строгая UTF-8 без BOM. SHA-256 включает весь отчёт, включая HEAD,
категории, evidence, checks и tail. Входной JSON ограничен 256 KiB;
повторяющиеся ключи и non-finite значения запрещены. Чрезмерная глубина JSON
возвращает обычный структурированный отказ команды. Hash не удостоверяет автора.

## Что требуется до рабочего среза A

1. Согласовать предложенные fixtures с OneBase #1908 и добавить reader в обеих
   частях. Привязать report hash к review/earliest claim/completion ID,
   repository/ref identity и server epoch. Проверять доверенного автора,
   edit/delete и полную стабильную timeline, а не верить файлу отчёта.
2. Реализовать совместимые `next/complete`, один executor, durable intent/done,
   непрерывную серию и конечные две дополнительные попытки; смена HEAD,
   blocker text или intent не сбрасывает бюджет. После исчерпания — инженерная
   парковка. Принятую в PR #144 локальную парковку не реализовывать заново.
3. Сохранить флаг off по умолчанию и legacy third-round fallback. Проверить
   публичный REVIEW → completion → ровно один FIX через fake GitHub transport,
   две реплики, crash в каждой фазе, поздний hold и смену actor/HEAD/proof,
   fork dispatch без запуска кода на машине с credentials. MERGE/TAIL должны
   отклонять неподдерживаемые proof; ship/CI/HEAD/epoch/CAS остаются обязательны.
4. После merge согласованной пары записать совместимые SHA в #151/#1908,
   выполнить парную приёмку и отдельно выпуск. Этот подготовительный PR
   ссылается на #151, но не закрывает её и OneBase #1836.
