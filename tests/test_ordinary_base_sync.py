"""The ordinary first hop, through public pipelinectl and the real Git graph."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading

import pytest

from promptpilot import ordinary_base_sync as update, project_pipeline as pp
from test_base_sync_carry import (FakeGitHub, NUMBER, REPO, TRUSTED, health_of,
                                 template as template, world as world)  # noqa: F401


class OrdinaryGitHub(FakeGitHub):
    def __init__(self, graph):
        super().__init__(graph)
        self.actor = TRUSTED
        self.lose_response = False
        self.ignore_update = False
        self.updated_head = None

    def pull(self):
        return dict(number=NUMBER, title="ordinary", state="open", draft=False,
                    head=dict(sha=self.head), base=dict(ref=self.base_ref),
                    labels=[dict(name=x) for x in sorted(self.labels)], body=self.body)

    def run(self, *args, **kwargs):
        if args[:2] == ("api", "--paginate") and "/pulls?" in args[2]:
            return json.dumps(self.pull()) if not self.merged else ""
        result = super().run(*args, **kwargs)
        if getattr(self, "refresh_hook", None): self.refresh_hook()
        return result

    def json(self, *args, input_value=None):
        try:
            return self._json(*args, input_value=input_value)
        finally:
            if getattr(self, "refresh_hook", None): self.refresh_hook()

    def _json(self, *args, input_value=None):
        if args == ("api", "user"):
            return {"login": self.actor}
        if len(args) > 1 and args[1].endswith("/update-branch"):
            assert input_value == {"expected_head_sha": self.head}
            self.mutations.append(("PUT", args[1], input_value))
            if self.update_conflict:
                raise pp.PipelineError("Merge conflict (HTTP 422)")
            if not self.ignore_update:
                self.synced()
                if self.updated_head:
                    self.head = self.updated_head
                    self.world.set_pr_head(self.head)
            if self.lose_response:
                raise pp.PipelineError("update response lost")
            return {"message": "Updating pull request branch."}
        return super().json(*args, input_value=input_value)


@pytest.fixture
def ordinary(world, tmp_path, monkeypatch, capsys):
    server = OrdinaryGitHub(world)
    path = tmp_path/"pipelinectl.json"
    raw = dict(repository=REPO, trusted_account=TRUSTED, health_command=["fixture-health", "-json"],
               base_sync_merge=True, base_sync_carry=True, required_checks=["build", "test"],
               base_sync_update_timeout_seconds=1)
    changes = {}
    binary = os.environ.get("PP_ACCEPTANCE_HEALTH_BIN")
    if binary:
        assert hashlib.sha256(Path(binary).read_bytes()).hexdigest() == os.environ["PP_ACCEPTANCE_HEALTH_SHA256"]
        contract = Path(os.environ["PP_ACCEPTANCE_HEALTH_CONTRACT"])
        assert contract.is_file() and contract.is_absolute()
        prs, issues = tmp_path/"prs.json", tmp_path/"issues.json"
        issues.write_text("[]", encoding="utf-8")
        def refresh():
            item = server.pull()
            try: parents = world.parents(server.head)
            except AssertionError: parents = []
            item.update(comments=server.comments, head_parents=parents,
                        created_at="2026-09-01T00:00:00Z", updated_at="2026-09-01T01:00:00Z")
            items = [item] if not server.merged else []
            if changes.get("integration_owner"):
                other = copy.deepcopy(item)
                other.update(number=99, head=dict(sha=world.to), head_parents=[world.a, world.m1])
                items.append(other)
            temporary = prs.with_name("prs-"+secrets.token_hex(8)+".tmp")
            temporary.write_text(json.dumps(items), encoding="utf-8")
            os.replace(temporary, prs)
        server.refresh_hook = refresh
        refresh()
        raw["health_command"] = [binary, "-prs", str(prs), "-issues", str(issues),
                                 "-owner", TRUSTED, "-contract", str(contract), "-json"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.chdir(world.work)
    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path/"data"))
    monkeypatch.setenv("PP_PIPELINE_STATE_DIR", str(tmp_path/"cache"))
    monkeypatch.setenv("PP_PIPELINE_LEASE_KEY_FILE", str(tmp_path/"private"/"lease.key"))
    monkeypatch.delenv("PP_PIPELINE_REPLICAS", raising=False)
    monkeypatch.delenv("PP_PIPELINE_EXCLUDED_NUMBERS", raising=False)
    monkeypatch.setattr(pp, "GitHub", lambda: server)
    def health(*args, **kwargs):
        if server.head == world.a:
            value = dict(state="green", findings=[], integration_owner=None, review_candidates=[],
                         content_review_candidates=[], merge_executable=[dict(number=NUMBER, head=server.head, stage="merge")])
        else:
            value = health_of(server)(*args, **kwargs)
        value.update(copy.deepcopy(changes))
        return value
    if not binary:
        monkeypatch.setattr(pp, "run_health", health)
    def invoke(*args):
        code = pp.run(["--config", str(path), *args])
        output = capsys.readouterr()
        return code, json.loads(output.out)
    invoke.path, invoke.config, invoke.changes = path, raw, changes
    invoke.raw = lambda *args: pp.run(["--config", str(path), *args])
    return server, invoke


def ticket(run):
    code, result = run("next", "merge")
    assert code == 0 and result["action"] == "merge", result
    value = pp.decode_signed_lease(result["lease"])
    assert value["purpose"] == update.PROTOCOL and value["head"] == result["target"]["head"]
    return result["lease"]


def puts(server):
    return [x for x in server.mutations if x[1].endswith("/update-branch")]


def test_ordinary_behind_public_update_then_carry_and_cleanup(ordinary, world):
    server, run = ordinary
    token = ticket(run)
    code, result = run("complete", "merge", "--lease", token)
    assert code == 0 and result["action"] == "updated" and result["new_head"] == world.to, result
    assert len(puts(server)) == 1 and server.published() == []
    code, replay = run("complete", "merge", "--lease", token)
    assert code == 0 and replay == result and len(puts(server)) == 1
    code, carry = run("next", "merge")
    assert code == 0 and carry["carry"] == "proven", carry
    code, done = run("complete", "merge", "--lease", carry["lease"])
    assert code == 0 and done["action"] == "completed", done
    assert "ship" not in server.labels and len(puts(server)) == 1


@pytest.mark.parametrize("change", ["hold", "actor", "head", "base", "ci", "owner", "config", "expiry", "proof"])
def test_live_changes_refuse_before_update(ordinary, world, monkeypatch, change):
    server, run = ordinary
    token = ticket(run)
    if change == "hold": server.labels.add("hold")
    elif change == "actor": server.actor = "someone-else"
    elif change == "head": server.head = "e"*40
    elif change == "base": world.main_tip = world.m0
    elif change == "ci": server.checks.pop()
    elif change == "owner": run.changes["integration_owner"] = dict(number=99, head="b"*40, stage="integration-review")
    elif change == "config":
        run.config["base_sync_carry"] = False
        run.path.write_text(json.dumps(run.config), encoding="utf-8")
    elif change == "expiry":
        monkeypatch.setattr(update.time, "time", lambda: pp.decode_signed_lease(token)["expires_at"]+1)
    else: server.find("IC_101")["lastEditedAt"] = "2026-09-02T00:00:00Z"
    code, failed = run("complete", "merge", "--lease", token)
    assert code == 2 and failed["action"] == "error", failed
    assert puts(server) == [] and server.published() == []


@pytest.mark.parametrize("fault", ["strip-signature", "bad-signature", "other-purpose"])
def test_wrong_signed_purpose_never_falls_into_legacy_merge(ordinary, fault):
    server, run = ordinary
    token = ticket(run)
    if fault == "strip-signature": token = token.split(".")[0]
    elif fault == "bad-signature": token = token[:-1]+("A" if token[-1] != "A" else "B")
    else:
        value = pp.decode_signed_lease(token); value["purpose"] = "some-other-purpose"
        token = pp.encode_signed_lease(value)
    code, failed = run("complete", "merge", "--lease", token)
    assert code == 2 and failed["action"] == "error", failed
    assert puts(server) == [] and server.published() == []


def test_response_loss_recovers_read_only_after_flag_off_and_expiry(ordinary, monkeypatch):
    server, run = ordinary
    token = ticket(run)
    server.lose_response = True
    code, failed = run("complete", "merge", "--lease", token)
    assert code == 2 and "response lost" in failed["error"]
    run.config["base_sync_carry"] = False
    run.path.write_text(json.dumps(run.config), encoding="utf-8")
    monkeypatch.setattr(update.time, "time", lambda: pp.decode_signed_lease(token)["expires_at"]+1)
    code, result = run("complete", "merge", "--lease", token)
    assert code == 0 and result["action"] == "updated", result
    assert len(puts(server)) == 1 and server.published() == []


@pytest.mark.parametrize("outcome", ["422", "invisible", "wrong-tree"])
def test_unconfirmed_delivery_is_not_repeated_or_published(ordinary, world, outcome):
    server, run = ordinary
    token = ticket(run)
    server.update_conflict = outcome == "422"
    server.ignore_update = outcome == "invisible"
    if outcome == "wrong-tree": server.updated_head = world.evil_merge()
    code, result = run("complete", "merge", "--lease", token)
    assert (code == 2 or result["action"] == "wait"), result
    run("complete", "merge", "--lease", token)
    assert len(puts(server)) == 1 and server.published() == []


def test_lost_receipt_file_cannot_bootstrap_a_second_update(ordinary, tmp_path):
    server, run = ordinary
    token = ticket(run)
    server.ignore_update = True
    assert run("complete", "merge", "--lease", token)[1]["action"] == "wait"
    path = update._directory()/"receipts.sqlite3"
    assert path.resolve().is_relative_to(tmp_path.resolve())
    path.rename(path.with_suffix(".preserved"))
    code, failed = run("complete", "merge", "--lease", token)
    assert code == 2 and "missing" in failed["error"], failed
    assert len(puts(server)) == 1


def test_gate_is_checked_again_after_receipt_commit(ordinary, monkeypatch):
    server, run = ordinary
    token = ticket(run)
    actual = update._fresh
    calls = 0
    def late_hold(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2: server.labels.add("hold")
        return actual(*args, **kwargs)
    monkeypatch.setattr(update, "_fresh", late_hold)
    assert run("complete", "merge", "--lease", token)[0] == 2
    assert puts(server) == []


def test_corrupted_completed_receipt_is_not_status_evidence(ordinary):
    server, run = ordinary
    token = ticket(run)
    assert run("complete", "merge", "--lease", token)[1]["action"] == "updated"
    with sqlite3.connect(update._directory()/"receipts.sqlite3") as connection:
        connection.execute("UPDATE receipts SET result=?", (json.dumps({"action": "updated"}),))
    code, failed = run("complete", "merge", "--lease", token)
    assert code == 2 and failed["action"] == "error", failed
    assert len(puts(server)) == 1


def test_expiry_after_writer_admission_never_updates(ordinary, monkeypatch):
    server, run = ordinary
    token = ticket(run)
    actual = update._store
    class Connection:
        def __init__(self, inner): self.inner = inner
        def __getattr__(self, name): return getattr(self.inner, name)
        def execute(self, command, *args):
            result = self.inner.execute(command, *args)
            if command == "BEGIN IMMEDIATE":
                monkeypatch.setattr(update.time, "time", lambda: pp.decode_signed_lease(token)["expires_at"]+1)
            return result
    @contextmanager
    def delayed():
        with actual() as connection: yield Connection(connection)
    monkeypatch.setattr(update, "_store", delayed)
    assert run("complete", "merge", "--lease", token)[0] == 2
    assert puts(server) == []


def test_two_public_completes_never_send_two_updates(ordinary, monkeypatch):
    server, run = ordinary
    token = ticket(run)
    entered, release = threading.Event(), threading.Event()
    actual = update._fresh
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return actual(*args, **kwargs)
    monkeypatch.setattr(update, "_fresh", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run.raw, "complete", "merge", "--lease", token)
        try:
            assert entered.wait(10)
            second = pool.submit(run.raw, "complete", "merge", "--lease", token)
            assert second.result(timeout=5) == 2
        finally:
            release.set()
        assert first.result(timeout=10) == 0
    assert len(puts(server)) == 1


@pytest.mark.parametrize("flag", ["base_sync_merge", "base_sync_carry"])
def test_off_keeps_ordinary_behind_fallback_without_receipts(ordinary, flag):
    server, run = ordinary
    run.config[flag] = False
    run.path.write_text(json.dumps(run.config), encoding="utf-8")
    code, result = run("next", "merge")
    assert code == 0 and result["action"] == "fallback", result
    assert not update._directory().exists() and puts(server) == []
