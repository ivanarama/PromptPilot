# Установка наблюдения Source для OneBase FIX на Mac

Обычный FIX сначала проверяет, можно ли использовать уже опубликованный снимок очереди. Снимок годится только при свежих данных проверяющего, действующем бюджете и совпадении выбранного PR и HEAD. Когда нужен полный обход, исполнитель один раз регистрирует причину до запуска Native. Отдельное позднее наблюдение ждёт подходящую публикацию в пределах оставшегося времени задачи.

## Что нужно исполнителю

Нужны macOS, Python 3.10+, checkout PromptPilot этой версии с установленными зависимостями, отдельный checkout OneBase и обычная рабочая авторизация GitHub CLI и Codex для аккаунта исполнителя. Для чистой станции соберите из выбранных ревизий публичные инструменты. Выполняйте команды в новой рабочей копии PromptPilot.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install . pyinstaller
./build.sh
# Укажите абсолютный путь к выбранному checkout OneBase.
WORK="$HOME/Projects/onebase"
DATA="$HOME/.promptpilot"
mkdir -p "$DATA/bin"
(cd "$WORK" && go build -o "$DATA/bin/pipelinehealth" ./tools/pipelinehealth)
NATIVE="$PWD/dist/pp"
CHECKER="$DATA/bin/pipelinehealth"
sha() { shasum -a 256 "$1" | awk '{print $1}'; }
"$NATIVE" pipelinectl source-refresh --help
"$NATIVE" pipelinectl budget-status --help
```

Храните исполняемые файлы используемых версий по постоянным отдельным путям. Для production укажите установленный бинарь работающего LaunchAgent через `--native`. При отдельном проверенном бинаре для cache-only и budget-status передайте также `--cache-native PATH --cache-native-sha256 SHA`. Если у прежней установки есть закреплённый модуль проверки рабочего процесса с `check_fix_task()` и `check_runtime()`, передайте `--legacy-runtime-guard PATH --legacy-runtime-guard-sha256 SHA` при миграции. Значения SHA сверяют с проверенной поставкой.

## Чистая станция

Команда `bootstrap` предназначена для первоначальной настройки FIX. Она создаёт локальную SQLite штатной схемы, профиль `onebase` с одной очередью FIX и приостановленную серию с первым вхождением. Задача получает обычную процедуру FIX из указанной копии OneBase, приоритет 1 и явно выбранное расписание. По умолчанию тайм-аут задачи равен 7200 секунд. Для рабочего профиля с уже настроенными очередями переходите сразу к подготовке версии.

```sh
INSTALL="$PWD/tools/install_onebase_fix_cache.py"
python -IB "$INSTALL" bootstrap --data-dir "$DATA" --working-dir "$WORK" \
  --checker "$CHECKER" --checker-sha256 "$(sha "$CHECKER")" \
  --review-sha256 "$(sha "$WORK/.claude/skills/review-queue/SKILL.md")" \
  --fix-sha256 "$(sha "$WORK/.claude/skills/fix-approved/SKILL.md")" \
  --budget "$PWD/docs/examples/onebase-fix-github-budget.json" --recurrence 30m
```

Проверьте `series_id` и `budget_policy_sha256` в ответе. Для опубликованного примера в чистом профиле SHA равен `4594620c770754856de292e6051d02195f829046fedfe6f7d15f3614d876b5be`. На рабочей станции с несколькими профилями PromptPilot учитывает общие резервы аккаунта. Закреплённое production-значение по умолчанию равно `4581b1924a8daf50e68ff41cc00f6c3f454810b440aa88e110c833ab0c0149fd`. При других согласованных лимитах укажите заранее проверенную сумму через `--budget-policy-sha256`. [Пример лимитов](examples/onebase-fix-github-budget.json)

Для выполнения задач с этой конфигурацией нужны локальный сервис `pp server` на `127.0.0.1:8420` с доступным cache-only `GET /api/pipeline-insights/onebase?refresh=false` и работающий пользовательский LaunchAgent `com.promptpilot.worker`. В нём `ProgramArguments` должны завершаться точными аргументами `$NATIVE`, `worker`. У сервера и worker должен быть один `PP_DATA_DIR` и тот же `pipeline_profiles.json`. При нестандартном пути задайте обоим `PP_PIPELINE_PROFILES`. Если у worker отдельный plist, передайте `--worker-plist` при подготовке. Путь к GitHub CLI и Codex, их авторизация и настройки прав задаются в окружении рабочего пользователя. Проверить регистрацию worker можно командой `launchctl print "gui/$(id -u)/com.promptpilot.worker"`.

Если на новой станции ещё нет этого LaunchAgent, следующий фрагмент создаёт plist с текущими выбранными путями и регистрирует worker. Выполняйте его после установки пакета и сверки путей. Серию оставьте приостановленной до проверки установки. Сервер можно запустить отдельным процессом командой `PP_DATA_DIR="$DATA" "$NATIVE" server`.

```sh
PLIST="$HOME/Library/LaunchAgents/com.promptpilot.worker.plist"
python - "$PLIST" "$NATIVE" "$DATA" <<'PY'
import os, plistlib, sys
from pathlib import Path
plist, binary, data = map(Path, sys.argv[1:])
plist.parent.mkdir(parents=True, exist_ok=True)
with plist.open("xb") as stream:
    plistlib.dump({"Label": "com.promptpilot.worker",
                   "ProgramArguments": [str(binary.resolve()), "worker"],
                   "RunAtLoad": True, "KeepAlive": True,
                   "EnvironmentVariables": {"PP_DATA_DIR": str(data.resolve()),
                                            "PATH": os.environ["PATH"]}}, stream)
PY
launchctl bootstrap "gui/$(id -u)" "$PLIST"
```

## Подготовка, включение и обновление

Для чистого примера подготовьте версию с явной суммой политики. На рабочей станции команда `prepare` использует существующий профиль и находит единственную обычную серию FIX по заголовку и точному рабочему каталогу. Удалите параметр `--budget-policy-sha256` из команды ниже, если используете закреплённое production-значение.

```sh
python -IB "$INSTALL" prepare --data-dir "$DATA" --working-dir "$WORK" \
  --native "$NATIVE" --native-sha256 "$(sha "$NATIVE")" \
  --budget-policy-sha256 4594620c770754856de292e6051d02195f829046fedfe6f7d15f3614d876b5be
```

В выводе `config` — абсолютный путь к установленному JSON, `config_sha256` — хеш версии. Команда собирает неизменяемый каталог `$DATA/onebase-fix-cache/<SHA>` из файлов checkout и проверяет SHA исполняемых файлов, процедур и бюджета. Для следующих команд подставьте дословные значения из результата.

```sh
CONFIG='/absolute/path/from/prepare/config.json'
SHA='config_sha256_from_prepare'
python -IB "$INSTALL" preview --config "$CONFIG" --config-sha "$SHA"
python -IB "$INSTALL" apply   --config "$CONFIG" --config-sha "$SHA"
python -IB "$INSTALL" verify  --config "$CONFIG" --config-sha "$SHA"
```

На станции с прежней локальной инструкцией `preview` вернёт `adoption_required` и `replace_block_sha`. После проверки прежнего блока повторите `preview` и `apply` с `--replace-block-sha <полученная сумма>`. Обновление заменяет управляемый начальный блок, обычные вызовы позднего наблюдения и инструкцию к отдельному наблюдению после push там, где оно уже разрешено. Авторские ограничения, отчёты и разрешения отдельных PR сохраняются. Уже начавшийся FIX завершится со своим исходным текстом. Новую версию получает серия и ещё не начатое вхождение. `verify` различает эти случаи. Повторный `prepare/apply` с теми же входами вернёт ту же версию. Следующее обновление проходит теми же командами.

Для чистой станции после проверки возобновите серию в расписании PromptPilot, используя выданный `series_id`. Остальные этапы конвейера, прежние операторские решения по отдельным PR и их локальные материалы имеют собственные процедуры восстановления. Доступ к GitHub и все проверки актуального HEAD нужны во время исполнения FIX.

## Проверка работы и откат

В установленной версии 2 обычный FIX может прочитать свежий полный снимок, когда
фоновый обход GitHub уступил приоритет другой задаче. Квитанция сохраняет причину
`priority_waiter` и маршрут `insights`. Проверяются возраст снимка и результата
проверяющей утилиты, подтверждение её выполнения, состав очереди, PR и HEAD.
При истечении 300 секунд, смене HEAD или ожидании очереди по причине
`fairness_waiter` такой снимок использовать нельзя.

На Mac с действующим отдельным разрешением для #2277 установщик также обновляет
проверку перед полным lint. Команда использует прежние ограничения владельца и
тот же счётчик попыток lint. Подготовка и откат этой версии не расходуют попытку.
Уже начавшаяся задача завершает работу по установленной для неё версии.

```sh
python -IB "$(dirname "$CONFIG")/onebase_fix_cache.py" \
  --config "$CONFIG" --config-sha "$SHA" --report
python -IB "$INSTALL" rollback --data-dir "$DATA" --working-dir "$WORK" --config-sha "$SHA"
```

Отчёт читает локальные сохранённые решения. `observed_tasks` считает уникальные задачи, `initial_cache_hits` — задачи с использованным снимком, `full_scan_claims` — задачи с зарегистрированной попыткой полного запроса. `full_scan_after_hit` показывает пересечение при смене подходящей цели или HEAD. Причины находятся в `full_scan_reasons`. Резервирование попытки может пережить сбой до ответа Native. В старых записях без причины будет `legacy_unclassified`; `unreadable_records` показывает количество записей, которые не удалось прочитать.

Откат требует точной суммы действующей версии. Он восстанавливает сохранённые участки предыдущей инструкции, проверяет их текущее содержимое и оставляет уже начавшиеся задачи на их исходной версии. Повреждённый новый helper не мешает откату.
