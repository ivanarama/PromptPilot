"""CLI interface."""

import click

from . import db
from .models import TaskCreate, TaskStatus


def _status_color(status: str) -> str:
    return {
        "pending": "white",
        "running": "cyan",
        "completed": "green",
        "failed": "red",
        "rate_limited": "yellow",
        "cancelled": "magenta",
    }.get(status, "white")


@click.group()
def cli():
    """PromptPilot — AI Prompt Scheduler"""
    pass


@cli.command()
@click.argument("prompt", required=False)
@click.option("-f", "--file", "file_path", type=click.Path(exists=True), help="File with prompts (one per line)")
@click.option("-p", "--priority", default=5, type=click.IntRange(1, 10), help="Priority 1-10 (1=highest)")
@click.option("-a", "--at", "scheduled_at", help="Schedule time (ISO format, e.g. 2026-03-25T03:00)")
@click.option("-d", "--dir", "working_dir", help="Working directory for claude execution")
@click.option("-c", "--cli", "provider", default=None, help="CLI provider: claude, claude-z, or custom command")
@click.option("-r", "--max-retries", default=5, type=int, help="Max retries on rate limit")
def add(prompt, file_path, priority, scheduled_at, working_dir, provider, max_retries):
    """Add a task (or multiple from file)."""
    from datetime import datetime

    prompts = []
    if file_path:
        with open(file_path) as f:
            prompts = [line.strip() for line in f if line.strip()]
    elif prompt:
        prompts = [prompt]
    else:
        click.echo("Provide a prompt or --file")
        return

    dt = datetime.fromisoformat(scheduled_at) if scheduled_at else None

    for p in prompts:
        task = db.create_task(TaskCreate(
            prompt=p,
            working_dir=working_dir,
            provider=provider,
            priority=priority,
            scheduled_at=dt,
            max_retries=max_retries,
        ))
        cli_info = f" [{provider}]" if provider else ""
        time_info = f" at {dt}" if dt else ""
        click.echo(f"  #{task.id} [P{priority}]{cli_info}{time_info} {p[:70]}")

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
@click.option("--desc", default="", help="Description")
@click.option("--env", "env_vars", multiple=True, help='Env vars: KEY=VALUE (repeat for multiple)')
def provider(action, name, cmd_template, desc, env_vars):
    """Manage CLI providers. Actions: list, add, remove.

    \b
    Examples:
      pp provider                              # list all
      pp provider add myai --cmd "myai {prompt}"
      pp provider remove myai
    """
    from .config import DEFAULT_CLI, load_providers, save_provider, remove_provider

    if action == "list" or (action is None and name is None):
        provs = load_providers()
        click.echo("Available providers:\n")
        for pname, info in provs.items():
            default = " (default)" if pname == DEFAULT_CLI else ""
            pdesc = info.get("description", "")
            click.echo(f"  {click.style(pname, fg='cyan')}{default}")
            if pdesc:
                click.echo(f"    {pdesc}")
            click.echo(f"    cmd: {info['cmd']}")
            click.echo()
        click.echo("  Add custom: pp provider add <name> --cmd \"<command> {prompt}\"")
        click.echo("  Config:     ~/.promptpilot/providers.json")

    elif action == "add":
        if not name:
            click.echo("Usage: pp provider add <name> --cmd \"<command> {prompt}\"")
            return
        if not cmd_template:
            # Default: treat name as the command, just append {prompt}
            cmd_template = f"{name} {{prompt}}"
        if "{prompt}" not in cmd_template:
            cmd_template += " {prompt}"
        env = {}
        for kv in env_vars:
            if "=" in kv:
                k, v = kv.split("=", 1)
                env[k.strip()] = v.strip()
        save_provider(name, cmd_template, desc, env=env)
        click.echo(click.style(f"Provider '{name}' added: {cmd_template}", fg="green"))
        if env:
            click.echo(f"  Env: {', '.join(env.keys())}")

    elif action == "remove":
        if not name:
            click.echo("Usage: pp provider remove <name>")
            return
        if remove_provider(name):
            click.echo(f"Provider '{name}' removed.")
        else:
            click.echo(f"Provider '{name}' not found in custom providers.")

    else:
        click.echo(f"Unknown action: {action}. Use: list, add, remove")


@cli.command()
def worker():
    """Start the worker (executes queued tasks)."""
    from .worker import run_worker
    run_worker()


@cli.command()
@click.option("-h", "--host", default=None, help="Host (default: 127.0.0.1)")
@click.option("-p", "--port", default=None, type=int, help="Port (default: 8420)")
def server(host, port):
    """Start the web UI server."""
    import uvicorn
    from .config import HOST, PORT

    h = host or HOST
    p = port or PORT
    click.echo(f"PromptPilot UI: http://{h}:{p}")
    uvicorn.run("promptpilot.api:app", host=h, port=p, log_level="info")


if __name__ == "__main__":
    cli()
