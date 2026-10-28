"""Real public boundary regressions; all external transports are offline doubles."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import smtplib
import sys
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from promptpilot import config, flow_connectors as fc, flows
from test_flow_connectors import FakeIMAP, letter


@pytest.fixture
def case(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setenv("PP_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setattr(config, "DB_DIR", tmp_path)
    monkeypatch.setenv("DELIVERY_USER", "sender@example.invalid")
    monkeypatch.setenv("DELIVERY_PASSWORD", "dummy-password")
    return SimpleNamespace(db=isolated_db, root=tmp_path)


def route(steps, **extra):
    return flows.FlowDef.model_validate({"name": "delivery", "trust": "owner", "steps": steps, **extra})


MAIL = {"type": "email", "host": "mail.example.invalid", "user_env": "DELIVERY_USER",
        "password_env": "DELIVERY_PASSWORD"}
GITLAB = {"url": "https://gitlab.example.invalid", "token_env": "DUMMY_GL", "project": "1"}


def request(flow):
    return flows.create_item(flow, {"from": "person@example.invalid", "subject": "Request"})


def advance(flow, item):
    return flows.advance_item(item["id"], {flow.name: flow})


def test_gitlab_step_claim_precedes_network(case, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def apply(*args, **kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(timeout=10)
        return {"iid": 1}
    monkeypatch.setattr(fc, "gitlab_apply", apply)
    flow = route([{"id": "create", "kind": "gitlab", "create": {"title": "New"}},
                  {"id": "done", "kind": "finish"}], gitlab=GITLAB)
    item = request(flow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(advance, flow, item)
        second = None
        try:
            assert entered.wait(timeout=5)
            second = pool.submit(advance, flow, item)
            assert second.result(timeout=3)["status"] == "active"
            assert len(calls) == 1
        finally:
            release.set()
            first.result(timeout=10)
            if second:
                second.result(timeout=10)
    assert flows.get_item(item["id"])["status"] == "done"


def test_reply_edit_does_not_inherit_old_grant(case, monkeypatch):
    raw = {"name": "delivery", "trust": "client", "input": MAIL, "steps": [
        {"id": "approve", "kind": "human", "text": "Approve", "notify": False},
        {"id": "reply", "kind": "email_reply", "text": "Approved wording", "smtp_host": "smtp.invalid"},
        {"id": "done", "kind": "finish"}]}
    flow = flows.FlowDef.model_validate(raw)
    item = request(flow)
    waiting = advance(flow, item)
    flows.decide(item["id"], "approve", expected_version=waiting["version"], flows={flow.name: flow})
    raw["steps"][1]["text"] = "Changed wording"
    changed = flows.FlowDef.model_validate(raw)
    sent = []
    monkeypatch.setattr(fc, "send_reply", lambda *args, **kwargs: sent.append(kwargs))
    stopped = advance(changed, item)
    assert stopped["status"] == "needs_human" and sent == []
    assert "new human approval" in stopped["error"]


@pytest.mark.parametrize("action", ["retry", "skip", "cancel"])
def test_smtp_ambiguous_delivery_requires_distinct_explicit_review(case, monkeypatch, action):
    sent = []
    class SMTP:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def login(self, *args): pass
        def send_message(self, message):
            sent.append(message)
            raise smtplib.SMTPServerDisconnected("response lost after possible acceptance")
    monkeypatch.setattr(fc.smtplib, "SMTP_SSL", SMTP)
    flow = route([{"id": "reply", "kind": "email_reply", "text": "Reply", "smtp_host": "smtp.invalid"},
                  {"id": "done", "kind": "finish"}], input=MAIL)
    item = request(flow)
    stopped = advance(flow, item)
    assert len(sent) == 1 and stopped["status"] == "needs_human"
    assert stopped["wait"]["side_effect"] == "smtp" and stopped["wait"]["ambiguous"]
    function = getattr(flows, action)
    kwargs = {"flows": {flow.name: flow}} if action != "cancel" else {}
    for confirmation in [{}, {"confirm_command_stopped": True, "note": "Processes stopped"},
                         {"confirm_side_effect_reviewed": True}]:
        with pytest.raises(flows.FlowError):
            function(item["id"], expected_version=stopped["version"], **kwargs, **confirmation)
    recovered = function(item["id"], expected_version=stopped["version"],
                         confirm_side_effect_reviewed=True, note="Stopped prior sender; inspected remote mailbox; chosen recovery", **kwargs)
    assert recovered["wait"] is None
    if action == "retry":
        advance(flow, item)
        assert len(sent) == 2  # Only the explicit, reviewed resend reaches SMTP.
    else:
        assert len(sent) == 1
    assert any(event["event_type"] == "connector.effect_verified" for event in flows.item_events(item["id"]))


def test_accepted_smtp_data_is_not_retried_on_quit_error(case, monkeypatch):
    sent = []
    class SMTP:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): raise smtplib.SMTPServerDisconnected("QUIT response lost")
        def login(self, *args): pass
        def send_message(self, message): sent.append(message)
    monkeypatch.setattr(fc.smtplib, "SMTP_SSL", SMTP)
    flow = route([{"id": "reply", "kind": "email_reply", "text": "Reply", "smtp_host": "smtp.invalid"},
                  {"id": "done", "kind": "finish"}], input=MAIL)
    assert advance(flow, request(flow))["status"] == "done"
    assert len(sent) == 1


def test_cancelling_owned_command_keeps_exclusive_section(case, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    marker = case.root / "second-ran.txt"
    code = "import sys,time,pathlib;time.sleep(15) if sys.argv[1]=='1' else pathlib.Path(sys.argv[2]).write_text('done')"
    flow = route([{"id": "critical", "kind": "command", "run": [sys.executable, "-c", code, "{{item.id}}", str(marker)], "timeout": 20},
                  {"id": "done", "kind": "finish"}], exclusive={"first": "critical", "last": "critical", "max": 1})
    first, second = request(flow), request(flow)
    original = flows._execute
    def execute(argv, cwd, timeout, env, **kwargs):
        if argv[-2] == str(first["id"]):
            check = kwargs["keep_running"]
            def hold_cleanup():
                entered.set()
                assert release.wait(timeout=10)
                return check()
            kwargs["keep_running"] = hold_cleanup
        return original(argv, cwd, timeout, env, **kwargs)
    monkeypatch.setattr(flows, "_execute", execute)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(advance, flow, first)
        try:
            assert entered.wait(timeout=5)
            running = flows.get_item(first["id"])
            assert flows.cancel(first["id"], expected_version=running["version"])["status"] == "cancelling"
            advance(flow, second)
            assert not marker.exists()
        finally:
            release.set()
            future.result(timeout=15)
    assert flows.get_item(first["id"])["status"] == "cancelled"
    assert advance(flow, second)["status"] == "done" and marker.exists()


def test_gitlab_intake_reaches_candidate_after_excluded_first_page(case, monkeypatch):
    calls = []
    def listing(connection, method, path, payload=None):
        calls.append(path)
        page = int(parse_qs(urlsplit(path).query)["page"][0])
        if page == 1:
            return [{"iid": n, "title": "Excluded", "labels": ["ready", "skip"]} for n in range(1, 101)]
        assert page == 2
        return [{"iid": 101, "title": "Allowed", "labels": ["ready"]}]
    monkeypatch.setattr(fc, "gitlab_request", listing)
    flow = route([{"id": "done", "kind": "finish"}], gitlab=GITLAB,
                 input={"type": "gitlab_issues", "labels": ["ready"], "exclude_labels": ["skip"]})
    items = flows.poll_input(flow)
    assert [item["data"]["input"]["iid"] for item in items] == [101]
    assert len(calls) == 2 and all(path.startswith("/issues?") for path in calls)


def test_attachment_rollback_cannot_reuse_a_private_letter_directory(case, monkeypatch):
    flow = route([{"id": "done", "kind": "finish"}], input=dict(MAIL, save_attachments=True))
    original = fc.write_attachments
    failed = []
    def fail_after_write(directory, parts):
        original(directory, parts)
        failed.append(directory)
        raise OSError("simulated transaction interruption")
    monkeypatch.setattr(fc, "write_attachments", fail_after_write)
    with pytest.raises(OSError):
        flows.poll_input(flow, connect=lambda *args: FakeIMAP([
            letter("first@example.invalid", attachment=("first-private.txt", b"private"), key="<first>")]))
    assert flows.list_items(flow=flow.name) == []
    monkeypatch.setattr(fc, "write_attachments", original)
    item = flows.poll_input(flow, connect=lambda *args: FakeIMAP([
        letter("second@example.invalid", attachment=("second.txt", b"second"), key="<second>")]))[0]
    assert item["id"] == 1  # SQLite rolls back and reuses this identifier.
    own = Path(item["data"]["attachments_dir"])
    assert own != failed[0]
    assert [path.name for path in own.iterdir()] == ["01-second.txt"]
    assert all(Path(path).parent == own for path in item["data"]["input"]["attachments"])
    assert (failed[0] / "01-first-private.txt").exists()


def test_empty_attachment_set_still_has_an_isolated_directory(case):
    flow = route([{"id": "done", "kind": "finish"}], input=dict(MAIL, save_attachments=True))
    item = flows.poll_input(flow, connect=lambda *args: FakeIMAP([letter("one@example.invalid")]))[0]
    assert item["data"]["input"]["attachments"] == []
    assert Path(item["data"]["attachments_dir"]).is_dir()


def test_expired_active_owner_cannot_be_recovered_and_late_success_is_not_repeated(case, monkeypatch):
    from datetime import datetime, timedelta
    entered, release = threading.Event(), threading.Event()
    calls = []
    def apply(*args, **kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(timeout=10)
        return {"iid": 1}
    monkeypatch.setattr(fc, "gitlab_apply", apply)
    flow = route([{"id": "create", "kind": "gitlab", "create": {"title": "New"}},
                  {"id": "done", "kind": "finish"}], gitlab=GITLAB)
    item = request(flow)
    class FutureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=1000)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(advance, flow, item)
        try:
            assert entered.wait(timeout=5)
            monkeypatch.setattr(flows, "datetime", FutureClock)
            parked = advance(flow, item)
            assert parked["status"] == "needs_human" and parked["wait"]["ambiguous"]
            with pytest.raises(flows.FlowError, match="still active"):
                flows.retry(item["id"], flows={flow.name: flow}, expected_version=parked["version"],
                            confirm_side_effect_reviewed=True, note="Cannot bypass an in-flight owner")
        finally:
            release.set()
            settled = future.result(timeout=10)
    assert settled["status"] == "needs_human" and settled["step_id"] == "done"
    flows.retry(item["id"], flows={flow.name: flow}, expected_version=settled["version"])
    assert advance(flow, item)["status"] == "done" and len(calls) == 1


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_current_final_reply_decision_replaces_older_route_context(case, monkeypatch, decision):
    raw = {"name": "delivery", "trust": "client", "input": MAIL, "steps": [
        {"id": "initial", "kind": "human", "text": "Approve work", "notify": False},
        {"id": "final", "kind": "human", "text": "Send reply?", "on_reject": "continue", "notify": False},
        {"id": "reply", "kind": "email_reply", "text": "Final wording", "smtp_host": "smtp.invalid",
         "when": {"step": "final", "field": "decision", "in": ["approve"]}},
        {"id": "done", "kind": "finish"}]}
    flow = flows.FlowDef.model_validate(raw)
    item = request(flow)
    waiting = advance(flow, item)
    flows.decide(item["id"], "approve", expected_version=waiting["version"], flows={flow.name: flow})
    raw["vars"] = {"new_context": "after initial approval"}
    changed = flows.FlowDef.model_validate(raw)
    final = advance(changed, item)
    flows.decide(item["id"], decision, expected_version=final["version"], flows={changed.name: changed})
    sent = []
    def send(*args, **kwargs):
        sent.append(kwargs["text"])
        return "person@example.invalid"
    monkeypatch.setattr(fc, "send_reply", send)
    assert advance(changed, item)["status"] == "done"
    assert sent == (["Final wording"] if decision == "approve" else [])


def test_gitlab_pagination_limit_is_explicit_instead_of_silent_truncation(case, monkeypatch):
    calls = []
    def listing(connection, method, path, payload=None):
        calls.append(path)
        return [{"iid": n, "title": "Excluded", "labels": ["skip"]} for n in range(100)]
    monkeypatch.setattr(fc, "gitlab_request", listing)
    flow = route([{"id": "done", "kind": "finish"}], gitlab=GITLAB,
                 input={"type": "gitlab_issues", "labels": ["ready"], "exclude_labels": ["skip"]})
    with pytest.raises(fc.ConnectorError, match="100 pages"):
        flows.poll_input(flow)
    assert len(calls) == 100 and flows.list_items(flow=flow.name) == []
