"""A legacy-history fence is local to one independent content-review candidate."""
import copy

import pytest

from promptpilot import project_pipeline as pp
from promptpilot.models import TaskCreate
from tests.test_explicit_full_current_review import history
from tests.test_pipeline_replicas import _candidate, _health, _set_replica_attempt_env
from tests.test_project_pipeline import HEAD, snapshot, trusted_comment


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PP_PIPELINE_LEASE_KEY_FILE", str(tmp_path / "lease.key"))
    monkeypatch.setenv("PP_PIPELINE_REPLICAS", "1")
    cfg = {"repository": "owner/repo", "trusted_account": "owner", "base_branch": "main",
           "review_completion_gate": "target-v1", "fallback_handoff": "target-v1"}
    health = _health(*[_candidate(n, HEAD) for n in (41, 42, 43)])
    edited = trusted_comment("c2", 1001, "Updated link to test build")
    edited["node"]["lastEditedAt"] = "2026-01-02T00:00:00Z"
    timelines = {41: snapshot(edited), 42: snapshot(), 43: snapshot()}
    reads = []

    def read(_gh, _cfg, number):
        reads.append(number)
        return timelines[number]

    monkeypatch.setattr(pp, "run_health", lambda *_a, **_k: health)
    monkeypatch.setattr(pp, "stable_timeline", read)
    return cfg, health, timelines, reads


@pytest.mark.parametrize("replicas", [1, 2])
def test_fenced_predecessor_yields_to_next_content_review(queue, isolated_db, monkeypatch, replicas):
    cfg, health, timelines, reads = queue
    original = copy.deepcopy(health)
    monkeypatch.setenv("PP_PIPELINE_REPLICAS", str(replicas))
    isolated_db.create_task(TaskCreate(prompt="review"))
    _set_replica_attempt_env(monkeypatch, isolated_db.get_next_runnable())
    result = pp.next_review(object(), cfg)
    assert (result["action"], result["target"]["number"]) == ("audit", 42)
    assert reads == [41, 42, 42]
    assert health == original
    reservations = isolated_db.list_pipeline_target_reservations(repository=cfg["repository"])
    if replicas == 1:
        assert not reservations
        return
    assert [r["number"] for r in reservations] == [42]
    # The same task already owns #42. A later fence keeps that exact election.
    timelines[42] = timelines[41]
    reads.clear()
    result = pp.next_review(object(), cfg)
    assert (result["action"], result["number"]) == ("wait", 42)
    assert reads == [42]
    assert isolated_db.list_pipeline_target_reservations(repository=cfg["repository"]) == reservations


def test_fallback_uses_filtered_election_and_preserves_source_health(queue):
    cfg, health, timelines, reads = queue
    # The next candidate needs the ordinary third-round fallback.
    timelines[42] = history()
    timelines[42]["edges"].pop()  # No newer owner request for another full audit.
    health["review_candidates"][1]["review_depth"] = 2
    original = copy.deepcopy(health)
    result = pp.next_review(object(), cfg)
    assert result["action"] == "fallback"
    lease = pp.decode_signed_lease(result["handoff"]["lease"])
    assert lease["target"] == {"stage": "review", "number": 42, "head": HEAD}
    assert health == original
    assert reads == [41, 42, 42]


def test_all_fenced_keeps_first_wait_without_a_reservation(queue, isolated_db, monkeypatch):
    cfg, _, timelines, reads = queue
    monkeypatch.setenv("PP_PIPELINE_REPLICAS", "2")
    isolated_db.create_task(TaskCreate(prompt="review"))
    _set_replica_attempt_env(monkeypatch, isolated_db.get_next_runnable())
    timelines[42] = timelines[43] = timelines[41]
    result = pp.next_review(object(), cfg)
    assert (result["action"], result["number"]) == ("wait", 41)
    assert "uncertain trusted history" in result["reason"]
    assert reads == [41, 42, 43]
    assert not isolated_db.list_pipeline_target_reservations(repository=cfg["repository"])


def test_integration_owner_fence_still_stops_its_lane(queue, isolated_db, monkeypatch):
    cfg, health, _, reads = queue
    owner = {"number": 41, "head": HEAD, "stage": "integration-review"}
    content = health["review_candidates"][1:]
    health.update(state="yellow", integration_owner=owner, review_candidates=[owner],
                  content_review_candidates=content, parallel_review_candidates=content,
                  findings=[{"code": "single_flight_barrier", "severity": "yellow", "pr": 41}])
    cfg["parallel_content_review"] = True
    monkeypatch.setenv("PP_PIPELINE_REPLICAS", "2")
    isolated_db.create_task(TaskCreate(prompt="review"))
    _set_replica_attempt_env(monkeypatch, isolated_db.get_next_runnable())
    result = pp.next_review(object(), cfg)
    assert (result["action"], result["number"]) == ("wait", 41)
    assert reads == [41]


def test_timeline_read_failure_stops_before_later_targets(queue, monkeypatch):
    cfg, _, _, reads = queue

    def failed(_gh, _cfg, number):
        reads.append(number)
        raise pp.PipelineError("timeline changed between stable reads")

    monkeypatch.setattr(pp, "stable_timeline", failed)
    with pytest.raises(pp.PipelineError, match="timeline changed"):
        pp.next_review(object(), cfg)
    assert reads == [41]


def test_legacy_fallback_cannot_redirect_to_original_fenced_candidate(queue):
    cfg, health, timelines, _ = queue
    cfg["fallback_handoff"] = "legacy"
    health["review_candidates"][1]["review_depth"] = 2
    timelines[42] = history()
    timelines[42]["edges"].pop()
    result = pp.next_review(object(), cfg)
    assert (result["action"], result["number"]) == ("wait", 41)
