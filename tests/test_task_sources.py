"""Real loopback HTTP peers; never connect to registered development instances."""
from contextlib import contextmanager
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from promptpilot import task_sources as sources


@contextmanager
def peer(respond):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append({"method": "GET", "path": self.path,
                             "authorization": self.headers.get("Authorization")})
            status, payload, headers = respond(self.path)
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            for key, value in {"Content-Type": "application/json", **headers}.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            requests.append({"method": "POST", "path": self.path})
            self.send_error(405)

        def log_message(self, *args):
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
        thread.join(timeout=2)


def registry(tmp_path, *entries):
    path = tmp_path / "task-sources.json"
    path.write_text(json.dumps({"version": 1, "sources": list(entries)}), encoding="utf-8")
    return path


def entry(url, source_id="studio-windows", **extra):
    return {"id": source_id, "label": "Studio Windows", "base_url": url, **extra}


def item(object_id="wf_one", **extra):
    return {"kind": "workflow", "object_id": object_id, "source_id": "local",
            "title": "One business task", "objective": "Build safely",
            "status": "active", "status_label": "В работе", **extra}


def workflow(object_id="wf_one"):
    return {"id": object_id, "slug": "order-one", "objective": "Approved plan",
            "status": "reviewing", "current_round": 2, "state_version": 8,
            "config": {"workorder_guard": {"lease_token": "LEASE-SECRET"},
                       "roles": {"executor": {"provider": "codex"}}}}


def assert_private_free(value):
    text = json.dumps(value)
    for hidden in ["LEASE-SECRET", "ACCESS-SECRET", "PROXY-PASSWORD",
                   "workorder_guard", "token_file", "base_url", "config_json"]:
        assert hidden not in text


def test_missing_registry_is_optional_and_has_no_network(tmp_path, monkeypatch):
    monkeypatch.setattr(sources.config, "DB_DIR", tmp_path)
    monkeypatch.delenv("PP_TASK_SOURCES_FILE", raising=False)
    monkeypatch.setattr(sources.urllib.request, "build_opener",
                        lambda *args: pytest.fail("unregistered network call"))
    assert sources.list_source_items() == {"items": [], "sources": [], "errors": []}


def test_upgraded_feed_pages_local_only_namespaces_and_strips_secrets(tmp_path, monkeypatch):
    def respond(path):
        query = parse_qs(urlsplit(path).query)
        assert urlsplit(path).path == "/api/task-feed"
        assert query["local_only"] == ["true"]
        start = int(query["offset"][0])
        rows = [item(f"wf_{n}", config={"token": "LEASE-SECRET"}, controls=["cancel"],
                     callback="ACCESS-SECRET") for n in range(start, min(start + 100, 101))]
        return 200, {"items": rows, "total": 101}, {}

    monkeypatch.setenv("REMOTE_PP_TOKEN", "ACCESS-SECRET")
    with peer(respond) as (url, calls):
        result = sources.list_source_items(sources_file=registry(
            tmp_path, entry(url, token_env="REMOTE_PP_TOKEN")))
    assert len(calls) == 2 and all(c["authorization"] == "Bearer ACCESS-SECRET" for c in calls)
    assert len(result["items"]) == 101
    assert result["items"][0]["id"] == "studio-windows:workflow:wf_0"
    assert all(v["readonly"] and v["source_id"] == "studio-windows" for v in result["items"])
    assert result["sources"][0]["status"] == "online"
    assert_private_free(result)


def test_nested_peer_sources_are_not_reimported(tmp_path):
    with peer(lambda path: (200, {"items": [item(), item("wf_other", source_id="another")],
                                 "total": 2}, {})) as (url, calls):
        result = sources.list_source_items(sources_file=registry(tmp_path, entry(url)))
    assert len(calls) == 1 and len(result["items"]) == 1
    assert result["sources"][0]["status"] == "partial"


def test_only_404_falls_back_to_legacy_workflows_never_raw_tasks(tmp_path):
    def respond(path):
        if urlsplit(path).path == "/api/task-feed":
            return 404, {}, {}
        assert urlsplit(path).path == "/api/workflows"
        return 200, [workflow()], {}

    with peer(respond) as (url, calls):
        result = sources.list_source_items(sources_file=registry(tmp_path, entry(url)))
    assert len(calls) == 2 and all(c["method"] == "GET" for c in calls)
    assert result["items"][0]["kind"] == "workflow"
    assert result["sources"][0]["status"] == "partial"
    assert result["sources"][0]["message"] == sources.LEGACY_MESSAGE
    assert_private_free(result)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_http_failures_are_unavailable_no_legacy_fallback_or_error_leak(tmp_path, status):
    with peer(lambda path: (status, {"error": "ACCESS-SECRET at private-host"}, {})) as (url, calls):
        result = sources.list_source_items(sources_file=registry(tmp_path, entry(url)))
    assert len(calls) == 1 and result["items"] == []
    assert result["sources"][0]["status"] == "unavailable"
    assert url not in json.dumps(result) and "private-host" not in json.dumps(result)
    assert_private_free(result)


def test_redirect_is_not_followed_and_token_not_sent_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_PP_TOKEN", "ACCESS-SECRET")
    with peer(lambda path: (200, {}, {})) as (target, target_calls):
        with peer(lambda path: (302, {}, {"Location": target + "/steal"})) as (url, calls):
            result = sources.list_source_items(sources_file=registry(
                tmp_path, entry(url, token_env="REMOTE_PP_TOKEN")))
    assert len(calls) == 1 and target_calls == []
    assert result["sources"][0]["status"] == "unavailable"
    assert_private_free(result)


def test_environment_proxy_credentials_are_not_used(tmp_path, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://user:PROXY-PASSWORD@127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://user:PROXY-PASSWORD@127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    with peer(lambda path: (200, {"items": [], "total": 0}, {})) as (url, calls):
        result = sources.list_source_items(sources_file=registry(tmp_path, entry(url)))
    assert len(calls) == 1 and result["sources"][0]["status"] == "online"
    assert_private_free(result)


def test_credential_file_backend_only_and_missing_credentials_safe(tmp_path):
    token = tmp_path / "private-token.txt"
    token.write_text("ACCESS-SECRET\n", encoding="utf-8")
    with peer(lambda path: (200, {"items": [item()], "total": 1}, {})) as (url, calls):
        config = registry(tmp_path, entry(url, token_file=token.name))
        first = sources.list_source_items(sources_file=config)
        token.unlink()
        second = sources.list_source_items(sources_file=config)
    assert len(calls) == 1 and calls[0]["authorization"] == "Bearer ACCESS-SECRET"
    assert second["sources"][0]["error"]["code"] == "credentials_unavailable"
    assert_private_free(first)
    assert_private_free(second)
    assert str(token) not in json.dumps(second)


@pytest.mark.parametrize("payload,headers", [
    (b"callback({\"items\":[]})", {}),
    (b"not JSON LEASE-SECRET", {}),
    ({"items": {}}, {}),
    ({"items": [], "total": 0}, {"Content-Type": "text/javascript"}),
])
def test_invalid_json_jsonp_or_schema_refused_without_fallback(tmp_path, payload, headers):
    with peer(lambda path: (200, payload, headers)) as (url, calls):
        result = sources.list_source_items(sources_file=registry(tmp_path, entry(url)))
    assert len(calls) == 1 and result["sources"][0]["status"] == "unavailable"
    assert_private_free(result)


def test_response_body_cap_and_request_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "MAX_BODY", 128)
    with peer(lambda path: (200, {"items": [item(objective="x" * 200)]}, {})) as (url, _):
        result = sources.list_source_items(sources_file=registry(tmp_path, entry(url)))
    assert result["sources"][0]["error"]["code"] == "response_too_large"
    monkeypatch.setattr(sources, "HTTP_TIMEOUT", 0.02)

    def slow(path):
        time.sleep(0.12)
        return 200, {"items": []}, {}

    with peer(slow) as (url, calls):
        result = sources.list_source_items(sources_file=registry(tmp_path, entry(url)))
    assert len(calls) == 1 and result["sources"][0]["status"] == "unavailable"


def test_request_count_is_bounded_even_if_peer_always_claims_more(tmp_path):
    def respond(path):
        start = int(parse_qs(urlsplit(path).query)["offset"][0])
        return 200, {"items": [item(f"wf_{i}") for i in range(start, start + 100)],
                     "total": 1000000}, {}

    with peer(respond) as (url, calls):
        result = sources.list_source_items(sources_file=registry(tmp_path, entry(url)))
    assert len(calls) == 10 and len(result["items"]) == 1000
    assert result["sources"][0]["status"] == "partial"


def test_bad_and_duplicate_sources_skip_without_losing_valid_peer(tmp_path, monkeypatch):
    with peer(lambda path: (200, {"items": [item()], "total": 1}, {})) as (url, calls):
        monkeypatch.setattr(sources.config, "PORT", 8420)
        path = registry(tmp_path, entry(url), entry(url, "dup"), entry(url, "dup"),
                        entry(url, "local"), {"id": "bad", "label": "Bad", "base_url":
                                             "http://user:ACCESS-SECRET@127.0.0.1:1234"},
                        entry("http://127.0.0.1:8420", "self"), 2)
        result = sources.list_source_items(sources_file=path)
    assert len(calls) == 1 and len(result["items"]) == 1
    assert len(result["errors"]) == 5
    assert_private_free(result)


@pytest.mark.parametrize("data", [[], {"sources": {}}, {"sources": [0] * 65},
                                  {"version": True, "sources": []}])
def test_invalid_registry_is_bounded_safe_error(tmp_path, data):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    result = sources.list_source_items(sources_file=path)
    assert result["items"] == [] and result["errors"]


def test_environment_registry_override(tmp_path, monkeypatch):
    path = registry(tmp_path)
    monkeypatch.setenv("PP_TASK_SOURCES_FILE", str(path))
    assert sources.list_source_items() == {"items": [], "sources": [], "errors": []}


def test_modern_detail_whitelists_all_nested_history_and_result(tmp_path):
    raw = {"item": item(config={"lease_token": "LEASE-SECRET"}),
           "history": {"runs": [{"id": "run_one", "role": "reviewer", "round_no": 2,
                                  "status": "completed", "task_id": 42,
                                  "output_json": {"token": "LEASE-SECRET"}}],
                       "events": [{"seq": 1, "event_type": "run.completed",
                                   "payload": {"token": "LEASE-SECRET"}}]},
           "result": {"available": True, "characters": 123, "sha256": "a" * 64,
                      "report": "LEASE-SECRET"},
           "artifacts": [{"id": "a_one", "kind": "build", "path": "build/result.zip",
                          "metadata": {"token": "LEASE-SECRET"}}]}
    with peer(lambda path: (200, raw, {})) as (url, calls):
        result = sources.get_source_detail("studio-windows", "workflow", "wf_one",
                                           sources_file=registry(tmp_path, entry(url)))
    assert calls[0]["path"] == "/api/task-feed/local/workflow/wf_one"
    assert result["item"]["id"] == "studio-windows:workflow:wf_one"
    assert result["history"]["role_groups"][0]["label"] == "Проверяющий"
    assert result["history"]["runs"][0]["round_no"] == 2
    assert result["result"]["characters"] == 123
    assert_private_free(result)


def test_legacy_workflow_detail_reads_rounds_runs_events_only(tmp_path):
    def respond(path):
        endpoint = urlsplit(path).path
        if endpoint.startswith("/api/task-feed/"):
            return 404, {}, {}
        if endpoint == "/api/workflows/wf_one":
            return 200, workflow(), {}
        if endpoint.endswith("/rounds"):
            return 200, [{"id": "round_two", "round_no": 2}], {}
        if endpoint.endswith("/events"):
            return 200, [{"seq": 1, "event_type": "workflow.created",
                          "payload_json": "LEASE-SECRET"}], {}
        assert endpoint.endswith("/rounds/round_two/runs")
        return 200, [{"id": "run_two", "round_id": "round_two", "role": "reviewer",
                      "status": "completed", "task_id": 99,
                      "input_sha256": "LEASE-SECRET", "output_json": "LEASE-SECRET"}], {}

    with peer(respond) as (url, calls):
        result = sources.get_source_detail("studio-windows", "workflow", "wf_one",
                                           sources_file=registry(tmp_path, entry(url)))
    assert len(calls) == 5 and all(v["method"] == "GET" for v in calls)
    assert result["source"]["status"] == "partial"
    assert result["source"]["message"] == sources.LEGACY_MESSAGE
    assert result["history"]["runs"][0]["round_no"] == 2
    assert result["result"] == {"available": False}
    assert_private_free(result)


def test_task_404_never_falls_back_to_legacy_raw_task(tmp_path):
    with peer(lambda path: (404, {}, {})) as (url, calls):
        result = sources.get_source_detail("studio-windows", "task", 10,
                                           sources_file=registry(tmp_path, entry(url)))
    assert len(calls) == 1 and result["source"]["status"] == "unavailable"
    assert result["item"] is None


@pytest.mark.parametrize("source_id,kind,object_id", [
    ("http://evil.invalid", "workflow", "wf_one"),
    ("unknown", "workflow", "wf_one"), ("studio-windows", "workflow", "../../api/tasks"),
    ("studio-windows", "workflow", "http://evil.invalid"), ("studio-windows", "command", "start"),
    ("studio-windows", "task", True), ("studio-windows", "task", "10"),
])
def test_caller_identity_cannot_supply_urls_or_commands(tmp_path, source_id, kind, object_id):
    with peer(lambda path: pytest.fail("invalid caller caused network")) as (url, calls):
        result = sources.get_source_detail(source_id, kind, object_id,
                                           sources_file=registry(tmp_path, entry(url)))
    assert calls == [] and result["item"] is None
    assert "evil.invalid" not in json.dumps(result)


def test_legacy_detail_history_is_bounded(tmp_path):
    def respond(path):
        endpoint = urlsplit(path).path
        if endpoint.startswith("/api/task-feed/"):
            return 404, {}, {}
        if endpoint.endswith("/rounds"):
            return 200, [{"id": f"round_{i}", "round_no": i} for i in range(50)], {}
        if endpoint.endswith("/events"):
            return 200, [], {}
        if endpoint.endswith("/runs"):
            return 200, [{"id": "r", "role": "executor"}], {}
        return 200, workflow(), {}

    with peer(respond) as (url, calls):
        result = sources.get_source_detail("studio-windows", "workflow", "wf_one",
                                           sources_file=registry(tmp_path, entry(url)))
    assert len(calls) == 9 and result["history"]["truncated"] is True


def test_untrusted_history_round_number_cannot_smuggle_nested_config(tmp_path):
    raw = {"item": item(),
           "history": {"runs": [{"id": "r", "role": "executor",
                                  "round_no": {"lease_token": "LEASE-SECRET"}}], "events": []},
           "result": {"available": False}, "artifacts": []}
    with peer(lambda path: (200, raw, {})) as (url, _):
        result = sources.get_source_detail("studio-windows", "workflow", "wf_one",
                                           sources_file=registry(tmp_path, entry(url)))
    assert result["history"]["runs"][0]["round_no"] is None
    assert_private_free(result)


def test_pathological_json_nesting_is_safe_source_error(tmp_path):
    with peer(lambda path: (200, b"[" * 2500 + b"0" + b"]" * 2500, {})) as (url, calls):
        result = sources.list_source_items(sources_file=registry(tmp_path, entry(url)))
    assert len(calls) == 1 and result["sources"][0]["status"] == "unavailable"
    path = tmp_path / "bad-registry.json"
    path.write_bytes(b"[" * 2500 + b"0" + b"]" * 2500)
    result = sources.list_source_items(sources_file=path)
    assert result["errors"] and result["items"] == []


def test_huge_task_identifier_is_not_used_in_remote_url(tmp_path):
    with peer(lambda path: pytest.fail("unbounded identity caused network")) as (url, calls):
        result = sources.get_source_detail("studio-windows", "task", 2**100,
                                           sources_file=registry(tmp_path, entry(url)))
    assert calls == [] and result["item"] is None


@pytest.mark.parametrize("page", [{"items": [], "total": 1},
                                  {"items": [], "total": True},
                                  {"items": [], "total": -1}])
def test_inconsistent_peer_page_is_unavailable_not_falsely_empty(tmp_path, page):
    with peer(lambda path: (200, page, {})) as (url, _):
        result = sources.list_source_items(sources_file=registry(tmp_path, entry(url)))
    assert result["sources"][0]["status"] == "unavailable"


def test_detail_cannot_substitute_a_different_object(tmp_path):
    raw = {"item": item("wf_another"), "history": {"runs": [], "events": []},
           "result": {"available": False}, "artifacts": []}
    with peer(lambda path: (200, raw, {})) as (url, _):
        result = sources.get_source_detail("studio-windows", "workflow", "wf_one",
                                           sources_file=registry(tmp_path, entry(url)))
    assert result["item"] is None and result["source"]["status"] == "unavailable"


def test_multiple_computers_keep_same_ids_and_credentials_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("PEER_A_TOKEN", "ACCESS-A")
    monkeypatch.setenv("PEER_B_TOKEN", "ACCESS-B")
    response = lambda path: (200, {"items": [item("wf_same")], "total": 1}, {})
    with peer(response) as (first, first_calls), peer(response) as (second, second_calls):
        with peer(lambda path: (503, {}, {})) as (failed, _):
            result = sources.list_source_items(sources_file=registry(
                tmp_path, entry(first, "pc-a", token_env="PEER_A_TOKEN"),
                entry(second, "pc-b", token_env="PEER_B_TOKEN"), entry(failed, "offline")))
    assert {v["id"] for v in result["items"]} == {
        "pc-a:workflow:wf_same", "pc-b:workflow:wf_same"}
    assert first_calls[0]["authorization"] == "Bearer ACCESS-A"
    assert second_calls[0]["authorization"] == "Bearer ACCESS-B"
    assert [v["status"] for v in result["sources"]] == ["online", "online", "unavailable"]
    text = json.dumps(result)
    assert "ACCESS-A" not in text and "ACCESS-B" not in text


def test_short_modern_pages_are_followed_until_reported_total(tmp_path):
    def respond(path):
        offset = int(parse_qs(urlsplit(path).query)["offset"][0])
        return 200, {"items": [item(f"wf_{offset}")], "total": 3}, {}
    with peer(respond) as (url, calls):
        result = sources.list_source_items(sources_file=registry(tmp_path, entry(url)))
    assert len(calls) == 3 and len(result["items"]) == 3
    assert result["sources"][0]["status"] == "online"


def test_total_overshoot_is_unavailable_not_reported_complete(tmp_path):
    with peer(lambda path: (200, {"items": [item()], "total": 0}, {})) as (url, calls):
        result = sources.list_source_items(sources_file=registry(tmp_path, entry(url)))
    assert len(calls) == 1 and result["sources"][0]["status"] == "unavailable"


def test_duplicate_ids_across_pages_are_reported_partial(tmp_path):
    with peer(lambda path: (200, {"items": [item()], "total": 2}, {})) as (url, calls):
        result = sources.list_source_items(sources_file=registry(tmp_path, entry(url)))
    assert len(calls) == 2 and len(result["items"]) == 1
    assert result["sources"][0]["status"] == "partial"


def test_drip_body_cannot_extend_absolute_caller_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "HTTP_TIMEOUT", 0.08)
    class Drip(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"items": [item()], "total": 1}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for byte in body:
                try:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.025)  # Every byte arrives before socket timeout.
                except (BrokenPipeError, ConnectionResetError):
                    return
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        start = time.monotonic()
        result = sources.list_source_items(sources_file=registry(
            tmp_path, entry(f"http://127.0.0.1:{server.server_port}")))
        elapsed = time.monotonic() - start
        assert elapsed < 0.6
        assert result["sources"][0]["error"]["code"] == "timeout"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_timed_out_os_io_is_bounded_and_slot_released_only_when_finished(monkeypatch):
    # Simulate an OS/DNS/header operation urllib cannot promptly interrupt.
    # No network access or production source is used by this negative control.
    slots = threading.BoundedSemaphore(1)
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(sources, "_IO_SLOTS", slots)
    monkeypatch.setattr(sources, "HTTP_TIMEOUT", 0.03)
    def blocked(*args):
        entered.set()
        release.wait(2)
        return {"items": [], "total": 0}
    monkeypatch.setattr(sources._Client, "_read", blocked)
    client = sources._Client(sources._Source("fixture", "Fixture", "http://127.0.0.1:1"))
    try:
        with pytest.raises(sources._Unavailable) as first:
            client.get("/api/task-feed")
        assert first.value.code == "timeout" and entered.is_set()
        with pytest.raises(sources._Unavailable) as second:
            client.get("/api/task-feed")
        assert second.value.code == "read_capacity"
    finally:
        release.set()
    assert slots.acquire(timeout=1)
    slots.release()


def test_io_thread_start_failure_returns_capacity_slot(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(sources, "_IO_SLOTS", slots)
    def refused(self):
        raise RuntimeError("not a public stack or credential")
    monkeypatch.setattr(sources.threading.Thread, "start", refused)
    client = sources._Client(sources._Source("fixture", "Fixture", "http://127.0.0.1:1"))
    with pytest.raises(sources._Unavailable):
        client.get("/api/task-feed")
    assert slots.acquire(blocking=False)
    slots.release()
