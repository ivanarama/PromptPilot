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
    # When frozen (PyInstaller) sys.executable IS pp itself and knows no "-m";
    # re-exec it with the original args instead.
    if getattr(sys, "frozen", False):
        argv = [sys.executable, *sys.argv[1:]]
    else:
        argv = [sys.executable, "-m", "promptpilot", *sys.argv[1:]]
    try:
        os.execv(argv[0], argv)
    except OSError:
        pass  # keep going with the current (possibly lossy) encoding


_ensure_utf8()


def _ensure_windows_utf8_streams():
    """Keep Unicode workflow/article text writable through Windows pipes.

    Interactive Windows consoles normally use Python's Unicode console path,
    but redirected stdout can still inherit CP1251. Workflow objectives and
    evidence legitimately contain arrows and other characters outside CP1251.
    """
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


_ensure_windows_utf8_streams()

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
        # Default: launch tray app when run without arguments. On a headless
        # server (no display, or pystray/Pillow missing) this can't work — show
        # help instead of dying with a traceback.
        try:
            from .tray import run_tray
        except Exception as e:
            click.echo(ctx.get_help())
            click.echo(f"\n(Трей недоступен: {e}. На сервере запускай подкоманды, "
                       "например `pp worker`, `pp server`.)")
            return
        try:
            run_tray()
        except Exception as e:
            click.echo(ctx.get_help())
            click.echo(f"\n(Трей не запустился: {e}. Нет графической среды? "
                       "Запусти `pp worker`/`pp server` напрямую.)")


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
        raise click.UsageError("Provide a prompt or --file")
    prompts = [_fix_mojibake(p) for p in prompts]

    try:
        dt = datetime.fromisoformat(scheduled_at) if scheduled_at else None
    except ValueError:
        raise click.UsageError(
            f"Не понимаю дату {scheduled_at!r}. Формат ISO, например 2026-03-25T03:00 "
            "(локальное время машины).")

    if provider:
        from .config import load_providers
        import shutil
        first = provider.split()[0] if provider else ""
        if provider not in load_providers() and not shutil.which(first):
            click.secho(f"⚠ Провайдер «{provider}» не найден среди известных и не в PATH — "
                        "задача, скорее всего, упадёт. Список: pp provider", fg="yellow")

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


@cli.command("note")
@click.argument("task_id", type=int)
@click.argument("text", required=False)
@click.option("--clear", is_flag=True, help="Убрать приписку")
def note_cmd(task_id, text, clear):
    """Дописать решателю: пара фраз, которые пойдут в следующий прогон задачи."""
    task = db.get_task(task_id)
    if not task:
        click.echo(f"Задача #{task_id} не найдена.")
        sys.exit(1)
    if clear:
        db.set_note(task_id, "")
        click.echo("Приписка убрана.")
        return
    if not text:
        click.echo(task.note or "(приписки нет)")
        return
    db.set_note(task_id, text)
    click.echo(click.style(f"Приписка записана к #{task_id}.", fg="green"))
    if task.status.value == "running":
        click.echo("Прогон уже идёт — эту приписку он не увидит: она уйдёт "
                   "в следующий прогон (после rate limit / срыва среды).")


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
        sys.exit(1)

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
    if task.verdict:
        click.echo(f"  Итог:      {task.verdict}")
    if task.note:
        click.echo(f"  Приписка:  {task.note}")
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
        sys.exit(1)


@cli.command()
@click.argument("task_id", type=int)
def delete(task_id):
    """Delete a task."""
    if db.delete_task(task_id):
        click.echo(f"Task #{task_id} deleted.")
    else:
        click.echo("Task not found.")
        sys.exit(1)


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


@cli.group("workflow")
def workflow_group():
    """Manage the W1 workflow pilot and its append-only history."""


@workflow_group.command("create")
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False))
def workflow_create(file_path):
    """Create a draft from a UTF-8 JSON WorkflowCreate document."""
    import json as _json
    from pydantic import ValidationError

    from .models import WorkflowCreate

    try:
        with open(file_path, encoding="utf-8") as handle:
            payload = WorkflowCreate.model_validate(_json.load(handle))
    except (OSError, ValueError, ValidationError) as exc:
        raise click.ClickException(f"Invalid workflow file: {exc}") from exc
    created = _workflow_call_cli(db.create_workflow, payload)
    click.echo(
        click.style(
            f"Created {created.slug} ({created.id}), version "
            f"{created.state_version}",
            fg="green",
        )
    )


@workflow_group.command("list")
@click.option("-s", "--status", type=click.Choice([
    "draft", "planning", "awaiting_plan_approval", "queued", "executing", "gating", "reviewing",
    "revision_required", "awaiting_human", "completed", "failed",
    "cancelled",
]))
@click.option("-n", "--limit", default=20, type=click.IntRange(1, 500))
@click.option("--json", "as_json", is_flag=True, help="Машинно-читаемый вывод")
def workflow_list(status, limit, as_json):
    """List workflows, newest activity first."""
    import json as _json

    workflows = db.list_workflows(status=status, limit=limit)
    if as_json:
        click.echo(_json.dumps(
            [item.model_dump(mode="json") for item in workflows],
            ensure_ascii=False,
            indent=2,
        ))
        return
    if not workflows:
        click.echo("Workflows not found.")
        return
    click.echo(f"{'Slug':<28} {'Status':<19} {'Round':>5} {'Ver':>4}  Objective")
    click.echo("-" * 100)
    for item in workflows:
        objective = item.objective.replace("\n", " ")[:40]
        click.echo(
            f"{item.slug:<28} {item.status.value:<19} "
            f"{item.current_round:>5} {item.state_version:>4}  {objective}"
        )


def _workflow_or_exit(reference):
    workflow = db.get_workflow_by_ref(reference)
    if not workflow:
        raise click.ClickException(f"Workflow {reference!r} not found")
    return workflow


def _workflow_call_cli(call, *args):
    try:
        return call(*args)
    except (db.WorkflowConflictError, db.WorkflowNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


@workflow_group.command("show")
@click.argument("reference")
@click.option("--json", "as_json", is_flag=True, help="Машинно-читаемый вывод")
def workflow_show(reference, as_json):
    """Show a workflow by id or slug."""
    import json as _json

    item = _workflow_or_exit(reference)
    if as_json:
        click.echo(_json.dumps(item.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return
    click.echo(f"Workflow {item.slug}")
    click.echo(f"  ID:         {item.id}")
    click.echo(f"  Status:     {item.status.value}")
    click.echo(f"  Round:      {item.current_round}")
    click.echo(f"  Version:    {item.state_version}")
    click.echo(f"  Repository: {item.repository_path}")
    click.echo(f"  Branch:     {item.candidate_branch}")
    click.echo(f"  Created:    {item.created_at}")
    click.echo(f"  Updated:    {item.updated_at}")
    click.echo(f"\n  Objective:\n    {item.objective}")
    if item.config:
        rendered = _json.dumps(item.config, ensure_ascii=False, indent=2)
        click.echo(f"\n  Config:\n{rendered}")


@workflow_group.command("rounds")
@click.argument("reference")
@click.option("--json", "as_json", is_flag=True, help="Машинно-читаемый вывод")
def workflow_rounds(reference, as_json):
    """List rounds of a workflow."""
    import json as _json

    workflow = _workflow_or_exit(reference)
    rounds = db.list_workflow_rounds(workflow.id)
    if as_json:
        click.echo(_json.dumps(
            [item.model_dump(mode="json") for item in rounds],
            ensure_ascii=False,
            indent=2,
        ))
        return
    if not rounds:
        click.echo("Rounds not found.")
        return
    click.echo(f"{'Round':>5} {'Status':<19} {'Base':<12} {'Candidate':<12} Started")
    for item in rounds:
        click.echo(
            f"{item.round_no:>5} {item.status.value:<19} "
            f"{(item.base_sha or '-')[:12]:<12} "
            f"{(item.candidate_sha or '-')[:12]:<12} {item.started_at}"
        )


@workflow_group.command("events")
@click.argument("reference")
@click.option("--after", "after_seq", default=0, type=click.IntRange(0))
@click.option("-n", "--limit", default=200, type=click.IntRange(1, 1000))
@click.option("--json", "as_json", is_flag=True, help="Машинно-читаемый вывод")
def workflow_events(reference, after_seq, limit, as_json):
    """Show append-only workflow events."""
    import json as _json

    workflow = _workflow_or_exit(reference)
    events = db.list_workflow_events(workflow.id, after_seq=after_seq, limit=limit)
    if as_json:
        click.echo(_json.dumps(
            [item.model_dump(mode="json") for item in events],
            ensure_ascii=False,
            indent=2,
        ))
        return
    if not events:
        click.echo("Events not found.")
        return
    for item in events:
        scope = item.round_id or item.run_id or "-"
        payload = _json.dumps(item.payload, ensure_ascii=False, sort_keys=True)
        click.echo(
            f"{item.seq:>6} {item.created_at.isoformat()} "
            f"{item.event_type:<24} {scope:<18} {payload}"
        )


@workflow_group.command("findings")
@click.argument("reference")
@click.option("-s", "--status", type=click.Choice([
    "open", "resolved", "reopened", "accepted_risk",
]))
@click.option("--json", "as_json", is_flag=True, help="Машинно-читаемый вывод")
def workflow_findings(reference, status, as_json):
    """Show current materialized findings and their fingerprints."""
    import json as _json

    workflow = _workflow_or_exit(reference)
    findings = db.list_workflow_findings(workflow.id, status=status)
    if as_json:
        click.echo(_json.dumps(
            [item.model_dump(mode="json") for item in findings],
            ensure_ascii=False,
            indent=2,
        ))
        return
    if not findings:
        click.echo("Findings not found.")
        return
    click.echo(f"{'Severity':<9} {'Status':<14} {'Rounds':<9} Fingerprint / title")
    click.echo("-" * 100)
    for item in findings:
        rounds = f"{item.first_seen_round}-{item.last_seen_round}"
        click.echo(
            f"{item.severity.value:<9} {item.status.value:<14} {rounds:<9} "
            f"{item.fingerprint} / {item.title}"
        )


@workflow_group.command("start")
@click.argument("reference")
@click.option("--base-sha", default=None, help="Commit/base identity первого нового раунда")
def workflow_start(reference, base_sha):
    """Start a draft and create its first non-historical round."""
    from . import workflows
    from .models import WorkflowStartRequest

    item = _workflow_or_exit(reference)
    updated = _workflow_call_cli(
        workflows.start_workflow,
        item.id,
        WorkflowStartRequest(
            expected_version=item.state_version,
            base_sha=base_sha,
        ),
    )
    click.echo(
        click.style(
            f"Workflow {updated.slug}: {updated.status.value}, "
            f"round {updated.current_round}, version {updated.state_version}",
            fg="green",
        )
    )


@workflow_group.command("dispatch")
@click.argument("reference")
@click.argument("role", type=click.Choice(["executor", "reviewer"]))
@click.argument("prompt", required=False)
@click.option("-f", "--file", "file_path", type=click.Path(exists=True, dir_okay=False))
@click.option("-c", "--cli", "provider", default=None)
@click.option("-m", "--model", default=None)
@click.option("-p", "--priority", default=5, type=click.IntRange(1, 10))
@click.option("-d", "--dir", "working_dir", default=None)
@click.option("-w", "--worktree", is_flag=True)
@click.option("--skip-permissions", is_flag=True)
@click.option("--timeout", "task_timeout", type=click.IntRange(0), default=None)
def workflow_dispatch(reference, role, prompt, file_path, provider, model,
                      priority, working_dir, worktree, skip_permissions,
                      task_timeout):
    """Queue an executor or reviewer task for the current round."""
    from . import workflows
    from .models import WorkflowRole, WorkflowTaskDispatch

    if file_path:
        with open(file_path, encoding="utf-8") as handle:
            prompt = handle.read().strip()
    if not prompt:
        raise click.UsageError("Provide PROMPT or --file")
    item = _workflow_or_exit(reference)
    result = _workflow_call_cli(
        workflows.dispatch_task,
        item.id,
        WorkflowTaskDispatch(
            expected_version=item.state_version,
            role=WorkflowRole(role),
            prompt=prompt,
            provider=provider,
            model=model,
            priority=priority,
            working_dir=working_dir,
            worktree=worktree,
            skip_permissions=skip_permissions,
            task_timeout=task_timeout,
        ),
    )
    click.echo(
        click.style(
            f"Task #{result.task.id}, run {result.run.id}, "
            f"workflow {result.workflow.status.value} v{result.workflow.state_version}",
            fg="green",
        )
    )


@workflow_group.command("gate")
@click.argument("reference")
@click.argument("verdict", type=click.Choice(["PASS", "FAIL", "HUMAN_REQUIRED"]))
@click.option("--gate-id", default="manual-gate")
@click.option("--summary", default="")
@click.option("--evidence", multiple=True)
def workflow_gate(reference, verdict, gate_id, summary, evidence):
    """Record the deterministic/manual gate verdict for the current round."""
    from . import workflows
    from .models import GateVerdict, WorkflowGateDecision

    item = _workflow_or_exit(reference)
    updated = _workflow_call_cli(
        workflows.record_gate,
        item.id,
        WorkflowGateDecision(
            expected_version=item.state_version,
            verdict=GateVerdict(verdict),
            gate_id=gate_id,
            summary=summary,
            evidence=list(evidence),
        ),
    )
    click.echo(f"Workflow {updated.slug}: {updated.status.value}")


@workflow_group.command("review")
@click.argument("reference")
@click.argument(
    "verdict",
    type=click.Choice(["PASS", "REVISION_REQUIRED", "HUMAN_REQUIRED"]),
)
@click.option("--summary", default="")
@click.option(
    "--findings-file",
    type=click.Path(exists=True, dir_okay=False),
    help="JSON array of ReviewFindingInput objects",
)
def workflow_review(reference, verdict, summary, findings_file):
    """Submit the manual W1 decision after a completed reviewer task."""
    import json as _json
    from pydantic import ValidationError

    from . import workflows
    from .models import ReviewFindingInput, ReviewVerdict, WorkflowReviewDecision

    findings = []
    if findings_file:
        try:
            with open(findings_file, encoding="utf-8") as handle:
                findings = [
                    ReviewFindingInput.model_validate(item)
                    for item in _json.load(handle)
                ]
        except (OSError, ValueError, TypeError, ValidationError) as exc:
            raise click.ClickException(f"Invalid findings file: {exc}") from exc
    item = _workflow_or_exit(reference)
    updated = _workflow_call_cli(
        workflows.record_review,
        item.id,
        WorkflowReviewDecision(
            expected_version=item.state_version,
            verdict=ReviewVerdict(verdict),
            summary=summary,
            findings=findings,
        ),
    )
    click.echo(f"Workflow {updated.slug}: {updated.status.value}")


@workflow_group.command("input")
@click.argument("reference")
@click.argument("text")
@click.option("--resume", is_flag=True)
def workflow_input(reference, text, resume):
    """Append a human decision and optionally resume the workflow."""
    from . import workflows
    from .models import WorkflowHumanInput

    item = _workflow_or_exit(reference)
    updated = _workflow_call_cli(
        workflows.human_input,
        item.id,
        WorkflowHumanInput(
            expected_version=item.state_version,
            text=text,
            resume=resume,
        ),
    )
    click.echo(f"Workflow {updated.slug}: {updated.status.value}")


@workflow_group.command("cancel")
@click.argument("reference")
def workflow_cancel(reference):
    """Cancel a non-terminal workflow and its queued/running linked tasks."""
    from . import workflows

    item = _workflow_or_exit(reference)
    updated = _workflow_call_cli(
        workflows.cancel_workflow, item.id, item.state_version
    )
    click.echo(click.style(f"Workflow {updated.slug}: cancelled", fg="yellow"))


@workflow_group.command("sync")
@click.argument("reference")
def workflow_sync(reference):
    """Reconcile linked queue tasks after a crash or manual task update."""
    from . import workflows

    item = _workflow_or_exit(reference)
    count = _workflow_call_cli(workflows.sync_all_tasks, item.id)
    updated = _workflow_or_exit(item.id)
    click.echo(
        f"Synced {count} run(s); workflow {updated.status.value} "
        f"v{updated.state_version}"
    )


@workflow_group.command("import-history")
@click.argument("reference")
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False))
def workflow_import_history(reference, file_path):
    """Import historical rounds/facts from a provenance-labelled JSON file."""
    import json as _json
    from pydantic import ValidationError

    from . import workflows
    from .models import WorkflowHistoryImport

    item = _workflow_or_exit(reference)
    try:
        with open(file_path, encoding="utf-8") as handle:
            raw = _json.load(handle)
        raw["expected_version"] = item.state_version
        payload = WorkflowHistoryImport.model_validate(raw)
    except (OSError, ValueError, ValidationError) as exc:
        raise click.ClickException(f"Invalid history file: {exc}") from exc
    # The file can be prepared before the workflow exists; the current version
    # is authoritative at import time and cannot be forged by stale JSON.
    updated = _workflow_call_cli(
        workflows.import_history, item.id, payload
    )
    click.echo(
        click.style(
            f"Imported history through round {updated.current_round}; "
            f"workflow version {updated.state_version}",
            fg="green",
        )
    )


@cli.command()
@click.argument("action", required=False, default="list")
@click.argument("name", required=False)
@click.option("--cmd", "cmd_template", help='Command template, e.g. "myai --run {prompt}"')
@click.option("--executor", type=click.Choice(["herdr"]), help="Run via an executor instead of a command template")
@click.option("--kind", default=None, help="Agent kind for --executor herdr (claude, codex, gemini, cursor, opencode, grok, ...)")
@click.option("--keep-pane", is_flag=True, help="herdr: keep the pane open after a successful task")
@click.option("--models", "models_csv", default="", help='Model list for the picker, comma-separated: "a,b,c"')
@click.option("--args", "args_str", default="", help='herdr: extra agent CLI args, e.g. "--model opus"')
@click.option("--effort", default="", help="Claude Code reasoning effort: low|medium|high|xhigh|max")
@click.option("--desc", default="", help="Description")
@click.option("--env", "env_vars", multiple=True, help='Env vars: KEY=VALUE (repeat for multiple)')
def provider(action, name, cmd_template, executor, kind, keep_pane, models_csv, args_str, effort, desc, env_vars):
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
                          kind=kind, keep_pane=keep_pane, models=models, args=extra_args,
                          effort=effort or None)
            extra = ", keep pane" if keep_pane else ""
            click.echo(click.style(f"Provider '{name}' added: executor {executor}, kind {kind or 'claude'}{extra}", fg="green"))
        else:
            if not cmd_template:
                # Default: treat name as the command, just append {prompt}
                cmd_template = f"{name} {{prompt}}"
            if "{prompt}" not in cmd_template:
                cmd_template += " {prompt}"
            save_provider(name, cmd_template, desc, env=env, models=models, effort=effort or None)
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
        sys.exit(1)


@cli.command()
def worker():
    """Start the worker (executes queued tasks)."""
    from .worker import run_worker
    run_worker()


@cli.command(
    "pipelinectl",
    hidden=True,
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def pipelinectl(args):
    """Run the bundled project pipeline adapter."""
    from .project_pipeline import run
    raise SystemExit(run(list(args)))


@cli.command()
def bot():
    """Start the Telegram bot (requires PP_TG_TOKEN env var)."""
    from .bot import run_bot
    try:
        run_bot()
    except RuntimeError as e:
        raise click.ClickException(
            f"{e}.\nПолучи токен у @BotFather и задай PP_TG_TOKEN (в .env рядом с pp "
            "или в окружении).")


@cli.command()
def tray():
    """Start the system tray launcher (default when run without arguments)."""
    try:
        from .tray import run_tray
        run_tray()
    except Exception as e:
        raise click.ClickException(
            f"Трей недоступен ({e}). На сервере запускай `pp worker`/`pp server`/`pp bot`.")


@cli.command()
@click.option("-h", "--host", default=None, help="Host (default: 127.0.0.1)")
@click.option("-p", "--port", default=None, type=int, help="Port (default: 8420)")
def server(host, port):
    """Start the web UI server."""
    import uvicorn
    from .config import ALLOW_INSECURE_BIND, API_TOKEN, HOST, PORT, is_loopback_host

    h = host or HOST
    p = port or PORT
    # Binding beyond loopback without a token would put an unauthenticated API
    # that can run commands on the network. Refuse rather than do that silently.
    # In Docker the bind is always 0.0.0.0 but the real boundary is the host port
    # publish, so PP_ALLOW_INSECURE_BIND=1 (set by the image) opts out.
    if not is_loopback_host(h) and not API_TOKEN and not ALLOW_INSECURE_BIND:
        raise click.ClickException(
            f"Отказ: сервер на {h} открыт наружу без авторизации. Задай PP_API_TOKEN "
            "(длинный случайный токен), слушай 127.0.0.1, или PP_ALLOW_INSECURE_BIND=1 "
            "если публикуешь порт только на loopback (Docker).")
    from .api import app
    click.echo(f"PromptPilot UI: http://{h}:{p}")
    uvicorn.run(app, host=h, port=p, log_level="info")


if __name__ == "__main__":
    cli()
