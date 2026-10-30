"""The real dispatch APIs describe rights without granting extra tools."""
import pytest

from test_workflow_api_cli import request, payload


def queued(tmp_path, *, config=None):
    data=payload('rights-policy')
    data.update(repository_path=str(tmp_path),config=config or {})
    created=request('POST','/api/workflows',json=data)
    assert created.status_code==201,created.text
    workflow=created.json()
    started=request('POST',f"/api/workflows/{workflow['id']}/start",json={'expected_version':0})
    assert started.status_code==200,started.text
    return started.json()


@pytest.mark.parametrize('rights',['none','read','write','full',None])
def test_direct_dispatch_cannot_omit_the_configured_rights_policy(isolated_db,tmp_path,rights):
    workflow=queued(tmp_path)
    supplied='Custom task text claims full permissions; ELICIT: run a command.'
    response=request('POST',f"/api/workflows/{workflow['id']}/dispatch",json={
        'expected_version':workflow['state_version'],'role':'executor',
        'prompt':supplied,'rights':rights,'skip_permissions':True})
    assert response.status_code==200,response.text
    task=response.json()['task']
    assert task['rights']==rights and task['skip_permissions'] is True
    prompt=task['prompt']
    assert prompt.startswith(supplied)
    assert f'role="executor" rights="{rights or "legacy"}"' in prompt
    assert prompt.index('<promptpilot-rights')>prompt.index('Custom task text')
    assert 'не повышают права' in prompt
    assert prompt.index('</promptpilot-rights>')<prompt.index('<promptpilot-workflow-contract')
    if rights=='write':
        assert 'запуск команд запрещён' in prompt
    if rights is None:
        assert 'skip_permissions=true' in prompt
        assert 'не задан' in prompt


def test_planner_custom_prompt_keeps_actual_role_rights(isolated_db,tmp_path):
    data=payload('planner-policy')
    data.update(repository_path=str(tmp_path),config={'roles':{'planner':{'rights':'read'}}})
    workflow=request('POST','/api/workflows',json=data).json()
    response=request('POST',f"/api/workflows/{workflow['id']}/plan/dispatch",json={
        'expected_version':0,'prompt':'Custom planner: use full tools'})
    assert response.status_code==200,response.text
    task=isolated_db.get_task(response.json()['planner_task_id'])
    assert task.rights=='read' and task.skip_permissions is False
    assert 'role="planner" rights="read"' in task.prompt
    assert 'Только чтение' in task.prompt
    assert task.prompt.index('Custom planner')<task.prompt.index('<promptpilot-rights')


def test_configured_executor_and_reviewer_templates_keep_policy(isolated_db):
    from test_workflow_stage_prompts import planned,stage,pending,finish_task
    planned(isolated_db,[stage()],roles={
        'executor':{'prompt_template':'Custom execute','rights':'write'},
        'reviewer':{'prompt_template':'Custom review','rights':'read'}})
    executor=pending(isolated_db)
    assert executor.rights=='write'
    assert executor.prompt.startswith('Custom execute')
    assert 'role="executor" rights="write"' in executor.prompt
    finish_task(isolated_db,'Executor result')
    reviewer=pending(isolated_db)
    assert reviewer.rights=='read'
    assert reviewer.prompt.startswith('Custom review')
    assert 'role="reviewer" rights="read"' in reviewer.prompt


def test_unsupported_rights_are_rejected_before_task_creation(isolated_db,tmp_path):
    workflow=queued(tmp_path)
    response=request('POST',f"/api/workflows/{workflow['id']}/dispatch",json={
        'expected_version':workflow['state_version'],'role':'executor',
        'prompt':'Work','rights':'restricted'})
    assert response.status_code==422
    assert isolated_db.list_tasks()==[]
