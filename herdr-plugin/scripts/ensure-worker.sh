#!/usr/bin/env bash
# Стартап-хук плагина: поднимает pp worker, если он ещё не запущен.
# Идемпотентен — уже работающий worker не трогается.
set -u

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/lib.sh
. "$script_dir/lib.sh"

state_dir=${HERDR_PLUGIN_STATE_DIR:-$HOME/.promptpilot}
mkdir -p "$state_dir"
log_file=$state_dir/startup.log

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" | tee -a "$log_file"
}

# Worker запускают минимум тремя способами: `pp worker` (README), `./dist/pp
# worker` (собранный бинарь) и `python3 -m promptpilot worker` (авто-релоад сам
# ре-execает себя именно так). Проверять надо все три: второй worker на одной
# базе сбрасывает running-задачи первого и ломает взаимную блокировку по
# рабочей директории. Скобки — чтобы grep не нашёл сам себя.
worker_running() {
    ps -eo pid,args 2> /dev/null \
        | grep -Eq "([-]m[[:space:]]+promptpilot([.]__main__)?|[/[:space:]][p]p)[[:space:]]+worker"
}

if worker_running; then
    log "pp worker уже работает — ничего не делаю"
    exit 0
fi

if ! pp_resolve; then
    pp_notify_missing | tee -a "$log_file"
    # Это не отказ плагина: его штатно ставят раньше самого PromptPilot, и
    # красной записи в `herdr plugin log` такая ситуация не заслуживает —
    # человеку уже сказали, что делать, уведомлением.
    exit 0
fi

worker_cmd=("${PP_CMD[@]}" worker)
if [ "${PP_CMD[0]}" = "python3" ]; then
    # -u обязателен: без него лог worker'а буферизуется и отстаёт от реальности.
    worker_cmd=(python3 -u -m promptpilot worker)
fi

log "pp worker не найден — запускаю: ${worker_cmd[*]}"
mkdir -p "$HOME/.promptpilot"
cd "$HOME" || exit 1
setsid nohup "${worker_cmd[@]}" >> "$HOME/.promptpilot/worker.log" 2>&1 < /dev/null &

sleep 1
if worker_running; then
    log "pp worker запущен (лог: ~/.promptpilot/worker.log)"
else
    log "не удалось запустить pp worker — смотри ~/.promptpilot/worker.log"
    exit 1
fi
