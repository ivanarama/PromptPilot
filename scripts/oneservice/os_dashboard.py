"""Дашборд «Айсберг» для oneservice (этап 5).

Собирает открытые и недавно закрытые issues проекта из GitLab API и
генерирует самодостаточную HTML-страницу: над ватерлинией — готовое,
под водой — кухня (работа, триаж, бэклог), у дна — заблокированное
платформой и отклонённое.

Запуск: py -3.11 scripts/oneservice/os_dashboard.py
Результат: scripts/oneservice/os_iceberg.html (открыть в браузере).
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from os_intake import load_env, project_api, ROOT  # noqa: E402

OUT = ROOT / "os_iceberg.html"

# порядок «глубины»: чем ниже, тем глубже под ватерлинией
LAYERS = [
    ("released", "🏔 НА ПОВЕРХНОСТИ", "закрыто · в релизе", 0),
    ("review", "🔍 ВАТЕРЛИНИЯ", "ревью · готово-к-мержу", 1),
    ("work", "🔨 ПОД ВОДОЙ", "в работе", 2),
    ("triage", "🧊 ГЛУБЖЕ", "целесообразность · триаж-ТЗ · подано", 3),
    ("bottom", "⚓ ДНО", "блокирована платформой · отклонено", 4),
]


def stage_of(issue: dict) -> str:
    labels = set(issue.get("labels", []))
    if issue.get("state") == "closed":
        return "released"
    if "блокирована платформой" in labels:
        return "bottom"
    if "отклонено" in labels:
        return "bottom"
    if "готово-к-мержу" in labels or "ревью" in labels:
        return "review"
    if "в работе" in labels:
        return "work"
    return "triage"


def card(issue: dict) -> str:
    author = ((issue.get("author") or {}).get("name")
              or (issue.get("author") or {}).get("username") or "аноним")
    assignee = ((issue.get("assignee") or {}).get("name")
                or (issue.get("assignee") or {}).get("username") or "—")
    labels = ", ".join(issue.get("labels", []))
    body = (issue.get("description") or "")[:400].replace("<", "&lt;")
    return f"""<div class="card">
  <div class="t"><a href="{issue['web_url']}" target="_blank" rel="noopener">#{issue['iid']}</a> {issue['title'][:90].replace('<', '&lt;')}</div>
  <div class="m">предложил: {author.replace('<', '&lt;')} · исполнитель: {assignee.replace('<', '&lt;')} · {labels}</div>
  <div class="d">{body}</div>
</div>"""


def build(env: dict) -> tuple[str, int]:
    issues = project_api(env, "/issues")
    items = [(issue, stage_of(issue)) for issue in issues]
    sections = []
    total = 0
    for key, title, sub, _depth in LAYERS:
        cards = [card(issue) for issue, stage in items if stage == key]
        total += len(cards)
        sections.append(
            f'<div class="layer {key}"><div class="lt">{title}'
            f'<span class="cnt">{len(cards)}</span>'
            f'<span class="sub">{sub}</span></div>'
            + ("".join(cards) if cards else '<div class="empty">пусто</div>')
            + "</div>")
    stamp = time.strftime("%Y-%m-%d %H:%M")
    html = HTML.replace("__STAMP__", stamp).replace("__TOTAL__", str(total))
    html = html.replace("__LAYERS__", "\n".join(sections))
    return html, total


HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Айсберг oneservice — путь задач</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Prata&family=Alegreya:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
<style>
  :root { --gold: #d4a017; --ink: #243447; --ink-soft: #46627f; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Alegreya', Georgia, serif; color: var(--ink);
    background: linear-gradient(180deg,
      #bfe3f2 0%, #bfe3f2 26%,           /* небо */
      #7fb6d9 26%, #7fb6d9 34%,          /* ватерлиния зона */
      #2e6da4 34%, #17456e 62%,          /* толща воды */
      #0d2c49 62%, #071a2e 100%);        /* глубина */
    min-height: 100vh;
  }
  header { text-align: center; padding: 2.2rem 1rem 1.2rem; color: #16324a; }
  header h1 { font-family: 'Prata', serif; font-size: 1.9rem; }
  header p { font-style: italic; }
  header .stamp { font-size: .82rem; opacity: .7; }
  .board { max-width: 1200px; margin: 0 auto 2rem; padding: 0 1rem;
           display: grid; gap: 1rem; }
  .layer { border-radius: 12px; padding: 1rem; }
  .lt { font-family: 'Prata', serif; font-size: 1.05rem; margin-bottom: .8rem; }
  .lt .cnt { display: inline-block; min-width: 1.5em; text-align: center;
             border-radius: 999px; padding: 0 .4em; margin-left: .5em;
             font-size: .85em; background: rgba(255,255,255,.45); }
  .lt .sub { display: block; font-family: 'Alegreya', serif; font-style: italic;
             font-size: .82rem; opacity: .8; }
  .released { background: #eef7fbcc; border: 1px solid rgba(22,50,74,.25); }
  .review   { background: #e2eef7cc; border: 1px solid rgba(22,50,74,.25); }
  .work     { background: #35699ecc; color: #eaf4fb; }
  .work .lt, .triage .lt, .bottom .lt { color: #cfe6f7; }
  .work .card, .triage .card, .bottom .card {
      background: #12395ccc; color: #eaf4fb; border: 1px solid rgba(160,208,240,.25); }
  .work .m, .triage .m, .bottom .m, .work .d, .triage .d, .bottom .d,
  .work .lt .sub, .triage .lt .sub { color: #b7d5ea; }
  .triage   { background: #23578acc; }
  .bottom   { background: #123a5ecc; }
  .card { background: #fff; border-radius: 10px; padding: .7rem .9rem;
          margin-bottom: .6rem; box-shadow: 0 3px 10px rgba(0,20,40,.25); }
  .card .t { font-weight: 700; font-size: .95rem; margin-bottom: .25rem; }
  .card .t a { color: inherit; text-decoration: none; border-bottom: 1px dotted; }
  .card .m { font-size: .78rem; opacity: .8; }
  .card .d { font-size: .85rem; margin-top: .4rem; max-height: 4.2em;
             overflow: hidden; }
  .card:hover .d { max-height: none; }
  .empty { font-style: italic; opacity: .65; font-size: .9rem; }
  footer { text-align: center; color: #cfe6f7; font-size: .8rem; padding-bottom: 2rem; }
  footer a { color: inherit; }
</style></head>
<body>
  <header>
    <h1>🏔 Айсберг задач oneservice</h1>
    <p>над водой — то, что уже у пользователей; глубина — работа и бэклог</p>
    <p class="stamp">обновлено __STAMP__ · задач: __TOTAL__</p>
  </header>
  <div class="board">
__LAYERS__
  </div>
  <footer>oneservice-cc_v2 · конвейер: целесообразность → триаж → фикс → ревью → мерж ·
    <a href="https://gitlab.icecorp.ru/1c/oneservice-cc_v2/-/issues">GitLab</a></footer>
</body></html>
"""


def main() -> int:
    env = load_env(ROOT / ".env")
    html, total = build(env)
    OUT.write_text(html, encoding="utf-8")
    print(f"Айсберг собран: {OUT} (задач: {total})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
