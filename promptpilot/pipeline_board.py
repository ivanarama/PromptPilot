"""Read-only, evidence-labelled view of delivery and outstanding work.

Never infer a delivery category from a PR title or a DONE verdict. Explicit
operator classifications may be supplied by a profile; unknown stays unknown.
No additional GitHub requests and no mutation/admission decisions live here.
"""
import copy


CATEGORIES = {
    "product": "Продукт",
    "pipeline": "Обслуживание конвейера",
    "docs_plans": "Документация и планы",
    "unclassified": "Не классифицировано",
}


def build_board(delivery, diagnostics, tasks, *, available, classifications=None):
    groups = {key: [] for key in CATEGORIES}
    configured = classifications if isinstance(classifications, dict) else {}
    for original in delivery.get("merged_prs") or []:
        item = copy.deepcopy(original)
        classification = configured.get(str(item.get("number")))
        category = "unclassified"
        if (isinstance(classification, dict)
                and classification.get("category") in CATEGORIES
                and classification.get("evidence")):
            category = classification["category"]
            item["classification_evidence"] = str(classification["evidence"])
        item["category"] = category
        groups[category].append(item)
    blocked = []
    waiting = []
    if available:
        waiting = [copy.deepcopy(item) for item in diagnostics.get("waiting_ci") or []
                   if isinstance(item, dict)]
        for finding in diagnostics.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            # A barrier protects ordering, it is not itself a defect or CI wait.
            if finding.get("code") == "single_flight_barrier":
                continue
            severity = finding.get("severity")
            if severity not in {"red", "yellow"}:
                continue
            target = copy.deepcopy(finding)
            number = finding.get("pr") or finding.get("issue")
            target["number"] = number
            if finding.get("code") in {"required_checks_pending", "ci_pending", "checks_pending"}:
                waiting.append(target)
            else:
                blocked.append(target)
    return {
        "available": bool(available),
        "delivery_exact": delivery.get("exact") is True,
        "delivery_groups": [{"id": key, "title": title, "items": groups[key],
                             "count": len(groups[key]) if delivery.get("exact") else None}
                            for key, title in CATEGORIES.items()],
        "classification_complete": (delivery.get("exact") is True
                                    and not groups["unclassified"]),
        "working": [copy.deepcopy(item) for item in tasks if item.get("status") == "running"],
        "waiting_ci": waiting,
        "ci_available": available and (isinstance(diagnostics.get("waiting_ci"), list) or bool(waiting)),
        "blocked": blocked,
        "waiting_ship": copy.deepcopy(diagnostics.get("reviewed_waiting_ship") or []) if available else [],
        "human_waiting": copy.deepcopy(diagnostics.get("human_waiting") or []) if available else [],
        "note": "Закрытые issues и влитые PR — отдельные события, не сумма выполненных задач.",
    }
