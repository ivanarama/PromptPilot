"""Дашборд «Айсберг» для oneservice (этап 5).

Тянет issues проекта из GitLab API (curl-транспорт) и генерирует
самодостаточную страницу os_iceberg.html: канбан по стадиям + вид
«айсберг» (данные вшиты в страницу, работает даже с file://).

Запуск: py -3.11 scripts/oneservice/os_dashboard.py [--out путь.html]
"""

import html as html_lib
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from os_intake import load_env, project_api, ROOT  # noqa: E402

OUT = ROOT / "os_iceberg.html"


def collect(env: dict) -> list[dict]:
    issues = project_api(env, "/issues")
    items = []
    for issue in issues:
        desc = issue.get("description") or ""
        images = [env["GITLAB_URL"].rstrip("/") + m
                  for m in re.findall(r"!\[[^\]]*\]\((/uploads/[^)\s\"'<>]+)\)", desc)]
        items.append({
            "iid": issue["iid"],
            "title": issue["title"],
            "state": issue["state"],
            "labels": issue.get("labels", []),
            "author": ((issue.get("author") or {}).get("name")
                       or (issue.get("author") or {}).get("username") or "аноним"),
            "assignee": ((issue.get("assignee") or {}).get("username") or "—"),
            "web_url": issue.get("web_url", "#"),
            "description": desc[:500],
            "images": images,
            "updated": issue.get("updated_at", "")[:16],
        })
    return items


TEMPLATE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Айсберг задач oneservice</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Prata&family=Alegreya:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
<style>
  :root { --ink:#243447; --blue:#2563eb; }
  * { margin:0; padding:0; box-sizing:border-box; }
  html { background:#0d2c49; }
  body { font-family:'Alegreya', Georgia, serif; color:var(--ink); min-height:100vh;
    background: linear-gradient(180deg,
        rgba(248,252,255,.82) 0%, rgba(220,240,250,.45) 34%,
        rgba(40,90,120,.35) 78%, rgba(15,45,65,.55) 100%),
      url("assets/iceberg-bg.png") center bottom / cover no-repeat fixed; }
  header { text-align:center; padding:2rem 1rem 1rem; position:relative; z-index:2; }
  h1 { font-family:'Prata', serif; font-weight:400; font-size:1.8rem; color:#123a5e; }
  .sub { font-style:italic; color:#3c5876; }
  .switch { position:absolute; right:1rem; top:1.6rem; display:flex; gap:.4rem; }
  .switch button { cursor:pointer; border:1px solid rgba(18,58,94,.4); border-radius:8px;
    background:rgba(255,255,255,.5); padding:.45rem 1rem;
    font-family:'Alegreya', serif; font-size:.95rem; color:var(--ink); }
  .switch button.active { background:rgba(37,99,235,.85); color:#fff; border-color:transparent; }
  .board { max-width:1340px; margin:0 auto 2.4rem; padding:0 1rem;
    display:grid; grid-auto-flow:column; grid-auto-columns:minmax(225px,1fr);
    gap:1rem; align-items:start; overflow-x:auto; }
  .col-head { font-family:'Prata', serif; font-size:1rem; margin:0 0 .8rem .2rem;
    color:#16324a; text-shadow:0 1px 6px rgba(255,255,255,.75); }
  .col-head small { display:block; font-style:italic; color:#3c5876; font-size:.8rem; }
  .col-head .cnt { display:inline-block; min-width:1.5em; text-align:center;
    border-radius:999px; background:rgba(37,99,235,.12); color:var(--blue);
    font-size:.85em; padding:0 .35em; }
  .cards { display:grid; gap:.7rem; }
  .card { background:rgba(255,255,255,.6); border:1px solid rgba(255,255,255,.55);
    border-radius:12px; padding:.7rem .85rem; cursor:pointer;
    box-shadow:0 4px 14px rgba(10,40,70,.2);
    backdrop-filter:blur(14px); -webkit-backdrop-filter:blur(14px);
    transition:transform .15s, box-shadow .15s; }
  .card:hover { transform:translateY(-2px); box-shadow:0 8px 22px rgba(10,40,70,.32); }
  .card.open { background:rgba(255,255,255,.92); }
  .card .t { font-weight:700; font-size:.92rem; margin-bottom:.3rem; }
  .card .t a { color:#123a5e; text-decoration:none; }
  .chips { display:flex; flex-wrap:wrap; gap:.3rem; margin-bottom:.3rem; }
  .chip { font-size:.72rem; border:1px solid rgba(37,99,235,.35); color:var(--blue);
    border-radius:999px; padding:0 .5em; background:rgba(255,255,255,.5); }
  .chip.author { color:#0d2c49; border-color:rgba(18,58,94,.35); }
  .d { display:none; font-size:.84rem; margin-top:.5rem;
       border-top:1px dashed rgba(37,99,235,.35); padding-top:.5rem; }
  .card.open .d { display:block; }
  .more-hint { font-size:.75rem; color:var(--blue); font-style:italic; margin-top:.35rem; }
  .empty { font-style:italic; opacity:.65; font-size:.9rem; }
  footer { text-align:center; color:#3c5876; font-size:.8rem; padding-bottom:2rem; }
  footer a { color:var(--blue); text-decoration:none; }
</style></head>
<body>
  <header>
    <h1>🏔 Айсберг задач oneservice</h1>
    <p class="sub">над водой — готовое для пользователей, глубина — работа и бэклог · обновлено __STAMP__</p>
    <div class="switch">
      <button id="btnKanban" class="active" onclick="setView('kanban')">📋 Канбан</button>
      <button id="btnIce" onclick="setView('iceberg')">🏔 Айсберг</button>
    </div>
  </header>
  <div class="board" id="kanban">
    <div><div class="col-head">🗺 Подано<span class="cnt" data-c="подано"></span><small>ждёт суда концепции</small></div><div class="cards" data-stage="подано"></div></div>
    <div><div class="col-head">✅ Принято<span class="cnt" data-c="принято"></span><small>целиком в ТЗ</small></div><div class="cards" data-stage="принято"></div></div>
    <div><div class="col-head">🔨 В работе<span class="cnt" data-c="в работе"></span><small>исполнитель за пером</small></div><div class="cards" data-stage="в работе"></div></div>
    <div><div class="col-head">🔍 Ревью и мерж<span class="cnt" data-c="ревью"></span><small>проверка и слияние</small></div><div class="cards" data-stage="ревью"></div></div>
    <div><div class="col-head">🚫 Отклонено<span class="cnt" data-c="отклонено"></span><small>не в ладах с концепцией</small></div><div class="cards" data-stage="отклонено"></div></div>
  </div>
  <div class="iceberg" id="iceberg" hidden>
    <div class="layer" data-stage="закрыто"><div class="lt">🏔 На поверхности<span class="cnt" data-c="закрыто"></span><small>закрыто · в релизе</small></div><div class="cards" data-stage="закрыто"></div></div>
    <div class="layer" data-stage="ревью"><div class="lt">🔍 Ватерлиния<span class="cnt" data-c="ревью"></span><small>ревью · готово-к-мержу</small></div><div class="cards" data-stage="ревью"></div></div>
    <div class="layer" data-stage="в работе"><div class="lt">🔨 Под водой<span class="cnt" data-c="в работе"></span><small>исполнитель за пером</small></div><div class="cards" data-stage="в работе"></div></div>
    <div class="layer" data-stage="подано"><div class="lt">🧊 Глубже<span class="cnt" data-c="подано"></span><small>целесообразность · триаж-ТЗ · подано</small></div><div class="cards" data-stage="подано"></div></div>
    <div class="layer" data-stage="отклонено"><div class="lt">⚓ Дно<span class="cnt" data-c="отклонено"></span><small>блокировано платформой · отклонено</small></div><div class="cards" data-stage="отклонено"></div></div>
  </div>
  <footer>oneservice-cc_v2 · конвейер: целесообразность → триаж → фикс → ревью → мерж ·
    <a href="__ISSUES_URL__">GitLab</a></footer>
<script>
  const FEED = __FEED__;
  // Issue text is written by whoever sent the request: escape for attributes too.
  const esc = x => String(x || "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  const STAGE_OF = item => {
    const l = item.labels || [];
    const v = (item.verdict || "").toUpperCase();
    if (item.state === "closed") return "закрыто";
    if (l.includes("готово-к-мержу") || l.includes("ревью")) return "ревью";
    if (l.includes("в работе")) return "в работе";
    if (l.includes("блокирована платформой")) return "блокирована";
    if (v.startsWith("ОТКЛОНИТЬ") || l.includes("отклонено")) return "отклонено";
    if (v === "ПРИНЯТЬ" || l.includes("принято")) return "принято";
    return "подано";
  };
  function cardHTML(item) {
    const chips = [];
    (item.labels || []).forEach(x => chips.push('<span class="chip">' + esc(x) + "</span>"));
    chips.push('<span class="chip author">✍ ' + esc(item.author) + "</span>");
    const imgs = (item.images || []).map(u =>
      '<img src="' + esc(u) + '" style="max-width:100%;border-radius:8px;margin:4px 0;">').join("");
    const open = 'window.open("' + item.web_url + '","_blank")';
    return `<div class="card" onclick="${open.replace(/"/g, "&quot;")}">
      <div class="t"><a href="${item.web_url}" target="_blank" rel="noopener">#${item.iid}</a> ${esc(item.title)}</div>
      <div class="chips">${chips.join("")}</div>
      ${item.description ? '<div class="d">' + esc(item.description) + "</div>" : ""}
      ${imgs}
    </div>`;
  }
  function render() {
    document.querySelectorAll("[data-stage]").forEach(box => {
      const stage = box.dataset.stage;
      const cards = FEED.items.filter(i => STAGE_OF(i) === stage);
      box.innerHTML = cards.map(cardHTML).join("") || '<div class="empty">пусто</div>';
      const head = document.querySelector(`.cnt[data-c="${stage}"]`);
      if (head) head.textContent = cards.length;
    });
  }
  function setView(v) {
    document.getElementById("kanban").hidden = (v === "iceberg");
    document.getElementById("iceberg").hidden = (v !== "iceberg");
    document.getElementById("btnKanban").classList.toggle("active", v !== "iceberg");
    document.getElementById("btnIce").classList.toggle("active", v === "iceberg");
  }
  document.getElementById("btnKanban").addEventListener("click", () => setView("kanban"));
  document.getElementById("btnIce").addEventListener("click", () => setView("iceberg"));
  render();
</script>
</body></html>
"""


def main() -> int:
    env = load_env(ROOT / ".env")
    items = collect(env)
    html = TEMPLATE.replace("__FEED__", json.dumps(
        {"updated": time.strftime("%Y-%m-%d %H:%M"), "items": items},
        ensure_ascii=False)).replace("__STAMP__", time.strftime("%Y-%m-%d %H:%M"))
    html = html.replace("__ISSUES_URL__", issues_url(env))
    out = ROOT / "os_iceberg.html"
    out.write_text(html, encoding="utf-8")
    bg = ROOT / "assets" / "iceberg-bg.png"
    custom_bg = Path(env["OS_DASHBOARD_BG"]) if env.get("OS_DASHBOARD_BG") else None
    if custom_bg and custom_bg.exists() and not bg.exists():
        bg.parent.mkdir(parents=True, exist_ok=True)
        bg.write_bytes(custom_bg.read_bytes())
    print(f"Айсберг собран: {out} (задач: {total_of(items)})")
    return 0


def issues_url(env: dict) -> str:
    """Link to the project's issue list, from the same settings the API uses."""
    project = urllib.parse.unquote(env.get("GITLAB_PROJECT", ""))
    return html_lib.escape(f"{env.get('GITLAB_URL', '').rstrip('/')}/{project}/-/issues")


def total_of(items):
    return len(items)


if __name__ == "__main__":
    sys.exit(main())
