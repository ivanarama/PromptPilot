"""Owned metadata command timeout and retained original control cleanup."""
from datetime import timezone
import os
import subprocess
import sys
import time

import pytest

from promptpilot import worker,pipeline_insights as insights
from promptpilot.models import TaskCreate
from promptpilot.process_tree import OwnedProcess
from promptpilot.review_routing_control import run,close_controls


@pytest.fixture
def control_case(isolated_db,tmp_path):
    task=isolated_db.create_task(TaskCreate(prompt='Project - FIX',working_dir=str(tmp_path)))
    task=isolated_db.get_next_runnable();assert task is not None
    env=dict(os.environ,PP_TASK_ID=str(task.id),PP_TASK_STARTED_AT=task.started_at.astimezone(timezone.utc).isoformat(),
             PP_PROVIDER_OWNERSHIP_KIND='headless',PP_DATA_DIR=str(isolated_db.DB_PATH.parent))
    return task,env


def test_actual_control_preflight_uses_owned_boundary_and_only_its_explicit_private_env(control_case,tmp_path,monkeypatch):
    task,env=control_case
    monkeypatch.setenv('PP_TECHNICAL_EXECUTION_NONCE','must-not-inherit')
    result=insights._tool_preflight(dict(_owned_control=True),[sys.executable,'-c',
        'import json,os; print(json.dumps(dict(action="empty",has_nonce="PP_TECHNICAL_EXECUTION_NONCE" in os.environ)))'],
        str(tmp_path),env_extra=env)
    assert result==dict(action='empty',has_nonce=False)
    assert close_controls(task.id)==''


def test_actual_control_timeout_kills_its_descendant_before_reporting_refusal(control_case,tmp_path):
    task,env=control_case;flag=tmp_path/'late-control-child'
    child='import pathlib,time; time.sleep(1.5); pathlib.Path('+repr(str(flag))+').write_text("escaped")'
    parent='import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",'+repr(child)+']); time.sleep(60)'
    with pytest.raises(subprocess.TimeoutExpired):
        run([sys.executable,'-c',parent],env=env,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=.3)
    time.sleep(2)
    assert not flag.exists() and close_controls(task.id)==''


def test_os_cleanup_error_retains_original_control_until_same_handle_retry(control_case,monkeypatch):
    task,env=control_case;original=OwnedProcess.start;retained=[];allow={'value':False}
    def start(*args,**kwargs):
        tree=original(*args,**kwargs);retained.append(tree);close=tree.close
        def held():
            if not allow['value']:raise OSError('controlled original boundary cleanup error')
            return close()
        tree.close=held
        return tree
    monkeypatch.setattr(OwnedProcess,'start',start)
    try:
        with pytest.raises(worker.ProviderOwnershipError) as error:
            run([sys.executable,'-c','print("complete-control")'],env=env,stdin=subprocess.DEVNULL,
                capture_output=True,text=True,timeout=10)
        assert not worker._close_registered_provider_tree(task.id)
        assert error.value.retry_cleanup() is False and len(retained)==1
        allow['value']=True
        assert error.value.retry_cleanup() is True and worker._close_registered_provider_tree(task.id)
    finally:
        allow['value']=True
        for tree in retained:tree.close();tree.process.wait(timeout=10)
