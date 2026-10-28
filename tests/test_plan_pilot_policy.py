# -*- coding: utf-8 -*-
"""F5: политика план-пилота — gate-канон миссии неприкосновенен."""
import sys
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "addons"))
from plan_pilot import approved_stage_amend, validate_amendment


def test_gate_canon_missing_check_blocks():
    ok, why = validate_amendment(
        "prm-full-service-v1",
        old_codes=["S01", "S02", "S27", "S30"],
        new_codes=["S01", "S02", "S03"],
        new_gates_text="uv run ... verify_stage.py S03",
    )
    assert not ok and "S27" in why


def test_gate_canon_present_allows():
    ok, why = validate_amendment(
        "prm-full-service-v1",
        old_codes=["S01", "S27", "S28", "S30"],
        new_codes=["S01", "S27", "S28", "S29A", "S30"],
        new_gates_text=("... verify_stage.py S27 ... verify_stage.py S28 "
                        "... verify_stage.py S30"),
    )
    assert ok, why


def test_unknown_slug_has_no_canon():
    ok, _ = validate_amendment("other-mission", [], [], "")
    assert ok


@pytest.fixture
def local_api():
    calls = []
    state = {"version": 7, "redirect": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(("GET", self.path, self.headers.get("Authorization")))
            if state["redirect"]:
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/stolen")
                self.end_headers()
                return
            payload = ([{"code": "S1"}] if self.path.endswith("/stages") else
                       {"id": "wf-id", "slug": "mission", "state_version": state["version"]})
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(("POST", self.path, body))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls, state
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_wrapper_checks_policy_before_posting_and_keeps_cas(local_api, monkeypatch):
    base, calls, state = local_api
    monkeypatch.setenv("PP_API_TOKEN", "private-token")
    stages = [{"code": "S2", "acceptance_gates": ["other-test"]}]
    with pytest.raises(ValueError, match="must-pass"):
        approved_stage_amend(base, "wf-id", 7, stages, "split stage", gate_canon={"mission": ["must-pass"]})
    assert not any(c[0] == "POST" for c in calls)
    stages[0]["acceptance_gates"] = ["must-pass"]
    result = approved_stage_amend(base, "wf-id", 7, stages, "split stage", gate_canon={"mission": ["must-pass"]})
    assert result == {"ok": True}
    assert calls[-1][2]["expected_version"] == 7
    assert calls[0][2] == "Bearer private-token"
    calls.clear()
    state["version"] = 8
    with pytest.raises(ValueError, match="changed"):
        approved_stage_amend(base, "wf-id", 7, stages, "split stage", gate_canon={})
    assert not any(c[0] == "POST" for c in calls)


@pytest.mark.parametrize("base", ["https://attacker.example", "file:///etc/passwd", "http://user:secret@localhost", "http://localhost/?key=x"])
def test_wrapper_cannot_send_environment_token_to_arbitrary_url(base, monkeypatch):
    monkeypatch.setenv("PP_API_TOKEN", "private-token")
    monkeypatch.setattr("urllib.request.build_opener", lambda *a: pytest.fail("must validate before network"))
    with pytest.raises(ValueError, match="loopback"):
        approved_stage_amend(base, "wf-id", 7, [], "split stage")


def test_wrapper_rejects_redirect_without_forwarding_token(local_api):
    base, calls, state = local_api
    state["redirect"] = True
    with pytest.raises(RuntimeError, match="request failed"):
        approved_stage_amend(base, "wf-id", 7, [], "split stage", token="private-token")
    assert len(calls) == 1 and calls[0][1] == "/api/workflows/wf-id"
