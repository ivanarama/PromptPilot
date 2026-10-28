# -*- coding: utf-8 -*-
"""F5 аддон: план-пилот — политика динамической перепланировки хвоста.

Ядро (workflows.amend_plan) даёт механику: замороженный префикс,
integration-финал, событие plan.amended. Этот модуль — ПОВЕДЕНИЕ:
правила, что можно менять без владельца, а что нельзя.

Политика (доктрина владельца 2026-10-04 «план — инструмент, который
ведут», гарантии неизменны):
- CAN: дробить/сливать/вставлять/убирать/переупорядочивать будущие
  (pending) карточки; менять их цели/gates; добавлять этапы-разблокеры.
- CANNOT (жёстко): трогать замороженный префикс (ядро гарантирует);
  убирать этап, чей gate входит в канон миссии (gate-canon ниже);
  менять цель МИССИИ; превышать бюджет этапов (канон missions_budget);
  amend без причины (min reason length — ядро).
- Событие plan.amended сохраняет старый и новый хвост в таймлайне.
  Этот аддон не подменяет независимое ревью и не включается автоматически.

Использование: watchdog/агент вызывает validate_amendment() перед
POST /api/workflows/{id}/plan/amend; approved_stage_amend() — готовый
обёрнутый вызов через локальный API."""
from __future__ import annotations

import json
import ipaddress
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote, urlsplit

# Канон: этапы, чьи gate-строки нельзя терять при перепланировке
# (пример — миссия prm-full-service-v1; каждая миссия может задать свой).
DEFAULT_GATE_CANON: dict[str, list[str]] = {
    "prm-full-service-v1": [
        # этап — подстрока, которая обязана остаться хоть в одном gate хвоста
        "verify_stage.py S27",   # сквозной e2e
        "verify_stage.py S28",   # release/устройство
        "verify_stage.py S30",   # финальная интеграция
    ],
}


def validate_amendment(
    slug: str,
    old_codes: list[str],
    new_codes: list[str],
    new_gates_text: str,
    *,
    gate_canon: dict[str, list[str]] | None = None,
) -> tuple[bool, str]:
    """(ok, причина отказа) — политика без побочных эффектов."""
    canon = (DEFAULT_GATE_CANON if gate_canon is None else gate_canon).get(slug)
    if canon:
        missing = [needle for needle in canon if needle not in new_gates_text]
        if missing:
            return False, ("gate-канон миссии нарушен: потеряны обязательные "
                           f"проверки {missing}")
    return True, ""


def approved_stage_amend(
    base_url: str,
    workflow_id: str,
    expected_version: int,
    stages: list[dict[str, Any]],
    reason: str,
    *,
    token: str | None = None,
    gate_canon: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Валидировать политику и вызвать POST /plan/amend.

    stages — полный список WorkflowStageSpec-подобных словарей
    (замороженный префикс как есть + новый хвост). Возвращает ответ API.
    Токен передаётся явно либо берётся из PP_API_TOKEN. Разрешён только
    локальный API; редиректы запрещены. CAS не заменяется свежей версией."""
    try:
        url = urlsplit(base_url)
        local = url.hostname == "localhost" or ipaddress.ip_address(url.hostname).is_loopback
        valid = (local and url.scheme in {"http", "https"} and url.port != 0
                 and url.username is None and url.password is None
                 and not url.query and not url.fragment and url.path in {"", "/"}
                 and not any(ord(c) <= 32 for c in base_url))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("Plan pilot requires a loopback HTTP(S) API URL")
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError("Invalid expected version")
    if not isinstance(stages, list) or not all(isinstance(s, dict) for s in stages):
        raise ValueError("Stages must be a list of stage specifications")
    tok = os.environ.get("PP_API_TOKEN", "") if token is None else token
    if any(ord(c) < 32 or ord(c) == 127 for c in tok):
        raise ValueError("Invalid API token")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    root = base_url.rstrip("/") + "/api/workflows/" + quote(workflow_id, safe="")

    def call(path, data=None):
        req = urllib.request.Request(path, data=data, method="POST" if data is not None else "GET")
        req.add_header("Content-Type", "application/json")
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        try:
            with opener.open(req, timeout=20) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("Response too large")
            return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            exc.close()
            raise RuntimeError("Local plan API request failed") from None
        except (OSError, ValueError, TypeError):
            raise RuntimeError("Local plan API request failed") from None

    workflow = call(root)
    previous = call(root + "/stages")
    if (not isinstance(workflow, dict) or workflow.get("id") != workflow_id
            or workflow.get("state_version") != expected_version):
        raise ValueError("Workflow changed; reload the plan before amending")
    if not isinstance(previous, list) or not all(isinstance(s, dict) for s in previous):
        raise ValueError("Invalid stage response")
    ok, why = validate_amendment(
        workflow.get("slug", ""), [s.get("code", "") for s in previous],
        [s.get("code", "") for s in stages],
        "\n".join(str(g) for s in stages for g in s.get("acceptance_gates", [])),
        gate_canon=gate_canon,
    )
    if not ok:
        raise ValueError(why)
    body = json.dumps({
        "expected_version": expected_version,
        "stages": stages,
        "reason": reason,
    }).encode("utf-8")
    return call(root + "/plan/amend", body)
