"""Релиз «Сказок Королевства» (шаг 3 конвейера).

Что делает:
  1. Gate: Godot headless import + прогон тест-сьютов (tests/test_runner.tscn)
     на чистом worktree ровно того коммита, который уйдёт в релиз.
  2. Bump VERSION (patch по умолчанию, --minor для фич) и версии APK в export_presets.cfg.
  3. Коммит + тег vX.Y.Z + push в origin (kingdom-tales-rpg).
  4. --export: сборка APK через export_presets.cfg (нужны Android build tools).
  5. --release: GitHub Release с APK (gh cli).
  6. Обновляет летопись на странице сайта (site/kt/index.html, блок v-версии).

Без флагов --export/--release публикует коммит и тег без APK и GitHub Release.

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
from typing import NamedTuple

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


class GateProof(NamedTuple):
    commit: str
    tree: str


def git_checked(*args: str, cwd: pathlib.Path | None = None) -> str:
    code, output = run(["git", *args], cwd=GAME if cwd is None else cwd)
    if code:
        sys.exit(f"release: git {args[0]} не прошёл:\n{output[-1500:]}")
    return output.strip()


def release_contents(proof: GateProof, version: str) -> dict[str, str]:
    old = git_checked("show", f"{proof.commit}:VERSION")
    try:
        major, minor, patch = (int(x) for x in old.split("."))
    except ValueError:
        sys.exit("release: некорректная исходная VERSION")
    if version not in (f"{major}.{minor}.{patch + 1}", f"{major}.{minor + 1}.0"):
        sys.exit("release: версия не соответствует patch/minor проверенного коммита")
    presets = git_checked("show", f"{proof.commit}:export_presets.cfg")
    if not re.search(r'^version/code=\d+$', presets, re.M) or not re.search(r'^version/name=".*"$', presets, re.M):
        sys.exit("release: в export_presets.cfg нет версии APK")
    return {"VERSION": version + "\n",
            "export_presets.cfg": bump_export_presets(presets + "\n", version)}


def require_current_proof(proof: GateProof, version: str | None = None) -> None:
    if (git_checked("rev-parse", "HEAD") != proof.commit
            or git_checked("rev-parse", "HEAD^{tree}") != proof.tree):
        sys.exit("release: HEAD изменился после gate; повторите проверку")
    expected = release_contents(proof, version) if version is not None else {}
    # Do not strip status: the first column distinguishes staged from unstaged.
    code, status = run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=GAME)
    if code:
        sys.exit("release: не удалось проверить рабочую папку")
    for entry in status.split("\0"):
        if entry and (entry[:2] != " M" or entry[3:] not in expected):
            sys.exit("release: посторонние изменения или индекс после gate; повторите проверку")
    for name, content in expected.items():
        if (GAME / name).read_text(encoding="utf-8") != content:
            sys.exit(f"release: {name} не соответствует проверенному bump")


def run(cmd: list[str], cwd: pathlib.Path | None = None, timeout: int = 600) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False,
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


def gate() -> GateProof:
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
    proof = GateProof(sha, git_checked("rev-parse", "HEAD^{tree}"))
    require_current_proof(proof)
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="kt-gate-"))
    checkout = workdir / "game"
    code, out = run(["git", "worktree", "add", "--detach", str(checkout), sha], cwd=GAME)
    if code != 0:
        shutil.rmtree(workdir, ignore_errors=True)
        sys.exit(f"gate: не удалось создать чистый worktree:\n{out[-1000:]}")
    try:
        check_godot(checkout)
        require_current_proof(proof)
        return proof
    finally:
        run(["git", "worktree", "remove", "--force", str(checkout)], cwd=GAME)
        shutil.rmtree(workdir, ignore_errors=True)


def check_godot(checkout: pathlib.Path) -> None:
    for args, timeout in [(["--import"], 1200), (["res://tests/test_runner.tscn"], 900)]:
        code, out = run([str(GODOT), "--headless", "--path", str(checkout), *args], timeout=timeout)
        fatal = find_fatal_lines(out)
        if code or fatal or (args != ["--import"] and "PASSED" not in out.upper()):
            sys.exit(f"gate: Godot не прошёл (exit {code}):\n" + "\n".join(fatal[:20] or out.splitlines()[-20:]))
    if git_checked("status", "--porcelain", "--untracked-files=all", cwd=checkout):
        sys.exit("gate: Godot изменил проверяемое дерево")


def bump_export_presets(text: str, version: str) -> str:
    """Версия APK: versionName = VERSION, versionCode +1 (иначе все сборки — «1.0», код 1)."""
    text = re.sub(r'^version/name=".*"$', f'version/name="{version}"', text, count=1, flags=re.MULTILINE)
    return re.sub(r"^version/code=(\d+)$", lambda m: f"version/code={int(m.group(1)) + 1}",
                  text, count=1, flags=re.MULTILINE)


def bump(minor: bool, proof: GateProof) -> str:
    require_current_proof(proof)
    version_file = GAME / "VERSION"
    major, minor_v, patch = (int(x) for x in
                             version_file.read_text().strip().split("."))
    if minor:
        minor_v, patch = minor_v + 1, 0
    else:
        patch += 1
    new = f"{major}.{minor_v}.{patch}"
    for name, content in release_contents(proof, new).items():
        (GAME / name).write_text(content, encoding="utf-8")
    print(f"[3/4] VERSION -> {new} (и версия APK в export_presets.cfg)")
    return new


def publish(version: str, do_export: bool, do_release: bool, *, proof: GateProof) -> None:
    repo = "ivanarama/kingdom-tales-rpg"
    require_current_proof(proof, version)
    # --only never captures the caller's index, even if another process stages a file.
    git_checked("commit", "--only", "-m", f"release: v{version}", "--", "VERSION", "export_presets.cfg")
    release_sha = git_checked("rev-parse", "HEAD")
    if git_checked("show", "-s", "--format=%P", release_sha) != proof.commit:
        sys.exit("release: родитель коммита изменился после gate")
    changed = git_checked("diff", "--name-only", proof.commit, release_sha).splitlines()
    if set(changed) != {"VERSION", "export_presets.cfg"}:
        sys.exit("release: в коммит попали посторонние файлы")
    with tempfile.TemporaryDirectory(prefix="kt-publish-") as folder:
        checkout = pathlib.Path(folder) / "game"
        apk = pathlib.Path(folder) / "fairytale_rpg.apk"
        git_checked("worktree", "add", "--detach", str(checkout), release_sha)
        try:
            # Verify the final versioned commit and export from that same frozen tree.
            check_godot(checkout)
            if do_export:
                code, out = run([str(GODOT), "--headless", "--path", str(checkout),
                                 "--export-release", "Android", str(apk)], timeout=1800)
                if code or not apk.is_file() or not apk.stat().st_size:
                    sys.exit(f"экспорт APK не удался:\n{out[-2000:]}")
                if git_checked("status", "--porcelain", "--untracked-files=all", cwd=checkout):
                    sys.exit("release: экспорт изменил проверенное дерево")
            if git_checked("rev-parse", "HEAD") != release_sha:
                sys.exit("release: HEAD изменился перед публикацией")
            git_checked("tag", f"v{version}", release_sha)
            git_checked("push", "--atomic", "origin", f"{release_sha}:refs/heads/main", f"refs/tags/v{version}")
            print(f"[4/4] запушен проверенный коммит {release_sha[:8]} + тег v{version}")
            if do_release:
                # Without --export never attach an APK left by an older release.
                code, out = run(["gh", "release", "create", f"v{version}", "--verify-tag",
                         *([str(apk)] if do_export else []),
                         "--repo", repo,
                         "--title", f"v{version}",
                         "--notes", f"Релиз v{version}. Подробнее — в летописи на сайте."])
                if code != 0:
                    sys.exit(f"gh release не удался:\n{out[-1500:]}")
                print(f"    GitHub Release v{version} опубликован")
            if do_export:
                destination = GAME / "builds" / f"fairytale_rpg-{version}-{release_sha[:8]}.apk"
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(apk, destination)
                print(f"    проверенный APK: {destination}")
        finally:
            git_checked("worktree", "remove", "--force", str(checkout))


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

    proof = gate()
    version = bump(args.minor, proof)
    publish(version, args.export, args.release, proof=proof)
    update_site_changelog(version)
    if args.closes and args.release:
        mark_released(version, [int(x) for x in args.closes.split(",") if x.strip()])
    print(f"Готово: v{version}" + (" (опубликована)" if args.release else " (локально, без публикации)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
