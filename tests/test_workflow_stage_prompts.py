"""A stage text supplements the role prompt instead of replacing its context (#158).

Ported from AlexanderYekat's fork (4a2b1e4d, 1d6f182d) without the parts that
depend on workflow candidates and external stages, which main does not have.
Queue tasks are completed by hand in an isolated database: no provider runs.
"""
import base64
import json
import os
import sys

from promptpilot import workflows
from promptpilot.models import (
    TaskStatus,
    WorkflowCreate,
    WorkflowPlanApproval,
    WorkflowPlanDispatch,
    WorkflowPlanReplace,
)


def shell_python(script):
    """A gate command that runs ``script`` with this Python on every OS."""
    if os.name == 'nt':
        encoded = base64.b64encode(script.encode()).decode()
        return ("& '" + sys.executable.replace("'", "''") + "' -c \"exec(__import__('base64')"
                ".b64decode('" + encoded + "'))\"")
    import shlex
    return shlex.quote(sys.executable) + ' -c ' + shlex.quote(script)


CONTRACT = workflows.AUDIT_RESPONSE_CONTRACT.strip()
GATE = shell_python('print("GATE-MARKER-42")')
CONTEXT = "<контекст-этапа>"
VERDICT = "<promptpilot-workflow-contract"
GOAL = "Этап: FINAL Проверка\nПроверяемая цель этапа:\nПроверить функцию"

# Built-in templates as they were before the answer format moved into a
# constant. Stages without their own prompts must keep exactly this text.
LEGACY_EXECUTOR = """Ты — исполнитель в автономном инженерном workflow PromptPilot.

Цель: {{objective}}
Текущий этап: {{stage_code}} {{stage_title}}
Цель этапа: {{stage_goal}}
Разрешённые пути: {{allowed_paths}}
Ожидаемые результаты: {{deliverables}}
Репозиторий: {{repository_path}}
Ветка кандидата: {{candidate_branch}}
Раунд: {{round_no}}

Замечания предыдущего независимого аудита:
{{previous_review}}

Результаты предыдущих deterministic gate:
{{gate_evidence}}

Исправь замечания в заданном scope, проверь результат, сохрани доказательства и
сделай локальные коммиты. Не меняй критерии приёмки и не объявляй готовность без
проверяемых фактов. В итоговом ответе перечисли изменения, команды проверок,
commit SHA, незакрытые ограничения и пути к evidence.
"""

LEGACY_REVIEWER = """Ты — независимый аудитор в автономном workflow PromptPilot.
Не исправляй код и не принимай заявления исполнителя на веру.

Цель: {{objective}}
Текущий этап: {{stage_code}} {{stage_title}}
Цель этапа: {{stage_goal}}
Репозиторий: {{repository_path}}
Ветка кандидата: {{candidate_branch}}
Раунд: {{round_no}}

Отчёт исполнителя:
{{executor_report}}

Deterministic gate:
{{gate_evidence}}

Незакрытые замечания предыдущих аудитов:
{{open_findings}}

Проверь diff, историю Git, тесты и evidence. В конце отчёта обязательно выведи
две машинно-читаемые строки (каждая целиком на одной строке):
AUDIT_FINDINGS_JSON: []
AUDIT_VERDICT: PASS

Весь текст отчёта и объяснения размещай перед этими строками. Выводи их
обычным текстом, вне цитат и блоков кода. После AUDIT_VERDICT допустима
только завершающая строка ИТОГ установленного workflow-формата.

Для замечаний верни JSON-массив объектов с полями fingerprint, severity
(blocker/high/medium/low/info), category, title, status (open/resolved/reopened/
accepted_risk), payload. AUDIT_VERDICT допускает только PASS,
REVISION_REQUIRED или HUMAN_REQUIRED.

Если список незакрытых замечаний выше не пуст, верни в AUDIT_FINDINGS_JSON
каждый прежний fingerprint: со status=resolved, когда исправление проверено, либо
со status=open/reopened и вердиктом REVISION_REQUIRED. Не возвращай PASS с
пустым массивом, пока в реестре есть незакрытые замечания.
"""

REVISION = (
    'AUDIT_FINDINGS_JSON: [{"fingerprint":"needs-fix","severity":"medium",'
    '"category":"test","title":"Нужно исправление"}]\n'
    "AUDIT_VERDICT: REVISION_REQUIRED\nИТОГ: ГОТОВО — нужны исправления"
)
RESOLVED = (
    'AUDIT_FINDINGS_JSON: [{"fingerprint":"needs-fix","severity":"medium",'
    '"category":"test","title":"Нужно исправление","status":"resolved"}]\n'
    "AUDIT_VERDICT: PASS\nИТОГ: ГОТОВО — этап принят"
)


def stage(**fields):
    return {"code": "FINAL", "title": "Проверка", "objective": "Проверить функцию",
            "stage_type": "integration", "allowed_paths": ["src/feature.py"],
            "deliverables": ["отчёт о проверке"], **fields}


def plan_result(stages):
    return ("WORKFLOW_PLAN_JSON_BEGIN\n" + json.dumps({"stages": stages}, ensure_ascii=False)
            + "\nWORKFLOW_PLAN_JSON_END\nИТОГ: ГОТОВО")


def planned(db, stages, edited=None, **config):
    cfg = {
        "planning": {"enabled": True, "require_approval": True, "max_stages": 10},
        "automation": {"enabled": True},
        "roles": {"planner": {"provider": None}, "executor": {"provider": None},
                  "reviewer": {"provider": None}},
        # These cases test prompt composition without deterministic commands;
        # opt out explicitly. Cases supplying GATE still exercise a real gate.
        "gate": {"enabled": False, "commands": []},
        "limits": {"max_rounds": 10},
    }
    cfg.update(config)
    workflow = db.create_workflow(WorkflowCreate(
        slug="prompts", objective="Сделать функцию", candidate_branch="feature/prompts",
        repository_path=str(db.DB_DIR), config=cfg))
    workflows.dispatch_planner(workflow.id, WorkflowPlanDispatch(
        expected_version=workflow.state_version))
    planner = db.get_next_runnable()
    db.mark_completed(planner.id, plan_result(stages), exit_code=0)
    db.set_verdict(planner.id, "ГОТОВО")
    workflows.sync_planner_task(planner.id)
    if edited is not None:  # the operator edits the cards before approval
        waiting = db.get_workflow(workflow.id)
        workflows.replace_plan(workflow.id, WorkflowPlanReplace(
            expected_version=waiting.state_version, stages=edited))
    waiting = db.get_workflow(workflow.id)
    workflows.approve_plan(workflow.id, WorkflowPlanApproval(
        expected_version=waiting.state_version))
    return workflows.advance_workflow(workflow.id)


def pending(db):
    tasks = db.list_tasks(status=TaskStatus.PENDING)
    assert len(tasks) == 1
    return tasks[0]


def finish_task(db, result, task=None):
    task = task or db.get_next_runnable()
    assert task is not None
    workflows.sync_task(task.id)
    db.mark_completed(task.id, result, exit_code=0)
    db.set_verdict(task.id, "ГОТОВО")
    workflows.sync_task(task.id)
    workflows.advance_linked_task(task.id)
    return task


def in_order(text, *parts):
    positions = [text.index(part) for part in parts]
    assert positions == sorted(positions), list(zip(parts, positions))


def test_stage_texts_keep_role_context_answer_format_and_revision_context(isolated_db):
    workflow = planned(isolated_db, [stage(
        executor_prompt="Сделай работу этапа.", reviewer_prompt="Проверь работу этапа.")],
        gate={"commands": [GATE]})

    executor = finish_task(isolated_db, "EXEC-REPORT-1")
    assert executor.prompt.startswith("Сделай работу этапа.")
    for part in ("src/feature.py", "отчёт о проверке",
                 "(нет: это первый раунд)", "(gate ещё не выполнялся)"):
        assert part in executor.prompt
    assert CONTRACT not in executor.prompt and "{{" not in executor.prompt
    # The card's goal reaches the agent although the stage text never states it.
    in_order(executor.prompt, "Сделай работу этапа.", CONTEXT, GOAL, "Разрешённые пути:", VERDICT)

    reviewer = finish_task(isolated_db, REVISION)
    assert reviewer.prompt.startswith("Проверь работу этапа.")
    assert reviewer.prompt.count(CONTRACT) == 1
    assert reviewer.prompt.count("AUDIT_VERDICT допускает только PASS") == 1
    for part in ("EXEC-REPORT-1", "GATE-MARKER-42", "Незакрытые замечания",
                 "src/feature.py", "отчёт о проверке"):
        assert part in reviewer.prompt
    in_order(reviewer.prompt, "Проверь работу этапа.", CONTEXT, GOAL, "Отчёт исполнителя:",
             "Незакрытые замечания", CONTRACT, VERDICT)

    # The revision round: the executor sees the audit remarks, the gate, its
    # scope and its previous report — not only the report.
    second = pending(isolated_db)
    assert isolated_db.get_workflow(workflow.id).current_round == 2
    for part in ("Нужно исправление", "GATE-MARKER-42", "src/feature.py", "EXEC-REPORT-1"):
        assert part in second.prompt
    in_order(second.prompt, "Сделай работу этапа.", CONTEXT, "Нужно исправление",
             "<предыдущий-запуск-исполнителя>", "EXEC-REPORT-1", VERDICT)

    finish_task(isolated_db, "EXEC-REPORT-2")
    reviewer = pending(isolated_db)
    assert reviewer.prompt.count(CONTRACT) == 1
    assert '"fingerprint": "needs-fix"' in reviewer.prompt
    assert "EXEC-REPORT-2" in reviewer.prompt
    finish_task(isolated_db, RESOLVED)

    done = isolated_db.get_workflow(workflow.id)
    assert done.status.value == "completed"
    events = [e.event_type for e in isolated_db.list_workflow_events(workflow.id, limit=1000)]
    assert "automation.paused" not in events


def test_sections_already_placed_by_the_stage_text_are_not_repeated(isolated_db):
    reviewer_text = ("Проверь {{stage_goal}}.\nОтчёт: {{executor_report}}\nGate: {{gate_evidence}}\n"
                     "Замечания: {{open_findings}}\nПути: {{allowed_paths}}\n"
                     "Результаты: {{deliverables}}\n{{audit_contract}}\n{{audit_contract}}")
    executor_text = ("Делай: {{stage_goal}}.\nПути: {{allowed_paths}}\nРезультаты: {{deliverables}}\n"
                     "Замечания: {{previous_review}}\nGate: {{gate_evidence}}\n{{audit_contract}}")
    planned(isolated_db, [stage(executor_prompt=executor_text, reviewer_prompt=reviewer_text)])

    executor = finish_task(isolated_db, "EXEC-REPORT")
    assert CONTEXT not in executor.prompt
    assert executor.prompt.count("src/feature.py") == 1
    assert executor.prompt.count("Проверить функцию") == 1
    assert CONTRACT not in executor.prompt and "{{" not in executor.prompt

    reviewer = pending(isolated_db)
    assert CONTEXT not in reviewer.prompt
    assert reviewer.prompt.count(CONTRACT) == 1
    assert reviewer.prompt.count("src/feature.py") == 1
    assert reviewer.prompt.count("Проверить функцию") == 1
    assert reviewer.prompt.count("EXEC-REPORT") == 1
    assert "{{" not in reviewer.prompt


def test_goal_edited_in_the_card_reaches_both_roles(isolated_db):
    texts = {"executor_prompt": "Реализуй по разделу 3 ТЗ.", "reviewer_prompt": "Сверь с разделом 3."}
    planned(isolated_db, [stage(**texts)],
            edited=[stage(objective="Посчитать без двойного учёта", **texts)])

    executor = finish_task(isolated_db, "EXEC-REPORT")
    assert "Проверяемая цель этапа:\nПосчитать без двойного учёта" in executor.prompt
    assert "Проверить функцию" not in executor.prompt
    reviewer = pending(isolated_db)
    assert "Проверяемая цель этапа:\nПосчитать без двойного учёта" in reviewer.prompt


def test_answer_format_is_placed_exactly_once():
    reviewer = workflows.WorkflowRole.REVIEWER
    copied = "Своя проверка.\n\n" + workflows.AUDIT_RESPONSE_CONTRACT
    assert workflows._context_block(copied, "reviewer").count(
        workflows.AUDIT_CONTRACT_PLACEHOLDER) == 0
    assert workflows._place_audit_contract(copied + "{{audit_contract}}", reviewer).count(CONTRACT) == 1
    twice = workflows._place_audit_contract("a {{audit_contract}} b {{audit_contract}}", reviewer)
    assert twice.count(CONTRACT) == 1 and "{{audit_contract}}" not in twice
    executor = workflows._place_audit_contract("x {{audit_contract}}", workflows.WorkflowRole.EXECUTOR)
    assert executor == "x "
    assert workflows.DEFAULT_REVIEWER_PROMPT == LEGACY_REVIEWER
    assert workflows.DEFAULT_EXECUTOR_PROMPT == LEGACY_EXECUTOR


def render(template, **values):
    for name, value in values.items():
        template = template.replace("{{" + name + "}}", value)
    return template.strip() + workflows.WORKFLOW_VERDICT_INSTRUCTION


def test_stage_without_own_prompts_keeps_builtin_text_before_rights_policy(isolated_db):
    workflow = planned(isolated_db, [stage()])
    common = dict(objective="Сделать функцию", stage_code="FINAL", stage_title="Проверка",
                  stage_goal="Проверить функцию", repository_path=str(isolated_db.DB_DIR),
                  candidate_branch="feature/prompts", round_no="1")

    executor = finish_task(isolated_db, "EXEC-REPORT")
    expected_executor = render(
        LEGACY_EXECUTOR, **common, allowed_paths="src/feature.py",
        deliverables="отчёт о проверке", previous_review="(нет: это первый раунд)",
        gate_evidence="(gate ещё не выполнялся)")
    assert executor.prompt.startswith(expected_executor.removesuffix(workflows.WORKFLOW_VERDICT_INSTRUCTION)
                                      + '\n\n<promptpilot-rights')
    assert executor.prompt.endswith(workflows.WORKFLOW_VERDICT_INSTRUCTION)

    reviewer = pending(isolated_db)
    expected_reviewer = render(
        LEGACY_REVIEWER, **common, executor_report="EXEC-REPORT",
        gate_evidence="Gate отключён настройками", open_findings="[]")
    assert reviewer.prompt.startswith(expected_reviewer.removesuffix(workflows.WORKFLOW_VERDICT_INSTRUCTION)
                                      + '\n\n<promptpilot-rights')
    assert reviewer.prompt.endswith(workflows.WORKFLOW_VERDICT_INSTRUCTION)
    assert isolated_db.get_workflow(workflow.id).status.value == "reviewing"


def test_custom_role_template_without_format_gets_it(isolated_db):
    planned(isolated_db, [stage()], roles={
        "planner": {"provider": None},
        "executor": {"provider": None, "prompt_template": "Исполни: {{stage_goal}}"},
        "reviewer": {"provider": None, "prompt_template": "Аудит: {{stage_goal}}"},
    })

    executor = finish_task(isolated_db, "EXEC-REPORT")
    assert executor.prompt.startswith("Исполни: Проверить функцию")
    assert CONTEXT in executor.prompt and "src/feature.py" in executor.prompt
    assert executor.prompt.count("Проверить функцию") == 1  # placed by the template

    reviewer = pending(isolated_db)
    assert reviewer.prompt.startswith("Аудит: Проверить функцию")
    assert reviewer.prompt.count(CONTRACT) == 1
    assert "EXEC-REPORT" in reviewer.prompt and "отчёт о проверке" in reviewer.prompt
    assert "Проверяемая цель этапа" not in reviewer.prompt


def test_report_quoting_a_placeholder_is_left_as_written(isolated_db):
    # Placeholders are filled in one pass: a report that quotes one placed
    # after it ({{open_findings}}) used to be rewritten by that later value.
    planned(isolated_db, [stage(executor_prompt="Сделай работу этапа.")])

    finish_task(isolated_db, "Отчёт цитирует {{open_findings}} как есть")
    reviewer = pending(isolated_db)

    assert "Отчёт цитирует {{open_findings}} как есть" in reviewer.prompt
