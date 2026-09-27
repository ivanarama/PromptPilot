"""Local browser UI for the parallel orchestrator add-on.

The UI is deliberately a separate localhost service.  It does not mount a
route into PromptPilot and it does not write the PromptPilot database.  The
browser edits a plan visually; this service validates it and runs the existing
API-only scheduler in a background thread.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .orchestrator import (
    CompositeAdjudicator,
    ParallelOrchestrator,
    Plan,
    PlanError,
    PromptPilotClient,
    RunState,
    StateStore,
    make_jev_from_env,
)


DEFAULT_CORE_URL = "http://127.0.0.1:8420"
DEFAULT_UI_HOST = "127.0.0.1"
DEFAULT_UI_PORT = 8431


def _default_state_file() -> Path:
    return Path.home() / ".promptpilot" / "parallel-orchestrator" / "ui-run.json"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return value


def _provider_names(base_url: str) -> tuple[list[str], str | None]:
    try:
        raw = PromptPilotClient(base_url).request("GET", "/api/providers")
    except Exception as exc:
        return [], str(exc)
    values: list[str] = []
    if isinstance(raw, dict):
        candidate = raw.get("providers", raw)
        if isinstance(candidate, dict):
            values = [str(key) for key in candidate]
        elif isinstance(candidate, list):
            raw = candidate
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict):
                name = item.get("key") or item.get("name") or item.get("id") or item.get("provider")
                if name:
                    values.append(str(name))
    return sorted(set(values), key=str.casefold), None


class UiRunner:
    """Owns exactly one optional scheduler run for the browser session."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._orchestrator: ParallelOrchestrator | None = None
        self._plan: Plan | None = None
        self._state_file = _default_state_file()
        self._base_url = DEFAULT_CORE_URL
        self._poll_seconds = 3.0
        self._last_error: str | None = None

    def config(self) -> dict[str, Any]:
        providers, provider_error = _provider_names(self._base_url)
        return {
            "base_url": self._base_url,
            "default_state_file": str(self._state_file),
            "default_working_dir": os.environ.get("BOOKAPP_DIR", ""),
            "providers": providers,
            "core_reachable": provider_error is None,
            "core_error": provider_error,
            "jev_enabled": bool(os.environ.get("TYPESAFE_API_KEY")),
        }

    def validate(self, raw_plan: Mapping[str, Any]) -> dict[str, Any]:
        plan = Plan.from_dict(raw_plan)
        return {
            "plan": plan.to_dict(),
            "waves": plan.topological_waves(),
            "node_count": len(plan.nodes),
            "max_parallel": plan.max_parallel,
        }

    def start(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise PlanError("этот визуальный запуск уже выполняется")
            raw_plan = payload.get("plan")
            if not isinstance(raw_plan, dict):
                raise PlanError("план не передан")
            plan = Plan.from_dict(raw_plan)
            base_url = str(payload.get("base_url") or DEFAULT_CORE_URL).rstrip("/")
            state_file = Path(str(payload.get("state_file") or _default_state_file())).expanduser()
            try:
                poll_seconds = float(payload.get("poll_seconds", 3))
            except (TypeError, ValueError) as exc:
                raise PlanError("интервал опроса должен быть числом") from exc
            poll_seconds = max(0.5, min(poll_seconds, 30.0))
            self._base_url = base_url
            self._state_file = state_file
            self._poll_seconds = poll_seconds
            self._last_error = None
            self._plan = plan
            jev = make_jev_from_env()
            self._orchestrator = ParallelOrchestrator(
                plan,
                PromptPilotClient(base_url),
                store=StateStore(state_file),
                adjudicator=CompositeAdjudicator(jev),
            )
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="promptpilot-parallel-ui", daemon=True)
            self._thread.start()
            return self.status()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                with self._lock:
                    orchestrator = self._orchestrator
                    if orchestrator is None:
                        return
                    state = orchestrator.tick()
                if orchestrator.is_terminal():
                    return
            except Exception as exc:  # keep the UI alive and expose the error
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                return
            self._stop.wait(self._poll_seconds)

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop.set()
            return self.status()

    def resolve(self, node_id: str, action: str) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise PlanError("сначала остановите визуальный запуск")
            if not self._state_file.exists():
                raise PlanError("файл состояния ещё не создан")
            try:
                state = RunState.from_dict(json.loads(self._state_file.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise PlanError(f"не удалось прочитать состояние: {exc}") from exc
            runtime = state.nodes.get(node_id)
            if runtime is None:
                raise PlanError(f"неизвестный узел: {node_id}")
            if runtime.status != "human_required":
                raise PlanError(f"узел {node_id} имеет состояние {runtime.status}")
            runtime.task_id = None
            if action == "retry":
                runtime.status = "pending"
                runtime.phase = "review" if runtime.last_decision.get("action") == "REPAIR" else "task"
                runtime.reason = "повтор разрешён оператором через UI"
            elif action == "accept":
                runtime.status = "completed"
                runtime.phase = "done"
                runtime.reason = "результат принят оператором через UI"
            elif action == "abort":
                runtime.status = "failed"
                runtime.phase = "done"
                runtime.reason = "запуск остановлен оператором через UI"
            else:
                raise PlanError("действие должно быть retry, accept или abort")
            runtime.touch()
            state.event("operator_resolution", node=node_id, action=action)
            statuses = [item.status for item in state.nodes.values()]
            if statuses and all(item == "completed" for item in statuses):
                state.status = "completed"
            elif any(item == "human_required" for item in statuses):
                state.status = "human_required"
            elif any(item in {"failed", "blocked", "cancelled"} for item in statuses):
                state.status = "failed"
            else:
                state.status = "running"
            StateStore(self._state_file).save(state)
            return self.status(state_override=state)

    def status(self, state_override: RunState | None = None) -> dict[str, Any]:
        with self._lock:
            state = state_override
            if state is None and self._orchestrator is not None:
                state = self._orchestrator.state
            alive = bool(self._thread and self._thread.is_alive())
            if state is None:
                runner_status = "idle"
            elif alive:
                runner_status = "running"
            elif self._last_error:
                runner_status = "error"
            else:
                runner_status = "finished"
            return {
                "runner_status": runner_status,
                "last_error": self._last_error,
                "base_url": self._base_url,
                "jev_enabled": bool(os.environ.get("TYPESAFE_API_KEY")),
                "state_file": str(self._state_file),
                "plan": self._plan.to_dict() if self._plan else None,
                "state": state.to_dict() if state else None,
            }


class UiHandler(BaseHTTPRequestHandler):
    server_version = "PromptPilotParallelUI/1.0"

    @property
    def ui_server(self) -> "ParallelUiServer":
        return self.server  # type: ignore[return-value]

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False, default=_jsonable).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"ok": False, "error": message})

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise PlanError("некорректный размер запроса") from exc
        if length > 8 * 1024 * 1024:
            raise PlanError("запрос слишком большой")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlanError("некорректный JSON") from exc
        if not isinstance(value, dict):
            raise PlanError("JSON должен быть объектом")
        return value

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            try:
                body = self.ui_server.index_path.read_bytes()
            except OSError as exc:
                self._error(500, str(exc))
                return
            self._send(200, body, "text/html; charset=utf-8")
            return
        if path == "/api/ui/config":
            self._json(200, self.ui_server.runner.config())
            return
        if path == "/api/ui/status":
            self._json(200, self.ui_server.runner.status())
            return
        self._error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/ui/validate":
                raw_plan = payload.get("plan")
                if not isinstance(raw_plan, dict):
                    raise PlanError("план не передан")
                self._json(200, {"ok": True, **self.ui_server.runner.validate(raw_plan)})
                return
            if path == "/api/ui/run":
                self._json(202, {"ok": True, **self.ui_server.runner.start(payload)})
                return
            if path == "/api/ui/stop":
                self._json(200, {"ok": True, **self.ui_server.runner.stop()})
                return
            if path == "/api/ui/resolve":
                node = str(payload.get("node") or "")
                action = str(payload.get("action") or "")
                self._json(200, {"ok": True, **self.ui_server.runner.resolve(node, action)})
                return
            self._error(404, "not found")
        except PlanError as exc:
            self._error(400, str(exc))
        except Exception as exc:
            self._error(500, f"{type(exc).__name__}: {exc}")

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[parallel-ui] {self.address_string()} - {format % args}")


class ParallelUiServer(ThreadingHTTPServer):
    def __init__(self, host: str, port: int, index_path: Path) -> None:
        super().__init__((host, port), UiHandler)
        self.runner = UiRunner()
        self.index_path = index_path


_SHARED_RUNNER: UiRunner | None = None


def get_shared_runner() -> UiRunner:
    """Return the singleton used when the add-on is mounted into PromptPilot."""
    global _SHARED_RUNNER
    if _SHARED_RUNNER is None:
        _SHARED_RUNNER = UiRunner()
    return _SHARED_RUNNER


def create_fastapi_router():
    """Create optional same-port routes for PromptPilot's existing FastAPI app.

    The import is kept here, rather than at module import time, so the
    add-on's standalone UI remains usable without importing FastAPI itself.
    """
    from fastapi import APIRouter, HTTPException

    router = APIRouter(prefix="/api/parallel", tags=["parallel-addon"])
    runner = get_shared_runner()

    @router.get("/config")
    def config() -> dict[str, Any]:
        result = runner.config()
        # Calling the same PromptPilot process over HTTP from this route would
        # deadlock a single-worker uvicorn instance. Read provider names in
        # process when the integration is mounted into PromptPilot.
        try:
            from promptpilot.config import load_providers

            result["providers"] = sorted(load_providers(), key=str.casefold)
            result["core_reachable"] = True
            result["core_error"] = None
        except Exception as exc:
            result["core_reachable"] = False
            result["core_error"] = str(exc)
        return result

    @router.get("/status")
    def status() -> dict[str, Any]:
        return runner.status()

    @router.post("/validate")
    def validate(payload: dict[str, Any]) -> dict[str, Any]:
        raw_plan = payload.get("plan")
        if not isinstance(raw_plan, dict):
            raise HTTPException(400, "план не передан")
        try:
            return {"ok": True, **runner.validate(raw_plan)}
        except PlanError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/run", status_code=202)
    def run(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"ok": True, **runner.start(payload)}
        except PlanError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/stop")
    def stop() -> dict[str, Any]:
        return {"ok": True, **runner.stop()}

    @router.post("/resolve")
    def resolve(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return {
                "ok": True,
                **runner.resolve(str(payload.get("node") or ""), str(payload.get("action") or "")),
            }
        except PlanError as exc:
            raise HTTPException(400, str(exc)) from exc

    return router


def addon_index_path() -> Path:
    return Path(__file__).with_name("ui") / "index.html"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PromptPilot Parallel visual add-on")
    parser.add_argument("--host", default=DEFAULT_UI_HOST)
    parser.add_argument("--port", type=int, default=int(os.environ.get("PP_PARALLEL_UI_PORT", DEFAULT_UI_PORT)))
    parser.add_argument("--open", action="store_true", help="open the UI in the default browser")
    args = parser.parse_args(argv)
    index_path = Path(__file__).with_name("ui") / "index.html"
    server = ParallelUiServer(args.host, args.port, index_path)
    url = f"http://{args.host}:{args.port}/"
    print(f"PromptPilot Parallel UI: {url}")
    print("Закройте это окно, чтобы остановить только визуальный контроллер.")
    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
