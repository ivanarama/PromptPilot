#!/usr/bin/env bash
# Popup-панель «enqueue»: спрашивает текст задачи и ставит её в очередь
# PromptPilot с рабочей директорией панели-источника.
set -u

ctx=${HERDR_PLUGIN_CONTEXT_JSON:-}
cwd=""
if [ -n "$ctx" ]; then
    if command -v jq > /dev/null 2>&1; then
        cwd=$(printf '%s' "$ctx" | jq -r '.cwd // empty' 2> /dev/null || true)
    else
        cwd=$(printf '%s' "$ctx" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("cwd") or "")
except Exception:
    pass' 2> /dev/null || true)
    fi
fi
[ -n "$cwd" ] || cwd=$PWD

if command -v pp > /dev/null 2>&1; then
    pp_cmd=(pp)
elif [ -x "$HOME/.local/bin/pp" ]; then
    pp_cmd=("$HOME/.local/bin/pp")
else
    pp_cmd=(python3 -m promptpilot)
fi

echo "PromptPilot — новая задача"
echo "Рабочая директория: $cwd"
printf 'Текст задачи: '
IFS= read -r prompt || prompt=""

if [ -z "$prompt" ]; then
    echo "Пустой ввод — задача не создана."
    read -r -p "Enter — закрыть" _ || true
    exit 0
fi

out=$("${pp_cmd[@]}" add --dir "$cwd" -- "$prompt" 2>&1)
rc=$?
printf '%s\n' "$out"

task_id=$(printf '%s\n' "$out" | sed -n 's/^ *#\([0-9][0-9]*\).*/\1/p' | head -n 1)
if [ "$rc" -eq 0 ] && [ -n "$task_id" ]; then
    echo
    echo "Задача #$task_id поставлена в очередь."
else
    echo
    echo "Не удалось создать задачу (код $rc)."
fi
read -r -p "Enter — закрыть" _ || true
