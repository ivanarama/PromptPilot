import sqlite3

import pytest

from promptpilot.models import (
    FindingSeverity,
    FindingStatus,
    TaskCreate,
    WorkflowArtifactCreate,
    WorkflowCreate,
    WorkflowEventCreate,
    WorkflowFindingUpsert,
    WorkflowRole,
    WorkflowRoundCreate,
    WorkflowRunCreate,
    WorkflowUpdate,
)


def workflow_input(slug="ut10-bp3-u3"):
    return WorkflowCreate(
        slug=slug,
        objective="Закрыть U3 с независимым аудитом",
        repository_path=r"D:\Projects\exchange",
        candidate_branch="checkpoint/u2-wip-20260824",
        config={"limits": {"max_rounds": 6}},
    )


def test_legacy_database_gets_w0_tables_without_losing_task(tmp_path, monkeypatch):
    from promptpilot import db

    legacy_path = tmp_path / "legacy.db"
    legacy_schema = db.SCHEMA.split(
        "CREATE TABLE IF NOT EXISTS workflows", 1
    )[0]
    with sqlite3.connect(legacy_path) as conn:
        conn.executescript(legacy_schema)
        conn.execute(
            """INSERT INTO tasks (prompt, status, priority, created_at)
               VALUES ('before W0', 'pending', 5, '2026-08-25T00:00:00+00:00')"""
        )

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", legacy_path)
    db.init_db()

    assert db.get_task(1).prompt == "before W0"
    with db._connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM workflows"
        ).fetchall() == []
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_init_db_is_idempotent_and_preserves_existing_tasks(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="legacy task"))

    isolated_db.init_db()
    isolated_db.init_db()

    assert isolated_db.get_task(task.id).prompt == "legacy task"
    with isolated_db._connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        migration = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = ?",
            (isolated_db.WORKFLOW_SCHEMA_VERSION,),
        ).fetchone()
    assert {
        "workflows",
        "workflow_rounds",
        "workflow_runs",
        "workflow_findings",
        "workflow_artifacts",
        "workflow_events",
    }.issubset(tables)
    assert migration["version"] == isolated_db.WORKFLOW_SCHEMA_VERSION


def test_create_get_list_workflow_and_creation_event(isolated_db):
    created = isolated_db.create_workflow(
        workflow_input(), workflow_id="wf_test"
    )

    assert created.id == "wf_test"
    assert created.status.value == "draft"
    assert created.state_version == 0
    assert created.config == {"limits": {"max_rounds": 6}}
    assert isolated_db.get_workflow_by_ref("ut10-bp3-u3").id == "wf_test"
    assert [item.id for item in isolated_db.list_workflows()] == ["wf_test"]

    events = isolated_db.list_workflow_events("wf_test")
    assert len(events) == 1
    assert events[0].event_type == "workflow.created"
    assert events[0].payload["slug"] == "ut10-bp3-u3"


def test_duplicate_slug_is_a_domain_conflict(isolated_db):
    isolated_db.create_workflow(workflow_input(), workflow_id="wf_one")

    with pytest.raises(isolated_db.WorkflowConflictError):
        isolated_db.create_workflow(workflow_input(), workflow_id="wf_two")


def test_update_workflow_uses_optimistic_version(isolated_db):
    workflow = isolated_db.create_workflow(
        workflow_input(), workflow_id="wf_version"
    )

    updated = isolated_db.update_workflow(
        workflow.id,
        WorkflowUpdate(
            objective="Уточнённая цель",
            config={"limits": {"max_rounds": 8}},
            expected_version=0,
        ),
    )
    assert updated.objective == "Уточнённая цель"
    assert updated.config["limits"]["max_rounds"] == 8
    assert updated.state_version == 1

    with pytest.raises(isolated_db.WorkflowConflictError):
        isolated_db.update_workflow(
            workflow.id,
            WorkflowUpdate(objective="Потерянное обновление", expected_version=0),
        )

    events = isolated_db.list_workflow_events(workflow.id)
    assert [event.event_type for event in events] == [
        "workflow.created",
        "workflow.updated",
    ]


def test_event_append_is_idempotent_but_rejects_key_reuse(isolated_db):
    workflow = isolated_db.create_workflow(
        workflow_input(), workflow_id="wf_events"
    )
    event = WorkflowEventCreate(
        workflow_id=workflow.id,
        event_type="test.observed",
        idempotency_key="test:wf_events:1",
        payload={"value": 1, "nested": {"b": 2, "a": 1}},
    )

    first = isolated_db.append_workflow_event(event)
    second = isolated_db.append_workflow_event(event)
    assert second.seq == first.seq
    assert len(isolated_db.list_workflow_events(workflow.id)) == 2

    with pytest.raises(isolated_db.WorkflowConflictError):
        isolated_db.append_workflow_event(event.model_copy(
            update={"payload": {"value": 2}}
        ))


def test_round_run_finding_artifact_graph_and_reopen(isolated_db):
    workflow = isolated_db.create_workflow(
        workflow_input(), workflow_id="wf_graph"
    )
    round_data = isolated_db.create_workflow_round(
        WorkflowRoundCreate(
            workflow_id=workflow.id,
            round_no=1,
            base_sha="abc123",
        ),
        round_id="round_one",
    )
    run = isolated_db.create_workflow_run(
        WorkflowRunCreate(
            workflow_id=workflow.id,
            round_id=round_data.id,
            role=WorkflowRole.EXECUTOR,
            input_sha256="input-sha",
        ),
        run_id="run_executor",
    )
    finding = isolated_db.upsert_workflow_finding(WorkflowFindingUpsert(
        workflow_id=workflow.id,
        fingerprint="semantic-map-incomplete",
        severity=FindingSeverity.BLOCKER,
        category="semantic_mapping",
        title="СБДС создаётся как ПБДС",
        round_no=1,
        payload={"document": "ТК000000016"},
    ))
    artifact = isolated_db.create_workflow_artifact(WorkflowArtifactCreate(
        workflow_id=workflow.id,
        round_id=round_data.id,
        run_id=run.id,
        kind="review_evidence",
        path="evidence/operation-probe.json",
        sha256="artifact-sha",
        size_bytes=128,
        metadata={"format": "json"},
    ))

    assert isolated_db.get_workflow(workflow.id).current_round == 1
    assert isolated_db.list_workflow_rounds(workflow.id)[0].id == "round_one"
    assert isolated_db.list_workflow_runs(round_data.id)[0].id == "run_executor"
    assert finding.first_seen_round == finding.last_seen_round == 1
    assert artifact.metadata == {"format": "json"}

    resolved = isolated_db.upsert_workflow_finding(WorkflowFindingUpsert(
        workflow_id=workflow.id,
        fingerprint=finding.fingerprint,
        severity=FindingSeverity.BLOCKER,
        category=finding.category,
        title=finding.title,
        status=FindingStatus.RESOLVED,
        round_no=2,
        payload={"fixed_by": "def456"},
    ))
    reopened = isolated_db.upsert_workflow_finding(WorkflowFindingUpsert(
        workflow_id=workflow.id,
        fingerprint=finding.fingerprint,
        severity=FindingSeverity.BLOCKER,
        category=finding.category,
        title=finding.title,
        status=FindingStatus.REOPENED,
        round_no=3,
        payload={"reproduced_by": "probe"},
    ))

    assert resolved.status is FindingStatus.RESOLVED
    assert reopened.status is FindingStatus.REOPENED
    assert reopened.reopen_count == 1
    assert reopened.first_seen_round == 1
    assert reopened.last_seen_round == 3
    event_types = [
        event.event_type
        for event in isolated_db.list_workflow_events(workflow.id)
    ]
    assert "finding.reopened" in event_types


def test_artifact_create_is_idempotent_and_conflict_safe(isolated_db):
    workflow = isolated_db.create_workflow(
        workflow_input(), workflow_id="wf_artifact"
    )
    round_data = isolated_db.create_workflow_round(
        WorkflowRoundCreate(workflow_id=workflow.id, round_no=1),
        round_id="round_artifact",
    )
    data = WorkflowArtifactCreate(
        workflow_id=workflow.id,
        round_id=round_data.id,
        kind="manifest",
        path="evidence/manifest.json",
        sha256="manifest-sha",
        size_bytes=42,
    )

    first = isolated_db.create_workflow_artifact(data, artifact_id="artifact_one")
    second = isolated_db.create_workflow_artifact(data, artifact_id="ignored_id")
    assert second.id == first.id

    with pytest.raises(isolated_db.WorkflowConflictError):
        isolated_db.create_workflow_artifact(
            data.model_copy(update={"size_bytes": 43})
        )


def test_foreign_key_scope_rejects_run_from_another_workflow(isolated_db):
    first = isolated_db.create_workflow(
        workflow_input("first"), workflow_id="wf_first"
    )
    second = isolated_db.create_workflow(
        workflow_input("second"), workflow_id="wf_second"
    )
    round_data = isolated_db.create_workflow_round(
        WorkflowRoundCreate(workflow_id=first.id, round_no=1),
        round_id="round_first",
    )

    with pytest.raises(isolated_db.WorkflowNotFoundError):
        isolated_db.create_workflow_run(WorkflowRunCreate(
            workflow_id=second.id,
            round_id=round_data.id,
            role=WorkflowRole.REVIEWER,
            input_sha256="x",
        ))


def test_events_cannot_be_updated_or_deleted_through_repository(isolated_db):
    workflow = isolated_db.create_workflow(
        workflow_input(), workflow_id="wf_append_only"
    )
    assert not hasattr(isolated_db, "update_workflow_event")
    assert not hasattr(isolated_db, "delete_workflow_event")
    with isolated_db._connect() as conn:
        event = conn.execute(
            "SELECT * FROM workflow_events WHERE workflow_id = ?",
            (workflow.id,),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE workflow_events SET event_type = 'forged' WHERE seq = ?",
                (event["seq"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM workflow_events WHERE seq = ?", (event["seq"],)
            )
    assert event["event_type"] == "workflow.created"


def test_raw_duplicate_round_constraint_remains_enforced(isolated_db):
    workflow = isolated_db.create_workflow(
        workflow_input(), workflow_id="wf_round_unique"
    )
    isolated_db.create_workflow_round(
        WorkflowRoundCreate(workflow_id=workflow.id, round_no=1)
    )
    with pytest.raises(isolated_db.WorkflowConflictError):
        isolated_db.create_workflow_round(
            WorkflowRoundCreate(workflow_id=workflow.id, round_no=1)
        )

    # Sanity check that the domain error wraps, rather than leaks, SQLite's
    # provider-specific exception to API callers.
    assert not isinstance(isolated_db.WorkflowConflictError(), sqlite3.Error)
