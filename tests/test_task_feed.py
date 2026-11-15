"""Business projection through the real GET API; no execution/remote services."""
import asyncio
import json
import sqlite3

import httpx
import pytest

from promptpilot import api, db, task_feed, workflows
from promptpilot.models import (
    TaskCreate, WorkflowArtifactCreate, WorkflowCreate, WorkflowEventCreate,
    WorkflowRoundCreate, WorkflowRunCreate,
)


def get(path, **params):
    async def call():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api.app),
            base_url="http://127.0.0.1:8420",
        ) as client:
            return await client.get(path, params=params)
    return asyncio.run(call())


def feed(**params):
    response = get("/api/task-feed", local_only=True, **params)
    assert response.status_code == 200, response.text
    return response.json()


def workflow(slug, config=None):
    return db.create_workflow(WorkflowCreate(
        slug=slug, objective="Business objective", repository_path="fixture-repo",
        candidate_branch="main", config=config or {},
    ))


def task(prompt="Independent task", **kwargs):
    return db.create_task(TaskCreate(prompt=prompt, **kwargs))


def round_for(wf, number=1):
    return db.create_workflow_round(WorkflowRoundCreate(workflow_id=wf.id, round_no=number))


def run_for(wf, rnd, child, role="executor", attempt=1):
    return db.create_workflow_run(WorkflowRunCreate(
        workflow_id=wf.id, round_id=rnd.id, role=role, attempt_no=attempt,
        task_id=child.id, input_sha256="fixture-input",
    ))


def sql(query, args=()):
    # Fixture-only direct writes also permit deliberately broken legacy links.
    with sqlite3.connect(db.DB_PATH) as connection:
        return connection.execute(query, args).fetchall()


def dump():
    with sqlite3.connect(db.DB_PATH) as connection:
        return list(connection.iterdump())


def test_one_workflow_replaces_all_roles_rounds_and_retries_before_pagination(isolated_db):
    wf = workflow("one-business-goal")
    for number in (1, 2):
        rnd = round_for(wf, number)
        for role in ("executor", "reviewer"):
            for attempt in (1, 2, 3):
                child = task(f"Internal {role} {attempt}")
                run_for(wf, rnd, child, role, attempt)
    planner = task("Internal planner")
    sql("""INSERT INTO workflow_plans(workflow_id,status,planner_task_id,created_at,updated_at)
           VALUES(?,'completed',?,'2026-01-01','2026-01-01')""", (wf.id, planner.id))
    standalone = [task("Standalone one"), task("Standalone two")]
    pages = [feed(limit=1, offset=n) for n in range(3)]
    assert all(page["total"] == 3 for page in pages)
    items = [page["items"][0] for page in pages]
    assert {r["id"] for r in items} == {
        "local:workflow:" + wf.id, *(f"local:task:{t.id}" for t in standalone)}
    assert feed(limit=1, offset=3)["items"] == []
    assert feed(kind="workflow")["items"][0]["title"] == "one-business-goal"


def test_same_task_referenced_by_valid_retry_runs_is_still_one_business_item(isolated_db):
    wf = workflow("same-role-retry")
    rnd = round_for(wf)
    child = task()
    run_for(wf, rnd, child, attempt=1)
    run_for(wf, rnd, child, attempt=2)
    assert feed()["total"] == 1


def test_unpaginated_local_helper_excludes_children_and_keeps_every_business_item(isolated_db):
    wf = workflow("all-business-items")
    run_for(wf, round_for(wf), task("Hidden role"))
    standalone = [task(f"Independent {n}") for n in range(7)]
    items = task_feed.list_local_items()
    assert len(items) == 8
    assert {r["object_id"] for r in items if r["kind"] == "task"} == {t.id for t in standalone}


def test_planner_history_uses_actual_task_state_not_plan_update_as_completion(isolated_db):
    wf = workflow("planner-history")
    child = task("Planner")
    sql("UPDATE tasks SET status='running',started_at='2026-01-01T10:00:00Z' WHERE id=?", (child.id,))
    sql("""INSERT INTO workflow_plans(workflow_id,status,planner_task_id,created_at,updated_at)
           VALUES(?,'planning',?,'2026-01-01','2026-01-02')""", (wf.id, child.id))
    detail = task_feed.get_detail("workflow", wf.id)
    planner = detail["history"]["runs"][0]
    assert planner["role"] == "planner" and planner["status"] == "running"
    assert planner["completed_at"] is None and planner["attempt_no"] is None


def planner_event(wf, child, key):
    db.append_workflow_event(WorkflowEventCreate(
        workflow_id=wf.id, event_type="planner.dispatched", idempotency_key=key,
        payload={"task_id": child.id},
    ))


@pytest.mark.parametrize("current_plan", ["replacement", "manual"])
def test_replaced_planners_stay_in_workflow_history_not_business_list(isolated_db, current_plan):
    wf = workflow("planner-retries")
    old, current = task("Old planner"), task("Current planner")
    planner_event(wf, old, "old-dispatch")
    planner_event(wf, current, "current-dispatch")
    sql("UPDATE tasks SET status='completed',started_at='2026-01-01',completed_at='2026-01-02' WHERE id=?",
        (old.id,))
    sql("""INSERT INTO workflow_plans(workflow_id,status,planner_task_id,created_at,updated_at)
           VALUES(?,'planning',?,'2026-01-01','2026-01-03')""",
        (wf.id, current.id if current_plan == "replacement" else None))
    assert feed(limit=1)["total"] == 1
    assert get(f"/api/task-feed/local/task/{old.id}").status_code == 404
    planners = task_feed.get_detail("workflow", wf.id)["history"]["runs"]
    assert [r["task_id"] for r in planners] == [old.id, current.id]
    assert planners[0]["status"] == "completed"
    assert planners[0]["completed_at"] == "2026-01-02"
    assert planners[1]["completed_at"] is None
    assert all(r["attempt_no"] is None for r in planners)


def test_historical_planner_with_conflicting_workflow_remains_visible(isolated_db):
    first, second = workflow("planner-first"), workflow("planner-second")
    child = task("Ambiguous planner")
    planner_event(first, child, "first-planner")
    planner_event(second, child, "second-planner")
    assert feed(kind="task")["items"][0]["object_id"] == child.id


@pytest.mark.parametrize("role", ["gate", "archiver"])
def test_all_canonical_workflow_roles_remain_inside_process(isolated_db, role):
    wf = workflow("canonical-role")
    child = task("Canonical internal role")
    run_for(wf, round_for(wf), child, role)
    assert feed()["total"] == 1
    assert task_feed.get_detail("workflow", wf.id)["history"]["runs"][0]["role"] == role


def test_ambiguous_cross_workflow_links_remain_visible(isolated_db):
    first, second = workflow("first"), workflow("second")
    child = task("Do not lose me")
    run_for(first, round_for(first), child)
    run_for(second, round_for(second), child)
    result = feed(kind="task")
    assert result["total"] == 1 and result["items"][0]["object_id"] == child.id
    assert "неоднозначна" in result["items"][0]["status_reason"]


@pytest.mark.parametrize("broken", ["missing-workflow", "wrong-round", "unknown-role"])
def test_invalid_or_orphan_canonical_links_do_not_hide_tasks(isolated_db, broken):
    wf, other = workflow("owner"), workflow("unrelated")
    child = task("Visible orphan")
    run = run_for(wf, round_for(wf), child)
    if broken == "missing-workflow":
        sql("UPDATE workflow_runs SET workflow_id='missing-workflow' WHERE id=?", (run.id,))
    elif broken == "wrong-round":
        sql("UPDATE workflow_runs SET round_id=? WHERE id=?", (round_for(other).id, run.id))
    else:
        sql("UPDATE workflow_runs SET role='unrecognized-role' WHERE id=?", (run.id,))
    assert feed(kind="task")["items"][0]["object_id"] == child.id


def test_planner_orphan_and_prompt_hints_are_not_ownership(isolated_db):
    child = task("[workflow fake-id] executor", parent_task_id=42)
    sql("""INSERT INTO workflow_plans(workflow_id,status,planner_task_id,created_at,updated_at)
           VALUES('missing-workflow','draft',?,'2026-01-01','2026-01-01')""", (child.id,))
    assert feed()["items"][0]["object_id"] == child.id


@pytest.mark.parametrize("raw,normalized", [
    ("draft", "queued"), ("queued", "queued"), ("planning", "active"),
    ("executing", "active"), ("gating", "active"), ("reviewing", "active"),
    ("awaiting_external", "blocked"), ("awaiting_plan_approval", "needs_attention"),
    ("awaiting_human", "needs_attention"), ("revision_required", "needs_attention"),
    ("failed", "needs_attention"), ("completed", "completed"), ("cancelled", "cancelled"),
    ("unknown-legacy-state", "needs_attention"),
])
def test_workflow_business_statuses_are_truthful(isolated_db, raw, normalized):
    wf = workflow("state-test")
    sql("UPDATE workflows SET status=? WHERE id=?", (raw, wf.id))
    item = feed()["items"][0]
    assert item["status"] == normalized
    assert item["status_reason"]
    if raw == "awaiting_external":
        assert item["status_label"] == "Ожидает внешнего результата"
        assert "не подтверждён" in item["status_reason"]


@pytest.mark.parametrize("raw,normalized", [
    ("pending", "queued"), ("running", "active"), ("rate_limited", "blocked"),
    ("failed", "needs_attention"), ("completed", "completed"), ("cancelled", "cancelled"),
])
def test_independent_task_states(isolated_db, raw, normalized):
    item = task()
    sql("UPDATE tasks SET status=? WHERE id=?", (raw, item.id))
    assert feed()["items"][0]["status"] == normalized


def test_paused_and_budget_waiting_task_are_blocked_not_running(isolated_db):
    first, second = task("Paused task"), task("Budget wait")
    sql("UPDATE tasks SET budget_wait_scope='fixture-scope' WHERE id=?", (second.id,))
    assert next(r for r in feed()["items"] if r["object_id"] == second.id)["status"] == "blocked"
    db.set_setting("worker_paused", "1")
    assert next(r for r in feed()["items"] if r["object_id"] == first.id)["status_label"] == "На паузе"


def test_business_filters_search_and_stable_page_counts(isolated_db):
    wf = workflow("unique-workflow")
    rnd = round_for(wf)
    for n in range(5):
        run_for(wf, rnd, task("Unique hidden child"), attempt=n + 1)
    one, two = task("Unique one"), task("Unrelated")
    sql("UPDATE tasks SET status='completed',completed_at='2026-01-02T00:00:00Z' WHERE id=?", (one.id,))
    result = feed(status="completed", q="UNIQUE", limit=1)
    assert result["total"] == 1 and result["items"][0]["object_id"] == one.id
    assert feed(q="hidden child")["total"] == 0
    assert feed(kind="workflow", q="unique")["total"] == 1
    assert feed(kind="task", q="unrelated")["items"][0]["object_id"] == two.id
    assert feed(limit=2) == feed(limit=2)


def test_details_group_role_attempts_and_never_forward_workorder_secret_config(isolated_db):
    secret = "never-show-this-workorder-lease"
    wf = workflow("website-run", {
        "workorder_guard": {"site_url": "https://example.invalid", "run_id": "fixture-run",
                            "worker_key": "fixture-worker", "lease_token": secret},
        "roles": {"executor": {"provider": "fixture", "model": "fixture-model", "machine": "fixture-pc"}},
    })
    sql("UPDATE workflows SET objective=? WHERE id=?", ("Goal " + secret, wf.id))
    rnd = round_for(wf)
    for n in (1, 2):
        run = run_for(wf, rnd, task("Internal reviewer"), "reviewer", n)
        sql("UPDATE workflow_runs SET input_json=?,output_json=?,output_sha256='known-output' WHERE id=?",
            (json.dumps({"LEASE_TOKEN": secret}), json.dumps({"config": secret}), run.id))
    db.append_workflow_event(WorkflowEventCreate(
        workflow_id=wf.id, event_type="workflow.updated", idempotency_key="secret-event",
        payload={"config": {"workorder_guard": {"LEASE_TOKEN": secret}}},
    ))
    db.create_workflow_artifact(WorkflowArtifactCreate(
        workflow_id=wf.id, round_id=rnd.id, kind="report", path="report.md",
        sha256="a" * 64, size_bytes=3, metadata={"LEASE_TOKEN": secret},
    ))
    response = get(f"/api/task-feed/local/workflow/{wf.id}")
    assert response.status_code == 200, response.text
    detail = response.json()
    assert secret not in response.text and "LEASE_TOKEN" not in response.text
    assert detail["item"]["executor"] == {"provider": "fixture", "model": "fixture-model", "machine": "fixture-pc"}
    group = detail["history"]["role_groups"][0]
    assert group["label"] == "Проверяющий" and len(group["runs"]) == 2
    assert [r["attempt_no"] for r in group["runs"]] == [1, 2]
    assert all(r["round_no"] == 1 for r in group["runs"])
    assert detail["result"]["available"] is True
    assert detail["artifacts"][0]["path"] == "report.md"
    assert "metadata" not in detail["artifacts"][0]
    assert "payload_json" not in detail["history"]["events"][0]
    assert secret not in json.dumps(feed())


def test_standalone_detail_is_metadata_not_raw_prompt_result_or_error(isolated_db):
    secret = "PRIVATE-LEASE-RESULT"
    item = task("Safe title\n" + secret)
    sql("UPDATE tasks SET result=?,error=?,status='completed' WHERE id=?", (secret, secret, item.id))
    response = get(f"/api/task-feed/local/task/{item.id}")
    assert response.status_code == 200
    assert secret not in response.text
    assert response.json()["result"]["characters"] == len(secret)
    assert response.json()["item"]["title"] == "Safe title"


def test_get_is_readonly_and_never_calls_execution_services(isolated_db, monkeypatch):
    wf = workflow("no-side-effects")
    task()
    before = dump()
    def forbidden(*args, **kwargs):
        pytest.fail("Read model invoked execution or initialization")
    for obj, name in [(workflows, "advance_workflow"), (workflows, "sync_task"),
                      (workflows, "advance_linked_task"), (db, "get_next_runnable"),
                      (db, "init_db"), (api, "load_providers")]:
        monkeypatch.setattr(obj, name, forbidden)
    assert feed()["total"] == 2
    assert get(f"/api/task-feed/local/workflow/{wf.id}").status_code == 200
    assert dump() == before
    with pytest.raises(task_feed.FeedUnavailable):
        with task_feed._snapshot() as connection:
            connection.execute("UPDATE workflows SET objective='forbidden'")
    assert dump() == before


def test_missing_database_is_not_created_or_migrated(isolated_db, monkeypatch, tmp_path):
    path = tmp_path / "not-created.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", path)
    assert get("/api/task-feed", local_only=True).status_code == 503
    assert not path.exists()


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"offset": -1},
                                   {"status": "running"}, {"kind": "series"}, {"local_only": "invalid"}])
def test_api_query_validation(isolated_db, params):
    assert get("/api/task-feed", **params).status_code == 422


def test_details_missing_and_hidden_children_are_not_independent_items(isolated_db):
    wf = workflow("hidden-child")
    child = task()
    run_for(wf, round_for(wf), child)
    assert get(f"/api/task-feed/local/task/{child.id}").status_code == 404
    assert get("/api/task-feed/local/task/not-a-number").status_code == 404
    assert get("/api/task-feed/local/workflow/missing").status_code == 404


def test_pure_peer_helpers_drop_unknown_nested_config_and_namespace_identity():
    secret = {"LEASE_TOKEN": "do-not-display"}
    item = task_feed.project_workflow({
        "id": "wf_demo", "slug": "Business goal", "status": "awaiting_external",
        "objective": "Fixture", "config": {"workorder_guard": secret},
        "current_round": 1, "state_version": 3,
    }, source_id="studio-windows", source_label="Studio Windows", readonly=True)
    assert item["id"] == "studio-windows:workflow:wf_demo" and item["readonly"] is True
    projected = task_feed.namespace_item({**item, "raw_config": secret}, "second", "Other")
    assert projected["id"] == "second:workflow:wf_demo"
    run = task_feed.project_run({"role": "reviewer", "round_no": secret, "input": secret})
    assert run["round_no"] is None and run["role_label"] == "Проверяющий"
    assert task_feed.project_run({"role": "reviewer"}, round_no=secret)["round_no"] is None
    assert "do-not-display" not in json.dumps([item, projected, run])
    with pytest.raises(ValueError):
        task_feed.namespace_item(item, "https://arbitrary.example", "No discovery")


def test_workflow_redacts_full_token_before_truncating_display_fields():
    token = "PRIVATE-LEASE-" + "x" * 1700
    item = task_feed.project_workflow({
        "id": "wf_private", "slug": token, "status": "draft",
        "objective": "Goal " + token,
        "config": {"workorder_guard": {"lease_token": token}},
    })
    assert item["title"] == "[redacted]"
    assert item["objective"] == "Goal [redacted]"
    assert "PRIVATE-LEASE" not in json.dumps(item)


@pytest.mark.parametrize("object_id", ["9" * 4301, str(2**100), "-1", "١", "0"])
def test_local_task_detail_rejects_out_of_range_identity_before_sql(isolated_db, object_id):
    assert get("/api/task-feed/local/task/" + object_id).status_code == 404


def test_configured_external_target_is_safe_and_does_not_claim_connection():
    row = {"id": "wf_external", "status": "awaiting_external", "config": {
        "external_executor": {"client_id": "glm", "label": "ZCode", "machine": "Windows",
                              "token": "PRIVATE-TARGET-CREDENTIAL"}}}
    item = task_feed.project_workflow(row)
    assert item["external_execution"] == {
        "client_id": "glm", "client_label": "ZCode", "machine": "Windows",
        "state": "awaiting_connection", "execution_observed": False, "last_seen_at": None,
    }
    assert "PRIVATE-TARGET-CREDENTIAL" not in json.dumps(item)
    assert "external_execution" not in task_feed.namespace_item(item, "peer", "Peer")
    for altered in (
        {**row, "status": "completed"},
        {**row, "config": {**row["config"], "workorder_guard": {}}},
        {**row, "config": {"external_executor": {"client_id": "bad/url"}}},
    ):
        assert "external_execution" not in task_feed.project_workflow(altered)
