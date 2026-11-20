"""Exercise the worker's ordinary handoff, recurrence and read-only Actions poll."""

from datetime import datetime, timedelta, timezone

import pytest

from promptpilot import fallback_handoff, merge_ci_wait, pipeline_insights as pi, project_pipeline as pp, worker
from promptpilot.models import TaskCreate
from tests.test_fallback_handoff import health
from tests.test_merge_checkpoint import dispatch
from tests.test_merge_ci_wait import elected as elected
from tests.test_merge_final_source import merge_dispatch as merge_dispatch
from tests.test_pipeline_source_refresh import continuation as continuation, targeted_merge as targeted_merge


def finish(e, *, repair=False):
    f = e.f
    assert e.call("claim")["send_allowed"] is True
    pi.release_execution_admission()
    result = "Recorded the external Actions attempt.\nИТОГ: ПУСТО"
    assert f.db.mark_completed(f.task.id, result, verdict="ПУСТО", usage={"input_tokens": 231, "output_tokens": 51})
    if repair:
        assert f.db.repair_active_series_occurrences() == [f.task.series_id]
    else:
        worker._recur_after_run(f.task)
    with f.db._connect() as conn:
        row = conn.execute("SELECT id,scheduled_at FROM tasks WHERE series_id=? AND status='pending'",
                           (f.task.series_id,)).fetchone()
        assert datetime.now(timezone.utc) < datetime.fromisoformat(row["scheduled_at"]) <= (
            datetime.now(timezone.utc) + timedelta(seconds=merge_ci_wait.POLL_SECONDS))
        usage = conn.execute("SELECT input_tokens,output_tokens FROM task_token_usage WHERE task_id=?",
                             (f.task.id,)).fetchone()
        assert tuple(usage) == (231, 51) and f.db.get_task(f.task.id).result == result
    other = f.db.create_task(TaskCreate(prompt="another runnable queue"))
    assert f.db.get_next_runnable().id == other.id
    with f.db._connect() as conn:
        conn.execute("UPDATE tasks SET scheduled_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), row["id"]))
    f.task = f.db.get_next_runnable()
    assert f.task.id == row["id"]


def cached(f):
    return {"repository": f.profile["repository"], "diagnostics": health("merge"),
            "cache": {"profile_hash": pi._profile_fingerprint(f.profile), "complete": True,
                      "available": True, "stale": False},
            "queues": [{"id": "merge", "membership_complete": True, "items": []}]}


@pytest.mark.parametrize("repair", [True, False])
def test_poll_releases_worker_and_reserves_two_reads_without_replaying_election(elected, monkeypatch, repair):
    e = elected
    finish(e, repair=repair)
    monkeypatch.setattr(pi, "read_cached", lambda *_: cached(e.f))
    monkeypatch.setattr(pi, "_tool_preflight", lambda *_: pytest.fail("polled the entire queue"))
    from promptpilot import merge_progress_probe
    monkeypatch.setattr(merge_progress_probe, "measurements", lambda *_: pytest.fail("polled the full PR history"))
    e.gh.run.update(run_attempt=2, status="in_progress", conclusion=None)
    e.gh.requests.clear()
    waiting = dispatch(e.f)
    assert waiting["action"] == "defer" and waiting["defer_policy"] == "hard_not_before", waiting
    assert len(e.gh.requests) == 2
    ledger = e.f.db.pipeline_github_budget_reservations("github-default")
    assert ledger["count"] == 1 and ledger["totals"] == {"core": 2, "graphql": 0, "search": 0}
    pi.release_execution_admission()
    assert e.f.db.pipeline_github_budget_reservations("github-default")["count"] == 0
    e.gh.run.update(status="completed", conclusion="success")
    terminal = dispatch(e.f)
    assert terminal["action"] == "defer" and "success" in terminal["reason"]
    assert worker._pipeline_defer_time(terminal) is not None
    assert merge_ci_wait.active_wait(e.repo, e.target) is None


def test_green_election_for_another_run_cannot_finish_the_saved_attempt(elected, monkeypatch):
    e = elected
    finish(e)
    monkeypatch.setattr(pi, "_tool_preflight", lambda *_: {"action": "merge", "target": e.target, "lease": "other"})
    e.gh.run.update(run_attempt=2, status="in_progress", conclusion=None)
    result = dispatch(e.f)
    assert result["action"] == "defer" and "attempt 2" in result["reason"], result
    pi.release_execution_admission()
    e.gh.pr["head"]["sha"] = "f" * 40
    result = dispatch(e.f)
    assert result["action"] == "defer" and "superseded" in result["reason"]
    assert merge_ci_wait.active_wait(e.repo, {"number": 42, "head": "f" * 40}) is None


def test_unsigned_skill_cannot_reissue_an_ambiguous_send(elected, monkeypatch):
    e = elected
    finish(e)
    monkeypatch.setattr(pi, "_tool_preflight", lambda *_: {"action": "fallback", "reason": "unavailable"})
    result = dispatch(e.f)
    assert result["action"] == "defer" and "targeted election" in result["reason"], result


def test_failed_saved_attempt_enters_signed_diagnosis_when_another_run_is_green(elected, monkeypatch):
    e = elected
    finish(e)
    e.gh.run.update(run_attempt=2, status="completed", conclusion="failure")
    config_path = e.f.profile["queues"][0]["execution"]["command"][2]
    live = health("merge")
    handoff = fallback_handoff.create(pp.load_config(config_path), live, "merge", live["merge_executable"][0],
                                      "inspect the saved CI failure")
    calls = []

    def preflight(execution, argv, directory, **kwargs):
        calls.append(argv)
        if "merge-ci-diagnose" in argv:
            assert e.f.db.pipeline_github_budget_reservations("github-default")["items"][0]["route"] == "tool_preflight"
            return handoff
        return {"action": "merge", "target": e.target, "lease": "another-green-run"}

    monkeypatch.setattr(pi, "_tool_preflight", preflight)
    first = dispatch(e.f)
    assert first["action"] == "defer" and "failed" in first["reason"]
    pi.release_execution_admission()
    route = dispatch(e.f)
    assert route["action"] == "prompt" and route["target"]["number"] == 42, route
    assert '"ci_wait_record"' in route["prompt"] and '"ci_wait_command"' in route["prompt"]
    assert route["preflight"] == handoff and route["gate_command"][-2] == "--lease-file"
    assert len(calls) == 3
