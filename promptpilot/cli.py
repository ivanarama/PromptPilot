"""CLI interface."""

import locale
import os
import sys


def _ensure_utf8():
    """Re-exec with PYTHONUTF8=1 when the locale would garble non-ASCII argv.

    SSH sessions without a UTF-8 locale make Python decode argv as Latin-1/
    ASCII, so Cyrillic prompts get stored double-encoded. Re-executing in
    UTF-8 Mode restores the original bytes (locale codecs round-trip argv).
    POSIX only: on Windows argv is Unicode natively.
    """
    if os.name != "posix" or sys.flags.utf8_mode:
        return
    enc = locale.getpreferredencoding(False) or ""
    if "utf" in enc.lower():
        return
    os.environ["PYTHONUTF8"] = "1"
    try:
        os.execv(sys.executable, [sys.executable, "-m", "promptpilot", *sys.argv[1:]])
    except OSError:
        pass  # keep going with the current (possibly lossy) encoding


_ensure_utf8()

import click

from . import db
from .models import TaskCreate, TaskStatus
from .worktree import branch_for as wt_branch


def _fix_mojibake(text: str) -> str:
    """Repair UTF-8 text that was mis-decoded as Latin-1 on the way in
    (broken terminal/clipboard paste): 'Ð¿ÑÐ¸Ð²ÐµÑ' → 'привет'.

    Only rewrites when the round-trip is byte-exact AND yields non-Latin
    script (Cyrillic etc.) — legit text like 'mañana' is left untouched.
    """
    try:
        repaired = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    if repaired != text and any(ord(c) >= 0x400 for c in repaired):
        click.secho("⚠ Текст пришёл в битой кодировке — исправлено автоматически.", fg="yellow")
        return repaired
    return text


def _status_color(status: str) -> str:
    return {
        "pending": "white",
        "running": "cyan",
        "completed": "green",
        "failed": "red",
        "rate_limited": "yellow",
        "cancelled": "magenta",
    }.get(status, "white")


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """PromptPilot — AI Prompt Scheduler"""
    if ctx.invoked_subcommand is None:
        # Default: launch tray app when run without arguments
        from .tray import run_tray
        run_tray()


@cli.command()
@click.argument("prompt", required=False)
@click.option("-f", "--file", "file_path", type=click.Path(exists=True), help="File with prompts (one per line)")
@click.option("-p", "--priority", default=5, type=click.IntRange(1, 10), help="Priority 1-10 (1=highest)")
@click.option("-a", "--at", "scheduled_at", help="Schedule time (ISO format, e.g. 2026-03-25T03:00)")
@click.option("-d", "--dir", "working_dir", help="Working directory for claude execution")
@click.option("-c", "--cli", "provider", default=None, help="CLI provider: claude, claude-z, or custom command")
@click.option("-r", "--max-retries", default=5, type=int, help="Max retries on rate limit")
@click.option("-w", "--worktree", is_flag=True, help="Run in a fresh git worktree of --dir (branch pp/t<id>)")
def add(prompt, file_path, priority, scheduled_at, working_dir, provider, max_retries, worktree):
    """Add a task (or multiple from file)."""
    from datetime import datetime

    prompts = []
    if file_path:
        with open(file_path, encoding="utf-8") as f:
            prompts = [line.strip() for line in f if line.strip()]
    elif prompt:
        prompts = [prompt]
    else:
        click.echo("Provide a prompt or --file")
        return
    prompts = [_fix_mojibake(p) for p in prompts]

    dt = datetime.fromisoformat(scheduled_at) if scheduled_at else None

    for p in prompts:
        task = db.create_task(TaskCreate(
            prompt=p,
            working_dir=working_dir,
            provider=provider,
            priority=priority,
            scheduled_at=dt,
            max_retries=max_retries,
            worktree=worktree,
        ))
        cli_info = f" [{provider}]" if provider else ""
        time_info = f" at {dt}" if dt else ""
        wt_info = f" [{wt_branch(task.id)}]" if worktree else ""
        click.echo(f"  #{task.id} [P{priority}]{cli_info}{time_info}{wt_info} {p[:70]}")

    click.echo(click.style(f"\n{len(prompts)} task(s) added.", fg="green"))


@cli.command("list")
@click.option("-s", "--status", type=click.Choice([s.value for s in TaskStatus]), help="Filter by status")
@click.option("-n", "--limit", default=20, help="Number of tasks to show")
def list_tasks(status, limit):
    """List tasks."""
    st = TaskStatus(status) if status else None
    tasks = db.list_tasks(status=st, limit=limit)

    if not tasks:
        click.echo("No tasks found.")
        return

    click.echo(f"{'ID':>5}  {'Status':<13}  {'P':>1}  {'Retries':>7}  Prompt")
    click.echo("-" * 80)
    for t in tasks:
        prompt_short = t.prompt[:50].replace("\n", " ")
        status_str = click.style(f"{t.status.value:<13}", fg=_status_color(t.status.value))
        click.echo(f"{t.id:>5}  {status_str}  {t.priority:>1}  {t.retry_count:>3}/{t.max_retries:<3}  {prompt_short}")


@cli.command("usage")
@click.option("--hours", default=5.0, help="Окно в часах (по умолчанию 5 — окно лимита)")
@click.option("--json", "as_json", is_flag=True, help="Машинно-читаемо")
def usage_cmd(hours, as_json):
    """Расход за окно — по всем сессиям Claude Code, не только по задачам pp."""
    import json as _json
    from .usage import summary

    data = summary(hours)
    if as_json:
        click.echo(_json.dumps(data, ensure_ascii=False, indent=2))
        return

    click.echo(f"За последние {data['window_hours']:g} ч — оценка по прайсу API:")
    click.echo(f"  всего:   ${data['cost']:.2f}   {data['tokens'] / 1e6:.1f} млн токенов"
               f"   сессий: {data['sessions']}")
    click.echo(f"  задачи:  ${data['cost_tasks']:.2f}")
    click.echo(f"  прочее:  ${data['cost_other']:.2f}   (живая переписка ест то же окно лимита)")
    if data["top"]:
        click.echo("\n  Дороже всего:")
        for row in data["top"]:
            mark = "задача" if row["task"] else "  —   "
            click.echo(f"    ${row['cost']:>8.2f}  {mark}  {row['model']:<18}  {row['dir']}")


@cli.command("guard-hook", hidden=True)
@click.option("--data-dir", default=None, help="Where guard.json and guard.log live")
def guard_hook(data_dir):
    """PreToolUse hook entry point (Claude Code calls this, not a human)."""
    if data_dir:
        os.environ["PP_DATA_DIR"] = data_dir
    from .guard import main
    sys.exit(main())


@cli.command("guard")
@click.argument("command", required=False)
@click.option("--rules", is_flag=True, help="List the rules in force")
def guard_cmd(command, rules):
    """Check what the guard would do with COMMAND (nothing runs)."""
    from .config import GUARD, guard_settings_file
    from .guard import check, load_rules

    if rules or not command:
        click.echo(f"PP_GUARD={GUARD}  settings: {guard_settings_file() or '(не записан)'}")
        for pattern, reason in load_rules():
            click.echo(f"  {pattern}\n      {reason}")
        if not command:
            return

    reason = check(command, os.getcwd(), "Bash")
    if reason:
        click.echo(click.style(f"ЗАПРЕЩЕНО. {reason}", fg="red"))
        sys.exit(2)
    click.echo(click.style("Разрешено.", fg="green"))


@cli.command()
@click.argument("task_id", type=int)
def status(task_id):
    """Show task details."""
    task = db.get_task(task_id)
    if not task:
        click.echo(f"Task #{task_id} not found.")
        return

    click.echo(f"Task #{task.id}")
    click.echo(f"  Status:    {click.style(task.status.value, fg=_status_color(task.status.value))}")
    click.echo(f"  Provider:  {task.provider or 'claude (default)'}")
    click.echo(f"  Priority:  {task.priority}")
    click.echo(f"  Created:   {task.created_at}")
    if task.scheduled_at:
        click.echo(f"  Scheduled: {task.scheduled_at}")
    if task.started_at:
        click.echo(f"  Started:   {task.started_at}")
    if task.completed_at:
        click.echo(f"  Completed: {task.completed_at}")
    click.echo(f"  Retries:   {task.retry_count}/{task.max_retries}")
    if task.working_dir:
        click.echo(f"  Dir:       {task.working_dir}")
    if task.worktree:
        click.echo(f"  Worktree:  {task.worktree_path or '(ещё не создан)'}"
                   f"  [{task.worktree_branch or wt_branch(task.id)}]")
    if task.next_run_at:
        click.echo(f"  Next run:  {task.next_run_at}")
    click.echo(f"\n  Prompt:\n    {task.prompt}")
    if task.result:
        click.echo(f"\n  Result:\n    {task.result[:500]}")
    if task.error:
        click.echo(f"\n  Error:\n    {task.error[:500]}")


@cli.command()
@click.argument("task_id", type=int)
def cancel(task_id):
    """Cancel a pending/rate_limited task."""
    if db.cancel_task(task_id):
        click.echo(click.style(f"Task #{task_id} cancelled.", fg="yellow"))
    else:
        click.echo("Cannot cancel (task not found or already running/completed).")


@cli.command()
@click.argument("task_id", type=int)
def delete(task_id):
    """Delete a task."""
    if db.delete_task(task_id):
        click.echo(f"Task #{task_id} deleted.")
    else:
        click.echo("Task not found.")


@cli.command()
@click.option("--days", default=7, help="Delete tasks older than N days")
def purge(days):
    """Delete old completed/failed/cancelled tasks."""
    count = db.purge_old(days)
    click.echo(f"Purged {count} task(s).")


@cli.command()
def stats():
    """Show task statistics."""
    s = db.get_stats()
    click.echo(f"  Pending:      {s.pending}")
    click.echo(f"  Running:      {s.running}")
    click.echo(f"  Rate Limited: {s.rate_limited}")
    click.echo(f"  Completed:    {s.completed}")
    click.echo(f"  Failed:       {s.failed}")
    click.echo(f"  Cancelled:    {s.cancelled}")
    click.echo(f"  Total:        {s.total}")


@cli.command()
@click.argument("action", required=False, default="list")
@click.argument("name", required=False)
@click.option("--cmd", "cmd_template", help='Command template, e.g. "myai --run {prompt}"')
@click.option("--executor", type=click.Choice(["herdr"]), help="Run via an executor instead of a command template")
@click.option("--kind", default=None, help="Agent kind for --executor herdr (claude, codex, gemini, cursor, opencode, grok, ...)")
@click.option("--keep-pane", is_flag=True, help="herdr: keep the pane open after a successful task")
@click.option("--models", "models_csv", default="", help='Model list for the picker, comma-separated: "a,b,c"')
@click.option("--args", "args_str", default="", help='herdr: extra agent CLI args, e.g. "--effort max"')
@click.option("--desc", default="", help="Description")
@click.option("--env", "env_vars", multiple=True, help='Env vars: KEY=VALUE (repeat for multiple)')
def provider(action, name, cmd_template, executor, kind, keep_pane, models_csv, args_str, desc, env_vars):
    """Manage CLI providers. Actions: list, add, remove, hide, unhide.

    \b
    Examples:
      pp provider                              # list all
      pp provider add myai --cmd "myai {prompt}"
      pp provider add codex-herdr --executor herdr --kind codex --desc "Codex в herdr"
      pp provider hide claude-z                # убрать из списков UI/бота
      pp provider remove myai
    """
    from .config import (DEFAULT_CLI, load_providers, provider_available,
                         remove_provider, save_provider, set_provider_hidden)

    if action == "list" or (action is None and name is None):
        provs = load_providers()
        click.echo("Available providers:\n")
        for pname, info in provs.items():
            default = " (default)" if pname == DEFAULT_CLI else ""
            flags = []
            if info.get("hidden"):
                flags.append("hidden")
            if not provider_available(info):
                flags.append("not installed")
            flag_str = click.style(f"  [{', '.join(flags)}]", fg="yellow") if flags else ""
            pdesc = info.get("description", "")
            click.echo(f"  {click.style(pname, fg='cyan')}{default}{flag_str}")
            if pdesc:
                click.echo(f"    {pdesc}")
            if info.get("executor"):
                extra = " (keep pane)" if info.get("keep_pane") else ""
                click.echo(f"    executor: {info['executor']}, kind: {info.get('kind', 'claude')}{extra}")
            else:
                click.echo(f"    cmd: {info.get('cmd', '')}")
            click.echo()
        click.echo("  Add custom: pp provider add <name> --cmd \"<command> {prompt}\"")
        click.echo("  herdr:      pp provider add <name> --executor herdr --kind <agent>")
        click.echo("  Config:     ~/.promptpilot/providers.json")

    elif action == "add":
        if not name:
            click.echo("Usage: pp provider add <name> --cmd \"...\" | --executor herdr --kind <agent>")
            return
        env = {}
        for kv in env_vars:
            if "=" in kv:
                k, v = kv.split("=", 1)
                env[k.strip()] = v.strip()
        models = [m.strip() for m in models_csv.split(",") if m.strip()]
        import shlex
        extra_args = shlex.split(args_str) if args_str else None
        if executor:
            save_provider(name, description=desc, env=env, executor=executor,
                          kind=kind, keep_pane=keep_pane, models=models, args=extra_args)
            extra = ", keep pane" if keep_pane else ""
            click.echo(click.style(f"Provider '{name}' added: executor {executor}, kind {kind or 'claude'}{extra}", fg="green"))
        else:
            if not cmd_template:
                # Default: treat name as the command, just append {prompt}
                cmd_template = f"{name} {{prompt}}"
            if "{prompt}" not in cmd_template:
                cmd_template += " {prompt}"
            save_provider(name, cmd_template, desc, env=env, models=models)
            click.echo(click.style(f"Provider '{name}' added: {cmd_template}", fg="green"))
        if env:
            click.echo(f"  Env: {', '.join(env.keys())}")

    elif action in ("hide", "unhide"):
        if not name:
            click.echo(f"Usage: pp provider {action} <name>")
            return
        if set_provider_hidden(name, action == "hide"):
            click.echo(f"Provider '{name}' {'hidden' if action == 'hide' else 'visible again'}.")
        else:
            click.echo(f"Provider '{name}' not found.")

    elif action == "remove":
        if not name:
            click.echo("Usage: pp provider remove <name>")
            return
        if remove_provider(name):
            click.echo(f"Provider '{name}' removed.")
        else:
            click.echo(f"Provider '{name}' not found in custom providers.")

    else:
        click.echo(f"Unknown action: {action}. Use: list, add, remove, hide, unhide")


@cli.command()
def worker():
    """Start the worker (executes queued tasks)."""
    from .worker import run_worker
    run_worker()


@cli.command()
def bot():
    """Start the Telegram bot (requires PP_TG_TOKEN env var)."""
    from .bot import run_bot
    run_bot()


@cli.command()
def tray():
    """Start the system tray launcher (default when run without arguments)."""
    from .tray import run_tray
    run_tray()


@cli.command()
@click.option("-h", "--host", default=None, help="Host (default: 127.0.0.1)")
@click.option("-p", "--port", default=None, type=int, help="Port (default: 8420)")
def server(host, port):
    """Start the web UI server."""
    import uvicorn
    from .config import HOST, PORT

    h = host or HOST
    p = port or PORT
    from .api import app
    click.echo(f"PromptPilot UI: http://{h}:{p}")
    uvicorn.run(app, host=h, port=p, log_level="info")


if __name__ == "__main__":
    cli()
