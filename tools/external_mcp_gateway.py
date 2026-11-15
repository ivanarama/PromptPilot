"""Fixed-backend, single-credential legacy MCP gateway (POSIX deployment).

Run: python tools/external_mcp_gateway.py --config /private/path/gateway.json
No native session, credentials, request bodies or response bodies are logged.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
import queue
from pathlib import Path
import re
import secrets
import ssl
import socket
import stat
import threading
import time
from urllib.parse import urlsplit

MAX_BODY = 4 * 1024 * 1024
TOOLS = frozenset({"pilot_next_task", "pilot_status", "pilot_read_input", "pilot_submit_result"})
PROTOCOLS = {"2025-06-18", "2025-03-26"}
_IO_SLOT = threading.BoundedSemaphore(1)


class Refused(ValueError):
    pass


class Uncertain(Exception):
    """An upstream outcome cannot be proven. Do not retry implicitly."""


def require(ok, message="request refused"):
    if not ok:
        raise Refused(message)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def parse(raw):
    def constant(_value):
        raise ValueError("non-finite JSON")
    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON")
        return number
    return json.loads(raw, parse_constant=constant, parse_float=finite)


def fields(value, required=(), optional=()):
    require(type(value) is dict and set(required) <= value.keys()
            and value.keys() <= set(required) | set(optional))


def private_metadata(st, directory=False):
    # This is a POSIX deployable tool, not an unverified Windows ACL assertion.
    require(os.name == "posix", "private-file gateway requires POSIX ownership checks")
    require(st.st_uid == os.getuid() and not stat.S_IMODE(st.st_mode) & 0o077,
            "private ownership/permissions required")
    require(stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode), "regular private path required")


def checked(path, *, exists=True):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path and not path.is_symlink(), "exact nonsymlink path required")
    private_metadata(path.parent.lstat(), directory=True)
    if exists:
        private_metadata(path.lstat())
    return path


def read_private(path, limit):
    path = checked(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        private_metadata(os.fstat(stream.fileno()))
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, "private file exceeds bound")
    return raw


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def lock_stream(stream, acquire):
    import fcntl
    fcntl.flock(stream, (fcntl.LOCK_EX | fcntl.LOCK_NB) if acquire else fcntl.LOCK_UN)


class State:
    def __init__(self, path):
        self.path = checked(path, exists=Path(path).exists())
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.thread_lock = threading.Lock()
        self.previous = None

    @contextmanager
    def locked(self):
        require(self.thread_lock.acquire(blocking=False), "gateway busy")
        stream = None
        acquired = False
        try:
            try:
                fd = os.open(checked(self.lock_path, exists=False),
                             os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                try:
                    sync_dir(self.path.parent)
                except BaseException:
                    os.close(fd)
                    raise
            except FileExistsError:
                fd = os.open(checked(self.lock_path), os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            stream = os.fdopen(fd, "r+b")
            private_metadata(os.fstat(stream.fileno()))
            try:
                lock_stream(stream, True)
            except (BlockingIOError, OSError):
                raise Refused("gateway busy") from None
            acquired = True
            raw = read_private(self.path, MAX_BODY) if self.path.exists() else None
            self.previous = digest(raw) if raw is not None else None
            value = parse(raw) if raw is not None else None
            yield value
        finally:
            try:
                if acquired:
                    lock_stream(stream, False)
            finally:
                try:
                    if stream is not None:
                        stream.close()
                finally:
                    self.thread_lock.release()

    def save(self, value):
        raw = encode(value)
        require(len(raw) <= MAX_BODY, "private history bound reached; reconcile before archival")
        current = read_private(self.path, MAX_BODY) if self.path.exists() else None
        require((digest(current) if current is not None else None) == self.previous, "private state changed")
        target = self.path.with_name(self.path.name + "." + secrets.token_hex(12) + ".writing")
        fd = os.open(checked(target, exists=False), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            # Recheck after I/O; an operator must not replace live state behind the lock.
            current = read_private(self.path, MAX_BODY) if self.path.exists() else None
            require((digest(current) if current is not None else None) == self.previous, "private state changed")
            os.replace(target, self.path)
            sync_dir(self.path.parent)
            self.previous = digest(raw)
        finally:
            if target.exists():
                target.unlink()  # Only this invocation's exclusive private temporary file.


def load_config(path):
    value = parse(read_private(path, 16384))
    fields(value, ("listen_host", "listen_port", "client_id", "upstream_url", "frontend_token_file", "backend_token_file", "state_file"), ("io_timeout",))
    require(type(value["listen_port"]) is int and 1 <= value["listen_port"] <= 65535)
    require(isinstance(value["listen_host"], str) and (value["listen_host"] == "localhost" or bool(ipaddress.ip_address(value["listen_host"]))))
    require(isinstance(value["client_id"], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value["client_id"]))
    timeout = value.get("io_timeout", 30)
    require(type(timeout) in (int, float) and math.isfinite(timeout) and 1 <= timeout <= 120)
    url = value["upstream_url"]
    require(isinstance(url, str) and len(url) <= 2048 and not any(ord(ch) <= 32 or ord(ch) == 127 for ch in url))
    parsed = urlsplit(url)
    require(parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment and "\\" not in url and "%" not in parsed.path)
    require(parsed.path == f"/api/external-clients/{value['client_id']}/mcp", "exact native inbox endpoint required")
    require(parsed.port is None or 1 <= parsed.port <= 65535)
    if parsed.scheme == "http":
        require(parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback, "HTTP upstream must be loopback")
    for name in ("frontend_token_file", "backend_token_file", "state_file"):
        value[name] = str(checked(value[name], exists=name != "state_file" or Path(value[name]).exists()))
    require(value["state_file"] not in {str(Path(path)), value["frontend_token_file"], value["backend_token_file"]})
    require(value["frontend_token_file"] != value["backend_token_file"], "separate frontend/backend credential files required")
    value["io_timeout"] = timeout
    return value


def token_file(path):
    value = read_private(path, 4096).decode("utf-8").strip()
    require(16 <= len(value) <= 1024 and value.isascii() and not any(ch.isspace() for ch in value), "invalid credential file")
    return value


def _post_once_io(url, headers, body, timeout, deadline, cancelled, holder):
    parsed = urlsplit(url)
    conn = (http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=timeout, context=ssl.create_default_context())
            if parsed.scheme == "https" else http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout))
    holder.append(conn)
    def remaining():
        left = deadline - time.monotonic()
        if left <= 0 or cancelled.is_set():
            raise Uncertain()
        if conn.sock is not None:
            conn.sock.settimeout(left)
        return left
    try:
        conn.connect()
        remaining()
        conn.request("POST", parsed.path, body=body, headers=headers)
        remaining()
        response = conn.getresponse()
        if response.status != 200:
            raise Uncertain()
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdecimal() or len(length) > 8 or int(length) > MAX_BODY):
            raise Uncertain()
        content_type = (response.getheader("Content-Type") or "").split(";")[0].lower()
        if content_type != "application/json" and not content_type.endswith("+json"):
            raise Uncertain()
        raw = bytearray()
        while True:
            remaining()
            chunk = response.read1(min(65536, MAX_BODY + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAX_BODY:
                raise Uncertain()
        remaining()
        return response.status, dict(response.headers), bytes(raw)
    except (OSError, ValueError, http.client.HTTPException):
        raise Uncertain() from None
    finally:
        conn.close()


def post_once(url, headers, body, timeout):
    """One direct POST with a total caller deadline, even during DNS/header reads.

    A resolver may outlive the deadline in a daemon thread; the cancellation/
    deadline check after connect prevents it sending a later POST. Once sent,
    an uncertain Native operation is reconciled, never implicitly retried.
    """
    slot = _IO_SLOT
    if not slot.acquire(blocking=False):
        raise Uncertain()
    deadline = time.monotonic() + timeout
    cancelled, holder, output = threading.Event(), [], queue.Queue(maxsize=1)
    def perform():
        try:
            reply = _post_once_io(url, headers, body, timeout, deadline, cancelled, holder)
            output.put((True, reply))
        except Exception:
            output.put((False, None))
        finally:
            slot.release()
    try:
        threading.Thread(target=perform, name="native-mcp-upstream", daemon=True).start()
    except BaseException:
        slot.release()
        raise Uncertain() from None
    try:
        ok, value = output.get(timeout=max(0, deadline - time.monotonic()))
        if not ok or time.monotonic() > deadline:
            raise Uncertain()
        return value
    except queue.Empty:
        raise Uncertain() from None
    finally:
        cancelled.set()
        # Do not close a buffered HTTP response from this thread: close can wait
        # on its reader lock. Socket shutdown wakes the owning I/O thread safely.
        owned_socket = holder[0].sock if holder else None
        if owned_socket is not None:
            try:
                owned_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def error(rid, message, *, unknown=False):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32001 if unknown else -32602,
        "message": message, **({"data": {"state": "UNKNOWN", "reconcile": "pilot_status; do not retry submit or reset the session"}} if unknown else {})}}


class Gateway:
    def __init__(self, config, upstream=post_once):
        self.config = config
        self.frontend_token = token_file(config["frontend_token_file"])
        self.backend_token = token_file(config["backend_token_file"])
        require(self.frontend_token != self.backend_token, "separate credentials required")
        self.state = State(config["state_file"])
        self.upstream = upstream
        self.binding = {"upstream_url": config["upstream_url"], "client_id": config["client_id"],
                        "frontend_token_sha256": digest(self.frontend_token.encode()),
                        "backend_token_sha256": digest(self.backend_token.encode())}

    def authorized(self, authorization):
        return isinstance(authorization, str) and hmac.compare_digest(authorization.encode(), ("Bearer " + self.frontend_token).encode())

    def _read_state(self, value):
        if value is None:
            return {"schema": "native-mcp-gateway-v1", "binding": self.binding, "phase": "uninitialized",
                    "history": [], "inflight": None, "submit_fence": None}
        require(type(value) is dict and value.get("schema") == "native-mcp-gateway-v1" and value.get("binding") == self.binding, "state/config identity mismatch")
        require(value.get("phase") in {"initializing", "ready", "unknown_initialization"} and type(value.get("history")) is list)
        if value["phase"] == "ready":
            require(isinstance(value.get("session"), str) and re.fullmatch(r"[0-9a-f]{64}", value["session"]))
            require(type(value.get("initialize_result")) is dict and value["initialize_result"].get("protocolVersion") in PROTOCOLS)
        return value

    def _call(self, request, state, initialize=False):
        headers = {"Authorization": "Bearer " + self.backend_token, "Content-Type": "application/json", "Accept": "application/json"}
        if not initialize:
            headers["Mcp-Session-Id"] = state["session"]
            headers["MCP-Protocol-Version"] = state["initialize_result"]["protocolVersion"]
        status, response_headers, raw = self.upstream(self.config["upstream_url"], headers, encode(request), self.config["io_timeout"])
        require(status == 200 and len(raw) <= MAX_BODY, "unproven upstream response")
        # A backend session/credential is never a frontend response field/header.
        require(self.backend_token.encode() not in raw and (not state.get("session") or state["session"].encode() not in raw), "private response refused")
        reply = parse(raw)
        canonical = encode(reply)
        require(self.backend_token.encode() not in canonical and (not state.get("session") or state["session"].encode() not in canonical), "private response refused")
        require(type(reply) is dict and reply.get("jsonrpc") == "2.0" and type(reply.get("id")) is type(request["id"])
                and reply.get("id") == request["id"] and (("result" in reply) != ("error" in reply)), "unproven upstream response")
        return reply, {key.lower(): value for key, value in response_headers.items()}

    def _unknown(self, state):
        inflight = state.get("inflight")
        if inflight:
            state["history"].append({**inflight, "outcome": "UNKNOWN"})
            if inflight["tool"] == "pilot_submit_result":
                state["submit_fence"] = {**inflight, "state": "unknown"}
            state["inflight"] = None

    def rpc(self, raw, authorization):
        if not self.authorized(authorization):
            return 401, None
        rid = None
        attempted_upstream = False
        try:
            require(len(raw) <= MAX_BODY)
            request = parse(raw)
            fields(request, ("jsonrpc",), ("id", "method", "params", "result", "error"))
            require(request["jsonrpc"] == "2.0")
            if "id" not in request or "method" not in request:
                return 202, None  # Notifications never reach Native or initialize a session.
            candidate = request["id"]
            require(type(candidate) in (str, int) and (not isinstance(candidate, str) or len(candidate) <= 128))
            rid = candidate
            method, params = request["method"], request.get("params", {})
            require(isinstance(method, str) and type(params) is dict and ("_meta" not in params or type(params["_meta"]) is dict))
            if method == "ping":
                fields(params, optional=("_meta",))
                return 200, {"jsonrpc": "2.0", "id": rid, "result": {}}
            if method not in {"initialize", "tools/list", "tools/call"}:
                return 200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}
            tool, args = None, {}
            if method == "initialize":
                fields(params, ("protocolVersion", "capabilities", "clientInfo"), ("_meta",))
                require(isinstance(params["protocolVersion"], str) and type(params["capabilities"]) is dict and type(params["clientInfo"]) is dict)
            elif method == "tools/list":
                fields(params, optional=("_meta",))
            else:
                fields(params, ("name",), ("arguments", "_meta"))
                tool, args = params["name"], params.get("arguments", {})
                require(isinstance(tool, str) and tool in TOOLS and type(args) is dict)
                if tool == "pilot_next_task":
                    fields(args)
                elif tool == "pilot_status":
                    fields(args, optional=("job_id",))
                elif tool == "pilot_read_input":
                    fields(args, ("job_id", "claim_token", "path", "offset", "length"))
                else:
                    fields(args, ("job_id", "claim_token", "outcome", "summary", "artifacts"))
                if "job_id" in args:
                    require(isinstance(args["job_id"], str) and re.fullmatch(r"[0-9a-f]{32}", args["job_id"]))
            with self.state.locked() as loaded:
                state = self._read_state(loaded)
                if method == "initialize":
                    if state["phase"] == "ready":
                        return 200, {"jsonrpc": "2.0", "id": rid, "result": state["initialize_result"]}
                    if state["phase"] != "uninitialized":
                        return 200, error(rid, "UNKNOWN initialization; reconcile without creating a new session", unknown=True)
                    state.update(phase="initializing", initialize_request_sha256=digest(encode(params)))
                    self.state.save(state)  # Durable intent before the sole initialization POST.
                    try:
                        attempted_upstream = True
                        reply, headers = self._call(request, state, initialize=True)
                        session = headers.get("mcp-session-id")
                        require(isinstance(session, str) and re.fullmatch(r"[0-9a-f]{64}", session))
                        result = reply.get("result")
                        require(type(result) is dict and result.get("protocolVersion") in PROTOCOLS and session.encode() not in encode(result))
                        state.update(phase="ready", session=session, initialize_result=result)
                        state["history"].append({"event": "initialized", "at": time.time()})
                        self.state.save(state)
                        return 200, {"jsonrpc": "2.0", "id": rid, "result": result}
                    except Exception:
                        state["phase"] = "unknown_initialization"
                        self.state.save(state)
                        return 200, error(rid, "UNKNOWN initialization; reconcile without creating a new session", unknown=True)
                require(state["phase"] == "ready", "initialize or reconcile this gateway first")
                # A previous process can have died after SEND. Classify, never repeat its call.
                self._unknown(state)
                fence = state.get("submit_fence")
                if fence and fence.get("state") == "unknown" and tool in {"pilot_next_task", "pilot_submit_result"}:
                    self.state.save(state)
                    return 200, error(rid, "UNKNOWN submit; pilot_status must prove the same job receipt first", unknown=True)
                intent = {"nonce": secrets.token_hex(16), "tool": tool or method,
                          "job_id": args.get("job_id"), "request_sha256": digest(encode(params)), "at": time.time()}
                state["inflight"] = intent
                self.state.save(state)
                try:
                    attempted_upstream = True
                    reply, _ = self._call(request, state)
                    if method == "tools/list":
                        listed = reply.get("result", {}).get("tools")
                        require(type(listed) is list and len(listed) == 4 and {v.get("name") for v in listed if type(v) is dict} == TOOLS)
                    if tool == "pilot_status" and fence and fence.get("state") == "unknown":
                        output = reply.get("result", {})
                        data = output.get("structuredContent", {})
                        if (output.get("isError") is False and type(data) is dict and data.get("state") == "submitted"
                                and data.get("job_id") == fence["job_id"] and isinstance(data.get("receipt_id"), str)
                                and bool(data["receipt_id"]) and isinstance(data.get("result_digest"), str)
                                and re.fullmatch(r"[0-9a-f]{64}", data["result_digest"])):
                            state["submit_fence"] = {**fence, "state": "resolved", "receipt_id": data["receipt_id"], "result_digest": data["result_digest"]}
                    completed = copy.deepcopy(state)
                    completed["history"].append({**intent, "outcome": "ACK", "response_sha256": digest(encode(reply))})
                    completed["inflight"] = None
                    self.state.save(completed)
                    return 200, reply
                except Exception:
                    self._unknown(state)
                    self.state.save(state)
                    return 200, error(rid, "UNKNOWN upstream outcome; reconcile the same owned Native session", unknown=True)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OSError):
            return 200, error(rid, "Gateway refused this request; inspect private state without resetting or replaying it", unknown=attempted_upstream)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def respond(self, status, value=None):
        raw = b"" if value is None else encode(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def do_POST(self):
        try:
            if not self.server.gateway.authorized(self.headers.get("Authorization")):
                self.respond(401)
                return
            if self.path != "/mcp":
                self.respond(404)
                return
            length = self.headers.get("Content-Length", "")
            if self.headers.get("Transfer-Encoding") is not None or not length.isdecimal() or len(length) > 8:
                self.respond(400)
                return
            size = int(length)
            if size > MAX_BODY:
                self.respond(413)
                return
            deadline = time.monotonic() + 15
            chunks, received = [], 0
            while received < size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(min(65536, size - received))
                if not chunk:
                    self.respond(400)
                    return
                chunks.append(chunk)
                received += len(chunk)
            raw = b"".join(chunks)
            status, reply = self.server.gateway.rpc(raw, self.headers.get("Authorization"))
            self.respond(status, reply)
        except (OSError, ValueError, TypeError, RecursionError):
            self.close_connection = True

    def do_GET(self):
        self.respond(405 if self.server.gateway.authorized(self.headers.get("Authorization")) else 401)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, *_args):
        pass  # Do not dump private request context or exception values.


def main(argv=None):
    parser = argparse.ArgumentParser(description="Single-owner native MCP compatibility gateway")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        gateway = Gateway(config)
        # Startup itself never initializes/claims a Native run or touches the legacy broker store.
        with Server((config["listen_host"], config["listen_port"]), Handler) as server:
            server.gateway = gateway
            server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return
    except Exception:
        raise SystemExit("Gateway refused startup; inspect the private config/state. No automatic reset.") from None


if __name__ == "__main__":
    main()