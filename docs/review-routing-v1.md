# Review routing v1: проект wire-контракта

Это внутренний подготовительный контракт #151. Сейчас доступны внутренние
Python parser/reducer, согласованное чтение timeline и локальный SQLite-журнал
write-ahead receipts. Внутренний coordinator публикации восстанавливает
publication/claim/completion, выбирает canonical owner, renewal/takeover и
проверяет lifetime/resume/post-push ограничения до нового root. Он требует
повторной semantic admission по последнему WRITE snapshot после journal commit
для каждой фазы: свежие root, canonical earliest claim и active lease/owner
должны по-прежнему совпадать перед POST;
уже начатая resuming publication не может заново потребить тот же grant в другой
эпохе. Known exact receipt восстанавливается чтением без повторного POST. Он требует
внешний полный stage guard, сам не аутентифицирует signed request и не меняет
метки. Draft-адаптер подключает его к существующим `next review` и `complete review`
за явным opt-in и полным контрактом четырёх читателей. Мост FIX ещё не завершён,
возможность выключена и не совместима с принятой парой OneBase. Срез не завершает A
плана OneBase195, не снимает hold #1908 и не даёт полномочий на FIX или MERGE.

`tests/fixtures/review-routing-wire-v1.json` фиксирует предлагаемые полные
UTF-8/LF записи, hashes и историю двух дополнительных попыток. Go-совместимость
нужно отдельно подтвердить читателем OneBase и парной публичной приёмкой.

Запись состоит из точного marker `pp:review-routing-v1` с kind и SHA-256,
затем canonical JSON и завершающего LF. Ключи JSON отсортированы; числа только
целые; server IDs — положительные decimal strings, UUID — canonical UUID4.
SHA-256 marker коммитит все JSON bytes, включая завершающий LF. Report hash
сохраняет A1 canonical serializer без Unicode-нормализации. Полное тело ограничено
60000 UTF-8 bytes: превышение отказывает до публикации, evidence не обрезается.

| Kind | Связь |
|---|---|
| publication | Полный report, PRIdentityV1, author identity, HEAD/epoch/anchor, audit baseline, depth, исторический бюджет/лимит, outcome, необязательный resume_id |
| lease | Root, active previous, UUID, context и отдельный purpose review-publication или technical-attempt |
| claim | Returned publication/active lease IDs и полный context hash |
| completion | Returned publication/canonical earliest claim/active lease IDs; завершённый REVIEW, а не разрешение на merge |
| attempt | Полный source tuple, series/previous/ordinal, один executor и UUID; canonical reservation расходует slot до dispatch |
| phase | Attempt/active lease/context и assigned, handoff-done, started, fix-done либо ambiguity barrier |
| resume | Точная canonical engineering parking completion, текущий HEAD, previous attempt или null при нулевом бюджете, причина |

В предлагаемый publication добавлен обязательный `audit`: snapshot_sha256,
last_edge_cursor, last_edge_node_id и base_ref_oid. Hash коммитит полный typed
header и точный prefix timeline, кроме отдельно проверяемого обычного продвижения
baseRefOid и служебного updatedAt. Cursor и node ID границы сохраняются вместе;
для пустого prefix оба явно null. Public gate всё равно обязан доказать HEAD
anchor, подпись исходного request и текущую пригодность цели: пустой prefix или
один совпавший hash не выдают полномочий.

Внутренний baseline reader сохраняет весь prefix и допускает в suffix только
immutable trusted publication/claim/completion/renewal этого полного context.
Произвольные pp-маркеры, комментарии, labels/lifecycle events и другой context
не выбрасываются ради совпадения hash. Canonical reducer, actor/identity/epoch,
current policy, returned-owner и последний TTL-check после медленной работы
выполняются отдельным StageGuard. Набор canonical bytes baseline
ограничен 8 MiB; размер считается при потоковом hash, без полного JSON-буфера.
Это обновление предложенного v1 и goldens внутри Draft, не обещание совместимости
со старым проектом контракта или установленным OneBase.

Context roots исключает только UUID владельца; разные report/source/executor
не считаются эквивалентными конкурентами. Выигрывает earliest(created_at,
numeric server ID). Renewal разрешён тем же UUID строго в последние 5 минут
30-минутного lease; takeover после expiry требует нового UUID и никогда не
доказывает остановку прежнего исполнителя. Устаревшая ветвь не оживает.

Бюджет относится к пожизненному repository node ID + PR node ID/number.
HEAD, blocker ID, текст и локальный cache не создают новую серию. Учитываются
канонические reservations, включая остановленные/неуспешные. Удалённая или
неполная история означает неизвестный бюджет и отказ, даже если не осталось
ни одного root. Исторический publication.max_attempts не является текущей
настройкой: next_attempt_admission отдельно принимает явный текущий лимит,
проверяет последний source, расходование slot и не выдаёт mutation authority.
Уменьшение лимита не удаляет уже потраченные slots.

Новый canonical REVIEW после attempt запрещён до fix-done, даже если следующей
попытки не будет. Новый HEAD связан с завершённой post-push фазой; явно разрешённый
engineering resume может выбрать другой точный HEAD после завершения прежнего
FIX. Resume ссылается на действующую engineering parking completion и последнее
lifetime состояние. Его потребляет один publication через resume_id; он не
пополняет бюджет. Insufficient-data parking с нулём attempts тоже возобновима.
Resume незапаркованной цели, старой parking или другого HEAD отказывает.
После завершённого возобновлённого REVIEW прежняя parking закрыта: она не
выдаёт ещё один resume и не мешает обычному следующему REVIEW после FIX.
Незавершённый возобновлённый publication сохраняет видимыми parking и
потреблённый grant для recovery, не для нового аудита.

Локальный журнал связывает owner/phase с точными body и PR. Pending/ambiguous
запрос блокирует новый owner/phase того же lifetime PR до точного read-back.
В journal v2 победитель prepare получает отдельный generation-token, которого
нет в get/recovery inventory. До последнего guard он фиксирует attempting.
Только прямая ветка этого вызова, которая получила отказ guard **до первого
POST**, может CAS-переходом записать not-sent. Новое поколение после такого
отказа снова проходит все серверные dedup/semantic/auth gates. Crash после
attempting, неизвестный commit, любая ошибка POST/response/receipt и старые
pending при миграции v1 сохраняют барьер. Отсутствие видимого комментария
никогда само не означает not-sent; потраченные budget slots не освобождаются.
Наблюдавшийся returned ID сохраняется отдельно от подтверждения: последующий
GET не может забыть противоречивый ID. Внутренний Transport делает один POST
либо восстанавливает receipt чтением; он требует внешнего полного stage guard,
сам его не реализует; подключение к next/complete выполняет отдельный адаптер.
Guard получает явный write-флаг: подтверждение уже существующего immutable
receipt может быть чтением после истечения write lease; новый POST всегда
повторяет актуальные права/TTL после write-ahead commit. Потеря локального
журнала не дублирует surviving exact own server phase.

Внутренний request serializer отдельно фиксирует HMAC-purpose audit либо
publication-recovery, полный PR/author/epoch/audit scope, текущие policy/reader
hashes, конечный budget/limit, срок, UUID/nonce и task reservation. UUID/nonce
создаёт сервис; report и публичный owner UUID их не выбирают. Другой task/retry
не может использовать reservation, а pipeline task не может взять unreserved
manual request. Recovery получает новый UUID и сохраняет canonical audit binding;
сам serializer не доказывает, что переданный ему root канонический.

`require_current` проверяет локальные scope/config/replica/attempt ограничения
и читает часы после сериализации/digest-проверок. Это не GitHub proof, не
обновление DB reservation и не полный публичный stage guard. Подпись не заменяет
свежие внешние гейты; parser/validator не создают ключ или DB. Ни нового CLI,
ни реального public writer, ни включённого capability этим модулем не добавлено.

Внутренний `StageGuard` связывает этот request с точным publication payload.
Для каждой фазы он заново загружает конфигурацию/checker, проверяет пользователя
GitHub, два полных согласованных чтения PR, author/identity/epoch, audit prefix
и канонический root. Обычное продвижение base подтверждается compare от точного
подписанного SHA; другой base не маскируется исключением поля из hash.
Обновление локальной task reservation предшествует финальной проверке срока.
После journal/reducer/exact matching Transport вызывает отдельный one-use
`final_before_post`: после ожидания renewal заново читает actor/checker и полную
серверную историю, повторяет semantic admission фазы. Затем одним локальным
снимком проверяет текущую running-попытку, cancel и точную живую reservation;
проверяет конфигурацию/четыре дерева обработчиков и сроки request/server lease.
Known receipt после expiry остаётся read-only recovery. Этот компонент используется
Draft-адаптером next/complete, но сам не выбирает цель и не назначает FIX.
При последующем изменении HEAD, hold или закрытии PR guard может подтвердить
только уже видимое точное тело receipt в проверенной полной истории. Для этого
сохраняются конфигурация/совместимость, текущий GitHub actor, author и вся
идентичность PR, кроме HEAD; полная история проверяется через `verified_depth`.
Такой READ не выдаёт WRITE ticket. Невидимый результат POST остаётся ambiguous;
продолжение незавершённой публикации на старом HEAD запрещено.

`verified_depth` считает завершённые REVIEW по полной истории PR: legacy
review/claim/completion должны образовать канонический proof в своём серверном
префиксе HEAD/epoch; повторный completion того же review не добавляет круг.
Typed proofs берутся из полного reducer, а объявленный publication.depth должен
совпадать с числом проверенных завершений до его root. Новый HEAD не обнуляет
историю; отсутствующие references, удаление, edited trusted history и неверный
epoch отказывают. Epoch/anchor typed root проверяются по реальному серверному
префиксу при публикации и при completion; смена HEAD/epoch между фазами запрещена.
Legacy и typed completion одной HEAD/epoch не считаются двумя кругами: конфликт
отказывает. Значение review_depth из health само proof не заменяет.
Прежние wire/reducer goldens показывают сериализацию и reduction, но не являются
публичной приёмкой issuer или доказательством двух предшествующих legacy кругов.

Внутренний preflight сохраняет проектируемые три параметра technical_handoff:
enabled, protocol и max_extra_attempts. Он не добавляет defaults в legacy config,
не меняя digest существующих leases. Для совместимости он сравнивает полный
сырой набор файлов четырёх canonical stage directories с reader_sha256 полного
checker-контракта; только marker в одном SKILL недостаточен. Формат tree digest:
UTF-8 `pp-review-routing-consumer-tree-v1` + LF, затем для каждого файла в порядке
UTF-8 path: relative path + NUL + lowercase SHA-256 содержимого + LF. Генерируемые
__pycache__/.pyc/.pyo не входят; symlinks и control characters запрещены.
Эта negotiation подключена к Draft-командам; принятый Go-контракт не поддерживает
её и запрещает включение до завершения всей цепочки и парной приёмки.

При ON адаптер считает реальные legacy/typed proof до прежнего барьера третьего
круга. Первые два обычных круга сохраняют старый формат; третий выдаёт новый audit
request. Незавершённый current root выдаёт `action=recover` с новым UUID и прежним
report/audit/budget; report при recovery не принимается. Незавершённая старая
публикация и невидимый результат POST блокируют новый audit. Same-HEAD completion
возвращает wait. Эти проверки не заменяют ещё не завершённый мост к FIX.

Очередь исполнителя дополнительно объявляет transport-версию
`execution.review_routing_protocol=review-routing-v1`: даже одна реплика получает
точный scheduler task/attempt и SQLite target reservation. Исполнитель проверяет
подписанный request и его совпадение с этой задачей. `recover` выполняет
`complete review --lease-file ...` непосредственно в допущенной области GitHub,
без provider/model и без нового report. Отсутствие версии, чужая reservation или
неизвестный результат не переводятся в свободный skill fallback. Эта версия
очереди ещё не включена в рабочей конфигурации.
Завершение каждой новой фазы повторно проверяет точное присутствие PR/HEAD/depth
в свежем content allowlist, а подтверждение уже существующего receipt остаётся
READ. Внешние action/protocol сверяются с протоколом и purpose подписанного
request; удаление внешнего protocol не может отправить recovery на новый audit
модели. Если после потерянного ответа completion стал виден уже на следующем
HEAD, `next` подтверждает только точное тело и известный returned ID в локальном
журнале по полной доверенной истории. Невидимая запись, конфликт ID или другой
GitHub actor сохраняют барьер и не запускают новый audit.

Внутренний `OneUseStart` подготавливает последнюю границу будущего pipeline FIX.
Он создаёт отдельную operational-таблицу только при явном использовании, в том же
SQLite, где атомарно проверяет running task, started_at, cancel и точную живую
reservation. До финального read-only guard и одного factory-call durable
фиксируется spawn-attempting. Неизвестный результат, crash или новая попытка
задачи не дают второго запуска; даже FileNotFoundError не включает старый
spawn-retry. Только прямой отказ guard до factory может дать not-started для
контролируемого повтора того же неизменного scope. Пауза выдачи не отменяет уже
claimed задачу. Этот компонент пока не подключён к настоящему запуску FIX и не
доказывает signed FIX-start, server started, author handoff или post-push.
Для авторского исправления reducer допускает fix-done после handoff-done без
фиктивного pipeline started/task_id. Для pipeline started по-прежнему обязателен.
Сам reducer проверяет только историю: author coordinator ниже доказывает
назначение; новый HEAD, transition и source trailer пока не подключены. Прежние предлагаемые
wire-векторы с optional авторским started сохраняются как структурная история,
но авторский coordinator не должен выдумывать такой запуск.

В частичном claimed FIX bridge `next fix` при явном ON выбирает отдельный
source-bound путь до legacy parallel FIX. Он использует уже running task,
started_at и точную headless reservation, без enqueue и изменения timeout.
Первые два legacy REVIEW и старые signed leases сохраняют свои bytes. Запрет
legacy parallel FIX при replicas<2 остаётся прежним; single-lane относится только
к новому явно versioned пути. Перед выдачей admission проверяются полный typed
stable history, последний canonical exclusively technical source, author/fork/
branch/HEAD/epoch, lifetime slot и текущий конечный лимит. Source сам по себе не
означает eligibility.

Для этого частичного пути checker дополнительно обязан вернуть точный
`fix_bridge_contract={"protocol":"claimed-fix-v1","snapshot":"complete",
"purposes":["author-handoff"]}` и полный `technical_fix_candidates`. Каждая цель
содержит ровно stage=fix-pr, number, head и полный canonical source tuple.
Это контракт тестового совместимого peer, не утверждение поддержки OneBase29a.
Все четыре reader tree hash по-прежнему обязательны. Наличие
`technical_fix_execution` явно отклоняется: pipeline policy и transport ещё не
реализованы и не могут стать неявным author→pipeline fallback.

Signed `review-routing-v1-fix-admission` связывает весь immutable attempt root,
source, executor, полный PRIdentity/author, config/readers/current budget,
task_id+started_at, reservation/token/ownership kind, nonce и ограниченный TTL.
Его отдельный FixGuard не ослабляет REVIEW StageGuard. `complete fix` публикует
canonical attempt через прежний exact-body/returned-ID journal. Затем он один
раз добавляет связанного автора через assignee API и проверяет authenticated
GET `/pulls/{number}`: полный REST PRIdentity и `assignees[].node_id` должны
совпасть с подписанным PR и author node ID. Ответ POST сам не считается доставкой.
Другие assignees не снимаются. После проверенного readback появляются assigned
и handoff-done, без started и fix-done.

Отдельная strict-schema таблица `technical_fix_assignments_v1` в actual scheduler
DB фиксирует immutable intent до assignment API. Только непосредственно создавший
root вызов может начать новое назначение. Recovery восстанавливает видимое
назначение чтением; отсутствие/неопределённость/ручной unassign/потеря локальной
записи не разрешают новый POST. Поздний hold/actor/identity/reader/config/cancel,
смена task occurrence или reservation отказывают до следующей записи. Renewal
проверяет точную живую reservation и running task внутри BEGIN IMMEDIATE с часами
после DB wait, не возрождая истёкшую запись. Последний one-use write ticket повторяет
task/reservation, config/readers и TTL после journal/SQLite waits.

Worker распознаёт signed FIX purpose до generic dispatch независимо от внешнего
action/protocol. Только явный versioned local headless transport может завершить
administrative author handoff; remote/detached/worktree/herdr отказывают. При
успехе сохраняется отдельный verdict «ПЕРЕДАНО АВТОРУ», не «ГОТОВО». Провайдер не
загружается и не запускается; wake_after_success и новый substantive REVIEW не
выдаются. Admission нельзя заменить progress/start-purpose, legacy lease или
invalid HMAC. Progress schema зарезервирована, но operational progress writer
не подключён; start purpose явно отклоняется. Токены не расширяют полномочий.

Reader/reducer теперь понимают отдельный trusted `settlement`: exact attempt/
context/source, ожидаемые barrier/started IDs, observed HEAD, reason, явный
executor_outcome и evidence hash. Author-unassigned, pipeline-stopped и not-started
не взаимозаменяемы. Settlement после успеха, с чужим receipt/owner или с последующей
старой phase отказывает. Это только историческое чтение доверенного issuer:
публичный writer, который независимо доказывает остановку/отмену, пока отсутствует.
Expiry, отсутствие PID, exit code и текст агента не создают settlement автоматически.
Slot остаётся потраченным, fix-done не появляется. Точный settlement становится
engineering parking ID. Один explicit resume на новый HEAD допускает один REVIEW;
same-HEAD rebaseline, неверный parking/previous и повторное потребление отказывают.
После завершения resumed REVIEW parking закрывается, lifetime budget сохраняется.
Новые vectors находятся отдельно в review-routing-settlement-v1.json; прежние
wire vectors и expected legacy bytes не переписаны.

При OFF, включая удаление technical_handoff из config, legacy next REVIEW/FIX
перед handoff читает историю выбранной PR. Trusted routing marker, редактирование
доверенного комментария, удаление либо запись уже существующего локального
v1-журнала сохраняют read-only fence, даже если тело marker исчезло. Изменённая
история не доказывает отсутствия v1. Для неизменённой полной истории без v1 и
локальных транзакций сохраняются прежние результаты и сериализация. OFF не
открывает reader trees, не создаёт и не мигрирует journal; проверка существующего
journal идёт только на чтение. Неопределённая история требует инженерной сверки.

Типизированные tokenless `wait`/`empty`/`error` — ответы без права на mutation.
Worker сохраняет для них обычный маршрут ожидания/ошибки без декодирования
несуществующего audit-token. Подмена outer action у подписанного lease по-прежнему
отказывает; read-only ответ не разрешает запуск модели или публикацию.

Оставшаяся граница A151 существенна: нет operational progress/start issuer,
policy-bound pipeline coordinator и canonical started; OneUseStart всё ещё не
подключён к настоящей worker factory, server heartbeat и post-push completion.
Нет публичного verified settlement/resume writer и проверки author push/source
trailer. Полный положительный pipeline путь, его factory/crash/retry matrix и
actual accepted OneBase/четыре peer readers требуют отдельной парной приёмки.
Author CLI/worker и settlement reader tests не закрывают эти пункты. Флаги остаются
OFF; этот partial slice не является завершением или приёмкой всего A151.

До будущей записи/dispatch обязательны полные public gates: совместимость всех
читателей, trusted actor/immutability, coherent identity/epoch/lifecycle/labels,
текущее владение returned ID/UUID, effective config и конечный budget. Неопределённый
POST/spawn требует durable ambiguity barrier и exact read-back, а не повтора.
Report evidence остаётся данными, не shell-командами. Author handoff не попадает
в executable FIX. Новая версия не расширяет действующую execution policy и
не создаёт ship, human decision или успешный legacy review marker.
