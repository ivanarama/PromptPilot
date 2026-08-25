# Роль

Ты исполнитель evidence-only раунда U3-FIX-3 для УТ10 → БП3. Работаешь
автономно в `D:\Projects\exchange` на ветке
`checkpoint/u2-wip-20260824`. Независимый проверяющий будет принимать точный
commit и не доверяет свободному тексту без файлового evidence.

# Обязательный вход

Сначала полностью прочитай:

- `docs/audit/UT10-BP3_U3_FIX2_REVIEW.md`;
- `docs/audit/evidence/ut10_u3_fix2_audit_20260825.txt`;
- `evidence/ut10-bp3/20260825_111804_605_e31295d1/manifest.json`.

Исходная точка раунда: audit commit `1e13c01`. Функциональный production-код
U3-0/U3-1 уже принят. Не переписывай loader, карту операций и runtime harness
без доказанной необходимости.

# Цель

Закрыть только два evidence-блокера:

1. Выпустить новый кандидат из чистого commit с новым run-id и manifest:
   `dirty_worktree=false`, точный `source_commit`.
2. Сделать manifest, proof и implementation report непротиворечивыми:
   правильные размеры/SHA, честная квалификация preliminary dual proof и
   корректные checks, относящиеся к одному кандидату.

# Порядок работы

1. Зафиксируй стартовые `git rev-parse HEAD` и `git status --porcelain`.
2. Если для честного clean-build требуется изменить только build/evidence
   tooling, внеси минимальную правку, тестируй и сначала закоммить её. Audit
   files не меняй.
3. Убедись, что дерево чистое до запуска build. Выполни новый изолированный
   build УТ10-БП3 с новым run-id.
4. На новом кандидате выполни обязательные loader/exporter/runtime gates,
   целевой строгий pytest и обычный полный pytest. Не выдавай полный проект за
   `-W error` clean: общий warning debt уже отдельно зафиксирован.
5. Preliminary dual generation называй только preliminary. Она не доказывает
   formal R1, пока `formal_r1_proven=false` или
   `uses_real_designer_exporter=false`.
6. Проверь manifest по `evidence/schema.json`, физическое наличие каждого
   указанного файла, размер и SHA-256.
7. Обнови implementation report точными значениями нового run. Не заявляй
   check=true без ссылки на evidence именно этого кандидата; допустимо оставить
   check=false с честным пояснением.
8. Закоммить tooling/production отдельно от evidence/report, если tooling всё
   же менялся. Заверши с чистым `git status`.

# Запреты

- Не изменяй `docs/audit/**`.
- Не повышай readiness самостоятельно: оставить R0.
- Не изменяй rollback baseline `build/UT10-BP3_v49.epf`.
- Не используй `git reset`, `git clean`, `git checkout --` и другие
  разрушительные команды.
- Не подменяй реальный runtime статическим тестом или старым логом.

# Итоговый ответ

Сообщи:

- итоговый commit и чистоту дерева;
- новый run-id;
- пути, размеры и SHA-256 loader/exporter/template/dump;
- точные результаты gates и тестов с командами;
- значения `source_commit`, `dirty_worktree` и checks из manifest;
- список изменённых файлов по категориям;
- любые оставшиеся ограничения без завышения статуса.
