"""Read-only operator progress, without changing workflow authority or history."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from . import db
from .native_external_gate import relative_path, VerificationError


class ProgressUnavailable(RuntimeError):
    pass


class ProgressNotFound(LookupError):
    pass


def _object(value):
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else value
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text(value, limit=600):
    return value[:limit] if isinstance(value, str) else ""


def external_diagnostic(output):
    """Parse only the public fixed-format artifact diagnostic, never raw stderr."""
    for line in reversed(_text(output, 16000).splitlines()):
        value = _object(line)
        if value.get("schema") != "external-result-error-v1":
            continue
        if value.get("code") == "artifact_paths_mismatch":
            try:
                paths = {}
                for key in ("expected_paths", "received_paths"):
                    names = value.get(key)
                    if not isinstance(names, list) or len(names) > 8:
                        return None
                    paths[key] = [relative_path(name) for name in names]
            except (VerificationError, TypeError):
                return None
            return {"kind": "artifact_paths_mismatch",
                    "summary": "Имена файлов результата не совпадают с правилами проекта.",
                    "action": "Передать файлы с ожидаемыми именами в поле path MCP. Локальная папка может отличаться.",
                    **paths}
        return None
    return None


def _round_reason(event):
    payload = _object(event["payload_json"])
    if event["event_type"] == "gate.failed":
        evidence = payload.get("evidence")
        if isinstance(evidence, list):
            for item in reversed(evidence[-20:]):
                diagnostic = external_diagnostic(item)
                if diagnostic:
                    return {**diagnostic, "basis": "recorded"}
        return {"kind": "gate_failed", "summary": "Техническая проверка результата не пройдена.",
                "action": "Проверить замечания и пересдать результат.", "basis": "recorded"}
    return {"kind": "review_revision", "summary": _text(payload.get("summary")) or "Проверяющий вернул результат на доработку.",
            "action": "Исправить замечания проверяющего и пересдать результат.", "basis": "recorded"}


def _artifact_comparison(conn, workflow, round_row, stage):
    """Legacy failures: report a current-policy comparison, never rewrite history.

    The current approved stage and project must agree. Only a proven, complete
    historical receipt with unchanged artifact hashes can supply received names.
    This is a display-only diagnostic; no old gate is replayed or marked passed.
    """
    configuration = _object(workflow["config_json"])
    expected = _object(configuration.get("external_result_policy")).get("artifacts")
    declared = _object(stage["spec_json"]).get("deliverables") if stage else None
    if (configuration.get("workorder_guard") is not None or not isinstance(expected, list)
            or not 0 < len(expected) <= 64 or not isinstance(declared, list)):
        return None
    try:
        expected = [relative_path(name) for name in expected]
        if sorted(declared) != sorted(expected):
            return None
        row = conn.execute("""SELECT c.job_id,c.client_id,c.expected_output_sha,
            c.input_sha256 AS claim_input,r.input_sha256,r.output_sha256,r.output_json
            FROM external_inbox_claims_v1 c JOIN workflow_runs r ON r.id=c.run_id
            AND r.workflow_id=c.workflow_id WHERE c.workflow_id=? AND r.round_id=?
            AND r.role='executor' AND r.status='completed' AND c.state='submitted'
            AND c.outcome='completed' ORDER BY c.created_at DESC LIMIT 1""",
            (workflow["id"], round_row["id"])).fetchone()
        if row is None or row["claim_input"] != row["input_sha256"]:
            return None
        output = _object(row["output_json"])
        canonical = json.dumps(output, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode()
        if not (hashlib.sha256(canonical).hexdigest() == row["output_sha256"] == row["expected_output_sha"]
                and _object(configuration.get("external_executor")).get("client_id") == row["client_id"]
                and _object(output.get("external")).get("performer") == "external-client:" + row["client_id"]):
            return None
        artifacts = conn.execute("SELECT path,sha256,content FROM external_inbox_artifacts_v1 WHERE job_id=?",
                                 (row["job_id"],)).fetchall()
        if not 0 < len(artifacts) <= 64 or sum(len(a["content"]) for a in artifacts) > 2 * 1024 * 1024:
            return None
        received = []
        for artifact in artifacts:
            if hashlib.sha256(bytes(artifact["content"])).hexdigest() != artifact["sha256"]:
                return None
            received.append(relative_path(artifact["path"]))
        if sorted(received) == sorted(expected):
            return None
        return {"kind": "artifact_paths_mismatch",
                "summary": "Имена файлов сдачи не совпадают с текущими правилами проекта.",
                "expected_paths": sorted(expected), "received_paths": sorted(received),
                "basis": "current_artifact_policy",
                "action": "Передать файлы с ожидаемыми именами в поле path MCP. Локальная папка может отличаться."}
    except (sqlite3.Error, VerificationError, ValueError, TypeError):
        return None


def _external_state(conn, workflow, client_id, tables):
    if "external_inbox_claims_v1" not in tables:
        return "available"
    row = conn.execute("""SELECT c.state,c.client_id,c.expected_version,c.input_sha256,
        r.input_sha256 AS run_input FROM workflow_runs r JOIN workflow_rounds q ON q.id=r.round_id
        LEFT JOIN external_inbox_claims_v1 c ON c.run_id=r.id AND c.workflow_id=r.workflow_id
        WHERE r.workflow_id=? AND q.round_no=? AND r.role='executor'
        ORDER BY r.attempt_no DESC LIMIT 1""", (workflow["id"], workflow["current_round"])).fetchone()
    if row is None or row["state"] is None:
        return "available"
    if (row["client_id"] != client_id or row["expected_version"] != workflow["state_version"]
            or row["input_sha256"] != row["run_input"]):
        return "unknown"
    return row["state"] if row["state"] in {"claimed", "submitting", "submitted"} else "unknown"


def _current(workflow, stages, config, external):
    status = workflow["status"]
    stage = next((s for s in stages if s["id"] == workflow["current_stage_id"]), None)
    roles = _object(config.get("roles"))
    provider = lambda role: _text(_object(roles.get(role)).get("provider"), 100) or "PromptPilot"
    client = _object(config.get("external_executor"))
    owner = _text(client.get("label") or client.get("client_id"), 120) or "Внешний исполнитель"
    owner = "ZCode" if owner.lower() == "zcode" else owner
    executor = provider("executor")
    reviewer = provider("reviewer")
    mapping = {
        "draft": ("План ещё не подготовлен", "Вы", "Подготовить план и отправить на утверждение.", "План"),
        "planning": ("Готовится план", provider("planner"), "Дождаться плана.", "План"),
        "awaiting_plan_approval": ("План ждёт вашего утверждения", "Вы", "Проверить план и нажать «Сохранить и утвердить».", "План"),
        "queued": ("Этап ожидает запуска", "PromptPilot", "Дождаться допуска к исполнению.", "Исполнение"),
        "executing": ("Исполнение этапа", executor, "Дождаться результата исполнителя.", "Исполнение"),
        "gating": ("Полученный результат проходит техническую проверку", "PromptPilot", "Дождаться проверки файлов; затем начнётся независимое ревью.", "Проверка файлов"),
        "reviewing": ("Ожидается заключение проверяющего", reviewer, "Дождаться заключения проверяющего.", "Ревью"),
        "revision_required": ("Результат возвращён на доработку", "PromptPilot", "Открыть следующий раунд исправлений.", "Доработка"),
        "awaiting_human": ("Нужно ваше решение", "Вы", "Открыть причину остановки и выбрать дальнейшее действие.", "Решение"),
        "failed": ("Выполнение остановлено с ошибкой", "Вы", "Разобрать причину остановки.", "Остановлено"),
        "cancelled": ("Задача отменена", "Вы", "Дальнейшее выполнение остановлено.", "Отменено"),
        "completed": ("Задача завершена", "PromptPilot", "Открыть принятый результат.", "Готово"),
    }
    headline, current_owner, action, detail = mapping.get(status, ("Состояние требует уточнения", "Вы", "Обновить карточку.", "Неизвестно"))
    automatic = _object(config.get("automation"))
    if status == "gating" and not (automatic.get("enabled") is True and automatic.get("auto_gate") is not False):
        headline, current_owner = "Нужно зафиксировать результат технических проверок", "Вы"
        action = "Выполнить проверки и выбрать их итог в карточке."
    if status == "awaiting_external":
        current_owner = owner
        if not client.get("client_id"):
            headline = "Ждём результат внешнего этапа"
            action = "Исполнителю сдать результат в карточке задачи."
        elif external == "available":
            headline = f"Ждём, когда {owner} заберёт " + ("доработку" if workflow["current_round"] > 1 else "задание")
            action = f"{owner}: продолжить обслуживание очереди через MCP."
        elif external == "claimed":
            headline = f"{owner} забрал задание; ждём результат"
            action = "Исполнитель должен сдать результат через MCP."
        elif external == "submitting":
            headline = "Подтверждается сдача результата"
            action = "Дождаться подтверждения сдачи и перехода к проверке файлов."
        else:
            headline = "Нужно сверить состояние сдачи исполнителя"
            action = "Сверить текущую сдачу; повторная отправка без сверки не требуется."
        detail = "Новый результат ещё не получен. Подключение и получение задания сами по себе не доказывают работу модели."
        if external == "submitting":
            detail = "Сдача началась; её окончательное подтверждение ещё не получено."
    return {"round_no": workflow["current_round"], "stage_index": stages.index(stage) + 1 if stage else None,
            "stage_total": len(stages), "stage_code": _text(stage["code"], 100) if stage else "",
            "stage_title": _text(stage["title"], 240) if stage else "", "status": status,
            "owner": current_owner, "headline": headline, "detail": detail, "next_action": action}


def _steps(status, current_events, round_no):
    ids = [("plan", "План"), ("execution", "Исполнение"), ("files", "Проверка файлов"), ("review", "Ревью"), ("done", "Готово")]
    position = {"draft": 0, "planning": 0, "awaiting_plan_approval": 0, "queued": 1,
                "executing": 1, "awaiting_external": 1, "gating": 2, "reviewing": 3, "completed": 4}.get(status, 1)
    if status == "revision_required":
        position = 3 if any(e["event_type"] == "review.revision_required" for e in current_events) else 2
    if not round_no and status in {"cancelled", "failed", "awaiting_human"}:
        position = 0
    blocked = status in {"revision_required", "awaiting_human", "failed", "cancelled"}
    return [{"id": key, "label": label, "state": "done" if index < position or status == "completed" else
             "blocked" if index == position and blocked else "current" if index == position else "waiting"}
            for index, (key, label) in enumerate(ids)]


def get_progress(workflow_id):
    if not isinstance(workflow_id, str) or not re.fullmatch(r"wf_[0-9a-f]{32}", workflow_id):
        raise ProgressNotFound()
    database = Path(db.DB_PATH).absolute()
    if not database.is_file():
        raise ProgressUnavailable()
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            workflow = conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            if workflow is None:
                raise ProgressNotFound()
            config = _object(workflow["config_json"])
            stages = list(conn.execute("SELECT * FROM workflow_stages WHERE workflow_id=? ORDER BY position", (workflow_id,)))
            rounds = list(conn.execute("SELECT * FROM workflow_rounds WHERE workflow_id=? ORDER BY round_no DESC LIMIT 200", (workflow_id,)))
            events = list(conn.execute("SELECT * FROM workflow_events WHERE workflow_id=? ORDER BY seq DESC LIMIT 1000", (workflow_id,)))
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            client = _object(config.get("external_executor")).get("client_id")
            external = _external_state(conn, workflow, client, tables) if client and workflow["status"] == "awaiting_external" else None
            current = _current(workflow, stages, config, external)
            from .workflow_review_recovery import stop_reason
            review_stop = stop_reason(conn, workflow)
            if review_stop:
                current.update(headline=review_stop['summary'], owner=review_stop['owner'],
                               detail=review_stop['detail'], next_action=review_stop['action'])
            stage = next((s for s in stages if s["id"] == workflow["current_stage_id"]), None)
            by_round = {r["id"]: r for r in rounds}
            last_return = None
            for event in events:
                round_row = by_round.get(event["round_id"])
                if round_row is None or round_row["stage_id"] != workflow["current_stage_id"]:
                    continue
                if event["event_type"] in {"gate.passed", "review.passed", "stage.advanced"}:
                    break
                if event["event_type"] not in {"gate.failed", "review.revision_required"}:
                    continue
                last_return = {**_round_reason(event), "round_no": round_row["round_no"]}
                if last_return["kind"] == "gate_failed" and "external_inbox_artifacts_v1" in tables:
                    comparison = _artifact_comparison(conn, workflow, round_row, stage)
                    if comparison:
                        last_return.update(comparison)
                break
            history = []
            for round_row in rounds:
                event = next((e for e in events if e["round_id"] == round_row["id"] and e["event_type"] in {"gate.failed", "review.revision_required"}), None)
                summary = _round_reason(event)["summary"] if event else ""
                if last_return and last_return["round_no"] == round_row["round_no"]:
                    summary = last_return["summary"]
                history.append({"round_no": round_row["round_no"], "status": round_row["status"], "summary": summary})
            current_round_ids = {r["id"] for r in rounds if r["round_no"] == workflow["current_round"]}
            result = {"workflow_id": workflow_id, "state_version": workflow["state_version"], "current": current,
                      "steps": _steps(workflow["status"], [e for e in events if e["round_id"] in current_round_ids], workflow["current_round"]),
                      "last_return": last_return, "history": history,
                      "review_stop": review_stop}
            token = _object(config.get("workorder_guard")).get("lease_token")
            if isinstance(token, str) and token:
                # Same owner-visible strings as the task feed; control values
                # must never be rendered even if embedded in a display label.
                def redact(value):
                    if isinstance(value, str):
                        return value.replace(token, "[redacted]")
                    if isinstance(value, list):
                        return [redact(item) for item in value]
                    if isinstance(value, dict):
                        return {key: redact(item) for key, item in value.items()}
                    return value
                result = redact(result)
            return result
    except sqlite3.Error:
        raise ProgressUnavailable() from None
