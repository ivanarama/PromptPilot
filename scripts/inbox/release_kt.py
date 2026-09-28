"""Релиз «Сказок Королевства» (шаг 3 конвейера).

Что делает:
  1. Gate: Godot headless import + прогон тест-сьютов (tests/test_runner.tscn)
     на чистом worktree ровно того коммита, который уйдёт в релиз.
  2. Bump VERSION (patch по умолчанию, --minor для фич) и версии APK в export_presets.cfg.
  3. Коммит + тег vX.Y.Z + push в origin (kingdom-tales-rpg).
  4. --export: сборка APK через export_presets.cfg (нужны Android build tools).
  5. --release: GitHub Release с APK (gh cli).
  6. Обновляет летопись на странице сайта (site/kt/index.html, блок v-версии).

Без флагов --export/--release выполняет только gate и подготовку (dry-режим
релиза: покажет, что будет сделано, ничего не публикуя).

Запуск: py -3.11 scripts/inbox/release_kt.py [--minor] [--export] [--release]
"""

import argparse
import datetime
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
GAME = pathlib.Path(r"C:\Projects\mm_rpg_monolithic_gemini")
GODOT = pathlib.Path(r"C:\Projects\tools\godot-4.7.2\Godot_v4.7.2-stable_win64_console.exe")
SITE_PAGE = pathlib.Path(r"C:\Projects\site\kt\index.html")

# Код и данные игры: всё, что здесь не закоммичено, в релиз не попадёт
GAME_CODE_PATHS = ("src", "tests", "data", "project.godot", "export_presets.cfg")

# Строки вывода Godot, при которых прогон провален, даже если в конце напечатано PASSED.
# --import выходит с кодом 0 при любых ошибках скриптов, а ошибка в коде игры
# не обрывает тест — только эти строки и выдают поломку.
FATAL_MARKERS = ("SCRIPT ERROR", "Parse Error", "Failed to load script",
                 "[TEST] FAIL", "RESULT: FAILED", "[TEST] TIMEOUT")


def run(cmd: list[str], cwd: pathlib.Path | None = None, timeout: int = 600) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # Зависший Godot (скрипт упал до quit()) — это провал гейта, а не трейсбек
        partial = exc.stdout or b""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        return 124, partial + f"\n[gate] процесс убит по таймауту {timeout} с"
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def find_fatal_lines(output: str) -> list[str]:
    """Строки с ошибками скриптов, проваленными проверками или таймаутом тестов."""
    return [line.strip() for line in output.splitlines()
            if any(marker in line for marker in FATAL_MARKERS)]


def uncommitted_game_files(game: pathlib.Path) -> list[str]:
    """Изменённые и новые файлы кода/данных, которых нет в коммите."""
    code, out = run(["git", "status", "--porcelain", "--untracked-files=all", "--", *GAME_CODE_PATHS], cwd=game)
    if code != 0:
        sys.exit(f"gate: git status не прошёл:\n{out[-1000:]}")
    return [line[3:] for line in out.splitlines() if line.strip()]


def gate() -> None:
    # Коммит 0009529 сослался на SettingsManager/DwellingData, которые остались
    # только в рабочей папке: гейт гонялся по ней и был зелёным, а в git
    # кампания не компилировалась. Поэтому проверяем ровно то, что уйдёт в релиз.
    dirty = uncommitted_game_files(GAME)
    if dirty:
        sys.exit("gate: в рабочей папке есть незакоммиченные файлы кода — "
                 "в релиз они не попадут, закоммитьте или уберите их:\n  "
                 + "\n  ".join(dirty[:30]))
    code, sha = run(["git", "rev-parse", "HEAD"], cwd=GAME)
    if code != 0:
        sys.exit(f"gate: не удалось определить HEAD:\n{sha[-500:]}")
    sha = sha.strip()
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="kt-gate-"))
    checkout = workdir / "game"
    code, out = run(["git", "worktree", "add", "--detach", str(checkout), sha], cwd=GAME)
    if code != 0:
        shutil.rmtree(workdir, ignore_errors=True)
        sys.exit(f"gate: не удалось создать чистый worktree:\n{out[-1000:]}")
    try:
        print(f"[1/4] Gate: Godot headless import (чистый checkout {sha[:8]})…")
        code, out = run([str(GODOT), "--headless", "--path", str(checkout), "--import"], timeout=1200)
        fatal = find_fatal_lines(out)
        if code != 0 or fatal:
            sys.exit(f"gate: импорт не прошёл (exit {code}):\n"
                     + "\n".join(fatal[:20] or out.strip().splitlines()[-20:]))
        print("[2/4] Gate: тесты tests/test_runner.tscn…")
        code, out = run([str(GODOT), "--headless", "--path", str(checkout),
                         "res://tests/test_runner.tscn"], timeout=900)
        tail = out.strip().splitlines()[-3:] if out.strip() else ["(нет вывода)"]
        print("\n".join("    " + line for line in tail))
        fatal = find_fatal_lines(out)
        if code != 0 or fatal or "PASSED" not in out.upper():
            details = "\n".join("    " + line for line in fatal[:20])
            sys.exit(f"gate: тесты не прошли (exit {code}) — релиз отменён\n{details}")
        print("    gate пройден ✔")
    finally:
        run(["git", "worktree", "remove", "--force", str(checkout)], cwd=GAME)
        shutil.rmtree(workdir, ignore_errors=True)


def bump_export_presets(text: str, version: str) -> str:
    """Версия APK: versionName = VERSION, versionCode +1 (иначе все сборки — «1.0», код 1)."""
    text = re.sub(r'^version/name=".*"$', f'version/name="{version}"', text, count=1, flags=re.M)
    return re.sub(r"^version/code=(\d+)$", lambda m: f"version/code={int(m.group(1)) + 1}",
                  text, count=1, flags=re.M)


def bump(minor: bool) -> str:
    version_file = GAME / "VERSION"
    major, minor_v, patch = (int(x) for x in
                             version_file.read_text().strip().split("."))
    if minor:
        minor_v, patch = minor_v + 1, 0
    else:
        patch += 1
    new = f"{major}.{minor_v}.{patch}"
    version_file.write_text(new + "\n", encoding="utf-8")
    presets = GAME / "export_presets.cfg"
    presets.write_text(bump_export_presets(presets.read_text(encoding="utf-8"), new), encoding="utf-8")
    print(f"[3/4] VERSION -> {new} (и версия APK в export_presets.cfg)")
    return new


def publish(version: str, do_export: bool, do_release: bool) -> None:
    repo = "ivanarama/kingdom-tales-rpg"
    code, out = run(["git", "add", "VERSION", "CONCEPT.md", "export_presets.cfg"], cwd=GAME)
    run(["git", "commit", "-m", f"release: v{version}"], cwd=GAME)
    run(["git", "tag", f"v{version}"], cwd=GAME)
    code, out = run(["git", "push", "origin", "main", f"v{version}"], cwd=GAME)
    if code != 0:
        sys.exit(f"push не прошёл:\n{out[-1500:]}")
    print(f"[4/4] запушено main + тег v{version}")
    apk = GAME / "builds" / "fairytale_rpg.apk"
    if do_export:
        print("    сборка APK (godot --export-release Android)…")
        code, out = run([str(GODOT), "--headless", "--path", str(GAME),
                         "--export-release", "Android", str(apk)], timeout=1800)
        if code != 0 or not apk.exists():
            sys.exit(f"экспорт APK не удался:\n{out[-2000:]}")
    if do_release:
        code, out = run(["gh", "release", "create", f"v{version}",
                         str(apk) if apk.exists() else "--verify",
                         "--repo", repo,
                         "--title", f"v{version}",
                         "--notes", f"Релиз v{version}. Подробнее — в летописи на сайте."])
        if code != 0:
            sys.exit(f"gh release не удался:\n{out[-1500:]}")
        print(f"    GitHub Release v{version} опубликован")


def update_site_changelog(version: str) -> None:
    html = SITE_PAGE.read_text(encoding="utf-8")
    today = datetime.date.today().isoformat()
    line = f'      <p><span class="ver">v{version}</span> — релиз от {today} (конвейер)</p>\n'
    marker = '    <div class="changelog card">\n'
    if marker in html and line not in html:
        html = html.replace(marker, marker + line, 1)
        SITE_PAGE.write_text(html, encoding="utf-8")
        print(f"    летопись сайта обновлена: v{version}")
    elif line in html:
        print("    летопись уже содержит эту версию")


def mark_released(version: str, closes: list[int]) -> None:
    """Отметить на стене предложений, какие идеи вошли в этот релиз."""
    feed_path = ROOT / ".." / ".." / "site" / "kt" / "feed.json"
    env_file = ROOT / ".env"
    env_lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    for line in env_lines:
        if line.strip().startswith("KT_SITE_FEED="):
            feed_path = pathlib.Path(line.split("=", 1)[1].strip())
    if not feed_path.exists():
        print("    фид не найден, стена не обновлена")
        return
    feed = json.loads(feed_path.read_text(encoding="utf-8"))
    for item in feed.get("items", []):
        if int(item.get("task_id", 0)) in closes:
            item["stage"] = f"в релизе v{version}"
    feed_path.write_text(json.dumps(feed, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    print(f"    стена: {len(closes)} предложений отмечены как в релизе v{version}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minor", action="store_true", help="поднять MINOR (фича), иначе PATCH")
    parser.add_argument("--export", action="store_true", help="собрать APK")
    parser.add_argument("--release", action="store_true", help="опубликовать GitHub Release")
    parser.add_argument("--closes", default="", help="id задач триажа через запятую, попавшие в релиз")
    args = parser.parse_args()

    gate()
    version = bump(args.minor)
    publish(version, args.export, args.release)
    update_site_changelog(version)
    if args.closes and args.release:
        mark_released(version, [int(x) for x in args.closes.split(",") if x.strip()])
    print(f"Готово: v{version}" + (" (опубликована)" if args.release else " (локально, без публикации)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
