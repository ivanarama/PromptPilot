# Review routing v1: проект wire-контракта

Это внутренний подготовительный контракт #151. Сейчас доступны внутренние
Python parser/reducer, согласованное чтение timeline и локальный SQLite-журнал
write-ahead receipts; публичных writer,
handoff/dispatch, нового CLI или включённого capability нет. Срез не завершает A
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
| publication | Полный report, PRIdentityV1, author identity, HEAD/epoch/anchor, depth, исторический бюджет/лимит, outcome, необязательный resume_id |
| lease | Root, active previous, UUID, context и отдельный purpose review-publication или technical-attempt |
| claim | Returned publication/active lease IDs и полный context hash |
| completion | Returned publication/canonical earliest claim/active lease IDs; завершённый REVIEW, а не разрешение на merge |
| attempt | Полный source tuple, series/previous/ordinal, один executor и UUID; canonical reservation расходует slot до dispatch |
| phase | Attempt/active lease/context и assigned, handoff-done, started, fix-done либо ambiguity barrier |
| resume | Точная canonical engineering parking completion, текущий HEAD, previous attempt или null при нулевом бюджете, причина |

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
Наблюдавшийся returned ID сохраняется отдельно от подтверждения: последующий
GET не может забыть противоречивый ID. Внутренний Transport делает один POST
либо восстанавливает receipt чтением; он требует внешнего полного stage guard,
сам его не реализует и пока не подключён к командам next/complete.

Внутренний preflight сохраняет проектируемые три параметра technical_handoff:
enabled, protocol и max_extra_attempts. Он не добавляет defaults в legacy config,
не меняя digest существующих leases. Для совместимости он сравнивает полный
сырой набор файлов четырёх canonical stage directories с reader_sha256 полного
checker-контракта; только marker в одном SKILL недостаточен. Формат tree digest:
UTF-8 `pp-review-routing-consumer-tree-v1` + LF, затем для каждого файла в порядке
UTF-8 path: relative path + NUL + lowercase SHA-256 содержимого + LF. Генерируемые
__pycache__/.pyc/.pyo не входят; symlinks и control characters запрещены.
Эта negotiation также ещё не подключена к публичным командам и требует
соответствующего Go-контракта/общих fixtures до включения.

До будущей записи/dispatch обязательны полные public gates: совместимость всех
читателей, trusted actor/immutability, coherent identity/epoch/lifecycle/labels,
текущее владение returned ID/UUID, effective config и конечный budget. Неопределённый
POST/spawn требует durable ambiguity barrier и exact read-back, а не повтора.
Report evidence остаётся данными, не shell-командами. Author handoff не попадает
в executable FIX. Новая версия не расширяет действующую execution policy и
не создаёт ship, human decision или успешный legacy review marker.
