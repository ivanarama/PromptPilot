"""Connect to an existing Codex daemon without owning its sessions.

The daemon control socket speaks WebSocket. ``app-server proxy`` relays raw
bytes, so the Sans-I/O WebSocket client also works over Windows pipes and SSH.
Only the proxy subprocess belongs to PromptPilot; closing it leaves Codex alive.
"""
import atexit
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

from websockets.client import ClientProtocol
from websockets.frames import Frame, Opcode
from websockets.http11 import Response
from websockets.uri import parse_uri

from .process_tree import OwnedProcess
from .remote import ssh_command


class Unavailable(RuntimeError):
    pass


class Unconfirmed(Unavailable):
    """A mutation may have reached Codex. Never resend through another backend."""


class RPCError(RuntimeError):
    def __init__(self, error):
        self.code = error.get("code")
        super().__init__(error.get("message", "Codex request rejected"))


def proxy_command(host=None):
    binary = os.environ.get("PP_CODEX_APP_SERVER_BIN") or shutil.which("codex") or "codex"
    if os.name == "nt" and Path(binary).suffix.lower() in {".cmd", ".ps1"}:
        candidates = [
            Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
            / "packages/app-server-daemon/current/bin/codex.exe",
            Path(binary).parent / "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64"
            / "vendor/x86_64-pc-windows-msvc/bin/codex.exe",
        ]
        binary = next((str(p) for p in candidates if p.is_file()), binary)
    if host:
        return ssh_command(host, ["codex", "app-server", "proxy"])
    socket = os.environ.get("PP_CODEX_APP_SERVER_SOCKET")
    if not socket:
        socket = str(Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
                     / "app-server-control/app-server-control.sock")
    if not Path(socket).exists():
        raise Unavailable("Codex daemon socket is not available")
    return [binary, "app-server", "proxy", "--sock", socket]


class Client:
    def __init__(self, argv, timeout=10):
        self.timeout = timeout
        self.lock = threading.RLock()
        self.pending = {}
        self.subscriptions = set()
        self.turns = {}
        self.responses = {}
        self.counter = 0
        self.failure = None
        self.ready = threading.Event()
        self.fragments = bytearray()
        self.ws = ClientProtocol(parse_uri("ws://localhost/"), max_size=16 * 1024 * 1024)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.child = OwnedProcess.start(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, creationflags=flags)
        self.process = self.child.process
        self.reader = threading.Thread(target=self._read, daemon=True, name="codex-app-server")
        self.reader.start()
        try:
            with self.lock:
                self.ws.send_request(self.ws.connect())
                self._flush()
            if not self.ready.wait(timeout) or self.failure:
                raise Unavailable("Codex WebSocket handshake failed")
            self.request("initialize", {
                "clientInfo": {"name": "promptpilot", "version": "0.6.0"},
                "capabilities": {"experimentalApi": True},
            })
            self.notify("initialized", {})
        except Exception:
            self.close()
            raise

    def _flush(self):
        for block in self.ws.data_to_send():
            if block:
                self.process.stdin.write(block)
                self.process.stdin.flush()

    def _send(self, message):
        with self.lock:
            if self.failure:
                raise Unavailable("Codex connection closed")
            self.ws.send_text(json.dumps(message, ensure_ascii=False).encode("utf-8"))
            self._flush()

    def _message(self, message):
        if "method" in message:
            params = message.get("params") or {}
            if message["method"] in {"turn/started", "turn/completed"}:
                turn = params.get("turn") or {}
                key = (params.get("threadId"), turn.get("id"))
                previous = self.turns.get(key, {})
                items = [v for v in (turn.get("items") or previous.get("items") or [])
                         if v.get("type") in {"userMessage", "agentMessage"}]
                self.turns[key] = {**previous, **turn, "items": items}
                completed = [k for k, v in self.turns.items()
                             if k[0] == key[0] and v.get("status") != "inProgress"]
                for old in completed[:-20]:
                    self.turns.pop(old, None)
            elif message["method"] in {"item/started", "item/completed"}:
                key = (params.get("threadId"), params.get("turnId"))
                turn = self.turns.setdefault(key, {"id": key[1], "status": "inProgress", "items": []})
                item = params.get("item") or {}
                if item.get("type") in {"userMessage", "agentMessage"}:
                    turn["items"] = [v for v in turn["items"] if v.get("id") != item.get("id")] + [item]
            if "id" in message:
                # Replays may get a new delivery timestamp; it isn't question identity.
                self.pending[json.dumps(message["id"])] = {
                    k: v for k, v in message.items() if k != "emittedAtMs"}
            elif message["method"] == "serverRequest/resolved":
                self.pending.pop(json.dumps(params.get("requestId")), None)
        elif "id" in message:
            target = self.responses.get(message["id"])
            if target:
                target.put(message)

    def _read(self):
        try:
            while True:
                block = self.process.stdout.read1(65536)
                if not block:
                    raise Unavailable("Codex proxy disconnected")
                with self.lock:
                    self.ws.receive_data(block)
                    for event in self.ws.events_received():
                        if isinstance(event, Response):
                            if event.status_code != 101:
                                raise Unavailable("Codex WebSocket rejected the connection")
                            self.ready.set()
                        elif isinstance(event, Frame):
                            if event.opcode in {Opcode.TEXT, Opcode.CONT}:
                                self.fragments.extend(event.data)
                                if len(self.fragments) > 16 * 1024 * 1024:
                                    raise Unavailable("Codex message exceeds the supported size")
                                if event.fin:
                                    message = json.loads(self.fragments)
                                    self.fragments.clear()
                                    self._message(message)
                            elif event.opcode is Opcode.CLOSE:
                                raise Unavailable("Codex closed the connection")
                    self._flush()
        except Exception as exc:
            with self.lock:
                self.failure = exc
                self.ready.set()
                for target in self.responses.values():
                    target.put(None)

    def request(self, method, params, *, mutation=False):
        with self.lock:
            self.counter += 1
            request_id = self.counter
            target = queue.Queue()
            self.responses[request_id] = target
        sent = False
        try:
            # A failed pipe write can still have sent a prefix or the full frame.
            sent = mutation
            self._send({"id": request_id, "method": method, "params": params})
            result = target.get(timeout=self.timeout)
            if result is None:
                raise Unavailable("Codex disconnected while awaiting a response")
            if "error" in result:
                raise RPCError(result["error"])
            value = result.get("result")
            with self.lock:
                if method in {"thread/start", "thread/resume", "thread/fork"}:
                    thread_id = (value or {}).get("thread", {}).get("id")
                    if thread_id:
                        self.subscriptions.add(thread_id)
                elif method in {"thread/unsubscribe", "thread/archive"}:
                    self.subscriptions.discard(params.get("threadId"))
            return value
        except (OSError, queue.Empty, Unavailable) as exc:
            if sent:
                raise Unconfirmed("Codex may have received the request; do not send it again") from exc
            raise Unavailable("Codex did not respond") from exc
        finally:
            with self.lock:
                self.responses.pop(request_id, None)

    def notify(self, method, params):
        self._send({"method": method, "params": params})

    def subscribe(self, thread_id):
        # No configuration overrides: preserve the live TUI's model and permissions.
        self.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})

    def questions(self, thread_id):
        with self.lock:
            return [dict(v) for v in self.pending.values()
                    if v.get("method") == "item/tool/requestUserInput"
                    and (v.get("params") or {}).get("threadId") == thread_id]

    def live_turns(self, thread_id):
        with self.lock:
            return json.loads(json.dumps([v for (thread, _), v in self.turns.items()
                                          if thread == thread_id]))

    def answer(self, request_id, answers, fingerprint):
        with self.lock:
            current = self.pending.get(json.dumps(request_id))
            if not current or current != fingerprint:
                raise Unavailable("This Codex question has changed or was already answered")
            try:
                self._send({"id": request_id, "result": {"answers": answers}})
            except (OSError, Unavailable) as exc:
                raise Unconfirmed("The answer may have reached Codex") from exc
            self.pending.pop(json.dumps(request_id), None)

    def close(self):
        if getattr(self, "child", None):
            self.child.terminate()
            self.child.close()
            self.child = None
            self.process.wait(timeout=5)
            if threading.current_thread() is not self.reader:
                self.reader.join(timeout=2)
            self.process.stdin.close()
            self.process.stdout.close()


_clients = {}
_failed = {}
_lock = threading.Lock()


def get_client(host=None):
    if os.environ.get("PP_CODEX_APP_SERVER", "auto").lower() in {"0", "off", "false"}:
        return None
    key = (os.getpid(), str(host or ""), os.environ.get("CODEX_HOME"),
           os.environ.get("PP_CODEX_APP_SERVER_SOCKET"))
    with _lock:
        previous = _clients.get(key)
        if previous and not previous.failure:
            return previous
        if time.monotonic() - _failed.get(key, -60) < 60:
            return None
        if previous:
            previous.close()
        try:
            client = Client(proxy_command(host))
            _clients[key] = client
            return client
        except (OSError, RuntimeError, ValueError):
            _failed[key] = time.monotonic()
            return None


def close_clients():
    for client in list(_clients.values()):
        client.close()
    _clients.clear()


atexit.register(close_clients)
