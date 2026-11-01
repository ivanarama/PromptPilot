# Срез B: межстадийная приёмка technical handoff

Рабочая граница PromptPilot #152 / OneBase #1909 после общей приёмки A:
PromptPilot main `fcbb725bbdcc93a828b932f400a43873014c3d9e`, OneBase main
`8fd157a9dad989d3e4c9d5528e815c8876baf533`.

Кодовый срез требует совместной приёмки точных версий и не является разрешением
обновить установку или включить рабочий флаг.

- Используются принятые parser, конечный lifetime budget, единственный FIX,
  post-push recovery, settlement и механизм парковки цели #144.
- Добавлены положительные потребители typed REVIEW в MERGE/TAIL без создания
  синтетического legacy-заключения. Technical/engineering handoff не является
  разрешением на merge или создание хвоста.
- Сохраняются полный proof, identity, trusted ship, CI точного HEAD, свежий main,
  HMAC/expiry, compare-and-merge, single-flight и обработку неизвестного исхода.
- Приёмка проверяет публичные transport/CLI: повтор next, два новых HEAD, переименование
  blocker, исчерпание бюджета, другой eligible PR, ABA, edit/delete и старый
  third-round human handoff.
- Общая приёмка требует точной пары исходников, настоящего Go checker,
  независимого ревью и всех пяти/восьми обязательных CI.

Dashboard, обновление установленной пары, включение и наблюдение пилота относятся
к C (#153 / OneBase #1910). Рабочие настройки и службы здесь не меняются.

## Положительный source

`inspect-routing-positive --number M --stage merge|tail` только читает полный
typed snapshot и исходный report. `source` содержит настоящие typed IDs и
GraphQL node IDs, полную identity/автора и epoch. Команда не создаёт legacy
заключение и возвращает `mutation_authorized:false`. TAIL использует immutable
publication ID/time и SHA-256 канонического JSON/LF каждого structured tail item,
сохраняя общий per-item lease, постоянный dedupe ref и запрет повторного create.

При совместимом `positive_review_contract` обычные `next/complete merge`
поддерживают только точный положительный typed proof и `CLEAN` ветку. Новый
contract требует отдельного `pp:positive-review-v1-consumer` marker каждого из
четырёх фактических entries; совпадения старых A tree hashes недостаточно.
Новый
purpose `typed-clean-merge-v1` подписан HMAC, ограничен 30 минутами и связан с
source/config/base/четырьмя reader trees. Проверяются trusted ship, первая
исполняемая цель, single-flight, current main в HEAD и успешные обязательные
GitHub Actions checks именно этого HEAD. Неопределённый merge PUT не повторяется:
постоянная source-bound send-запись находится рядом с signing key, отдельно от
восстанавливаемого комментарного кэша. Все live-гейты повторяются после writer
admission и до мутации. Подтверждённый merge использует общую native cleanup.

Typed BEHIND не получает legacy carry: нужен свежий content HEAD и независимый
REVIEW. Открытый PR с неопределённым send требует инженерного readback, а не
второй отправки. Служебный worker допускает положительный typed MERGE только с
явным `direct_complete:true` и согласованными signed purpose/outer target;
модельный fallback ему не предоставляется. Настройка
`review_routing_protocol:review-routing-v1` остаётся транспортом REVIEW/FIX,
а не способом включить MERGE.
