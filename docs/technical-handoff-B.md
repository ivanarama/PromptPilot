# Срез B: межстадийная приёмка technical handoff

Рабочая граница PromptPilot #152 / OneBase #1909 после общей приёмки A:
PromptPilot main `fcbb725bbdcc93a828b932f400a43873014c3d9e`, OneBase main
`8fd157a9dad989d3e4c9d5528e815c8876baf533`.

Этот Draft ещё не является поставкой или разрешением включить флаг.

- Использовать принятые parser, конечный lifetime budget, единственный FIX,
  post-push recovery, settlement и механизм парковки цели #144.
- Добавить положительные потребители typed REVIEW в MERGE/TAIL без создания
  синтетического legacy-заключения. Technical/engineering handoff не является
  разрешением на merge или создание хвоста.
- Сохранить полный proof, identity, trusted ship, CI точного HEAD, свежий main,
  HMAC/expiry, compare-and-merge, single-flight и обработку неизвестного исхода.
- Проверить публичные transport/CLI: повтор next, два новых HEAD, переименование
  blocker, исчерпание бюджета, другой eligible PR, ABA, edit/delete и старый
  third-round human handoff.
- Общая приёмка требует точной пары исходников, настоящего Go checker,
  независимого ревью и всех пяти/восьми обязательных CI.

Dashboard, обновление установленной пары, включение и наблюдение пилота относятся
к C (#153 / OneBase #1910). Рабочие настройки и службы здесь не меняются.
