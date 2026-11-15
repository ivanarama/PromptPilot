"""Private files + actual loopback HTTP, never the deployed broker or Native API.

On Windows only the POSIX uid/mode/flock/directory-fsync boundary is mocked.
The deployment CLI is deliberately POSIX-only. Mac runs use actual safeguards.
"""
from contextlib import contextmanager
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import threading

import pytest

from tools import external_mcp_gateway as gateway

FRONT = "frontend-test-credential-" + "f" * 32
BACK = "backend-test-credential-" + "b" * 32
SESSION = "a" * 64
JOB = "1" * 32


@pytest.fixture(autouse=True)
def portable_posix_boundary(monkeypatch):
    monkeypatch.setattr(gateway, "_IO_SLOT", threading.BoundedSemaphore(1))
    if os.name != "posix":
        def metadata(st, directory=False):
            assert stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
        monkeypatch.setattr(gateway, "private_metadata", metadata)
        monkeypatch.setattr(gateway, "sync_dir", lambda _path: None)
        monkeypatch.setattr(gateway, "lock_stream", lambda _stream, _acquire: None)


def private_file(path, content):
    path.write_text(content, encoding="utf8")
    path.chmod(0o600)
    return str(path.resolve())


@pytest.fixture
def config(tmp_path):
    directory = tmp_path / "gateway-private"
    directory.mkdir(mode=0o700)
    return {"listen_host": "127.0.0.1", "listen_port": 38499, "client_id": "glm",
        "upstream_url": "http://127.0.0.1:38500/api/external-clients/glm/mcp",
        "frontend_token_file": private_file(directory / "frontend", FRONT),
        "backend_token_file": private_file(directory / "backend", BACK),
        "state_file": str(directory / "state.json"), "io_timeout": 2}


def request(method="initialize", params=None, rid=1):
    if params is None:
        params = ({"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "legacy"}}
                  if method == "initialize" else {})
    return gateway.encode({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})


def tool(name, args=None, rid=2):
    return request("tools/call", {"name": name, "arguments": args or {}}, rid)


def submit_args():
    return {"job_id": JOB, "claim_token": "c" * 48, "outcome": "completed",
            "summary": "Unreviewed report", "artifacts": []}


def read_state(config):
    return json.loads(Path(config["state_file"]).read_text(encoding="utf8"))


class Upstream:
    def __init__(self, config):
        self.config, self.calls, self.failures = config, [], []
        self.status_job, self.status_state = JOB, "submitted"

    def __call__(self, url, headers, body, timeout):
        message = json.loads(body)
        self.calls.append({"url": url, "headers": dict(headers), "message": message})
        state = read_state(self.config)
        if message["method"] == "initialize":
            assert state["phase"] == "initializing"  # intent durable before network
        else:
            assert state["phase"] == "ready" and state["inflight"]
            assert headers["Mcp-Session-Id"] == SESSION
        assert headers["Authorization"] == "Bearer " + BACK
        assert headers["Authorization"] != "Bearer " + FRONT
        if self.failures:
            error = self.failures.pop(0)
            if error:
                raise error
        if message["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "native", "version": "1"}}
            response_headers = {"Mcp-Session-Id": SESSION}
        elif message["method"] == "tools/list":
            result = {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in sorted(gateway.TOOLS)]}
            response_headers = {}
        else:
            name = message["params"]["name"]
            if name == "pilot_status":
                data = {"job_id": self.status_job, "state": self.status_state,
                        "receipt_id": "receipt-one", "result_digest": "d" * 64}
            elif name == "pilot_next_task":
                data = {"job_id": JOB, "state": "claimed", "claim_token": "c" * 48}
            elif name == "pilot_read_input":
                data = {"path": "assignment.txt", "text": "supplied input"}
            else:
                data = {"state": "submitted", "receipt_id": "receipt-one", "result_digest": "d" * 64}
            result = {"structuredContent": data, "content": [{"type": "text", "text": json.dumps(data)}], "isError": False}
            response_headers = {}
        return 200, response_headers, gateway.encode({"jsonrpc": "2.0", "id": message["id"], "result": result})


def call(app, raw):
    return app.rpc(raw, "Bearer " + FRONT)


def test_authentication_precedes_parsing_and_every_upstream_operation(config):
    upstream = Upstream(config)
    app = gateway.Gateway(config, upstream)
    assert app.rpc(b"{" * 5000, None) == (401, None)
    assert app.rpc(request(), "Bearer wrong") == (401, None)
    assert upstream.calls == [] and not Path(config["state_file"]).exists()


def test_initialize_persistence_restart_and_cached_rpc_id_only(config):
    upstream = Upstream(config)
    app = gateway.Gateway(config, upstream)
    code, first = call(app, request(rid="first"))
    assert code == 200 and first["id"] == "first"
    raw = gateway.encode(first)
    assert SESSION.encode() not in raw and BACK.encode() not in raw and FRONT.encode() not in raw
    saved = Path(config["state_file"]).read_bytes()
    restarted = gateway.Gateway(config, upstream)
    code, second = call(restarted, request(rid="reconnect"))
    assert second == {**first, "id": "reconnect"} and len(upstream.calls) == 1
    assert Path(config["state_file"]).read_bytes() == saved
    assert call(restarted, request("ping"))[1]["result"] == {}
    assert len(upstream.calls) == 1
    assert len(call(restarted, request("tools/list"))[1]["result"]["tools"]) == 4
    assert len(upstream.calls) == 2


def test_unknown_initialize_never_creates_another_native_session(config):
    upstream = Upstream(config)
    upstream.failures = [TimeoutError("PRIVATE UPSTREAM DETAIL")]
    app = gateway.Gateway(config, upstream)
    response = call(app, request())[1]
    assert response["error"]["data"]["state"] == "UNKNOWN"
    assert "PRIVATE" not in json.dumps(response)
    assert read_state(config)["phase"] == "unknown_initialization"
    assert call(gateway.Gateway(config, upstream), request(rid=3))[1]["error"]["data"]["state"] == "UNKNOWN"
    assert len(upstream.calls) == 1


def test_accepted_initialize_but_failed_local_receipt_remains_unknown(config, monkeypatch):
    upstream = Upstream(config)
    app = gateway.Gateway(config, upstream)
    original = app.state.save
    def fail_ready(value):
        if value["phase"] == "ready":
            raise OSError("simulated fsync problem")
        original(value)
    monkeypatch.setattr(app.state, "save", fail_ready)
    assert call(app, request())[1]["error"]["data"]["state"] == "UNKNOWN"
    assert read_state(config)["phase"] == "unknown_initialization"
    assert len(upstream.calls) == 1
    assert "error" in call(gateway.Gateway(config, upstream), request())[1]
    assert len(upstream.calls) == 1


def test_unknown_submit_fenced_until_same_job_proven_status_not_status_timeout(config):
    upstream = Upstream(config)
    app = gateway.Gateway(config, upstream)
    call(app, request())
    upstream.failures = [TimeoutError("reply lost after Native accepted")]
    response = call(app, tool("pilot_submit_result", submit_args()))[1]
    assert response["error"]["data"]["state"] == "UNKNOWN" and len(upstream.calls) == 2
    assert read_state(config)["submit_fence"]["job_id"] == JOB
    assert "error" in call(app, tool("pilot_next_task"))[1]
    assert "error" in call(app, tool("pilot_submit_result", submit_args()))[1]
    assert len(upstream.calls) == 2  # no automatic or client mutation retry under fence
    upstream.failures = [TimeoutError("status also timed out")]
    assert call(app, tool("pilot_status", {"job_id": JOB}))[1]["error"]["data"]["state"] == "UNKNOWN"
    assert read_state(config)["submit_fence"]["job_id"] == JOB
    upstream.status_job = "2" * 32
    call(app, tool("pilot_status", {"job_id": JOB}))
    assert read_state(config)["submit_fence"]["state"] == "unknown"
    upstream.status_job = JOB
    call(app, tool("pilot_status", {"job_id": JOB}))
    state = read_state(config)
    assert state["submit_fence"]["state"] == "resolved"
    assert any(entry.get("tool") == "pilot_submit_result" and entry["outcome"] == "UNKNOWN" for entry in state["history"])
    assert call(app, tool("pilot_next_task"))[1]["result"]["structuredContent"]["state"] == "claimed"
    assert [c["message"].get("params", {}).get("name") for c in upstream.calls].count("pilot_submit_result") == 1


@pytest.mark.parametrize("name,args", [
    ("pilot_next_task", {}),
    ("pilot_read_input", {"job_id": JOB, "claim_token": "c" * 48, "path": "assignment.txt", "offset": 0, "length": 10}),
])
def test_explicit_next_or_read_retry_resumes_same_native_session_no_automatic_retry(config, name, args):
    upstream = Upstream(config)
    app = gateway.Gateway(config, upstream)
    call(app, request())
    upstream.failures = [TimeoutError()]
    assert "error" in call(app, tool(name, args))[1]
    assert len(upstream.calls) == 2 and read_state(config)["submit_fence"] is None
    assert "result" in call(gateway.Gateway(config, upstream), tool(name, args, rid=99))[1]
    assert len(upstream.calls) == 3
    assert all(c["headers"]["Mcp-Session-Id"] == SESSION for c in upstream.calls[1:])


def test_crash_after_send_intent_is_classified_without_implicit_replay(config):
    upstream = Upstream(config)
    app = gateway.Gateway(config, upstream)
    call(app, request())
    with app.state.locked() as state:
        state["inflight"] = {"nonce": "prior-send", "tool": "pilot_submit_result", "job_id": JOB,
                             "request_sha256": "e" * 64, "at": 1}
        app.state.save(state)
    assert "error" in call(gateway.Gateway(config, upstream), tool("pilot_next_task"))[1]
    assert len(upstream.calls) == 1
    assert read_state(config)["submit_fence"]["nonce"] == "prior-send"


def test_one_inflight_request_is_serialized_and_rejected_without_second_send(config):
    upstream = Upstream(config)
    entered, release = threading.Event(), threading.Event()
    def slow(*args):
        if json.loads(args[2])["method"] != "initialize":
            entered.set()
            assert release.wait(3)
        return upstream(*args)
    app = gateway.Gateway(config, slow)
    call(app, request())
    replies = []
    thread = threading.Thread(target=lambda: replies.append(call(app, tool("pilot_next_task"))))
    thread.start()
    assert entered.wait(3)
    assert "error" in call(app, tool("pilot_next_task"))[1]
    release.set()
    thread.join(3)
    assert not thread.is_alive() and len(upstream.calls) == 2 and "result" in replies[0][1]


@pytest.mark.parametrize("change", ["client", "upstream", "frontend", "backend"])
def test_state_binding_mismatch_never_calls_upstream_or_overwrites_state(config, change):
    upstream = Upstream(config)
    app = gateway.Gateway(config, upstream)
    call(app, request())
    before = Path(config["state_file"]).read_bytes()
    altered = copy.deepcopy(config)
    credential = FRONT
    if change == "client":
        altered["client_id"] = "other"
    elif change == "upstream":
        altered["upstream_url"] = "http://127.0.0.1:38501/api/external-clients/glm/mcp"
    elif change == "frontend":
        credential = "changed-frontend-" + "z" * 32
        Path(config["frontend_token_file"]).write_text(credential)
    else:
        Path(config["backend_token_file"]).write_text("changed-backend-" + "z" * 32)
    response = gateway.Gateway(altered, upstream).rpc(request(), "Bearer " + credential)[1]
    assert "error" in response and len(upstream.calls) == 1
    assert Path(config["state_file"]).read_bytes() == before


def test_notifications_unknown_tools_and_malformed_json_do_not_forward(config):
    upstream = Upstream(config)
    app = gateway.Gateway(config, upstream)
    assert call(app, gateway.encode({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "pilot_next_task"}})) == (202, None)
    assert "error" in call(app, tool("arbitrary_command", {"url": "http://169.254.169.254"}))[1]
    assert "error" in call(app, b"[" * 5000 + b"0" + b"]" * 5000)[1]
    assert "error" in call(app, b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}')[1]
    assert "error" in call(app, b"x" * (gateway.MAX_BODY + 1))[1]
    assert upstream.calls == [] and not Path(config["state_file"]).exists()


def test_config_is_exact_endpoint_private_and_no_remote_plaintext(config):
    path = Path(config["state_file"]).parent / "config.json"
    for key, value in [("upstream_url", "http://192.168.1.57:8420/api/external-clients/glm/mcp"),
                       ("upstream_url", "http://127.0.0.1:8420/api/tasks"),
                       ("upstream_url", "http://user:password@127.0.0.1:8420/api/external-clients/glm/mcp"),
                       ("upstream_url", "http://127.0.0.1:8420/api/external-clients/glm/mcp?other=1"),
                       ("client_id", "../../other"), ("listen_port", True)]:
        private_file(path, json.dumps({**config, key: value}))
        with pytest.raises((gateway.Refused, ValueError)):
            gateway.load_config(path)
    private_file(path, json.dumps(config))
    assert gateway.load_config(path)["upstream_url"] == config["upstream_url"]


def test_symlink_state_is_never_followed(config, tmp_path):
    original = tmp_path / "other.json"
    original.write_text("{\"untouched\":true}")
    try:
        Path(config["state_file"]).symlink_to(original)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(gateway.Refused):
        gateway.Gateway(config)
    assert original.read_text() == '{"untouched":true}'


@contextmanager
def http_peer(respond):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append({"path": self.path, "headers": dict(self.headers), "body": raw})
            status, headers, body = respond(raw)
            self.send_response(status)
            for key, value in {"Content-Type": "application/json", "Content-Length": str(len(body)), **headers}.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (OSError, ConnectionError):
                pass
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_real_direct_http_ignores_environment_proxies_and_does_not_expose_session(config, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    fake = Upstream(config)
    def respond(raw):
        status, headers, body = fake(config["upstream_url"], {"Authorization": "Bearer " + BACK}, raw, 2)
        return status, headers, body
    with http_peer(respond) as (base, seen):
        config["upstream_url"] = base + "/api/external-clients/glm/mcp"
        app = gateway.Gateway(config)
        code, reply = call(app, request())
        assert code == 200 and "result" in reply
    assert len(seen) == 1 and seen[0]["headers"]["Authorization"] == "Bearer " + BACK
    assert SESSION not in json.dumps(reply) and FRONT not in json.dumps(reply) and BACK not in json.dumps(reply)


def test_real_redirect_is_refused_without_following_or_retrying(config):
    with http_peer(lambda raw: (200, {}, b"{}")) as (target, target_calls):
        with http_peer(lambda raw: (302, {"Location": target + "/steal"}, b"{}")) as (source, calls):
            config["upstream_url"] = source + "/api/external-clients/glm/mcp"
            reply = call(gateway.Gateway(config), request())[1]
    assert reply["error"]["data"]["state"] == "UNKNOWN"
    assert len(calls) == 1 and target_calls == []


def test_frontend_http_auth_before_json_and_no_session_header(config):
    import http.client
    upstream = Upstream(config)
    app = gateway.Gateway(config, upstream)
    server = gateway.Server(("127.0.0.1", 0), gateway.Handler)
    server.gateway = app
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def send(authorization, raw):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        conn.request("POST", "/mcp", body=raw, headers={"Authorization": authorization})
        response = conn.getresponse()
        value = (response.status, dict(response.headers), response.read())
        conn.close()
        return value
    try:
        assert send("Bearer wrong", b"not JSON")[0] == 401 and upstream.calls == []
        code, headers, body = send("Bearer " + FRONT, request())
        assert code == 200 and "Mcp-Session-Id" not in headers and SESSION.encode() not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_upstream_oversize_or_private_secret_echo_is_unknown_not_forwarded(config):
    for raw in [b"x" * (gateway.MAX_BODY + 1), gateway.encode({"jsonrpc": "2.0", "id": 1, "result": {"secret": BACK}})]:
        # Separate new private state per independent fault case.
        path = Path(config["state_file"])
        altered = {**config, "state_file": str(path.with_name("fault-" + str(len(raw)) + ".json"))}
        app = gateway.Gateway(altered, lambda *a: (200, {"Mcp-Session-Id": SESSION}, raw))
        reply = call(app, request())[1]
        assert reply["error"]["data"]["state"] == "UNKNOWN"
        assert BACK not in json.dumps(reply) and SESSION not in json.dumps(reply)

def test_gateway_real_native_inbox_session_report_and_single_workflow_card(config, isolated_db, tmp_path, monkeypatch):
    from promptpilot import api
    from test_external_inbox import ASGIClient, waiting
    monkeypatch.setattr(api, "API_TOKEN", BACK)
    wf = waiting(tmp_path)
    client = ASGIClient(api.app)
    calls = []
    def native(url, headers, raw, timeout):
        calls.append(json.loads(raw))
        response = client.post("/api/external-clients/glm/mcp", content=raw, headers=headers)
        return response.status_code, dict(response.headers), response.content
    app = gateway.Gateway(config, native)
    initialized = call(app, request())[1]
    native_session = read_state(config)["session"]
    assert native_session not in json.dumps(initialized)
    claimed = call(app, tool("pilot_next_task"))[1]["result"]["structuredContent"]
    assert claimed["workflow_id"] == wf.id
    args = {"job_id": claimed["job_id"], "claim_token": claimed["claim_token"], "outcome": "completed",
            "summary": "Real native unreviewed result", "artifacts": [{"path": "proposal.md", "text": "Draft report"}]}
    ack = call(app, tool("pilot_submit_result", args))[1]["result"]["structuredContent"]
    assert ack["state"] == "submitted" and ack["unreviewed"]
    response = client.get("/api/task-feed?local_only=true", headers={"Authorization": "Bearer " + BACK})
    assert response.status_code == 200 and response.json()["total"] == 1
    assert response.json()["items"][0]["id"] == "local:workflow:" + wf.id
    detail = client.get(f"/api/task-feed/local/workflow/{wf.id}", headers={"Authorization": "Bearer " + BACK}).json()
    assert detail["external_result"]["receipt_id"] == ack["receipt_id"]
    assert detail["external_result"]["artifacts"][0]["path"] == "proposal.md"
    assert native_session not in json.dumps(detail)
    assert len(calls) == 3


def test_gateway_lost_ack_reconciles_real_native_receipt_without_repeating_core(config, isolated_db, tmp_path, monkeypatch):
    from promptpilot import api, workflows
    from test_external_inbox import ASGIClient, waiting
    monkeypatch.setattr(api, "API_TOKEN", BACK)
    waiting(tmp_path)
    client = ASGIClient(api.app)
    original = workflows.submit_external_result
    core_calls = []
    def counted(*args):
        core_calls.append(args)
        return original(*args)
    monkeypatch.setattr(workflows, "submit_external_result", counted)
    lose_once = [True]
    def native(url, headers, raw, timeout):
        message = json.loads(raw)
        response = client.post("/api/external-clients/glm/mcp", content=raw, headers=headers)
        if message.get("params", {}).get("name") == "pilot_submit_result" and lose_once[0]:
            lose_once[0] = False
            raise TimeoutError("simulated loss after Native commit")
        return response.status_code, dict(response.headers), response.content
    app = gateway.Gateway(config, native)
    call(app, request())
    claimed = call(app, tool("pilot_next_task"))[1]["result"]["structuredContent"]
    args = {"job_id": claimed["job_id"], "claim_token": claimed["claim_token"], "outcome": "completed",
            "summary": "Report needs audit", "artifacts": []}
    assert call(app, tool("pilot_submit_result", args))[1]["error"]["data"]["state"] == "UNKNOWN"
    assert len(core_calls) == 1
    assert "error" in call(app, tool("pilot_next_task"))[1]
    status = call(app, tool("pilot_status", {"job_id": claimed["job_id"]}))[1]["result"]["structuredContent"]
    assert status["state"] == "submitted" and status["receipt_id"]
    assert read_state(config)["submit_fence"]["state"] == "resolved"
    retried = call(app, tool("pilot_submit_result", args))[1]["result"]["structuredContent"]
    assert retried["receipt_id"] == status["receipt_id"] and len(core_calls) == 1


@pytest.mark.parametrize("secret_kind", ["backend", "session"])
def test_unicode_escaped_private_upstream_values_are_never_forwarded(config, secret_kind):
    upstream = Upstream(config)
    app = gateway.Gateway(config, upstream)
    if secret_kind == "session":
        call(app, request())
    secret = BACK if secret_kind == "backend" else SESSION
    def escaped(url, headers, raw, timeout):
        message = json.loads(raw)
        reply = {"jsonrpc": "2.0", "id": message["id"], "result": {"escaped": secret, "protocolVersion": "2025-06-18"}}
        escaped_value = "".join("\\u%04x" % ord(ch) for ch in secret).encode()
        body = gateway.encode(reply).replace(secret.encode(), escaped_value)
        assert secret.encode() not in body and json.loads(body)["result"]["escaped"] == secret
        return 200, {"Mcp-Session-Id": SESSION}, body
    app.upstream = escaped
    response = call(app, request() if secret_kind == "backend" else tool("pilot_next_task"))[1]
    assert response["error"]["data"]["state"] == "UNKNOWN"
    assert BACK not in json.dumps(response) and SESSION not in json.dumps(response)


def test_total_upstream_deadline_returns_before_delayed_connect_and_never_sends_later(monkeypatch):
    import time
    release, closed = threading.Event(), threading.Event()
    sent = []
    class DelayedConnection:
        sock = None
        def __init__(self, *args, **kwargs):
            pass
        def connect(self):
            release.wait(2)
        def request(self, *args, **kwargs):
            sent.append(True)
        def close(self):
            closed.set()
    monkeypatch.setattr(gateway.http.client, "HTTPConnection", DelayedConnection)
    started = time.monotonic()
    with pytest.raises(gateway.Uncertain):
        gateway.post_once("http://127.0.0.1:1/api/external-clients/glm/mcp", {}, b"{}", .05)
    assert time.monotonic() - started < .8 and sent == []
    release.set()
    assert closed.wait(1) and sent == []


def test_total_upstream_deadline_bounds_drip_fed_http_headers():
    import socketserver
    import time
    stop = threading.Event()
    calls = []
    class Drip(socketserver.BaseRequestHandler):
        def handle(self):
            incoming = b""
            while b"\r\n\r\n" not in incoming:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                incoming += chunk
            calls.append(True)
            try:
                self.request.sendall(b"HTTP/1.1 200 OK\r\n")
                for byte in b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}":
                    if stop.wait(.02):
                        return
                    self.request.sendall(bytes([byte]))
            except OSError:
                pass
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Drip)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises(gateway.Uncertain):
            gateway.post_once(f"http://127.0.0.1:{server.server_address[1]}/api/external-clients/glm/mcp", {}, b"{}", .1)
        assert time.monotonic() - started < .8 and calls == [True]
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_stuck_io_occupies_one_slot_and_later_request_starts_no_second_thread_or_send(monkeypatch):
    release, entered, closed = threading.Event(), threading.Event(), threading.Event()
    connections, sends = [], []
    class StuckConnection:
        sock = None
        def __init__(self, *args, **kwargs):
            connections.append(True)
        def connect(self):
            entered.set()
            release.wait(2)
        def request(self, *args, **kwargs):
            sends.append(True)
        def close(self):
            closed.set()
    monkeypatch.setattr(gateway.http.client, "HTTPConnection", StuckConnection)
    with pytest.raises(gateway.Uncertain):
        gateway.post_once("http://127.0.0.1:1/api/external-clients/glm/mcp", {}, b"{}", .05)
    assert entered.is_set() and connections == [True]
    with pytest.raises(gateway.Uncertain):
        gateway.post_once("http://127.0.0.1:1/api/external-clients/glm/mcp", {}, b"{}", .05)
    assert connections == [True] and sends == []
    release.set()
    assert closed.wait(1) and sends == []


def test_thread_start_failure_releases_io_slot_without_network(monkeypatch):
    class BrokenThread:
        def __init__(self, *args, **kwargs):
            pass
        def start(self):
            raise RuntimeError("no thread created")
    monkeypatch.setattr(gateway.threading, "Thread", BrokenThread)
    with pytest.raises(gateway.Uncertain):
        gateway.post_once("http://127.0.0.1:1/api/external-clients/glm/mcp", {}, b"{}", .05)
    assert gateway._IO_SLOT.acquire(blocking=False)
    gateway._IO_SLOT.release()
