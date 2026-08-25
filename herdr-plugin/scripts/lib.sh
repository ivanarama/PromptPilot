#!/usr/bin/env bash
# Общее для обеих половинок плагина: как найти PromptPilot и что сказать, если
# его на машине нет. Раньше порядок поиска был скопирован в ensure-worker.sh и
# enqueue-pane.sh, и правка одного разводила половинки по разным инсталляциям.

# pp_resolve заполняет массив PP_CMD командой запуска PromptPilot и возвращает
# 1, если приложения на машине нет.
#
# Импорт модуля проверяется явно: `python3 -m promptpilot` в роли безусловного
# фолбэка был не запасным вариантом, а способом упасть позже и непонятнее.
# Человек, поставивший плагин из маркетплейса раньше самого PromptPilot, видел
# только строчку в ~/.promptpilot/startup.log — плагин молча ничего не делал.
pp_resolve() {
    if command -v pp > /dev/null 2>&1; then
        PP_CMD=(pp)
    elif [ -x "$HOME/.local/bin/pp" ]; then
        PP_CMD=("$HOME/.local/bin/pp")
    elif command -v python3 > /dev/null 2>&1 && python3 -c 'import promptpilot' > /dev/null 2>&1; then
        PP_CMD=(python3 -m promptpilot)
    else
        PP_CMD=()
        return 1
    fi
    return 0
}

PP_MISSING_TITLE="PromptPilot не установлен"
PP_MISSING_HINT="Плагин ставит задачи в очередь PromptPilot, но самого приложения на этой машине нет.
Установка: git clone https://github.com/ivanarama/PromptPilot && cd PromptPilot && pip install -e .
Подробности: https://github.com/ivanarama/PromptPilot#установка"

# pp_notify_missing печатает подсказку в stdout вызвавшего скрипта (то есть в
# лог startup-хука или прямо в popup-панель) и дублирует её нативным
# уведомлением herdr — из лога плагина о проблеме никто сам не догадается.
pp_notify_missing() {
    printf '%s\n%s\n' "$PP_MISSING_TITLE" "$PP_MISSING_HINT"
    herdr_bin=${HERDR_BIN_PATH:-herdr}
    command -v "$herdr_bin" > /dev/null 2>&1 || return 0
    "$herdr_bin" notification show "$PP_MISSING_TITLE" \
        --body "$PP_MISSING_HINT" --sound request > /dev/null 2>&1 || true
}
