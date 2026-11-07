"""Public CLI replay of the real #2480 source/intent, never production state."""
import copy
import json
from pathlib import Path
import sys

import pytest

from promptpilot import project_pipeline as pp
from promptpilot import merge_admission_progress as progress

FIXTURE = Path(__file__).parent / "fixtures/canonical-merge-progress-2480.json"


class ArchiveGitHub:
    def __init__(self, archive, *, pending=True):
        self.archive = copy.deepcopy(archive)
        self.snapshot = self.archive["snapshot"]
        if not pending:
            assert self.snapshot["edges"][-1]["node"]["fullDatabaseId"] == "6701643279"
            self.snapshot["edges"].pop()
        self.calls, self.graph_calls = [], 0
        self.unstable = False

    def json(self, *args, input_value=None):
        assert input_value is None and args[0] == "api" and len(args) == 2
        self.calls.append(args)
        endpoint = args[1]
        if endpoint == "user":
            return {"login": "ivanarama"}
        stem = "repos/ivanarama/onebase/"
        assert endpoint.startswith(stem)
        tail = endpoint[len(stem):]
        if tail == "pulls/2480":
            return copy.deepcopy(self.archive["pr"])
        if tail.startswith("git/commits/"):
            assert tail == "git/commits/" + self.snapshot["headRefOid"]
            return copy.deepcopy(self.archive["commit"])
        if tail == "git/ref/heads/main":
            return copy.deepcopy(self.archive["main"])
        if tail.startswith("commits/") and tail.endswith("/check-runs?per_page=100"):
            return copy.deepcopy(self.archive["checks"])
        if tail.startswith("compare/"):
            return {"status": "diverged", "behind_by": 1}
        raise AssertionError("unexpected/non-read API request: " + endpoint)

    def graphql_page(self, owner, name, number, cursor):
        assert (owner, name, number) == ("ivanarama", "onebase", 2480)
        self.graph_calls += 1
        value = copy.deepcopy(self.snapshot)
        edges = value.pop("edges")
        split = len(edges) // 2
        after = edges[split - 1]["cursor"]
        assert cursor in (None, after)
        page = edges[:split] if cursor is None else edges[split:]
        labels = value.pop("labels")
        value.pop("labelsComplete")
        value["labels"] = {"nodes": [{"name": n} for n in labels], "pageInfo": {"hasNextPage": False}}
        if self.unstable and self.graph_calls >= 3:
            value["updatedAt"] = "changed-between-complete-passes"
        value["timelineItems"] = {"updatedAt": value.pop("updatedAt"), "edges": page,
            "pageInfo": {"hasNextPage": cursor is None, "endCursor": after if cursor is None else None}}
        return {"data": {"repository": {"pullRequest": value}}}


@pytest.fixture
def replay(tmp_path, monkeypatch):
    archive = json.loads(FIXTURE.read_text(encoding="utf8"))
    config_path = tmp_path / "pipelinectl.json"
    config_path.write_text(json.dumps({"repository": "ivanarama/onebase", "trusted_account": "ivanarama",
        "health_command": [sys.executable, "-c", "raise SystemExit('not used')"],
        "required_checks": archive["required_checks"], "fallback_handoff": "target-v1",
        "review_completion_gate": "target-v1"}), encoding="utf8")
    key = tmp_path / "test-existing-key"
    key.write_bytes(bytes(range(32)))  # Public fixture bytes, never a production key.
    assert key.stat().st_size == 32
    monkeypatch.setenv("PP_PIPELINE_LEASE_KEY_FILE", str(key))
    monkeypatch.chdir(tmp_path)
    return archive, config_path, key


def inspect(replay, monkeypatch, capsys, *, pending=True, change=None):
    archive, path, _key = replay
    gh = ArchiveGitHub(archive, pending=pending)
    if change:
        change(gh)
    monkeypatch.setattr(pp, "GitHub", lambda: gh)
    result = pp.run(["--config", str(path), "inspect-merge-progress", "--number", "2480",
                     "--head", archive["snapshot"]["headRefOid"]])
    out = capsys.readouterr()
    return result, json.loads(out.out) if out.out.strip() else None, gh, out.err


def test_public_cli_proves_real_source_then_exact_pending_intent(replay, monkeypatch, capsys):
    before = inspect(replay, monkeypatch, capsys, pending=False)
    after = inspect(replay, monkeypatch, capsys)
    assert before[0] == after[0] == 0
    archive, path, _key = replay
    def verified(value):
        return progress.VerifiedProgress(value["attestation"], config=pp.load_config(str(path)),
            config_path=path, checkout=path.parent, number=2480, head=archive["snapshot"]["headRefOid"])
    first, second = verified(before[1]), verified(after[1])
    assert progress.advanced(first.facts, second.facts)
    assert second.facts["intent"]["id"] == 6701643279
    assert second.facts["source"]["review_id"] == 6701527738
    assert second.facts["source"]["claim_id"] == 6701528225
    assert second.facts["source"]["completion_id"] == 6701529129
    assert after[1]["mutation_authorized"] is False
    assert before[2].graph_calls == after[2].graph_calls == 8  # Four FULL two-page passes.


def _change(gh, kind):
    snapshot, archive = gh.snapshot, gh.archive
    intent = snapshot["edges"][-1]["node"]
    if kind == "foreign": intent["author"]["login"] = "someone-else"
    if kind == "edited": intent["lastEditedAt"] = "2026-11-07T03:30:00Z"
    if kind == "unknown_neighbor": intent["body"] += "\n<!-- pp:base-sync-unknown x -->"
    if kind == "wrong_proof": intent["body"] = intent["body"].replace("review-comment=6701527738", "review-comment=1")
    if kind == "wrong_base": intent["body"] = intent["body"].replace(archive["main"]["object"]["sha"], "c" * 40)
    if kind == "previous": intent["body"] = intent["body"].replace("previous=none", "previous=123")
    if kind == "wrong_graph": archive["commit"]["sha"] = "c" * 40
    if kind == "wrong_actor": gh.json = lambda *_a, **_k: {"login": "someone-else"}
    if kind == "ci_failure":
        next(r for r in archive["checks"]["check_runs"] if r["name"] == "build")["conclusion"] = "failure"
    if kind == "incomplete_ci": archive["checks"]["total_count"] += 1
    if kind == "unstable": gh.unstable = True
    if kind == "postread_changed":
        original = gh.graphql_page
        def changed(*args):
            value = original(*args)
            if gh.graph_calls >= 5:
                value["data"]["repository"]["pullRequest"]["timelineItems"]["updatedAt"] = "2026-11-08T00:00:00Z"
            return value
        gh.graphql_page = changed
    if kind == "main_changed":
        original = gh.json
        def changed(*args, **kwargs):
            value = original(*args, **kwargs)
            if args[1].endswith("git/ref/heads/main") and gh.graph_calls >= 8:
                value["object"]["sha"] = "b" * 40
            return value
        gh.json = changed
    if kind in {"hold", "manual", "needs-decision", "changes-requested"}:
        snapshot["labels"].append(kind)
        archive["pr"]["labels"].append({"name": kind})
    if kind == "revoked_ship":
        snapshot["edges"].append({"cursor": "revoked", "node": {"id": "revoked-node",
            "__typename": "UnlabeledEvent", "actor": {"login": "ivanarama"}, "label": {"name": "ship"}}})
    if kind == "deleted":
        snapshot["edges"].pop()
        snapshot["edges"].append({"cursor": "deleted", "node": {"id": "deleted-node",
            "__typename": "CommentDeletedEvent", "createdAt": "2026-11-07T03:30:00Z"}})
    if kind == "duplicate":
        copy_node = copy.deepcopy(snapshot["edges"][-1])
        copy_node["cursor"] = "duplicate-cursor"
        copy_node["node"].update(id="duplicate-node", fullDatabaseId="6701643280")
        snapshot["edges"].append(copy_node)
    if kind == "cleanup": intent["body"] += "\n<!-- pp:merge-cleanup-intent unknown -->"
    if kind == "typed": intent["body"] += "\n<!-- pp:review-routing-unknown -->"
    if kind == "late_source": intent["createdAt"] = "2020-01-01T00:00:00Z"
    if kind == "read_bound":
        old = gh.graphql_page
        def endless(owner, name, number, cursor):
            value = old(owner, name, number, None)
            value["data"]["repository"]["pullRequest"]["timelineItems"]["pageInfo"]["hasNextPage"] = True
            return value
        gh.graphql_page = endless


@pytest.mark.parametrize("kind", ["foreign", "edited", "unknown_neighbor", "wrong_proof", "wrong_base",
    "previous", "wrong_graph", "wrong_actor", "ci_failure", "incomplete_ci", "unstable",
    "postread_changed", "main_changed", "hold", "manual",
    "needs-decision", "changes-requested", "revoked_ship", "deleted", "duplicate", "cleanup",
    "typed", "late_source", "read_bound"])
def test_public_cli_rejects_unproven_or_stopped_progress(replay, monkeypatch, capsys, kind):
    result, value, gh, error = inspect(replay, monkeypatch, capsys, change=lambda gh: _change(gh, kind))
    assert result == 2 and value["action"] == "error"
    assert "pipelinectl inspect-merge-progress" in error
    assert gh.graph_calls <= progress.MAX_READS


def test_missing_signing_key_is_read_only(replay, monkeypatch, capsys):
    archive, path, _key = replay
    missing = path.parent / "absent-directory" / "key"
    monkeypatch.setenv("PP_PIPELINE_LEASE_KEY_FILE", str(missing))
    result, value, gh, _error = inspect(replay, monkeypatch, capsys)
    assert result == 2 and value["action"] == "error" and not missing.parent.exists()
    assert gh.calls == [] and gh.graph_calls == 0


@pytest.mark.parametrize("command", [["complete", "merge"], ["complete", "review"], ["gate-fallback", "merge"]])
def test_admission_attestation_never_substitutes_for_mutation_lease(replay, monkeypatch, capsys, command):
    result, value, _gh, _error = inspect(replay, monkeypatch, capsys)
    assert result == 0
    class NeverNetwork:
        def json(self, *_a, **_k):
            pytest.fail("wrong-purpose token reached GitHub")
    monkeypatch.setattr(pp, "GitHub", NeverNetwork)
    code = pp.run(["--config", str(replay[1]), *command, "--lease", value["attestation"]])
    assert code == 2



@pytest.mark.parametrize("binding", ["config", "checkout", "number", "head", "expired", "tampered"])
def test_attestation_rejects_wrong_execution_binding(replay, monkeypatch, capsys, binding):
    code, value, _gh, _error = inspect(replay, monkeypatch, capsys)
    assert code == 0
    archive, path, _key = replay
    arguments = dict(config=pp.load_config(str(path)), config_path=path, checkout=path.parent,
                     number=2480, head=archive["snapshot"]["headRefOid"])
    token = value["attestation"]
    if binding == "config": arguments["config"]["base_branch"] = "different"
    if binding == "checkout": arguments["checkout"] = path.parent / "other"
    if binding == "number": arguments["number"] = 9
    if binding == "head": arguments["head"] = "b" * 40
    if binding == "expired":
        expires = pp.decode_signed_lease(token)["expires_at"]
        monkeypatch.setattr(progress.time, "time", lambda: expires + 1)
    if binding == "tampered": token = ("A" if token[0] != "A" else "B") + token[1:]
    with pytest.raises(pp.PipelineError):
        progress.VerifiedProgress(token, **arguments)
