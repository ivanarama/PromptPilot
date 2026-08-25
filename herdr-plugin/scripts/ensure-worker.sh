#!/usr/bin/env bash
# Стартап-хук плагина: поднимает pp worker, если он ещё не запущен.
# Идемпотентен — уже работающий worker не трогается.
set -u

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

# Тот же порядок поиска, что в enqueue-pane.sh, иначе половинки плагина
# запускают разные инсталляции.
if command -v pp > /dev/null 2>&1; then
    worker_cmd=(pp worker)
elif [ -x "$HOME/.local/bin/pp" ]; then
    worker_cmd=("$HOME/.local/bin/pp" worker)
else
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
