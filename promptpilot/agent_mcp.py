"""Stateless Streamable HTTP MCP for owner-scoped agent task intake.

This endpoint implements the explicitly negotiated 2025 MCP revisions. It does
not advertise later protocol revisions, server-initiated requests, or execution
authority. Native workflow approval and execution remain separate owner actions.
"""
from __future__ import annotations

import ipaddress
import json
import math
import sqlite3
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from . import agent_control as control, config

router = APIRouter()
MCP_PATH = "/api/agents/mcp"
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26")
MAX_BODY = 1024 * 1024


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Empty(_Args):
    pass


class _Get(_Args):
    task_id: str = Field(min_length=1, max_length=100)


class _List(_Args):
    project_id: str | None = Field(default=None, min_length=1, max_length=64)
    limit: int = Field(default=25, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=10000)


class _ReadFile(_Args):
    project_id: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=1024)
    offset: int = Field(default=0, ge=0, le=128 * 1024)
    length: int = Field(default=16384, ge=1, le=16384)


_TOOLS = {
    "pilot_list_projects": ("List the owner-configured projects granted to this connection.", _Empty),
    "pilot_create_task": ("Propose an ordinary external-executor workflow for human plan approval. If plan is omitted, propose one integration stage. Reuse the same request_id to reconcile a lost response. This never approves or starts work.", control.TaskCreate),
    "pilot_get_task": ("Read safe status metadata for an agent-created workflow in a granted project.", _Get),
    "pilot_list_tasks": ("List agent-created workflows in granted projects, including tasks from other authorized clients.", _List),
    "pilot_read_project_file": ("Read a bounded owner-permitted committed Git text file. No arbitrary filesystem or URL access.", _ReadFile),
}


def _parse(raw):
    text = raw.decode("utf-8")
    # Bound nesting before json.loads, whose own recursion threshold is larger.
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > 64:
                raise ValueError("JSON nesting limit")
        elif char in "]}":
            depth -= 1

    def pairs(items):
        value = {}
        for key, child in items:
            if key in value:
                raise ValueError("Duplicate JSON field")
            value[key] = child
        return value

    def constant(_):
        raise ValueError("Non-finite JSON")

    def floating(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Non-finite JSON")
        return result

    value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=floating)
    nodes = 0

    def check(item):
        nonlocal nodes
        nodes += 1
        if nodes > 20000:
            raise ValueError("JSON node limit")
        if isinstance(item, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in item):
            raise ValueError("Invalid Unicode")
        if isinstance(item, dict):
            for key, child in item.items():
                check(key)
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
    check(value)
    return value


def _origin_key(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
            return None
        return parsed.scheme, parsed.hostname.lower().rstrip("."), parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def _origin_allowed(request):
    value = request.headers.get("origin")
    if value is None:
        return True  # Native clients authenticate with their scoped bearer.
    origin = _origin_key(value)
    own = _origin_key(request.url.scheme + "://" + request.headers.get("host", ""))
    if origin is None or own is None:
        return False
    if any(origin == _origin_key(item) for item in config.ALLOWED_ORIGINS):
        return True
    hostname = own[1]
    trusted = hostname in {"localhost", "localhost.localdomain"} or hostname in set(config.ALLOWED_HOSTS) - {"*"}
    try:
        ipaddress.ip_address(hostname)
        trusted = True  # IP literals cannot be DNS-rebound to another address.
    except ValueError:
        pass
    return trusted and origin == own


def _response(value, status=200, version=None):
    headers = {"Cache-Control": "no-store"}
    if version:
        headers["MCP-Protocol-Version"] = version
    return JSONResponse(value, status_code=status, headers=headers)


def _error(identity, code, message, status=200):
    return _response({"jsonrpc": "2.0", "id": identity, "error": {"code": code, "message": message}}, status)


def _call(connection_id, name, arguments):
    if name == "pilot_list_projects":
        return {"projects": control.agent_projects(connection_id)}
    if name == "pilot_create_task":
        return control.create_task(connection_id, arguments)
    if name == "pilot_get_task":
        return control.get_task(connection_id, arguments.task_id)
    if name == "pilot_list_tasks":
        return {"tasks": control.list_tasks(connection_id, **arguments.model_dump())}
    if name == "pilot_read_project_file":
        from .agent_context import ContextError, read_project_file
        try:
            return read_project_file(connection_id, **arguments.model_dump())
        except ContextError as exc:
            raise control.AgentControlError(str(exc)) from exc
    raise control.AgentControlError("Unknown tool")


@router.get(MCP_PATH, include_in_schema=False)
def no_event_stream(request: Request):
    if not _origin_allowed(request):
        return _error(None, -32000, "Origin is not allowed", 403)
    return Response(status_code=405, headers={"Allow": "POST", "Cache-Control": "no-store"})


@router.post(MCP_PATH, include_in_schema=False)
async def mcp(request: Request):
    if not _origin_allowed(request):
        return _error(None, -32000, "Origin is not allowed", 403)
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return _response({"error": "Agent connection bearer token required"}, 401)
    try:
        connection_id = await run_in_threadpool(control.authenticate, authorization[7:])
    except control.AgentForbidden:
        return _response({"error": "Agent connection bearer token required"}, 401)
    except sqlite3.Error:
        return _error(None, -32000, "Connection registry is temporarily unavailable", 503)
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        return _error(None, -32600, "application/json required", 415)
    accept = request.headers.get("accept", "*/*")
    if not any(part.split(";", 1)[0].strip() in {"application/json", "*/*"} for part in accept.split(",")):
        return _error(None, -32600, "A JSON response must be accepted", 406)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY:
            return _error(None, -32600, "Request body exceeds the limit", 413)
    try:
        message = _parse(raw)
    except (ValueError, UnicodeError, RecursionError):
        return _error(None, -32700, "Invalid JSON", 400)
    if (type(message) is not dict or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
            or set(message) - {"jsonrpc", "id", "method", "params"}
            or type(message.get("params", {})) is not dict):
        return _error(None, -32600, "Invalid JSON-RPC request", 400)
    method, params = message["method"], message.get("params", {})
    if "_meta" in params and type(params["_meta"]) is not dict:
        return _error(message.get("id"), -32602, "Invalid request metadata")
    version = request.headers.get("mcp-protocol-version", "2025-03-26")
    if method != "initialize" and version not in PROTOCOLS:
        return _error(None, -32600, "Unsupported MCP protocol version", 400)
    if "id" not in message:
        if not method.startswith("notifications/"):
            return _error(None, -32600, "Requests require an id", 400)
        return Response(status_code=202, headers={"Cache-Control": "no-store"})
    identity = message["id"]
    if type(identity) not in {str, int} or isinstance(identity, str) and len(identity) > 200:
        return _error(None, -32600, "Invalid request id", 400)
    result = None
    if method == "initialize":
        if (not isinstance(params.get("protocolVersion"), str)
                or type(params.get("capabilities")) is not dict
                or type(params.get("clientInfo")) is not dict
                or any(not isinstance(params["clientInfo"].get(key), str)
                       or not 1 <= len(params["clientInfo"][key]) <= 200 for key in ("name", "version"))):
            return _error(identity, -32602, "Invalid initialization parameters")
        version = params["protocolVersion"] if params["protocolVersion"] in PROTOCOLS else PROTOCOLS[0]
        result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "PromptPilot", "version": "1.0"}, "instructions": "List owner-granted projects first; read their permitted committed context if needed. Propose a task with an objective and optional title/objective plan. Creation always awaits human plan approval and never starts work. Reuse the same request_id for the same input to reconcile a lost response. Follow dashboard_path to the owner task card; poll task status without requesting approval or execution authority."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        if set(params) - {"_meta"}:
            return _error(identity, -32602, "Invalid tool list parameters")
        result = {"tools": [{"name": name, "description": item[0], "inputSchema": item[1].model_json_schema(), "annotations": {"readOnlyHint": name != "pilot_create_task", "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}} for name, item in _TOOLS.items()]}
    elif method == "tools/call":
        if (set(params) - {"name", "arguments", "_meta"} or not isinstance(params.get("name"), str)
                or params["name"] not in _TOOLS
                or type(params.get("arguments", {})) is not dict):
            return _error(identity, -32602, "Unknown tool or invalid parameters")
        name = params["name"]
        try:
            arguments = _TOOLS[name][1].model_validate(params.get("arguments", {}))
        except ValidationError:
            return _error(identity, -32602, "Invalid tool arguments")
        try:
            value = await run_in_threadpool(_call, connection_id, name, arguments)
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, allow_nan=False)}], "isError": False}
            if version != "2025-03-26":
                result["structuredContent"] = value
        except control.AgentControlError as exc:
            value = {"error": {"code": exc.code, "message": str(exc)}}
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": True}
        except (sqlite3.Error, ValueError, OSError):
            result = {"content": [{"type": "text", "text": "Operation unavailable; reconcile task creation with the same request_id"}], "isError": True}
    else:
        return _error(identity, -32601, "Method not found")
    return _response({"jsonrpc": "2.0", "id": identity, "result": result}, version=version)
