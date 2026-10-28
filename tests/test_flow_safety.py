"""Public lifecycle regressions for approval, process and mailbox boundaries."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from email.message import EmailMessage
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from click.testing import CliRunner

from promptpilot import api, bot, config, flows, flow_connectors
from promptpilot.cli import cli


@pytest.fixture
def case(isolated_db, tmp_path, monkeypatch):
    directory = tmp_path / "flows"
    directory.mkdir()
    monkeypatch.setenv("PP_FLOWS_DIR", str(directory))
    monkeypatch.setattr(config, "DB_DIR", tmp_path)
    monkeypatch.setenv("TEST_FLOW_USER", "dummy-user")
    monkeypatch.setenv("TEST_FLOW_PASSWORD", "dummy-password")
    return SimpleNamespace(db=isolated_db, root=tmp_path, directory=directory)


def install(case, steps, *, trust="public", mail=False, limits=None):
    raw = {"name": "safe", "trust": trust, "steps": steps}
    if mail:
        raw["input"] = {"type": "email", "host": "mail.example", "folders": ["INBOX"],
                        "user_env": "TEST_FLOW_USER", "password_env": "TEST_FLOW_PASSWORD",
                        "require_from_domain": "forms.example", "dkim_authserv": "trusted.mail"}
    if limits is not None:
        raw["limits"] = limits
    (case.directory / "safe.json").write_text(json.dumps(raw), encoding="utf-8")
    loaded, errors = flows.load_flows()
    assert errors == {}
    return loaded["safe"]


def call(method, path, payload=None):
    async def request():
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8420") as client:
            return await client.request(method, path, json=payload)
    return asyncio.run(request())


def decide(item, decision="approve", *, version=None):
    return call("POST", f"/api/flows/items/{item['id']}/decision",
                {"decision": decision, "step": item["wait"]["approval"],
                 "expected_version": item["version"] if version is None else version})


def repeating_approval(case):
    flow = install(case, [
        {"id": "first", "kind": "human", "text": "First approval", "notify": False},
        {"id": "second", "kind": "human", "text": "Second approval", "notify": False,
         "repeat": {"from": "first", "when": {"step": "first", "field": "decision", "in": ["approve"]},
                    "max": 1, "on_exhausted": "continue"}},
        {"id": "done", "kind": "finish", "status": "done"}])
    item = flows.create_item(flow, {}, title="Cycle")
    first = flows.advance_item(item["id"])
    assert decide(first).status_code == 200
    second = flows.advance_item(item["id"])
    assert decide(second).status_code == 200
    later = flows.advance_item(item["id"])
    assert later["wait"]["approval"] == "first" and later["version"] > first["version"]
    return first, later


def test_api_rejects_replayed_same_step_occurrence_and_missing_version(case):
    first, later = repeating_approval(case)
    stale = decide(later, version=first["version"])
    missing = call("POST", f"/api/flows/items/{later['id']}/decision", {"decision": "approve", "step": "first"})
    assert stale.status_code == 409
    assert missing.status_code == 422
    assert flows.get_item(later["id"])["version"] == later["version"]
    assert decide(later).status_code == 200


@pytest.mark.parametrize("legacy", [False, True])
def test_telegram_rejects_old_cycle_and_legacy_button(case, monkeypatch, legacy):
    first, later = repeating_approval(case)
    monkeypatch.setattr(bot, "is_authorized", lambda _id: True)
    data = (f"flowdec:{later['id']}:first:a" if legacy
            else f"flowdec2:{later['id']}:{first['version']}:a")
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_reply_markup=AsyncMock(),
                            message=SimpleNamespace(reply_text=AsyncMock()))
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1, username="operator"),
                             effective_chat=SimpleNamespace(type="private"), callback_query=query)
    asyncio.run(bot.cb_flow(update, None))
    assert flows.get_item(later["id"])["version"] == later["version"]
    assert query.answer.await_args.kwargs.get("show_alert") is True


def test_versioned_callbacks_fit_telegram_and_old_notifications_do_not_gain_authority():
    for kind in ("approval", "action"):
        keyboard = bot._flow_keyboard(f"{2**63-1}:{kind}:{2**63-1}")
        assert all(len(button.callback_data.encode()) <= 64 for row in keyboard.inline_keyboard for button in row)
    assert bot._flow_keyboard("7:first") is None


@pytest.mark.parametrize("upgrade", ["insert_approval", "change_trust"])
def test_live_edit_cannot_raise_original_request_rights_without_approval(case, upgrade):
    work = {"id": "work", "kind": "agent", "rights": "read", "provider": "claude", "prompt": "Read"}
    finish = {"id": "done", "kind": "finish", "status": "done"}
    original = install(case, [work, finish])
    item = flows.create_item(original, {}, title="Public request")
    if upgrade == "insert_approval":
        install(case, [{"id": "approve", "kind": "human", "text": "Approve"},
                       {**work, "rights": "write"}, finish])
    else:
        install(case, [{**work, "rights": "full"}, finish], trust="owner")
    blocked = flows.advance_item(item["id"])
    assert blocked["status"] == "needs_human" and blocked["trust"] == "public"
    assert case.db.list_tasks() == []
    assert not any(event["event_type"] == "approval.decided" for event in flows.item_events(item["id"]))
    retry = call("POST", f"/api/flows/items/{item['id']}/retry", {"expected_version": blocked["version"]})
    assert retry.status_code == 200
    assert flows.advance_item(item["id"])["status"] == "needs_human"
    assert case.db.list_tasks() == []


def test_prompt_file_change_invalidates_question_before_runner_refresh(case):
    prompt = case.directory / "executor.md"
    prompt.write_text("Original instructions", encoding="utf-8")
    flow = install(case, [{"id": "approve", "kind": "human", "text": "Approve", "notify": False},
                          {"id": "work", "kind": "agent", "rights": "write", "provider": "claude",
                           "prompt_file": "executor.md"},
                          {"id": "done", "kind": "finish", "status": "done"}])
    item = flows.create_item(flow, {}, title="Context")
    old = flows.advance_item(item["id"])
    prompt.write_text("Changed instructions", encoding="utf-8")
    assert decide(old).status_code == 409
    assert not any(e["event_type"] == "approval.decided" for e in flows.item_events(item["id"]))
    fresh = flows.advance_item(item["id"])
    assert fresh["version"] > old["version"]
    assert fresh["wait"]["flow_revision"] != old["wait"]["flow_revision"]
    assert decide(fresh).status_code == 200
    active = flows.advance_item(item["id"])
    task = case.db.get_task(active["wait"]["task_id"])
    assert task.rights == "write" and "Changed instructions" in task.prompt


def process_flow(case, child_marker, *, timeout=1, started=None):
    child = "import time,pathlib;time.sleep(1.8);pathlib.Path(" + repr(str(child_marker)) + ").write_text('escaped')"
    program = "import subprocess,sys,time,pathlib;subprocess.Popen([sys.executable,'-c'," + repr(child) + "]);"
    if started:
        program += "pathlib.Path(" + repr(str(started)) + ").write_text('started');"
    program += "time.sleep(10)"
    return install(case, [{"id": "command", "kind": "command", "run": [sys.executable, "-c", program],
                           "timeout": timeout}, {"id": "done", "kind": "finish", "status": "done"}], trust="owner")


def test_timeout_stops_descendants_before_handoff_without_killing_unrelated(case):
    marker, unrelated = case.root / "escaped.txt", case.root / "unrelated.txt"
    program = "import time,pathlib;time.sleep(1.8);pathlib.Path(" + repr(str(unrelated)) + ").write_text('alive')"
    other = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        flow = process_flow(case, marker)
        item = flows.create_item(flow, {}, title="Timeout")
        stopped = flows.advance_item(item["id"])
        assert stopped["status"] == "needs_human"
        assert stopped["data"]["steps"]["command"]["exit_code"] == 124
        other.wait(timeout=5)
        assert unrelated.exists() and not marker.exists()
    finally:
        if other.poll() is None:
            other.kill()
            other.wait(timeout=5)


def test_cancel_waits_for_owned_command_cleanup(case):
    marker, started = case.root / "escaped.txt", case.root / "started.txt"
    flow = process_flow(case, marker, timeout=10, started=started)
    item = flows.create_item(flow, {}, title="Cancel")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(flows.advance_item, item["id"])
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert started.exists()
        running = flows.get_item(item["id"])
        requested = call("POST", f"/api/flows/items/{item['id']}/cancel", {"expected_version": running["version"]})
        assert requested.status_code == 200 and requested.json()["status"] == "cancelling"
        assert future.result(timeout=5)["status"] == "cancelled"
    time.sleep(2)
    assert not marker.exists()
    assert any(e["event_type"] == "item.cancelled" and e["payload"].get("command_stopped")
               for e in flows.item_events(item["id"]))


@pytest.mark.parametrize("action", ["retry", "skip", "cancel"])
def test_ambiguous_owner_blocks_recovery_until_observed_verified_stop(case, action):
    flow = install(case, [{"id": "command", "kind": "command", "run": [sys.executable, "-c", "pass"]},
                          {"id": "done", "kind": "finish", "status": "done"}], trust="owner")
    item = flows.create_item(flow, {}, title="Crash recovery")
    # A durable interrupted-owner state, as recovered from a previous process.
    with case.db._connect(immediate=True) as conn:
        conn.execute("UPDATE flow_items SET wait_json=? WHERE id=?", (json.dumps(
            {"command": "command", "owner": "previous-owner", "until": "2000-01-01T00:00:00+00:00"}), item["id"]))
    blocked = flows.advance_item(item["id"])
    assert blocked["status"] == "needs_human" and blocked["wait"]["ambiguous"]
    url = f"/api/flows/items/{item['id']}/{action}"
    assert call("POST", url, {"expected_version": blocked["version"]}).status_code == 409
    assert call("POST", url, {"expected_version": blocked["version"], "confirm_command_stopped": True}).status_code == 409
    assert flows.get_item(item["id"])["version"] == blocked["version"]
    verified = call("POST", url, {"expected_version": blocked["version"], "confirm_command_stopped": True,
                                  "note": "Verified the previous executor and descendants are stopped"})
    assert verified.status_code == 200
    assert any(e["event_type"] == "command.stop_verified" for e in flows.item_events(item["id"]))


def letter(subject="Request", domain="forms.example", headers=None, *, message_id=None):
    msg = EmailMessage()
    msg["From"], msg["Subject"] = "Submitter <form@forms.example>", subject
    if message_id:
        msg["Message-ID"] = message_id
    for value in headers or [f"trusted.mail; dkim=pass header.d={domain}"]:
        msg["Authentication-Results"] = value
    msg.set_content("A submitted request")
    return msg


class Mailbox:
    def __init__(self, messages, validity=100):
        self.messages = messages
        self.validity = validity

    def select(self, folder, readonly=False):
        assert readonly
        return "OK", [str(len(self.messages)).encode()]

    def response(self, name):
        return name, [str(self.validity).encode()]

    def uid(self, command, *args):
        if command == "search":
            return "OK", [" ".join(str(uid) for uid in sorted(self.messages)).encode()]
        uid, parts = args
        return self._fetch(int(uid), parts)

    def search(self, charset, criteria):
        return "OK", [" ".join(str(i + 1) for i in range(len(self.messages))).encode()]

    def fetch(self, sequence, parts):
        return self._fetch(sorted(self.messages)[int(sequence) - 1], parts)

    def _fetch(self, uid, parts):
        raw = self.messages[uid].as_bytes()
        if "HEADER" in parts:
            raw = raw.split(b"\n\n", 1)[0] + b"\n\n"
        return "OK", [(f"1 (UID {uid})".encode(), raw)]

    def login(self, user, password):
        assert (user, password) == ("dummy-user", "dummy-password")

    def _simple_command(self, *args):
        return "OK", []

    def logout(self):
        pass


@pytest.mark.parametrize("domain,accepted", [("forms.example.attacker.invalid", False),
                                                ("forms.example-other.invalid", False),
                                                ("forms.example", True), ("sub.forms.example", True)])
def test_intake_requires_complete_signing_domain(case, domain, accepted):
    flow = install(case, [{"id": "done", "kind": "finish", "status": "done"}], mail=True)
    box = Mailbox({11: letter(domain=domain)})
    assert bool(flows.poll_input(flow, connect=lambda *args: box)) is accepted


def test_intake_honors_first_trusted_receiver_result(case):
    flow = install(case, [{"id": "done", "kind": "finish", "status": "done"}], mail=True)
    box = Mailbox({11: letter(headers=["trusted.mail; dkim=fail header.d=forms.example",
                                     "trusted.mail; dkim=pass header.d=forms.example"])})
    assert flows.poll_input(flow, connect=lambda *args: box) == []


def test_intake_connection_verifies_tls_with_finite_timeout(case, monkeypatch):
    flow = install(case, [{"id": "done", "kind": "finish", "status": "done"}], mail=True)
    observed = {}
    def constructor(host, port, **kwargs):
        observed.update(kwargs)
        return Mailbox({})
    monkeypatch.setattr(flow_connectors.imaplib, "IMAP4_SSL", constructor)
    assert flows.poll_input(flow) == []
    context = observed["ssl_context"]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert 0 < observed["timeout"] <= 60


@pytest.mark.parametrize("reset_validity", [False, True])
def test_missing_message_id_survives_expunge_and_uidvalidity_reset(case, reset_validity):
    flow = install(case, [{"id": "done", "kind": "finish", "status": "done"}], mail=True)
    box = Mailbox({11: letter("First")})
    first = flows.poll_input(flow, connect=lambda *args: box)
    assert len(first) == 1
    box.messages = {(11 if reset_validity else 22): letter("Different request")}
    if reset_validity:
        box.validity = 200
    second = flows.poll_input(flow, connect=lambda *args: box)
    assert len(second) == 1 and second[0]["id"] != first[0]["id"]


@pytest.mark.parametrize("limit", ["day", "author"])
def test_concurrent_intake_obeys_limits_and_preserves_deferred_mail(case, monkeypatch, limit):
    flow = install(case, [{"id": "done", "kind": "finish", "status": "done"}], mail=True,
                   limits={"per_day": 1 if limit == "day" else 0,
                           "per_author": 1 if limit == "author" else 0})
    boxes = [Mailbox({11: letter("First", message_id="<first@example>")}),
             Mailbox({22: letter("Second", message_id="<second@example>")})]
    fetched = threading.Barrier(2)
    real_poll = flow_connectors.poll_mailbox
    def synchronized_poll(*args, **kwargs):
        result = real_poll(*args, **kwargs)
        fetched.wait(timeout=5)
        return result
    monkeypatch.setattr(flow_connectors, "poll_mailbox", synchronized_poll)
    measured = flows.created_today
    old_count_race = threading.Barrier(2)
    def synchronized_legacy_count(*args, **kwargs):
        count = measured(*args, **kwargs)
        if kwargs.get("conn") is None:
            old_count_race.wait(timeout=5)
        return count
    monkeypatch.setattr(flows, "created_today", synchronized_legacy_count)
    today = date.today()
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(flows.poll_input, flow, connect=lambda *a, box=box: box, today=today) for box in boxes]
        results = [attempt.result(timeout=10) for attempt in attempts]
    assert sum(len(result) for result in results) == 1
    monkeypatch.setattr(flow_connectors, "poll_mailbox", real_poll)
    monkeypatch.setattr(flows, "created_today", measured)
    state = json.loads(case.db.get_setting("flow_input:v1:safe"))
    assert len(state["later"]) == 1 and len(state["seen"]) == 1
    both = Mailbox({11: boxes[0].messages[11], 22: boxes[1].messages[22]})
    next_day = flows.poll_input(flow, connect=lambda *args: both, today=today + timedelta(days=1))
    assert len(next_day) == 1
    assert len(flows.list_items()) == 2



def launch_runner(case, marker, pidfile):
    script = (
        "import os;from pathlib import Path;"
        + "Path(" + repr(str(pidfile)) + ").write_text(str(os.getpid()));"
        + "from promptpilot import db,flows;db.init_db();"
        + "flows.run_forever(interval=1,log=lambda *args:None)"
    )
    env = {**os.environ, "PP_DATA_DIR": str(case.root)}
    return subprocess.Popen([sys.executable, "-X", "utf8", "-c", script],
                            cwd=Path(__file__).resolve().parents[1], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def wait_file(path, seconds=5):
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), str(path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM; Windows kernel owner death is tested separately")
def test_runner_sigterm_stops_owned_command_before_recovery(case):
    marker, started, pidfile = (case.root / name for name in ("escaped.txt", "started.txt", "owner.pid"))
    flow = process_flow(case, marker, timeout=10, started=started)
    item = flows.create_item(flow, {}, title="Graceful shutdown")
    runner = launch_runner(case, started, pidfile)
    try:
        wait_file(started)
        runner.terminate()
        _, stderr = runner.communicate(timeout=10)
        assert runner.returncode is not None, stderr
        time.sleep(2)
        assert not marker.exists()
        stopped = flows.get_item(item["id"])
        assert stopped["status"] == "needs_human"
        assert stopped["wait"]["command_cleanup_confirmed"]
        retry = call("POST", f"/api/flows/items/{item['id']}/retry", {"expected_version": stopped["version"]})
        assert retry.status_code == 200
    finally:
        if runner.poll() is None:
            runner.kill()
        runner.communicate(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Windows private Job Object owner-death integration")
def test_windows_real_owner_death_stops_descendants_and_keeps_recovery_barrier(case):
    import ctypes
    from ctypes import wintypes

    marker, started, pidfile = (case.root / name for name in ("escaped.txt", "started.txt", "owner.pid"))
    flow = process_flow(case, marker, timeout=10, started=started)
    item = flows.create_item(flow, {}, title="Windows owner death")
    runner = launch_runner(case, started, pidfile)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = None
    try:
        wait_file(pidfile)
        actual_pid = int(pidfile.read_text())
        assert actual_pid != os.getpid() and actual_pid > 0
        # Hold the actual interpreter's kernel handle: the Windows venv launcher
        # can have a different PID, and no later PID reuse can select another process.
        handle = kernel.OpenProcess(0x00100000 | 0x0001, False, actual_pid)
        assert handle
        wait_file(started)
        assert kernel.TerminateProcess(handle, 79)
        assert kernel.WaitForSingleObject(handle, 10000) == 0
        runner.communicate(timeout=10)
        time.sleep(2)
        assert not marker.exists()
        current = flows.get_item(item["id"])
        assert current["wait"]["owner"]
        with case.db._connect(immediate=True) as conn:
            wait = {**current["wait"], "until": "2000-01-01T00:00:00+00:00"}
            conn.execute("UPDATE flow_items SET wait_json=? WHERE id=?", (json.dumps(wait), item["id"]))
        uncertain = flows.advance_item(item["id"])
        assert uncertain["status"] == "needs_human" and uncertain["wait"]["ambiguous"]
        assert call("POST", f"/api/flows/items/{item['id']}/retry",
                    {"expected_version": uncertain["version"]}).status_code == 409
    finally:
        if handle:
            if kernel.WaitForSingleObject(handle, 0) == 0x102:
                kernel.TerminateProcess(handle, 80)
                kernel.WaitForSingleObject(handle, 10000)
            kernel.CloseHandle(handle)
        if runner.poll() is None:
            runner.kill()
        runner.communicate(timeout=10)



def test_agent_uses_only_approved_immutable_prompt_buffer(case, monkeypatch):
    prompt = case.directory / "executor.md"
    approved = "Approved original instructions"
    prompt.write_text(approved, encoding="utf-8")
    flow = install(case, [{"id": "approve", "kind": "human", "text": "Approve", "notify": False},
                          {"id": "work", "kind": "agent", "rights": "write", "provider": "claude",
                           "prompt_file": "executor.md"},
                          {"id": "done", "kind": "finish", "status": "done"}])
    item = flows.create_item(flow, {}, title="Immutable prompt")
    assert decide(flows.advance_item(item["id"])).status_code == 200
    read_text = Path.read_text
    def transient_text(path, *args, **kwargs):
        if path.resolve() == prompt.resolve():
            path.write_text("Changed unapproved instructions", encoding="utf-8")
            try:
                return read_text(path, *args, **kwargs)
            finally:
                path.write_text(approved, encoding="utf-8")
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", transient_text)
    active = flows.advance_item(item["id"], {flow.name: flow})
    assert read_text(prompt, encoding="utf-8") == approved
    task = case.db.get_task(active["wait"]["task_id"])
    assert approved in task.prompt and "Changed unapproved instructions" not in task.prompt


@pytest.mark.parametrize("already_approved", [False, True])
def test_moving_identical_route_changes_effective_approval_context(case, monkeypatch, already_approved):
    flow = install(case, [{"id": "approve", "kind": "human", "text": "Approve", "notify": False},
                          {"id": "work", "kind": "agent", "rights": "write", "provider": "claude",
                           "working_dir": "{{flow.dir}}/work", "prompt": "Write"},
                          {"id": "done", "kind": "finish", "status": "done"}])
    item = flows.create_item(flow, {}, title="Moved route")
    question = flows.advance_item(item["id"])
    if already_approved:
        assert decide(question).status_code == 200
    moved = case.root / "other-routes"
    moved.mkdir()
    (moved / "safe.json").write_bytes((case.directory / "safe.json").read_bytes())
    monkeypatch.setenv("PP_FLOWS_DIR", str(moved))
    if already_approved:
        assert flows.advance_item(item["id"])["status"] == "needs_human"
        assert case.db.list_tasks() == []
    else:
        assert decide(question).status_code == 409



@pytest.mark.parametrize("version", [True, "1", None])
def test_remote_decision_requires_strict_observed_integer(case, version):
    flow = install(case, [{"id": "approve", "kind": "human", "text": "Approve", "notify": False},
                          {"id": "done", "kind": "finish", "status": "done"}])
    item = flows.create_item(flow, {}, title="Strict version")
    question = flows.advance_item(item["id"])
    reply = call("POST", f"/api/flows/items/{item['id']}/decision",
                 {"decision": "approve", "expected_version": version})
    assert reply.status_code == 422
    assert flows.get_item(item["id"])["version"] == question["version"]


def test_cli_uses_current_cas_unless_observed_version_is_explicit(case):
    flow = install(case, [{"id": "approve", "kind": "human", "text": "Approve", "notify": False},
                          {"id": "done", "kind": "finish", "status": "done"}])
    item = flows.create_item(flow, {}, title="CLI intent")
    question = flows.advance_item(item["id"])
    runner = CliRunner()
    stale = runner.invoke(cli, ["flows", "approve", str(item["id"]), "--expected-version", str(question["version"]-1)])
    assert stale.exit_code != 0
    assert flows.get_item(item["id"])["version"] == question["version"]
    current = runner.invoke(cli, ["flows", "approve", str(item["id"])])
    assert current.exit_code == 0, current.output
    grants = [e for e in flows.item_events(item["id"]) if e["event_type"] == "approval.decided"]
    assert len(grants) == 1 and grants[0]["payload"]["approval_version"] == question["version"]


@pytest.mark.parametrize("validity", [0, 2**32])
def test_missing_message_id_waits_for_proven_uidvalidity(case, validity):
    flow = install(case, [{"id": "done", "kind": "finish", "status": "done"}], mail=True)
    box = Mailbox({11: letter()}, validity=validity)
    with pytest.raises(RuntimeError, match="UIDVALIDITY"):
        flows.poll_input(flow, connect=lambda *args: box)
    assert flows.list_items() == []
    assert case.db.get_setting("flow_input:v1:safe") is None
    box.validity = 100
    assert len(flows.poll_input(flow, connect=lambda *args: box)) == 1
