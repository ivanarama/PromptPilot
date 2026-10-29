# Идентичность PR: первый срез плана 196

`pipelinectl inventory` добавляет идентичность PR в отдельное представление
очередей в режиме чтения. Это часть PromptPilot из задачи #159;
изменение `pipelinehealth` в OneBase и переход на протокол разрешений v2
выполняются следующими срезами плана.

```console
pp pipelinectl --config pipelinectl.json inventory
pp pipelinectl --config pipelinectl.json inventory --health-file health.json
```

Без `--health-file` запускается настроенная `health_command`, но отключается
предварительный fast-forward рабочей копии и перечитывание конфигурации.
Используйте актуальную рабочую копию с проверенным инструментом health.
С файлом читается существующий JSON-снимок; сведения о PR в обоих случаях
запрашиваются заново в GitHub. Файл должен быть в строгой UTF-8 без BOM.
Установленный релиз, scheduler, leases, комментарии, метки и ветки команда
не меняет. Как и для прежней `health_command`, сам настроенный исполняемый
инструмент должен быть доверенным инструментом чтения.

Из REST берётся `Repository.node_id`, а не числовой database ID. Для каждой
цели GraphQL возвращает `PullRequest.id`, номера PR, base/head repository
ID и точные имена, ref и SHA, `isCrossRepository`, состояние и признак Draft.
Метаданные не извлекаются из текста PR или настроек локального remote.
При наличии `base_repository_id` в конфигурации проверяется также этот ID;
всегда проверяется совпадение base с актуальным репозиторием GitHub,
полученным по настроенному `repository`, и с веткой `main`.

## Байтовый контракт

`PRIdentityV1.canonical_bytes()` возвращает запись с фиксированным порядком:

```text
pp-pr-identity-v1
base-repository-id=<GraphQL Repository.id>
base-repository=<owner/name>
base-ref=<baseRefName>
pr-id=<GraphQL PullRequest.id>
pr-number=<decimal>
head-repository-id=<GraphQL Repository.id>
head-repository=<owner/name>
head-ref=<headRefName>
head-sha=<40 lowercase hex>
cross-repository=<true|false>
```

Кодировка — строгая UTF-8 без BOM и Unicode-нормализации. Разделитель — LF,
после последней строки тоже обязателен LF. SHA-256 этих точных байтов —
`identity_sha256` в JSON. Имена репозиториев также входят в digest: rename
или transfer должен быть виден. `baseRefOid` хранится отдельно как
`base_ref_oid` и в digest не входит.

Общие эталонные данные для проверки на Windows, Linux и последующей части
OneBase: `tests/fixtures/pr-identity-v1.json`. В них есть GraphQL/REST снимки,
точные байты в `canonical_utf8_hex`, ожидаемый SHA-256 и кириллический
`head-ref=ветка/проверка`. Проверки сравнивают байты; переносы строк самой
JSON-фикстуры на результат не влияют.

## Результат и ограничения среза

В существующих массивах кандидатов сохраняются порядок и исходные поля.
Добавляются `pr_identity`, `identity_sha256` и `base_ref_oid`. Один PR читается
один раз за вызов даже при присутствии в нескольких очередях.

Удалённый fork, пустой ref/SHA, non-main base, неожиданный repository ID,
некорректная UTF-8 и противоречивые сведения закрывают эту цель: она исключается
из представления очередей и получает finding `invalid_pr_identity` с причиной.
То же происходит, если HEAD уже отличается от снимка health, PR закрыт или
переведён в Draft. Ошибки всего источника возвращают `action: error` и код 2.
Состояние health и существующие findings не переписываются; потребитель должен
учитывать новые findings, а не судить только по прежнему `state`.

Выдача помечена `identity_authority: read-only`, `identity_v2_enabled: false`.
Она не является lease или разрешением выполнить код форка, опубликовать ревью,
push или merge. Действующие `next`, `complete`, v1 proof и их digest не меняются
и пока не используют эту инвентаризацию. Изоляция fork-кода, v2-маркеры,
миграция разрешений и автоматическое включение нового протокола находятся
в последующих срезах принятого плана.
