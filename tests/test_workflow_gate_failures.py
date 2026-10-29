"""Exercise automatic gate decisions through the real workflow lifecycle."""
import os
import base64
import shlex
import sys

import pytest

from promptpilot import workflows
from promptpilot.models import WorkflowCreate,WorkflowStartRequest,TaskStatus


def command(source):
    encoded=base64.b64encode(source.encode('utf-8')).decode('ascii')
    argv=[sys.executable,'-c',f"exec(__import__('base64').b64decode('{encoded}'))"]
    # Production runs gate strings through PowerShell or /bin/sh. Quote the
    # actual child interpreter for that same shell, including paths with spaces.
    if os.name=='nt':
        return '& '+ ' '.join("'"+arg.replace("'","''")+"'" for arg in argv)
    return shlex.join(argv)


@pytest.mark.parametrize('first,second,stop,expected_runs',[
    (7,0,False,2),(0,5,False,2),(7,5,False,2),(0,0,False,2),(7,0,True,1),
])
def test_nonstop_gate_collects_evidence_without_turning_failure_into_pass(
        isolated_db,tmp_path,first,second,stop,expected_runs):
    commands=[]
    for index,exit_code in enumerate([first,second]):
        marker=tmp_path/f'command-{index}.txt'
        commands.append(command(f'from pathlib import Path; Path({str(marker)!r}).write_text("ran"); raise SystemExit({exit_code})'))
    workflow=isolated_db.create_workflow(WorkflowCreate(
        slug='nonstop-gate',objective='Reject failed gate commands',
        repository_path=str(isolated_db.DB_DIR),candidate_branch='feature/gates',
        config={'automation':{'enabled':True},'roles':{'executor':{'provider':None},'reviewer':{'provider':None}},
                'gate':{'enabled':True,'commands':commands,'stop_on_failure':stop},
                'limits':{'max_rounds':1}}))
    workflows.start_workflow(workflow.id,WorkflowStartRequest(expected_version=workflow.state_version))
    workflows.advance_workflow(workflow.id)
    task=isolated_db.get_next_runnable()
    assert task is not None
    workflows.sync_task(task.id)
    isolated_db.mark_completed(task.id,'executor completed',exit_code=0)
    isolated_db.set_verdict(task.id,'ГОТОВО')
    workflows.sync_task(task.id)
    result=workflows.advance_workflow(workflow.id)
    events=isolated_db.list_workflow_events(workflow.id,limit=1000)
    gates=[event for event in events if event.event_type in {'gate.passed','gate.failed'}]
    assert len(gates)==1
    assert sum(path.exists() for path in [tmp_path/'command-0.txt',tmp_path/'command-1.txt'])==expected_runs,gates[0].payload
    assert len(gates[0].payload['evidence'])==expected_runs
    failed=first!=0 or second!=0
    assert gates[0].event_type==('gate.failed' if failed else 'gate.passed')
    assert result.status.value==('awaiting_human' if failed else 'reviewing')
    pending=isolated_db.list_tasks(status=TaskStatus.PENDING)
    assert len(pending)==(0 if failed else 1)
