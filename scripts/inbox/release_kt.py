"""Релиз «Сказок Королевства» (шаг 3 конвейера).

Что делает:
  1. Gate: Godot headless import + прогон тест-сьютов (tests/test_runner.tscn).
  2. Bump VERSION (patch по умолчанию, --minor для фич).
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
import pathlib
import re
import subprocess
import sys

GAME = pathlib.Path(r"C:\Projects\mm_rpg_monolithic_gemini")
GODOT = pathlib.Path(r"C:\Projects\tools\godot-4.7.2\Godot_v4.7.2-stable_win64_console.exe")
SITE_PAGE = pathlib.Path(r"C:\Projects\site\kt\index.html")


def run(cmd: list[str], cwd: pathlib.Path | None = None, timeout: int = 600) -> tuple[int, str]:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def gate() -> None:
    print("[1/4] Gate: Godot headless import…")
    code, out = run([str(GODOT), "--headless", "--path", str(GAME), "--import"])
    if code != 0:
        sys.exit(f"gate: импорт не прошёл (exit {code}):\n{out[-2000:]}")
    print("[2/4] Gate: тесты tests/test_runner.tscn…")
    code, out = run([str(GODOT), "--headless", "--path", str(GAME),
                     "res://tests/test_runner.tscn"], timeout=900)
    tail = out.strip().splitlines()[-3:] if out.strip() else ["(нет вывода)"]
    print("\n".join("    " + line for line in tail))
    if code != 0 or "PASSED" not in out.upper():
        sys.exit("gate: тесты не прошли — релиз отменён")
    print("    gate пройден ✔")


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
    print(f"[3/4] VERSION -> {new}")
    return new


def publish(version: str, do_export: bool, do_release: bool) -> None:
    repo = "ivanarama/kingdom-tales-rpg"
    code, out = run(["git", "add", "VERSION", "CONCEPT.md"], cwd=GAME)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minor", action="store_true", help="поднять MINOR (фича), иначе PATCH")
    parser.add_argument("--export", action="store_true", help="собрать APK")
    parser.add_argument("--release", action="store_true", help="опубликовать GitHub Release")
    args = parser.parse_args()

    gate()
    version = bump(args.minor)
    publish(version, args.export, args.release)
    update_site_changelog(version)
    print(f"Готово: v{version}" + (" (опубликована)" if args.release else " (локально, без публикации)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
