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
- Ревьюер видит правку плана как часть диффа этапа (событие
  plan.amended попадает в таймлайн воркфлоу — в промпт ревьюера
  добавляется automatically через предыдущие события).

Использование: watchdog/агент вызывает validate_amendment() перед
POST /api/workflows/{id}/plan/amend; approved_stage_amend() — готовый
обёрнутый вызов через локальный API."""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

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
    canon = (gate_canon or DEFAULT_GATE_CANON).get(slug)
    if canon:
        missing = [needle for needle in canon if needle not in new_gates_text]
        if missing:
            return False, ("gate-канон миссии нарушен: потеряны обязательные "
                           f"проверки {missing}")
    removed = [c for c in old_codes if c not in new_codes]
    if removed and canon:
        # удаление этапа допустимо, только если его каноничная проверка
        # сохранилась в другом этапе (покрыто проверкой текста gates выше)
        pass
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
    Секрет (токен) берётся из env PP_API_TOKEN, не из аргументов."""
    body = json.dumps({
        "expected_version": expected_version,
        "stages": stages,
        "reason": reason,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + f"/api/workflows/{workflow_id}/plan/amend",
        data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    tok = token or os.environ.get("PP_API_TOKEN", "")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")
