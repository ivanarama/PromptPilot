"""Independent exact-head review probes, private fixtures only."""
import json
import subprocess
import sys
import pytest
from promptpilot import worker,pipeline_insights as insights
from promptpilot.process_tree import OwnedProcess
from promptpilot.review_routing_control import run as run_control,close_controls
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401
from test_review_routing_fix import author_bridge as author_bridge  # noqa: F401

@pytest.mark.parametrize('startup_delay',[0,3])
def test_author_control_cleanup_preserves_running_task(author_bridge,monkeypatch,tmp_path,startup_delay):
    peer,health,path,task,run,database=author_bridge
    queue=dict(id='fix',execution=dict(mode='auto',command=['pipelinectl','next','fix'],review_routing_protocol='review-routing-v1'))
    monkeypatch.setattr(insights,'_matching_queue',lambda _:('private',{'repository':'owner/repo'},queue))
    monkeypatch.setattr(insights,'_tool_available',lambda *a:(True,''))
    monkeypatch.setattr(insights,'load_providers',lambda:{})
    monkeypatch.setattr(insights,'dispatch_gate',lambda _:None)
    monkeypatch.setattr(worker,'effective_prompt',lambda _:'Project - FIX')
    monkeypatch.setattr(worker,'build_cmd',lambda *a,**k:pytest.fail('model fallback'))
    task.series_id=task.id
    original=OwnedProcess.start
    held=[];permit={'close':False}
    ready=tmp_path/'control-descendant-live'
    def start(*a,**kw):
        tree=original(*a,**kw);close=tree.close;held.append((tree,close))
        def refused():
            if not permit['close']:raise OSError('independent OS cleanup refusal')
            return close()
        tree.close=refused
        return tree
    def control(execution,command,directory,*,env_extra=None):
        if 'complete' in command:
            child=('import pathlib,time; time.sleep('+str(startup_delay)+'); pathlib.Path('
                   +repr(str(ready))+').write_text("alive"); time.sleep(60)')
            parent=('import pathlib,subprocess,sys,time\n'
                'subprocess.Popen([sys.executable,"-c",'+repr(child)+'],stdin=subprocess.DEVNULL,'
                'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))\n'
                'ready=pathlib.Path('+repr(str(ready))+')\n'
                'deadline=time.monotonic()+20\n'
                'while not ready.exists() and time.monotonic()<deadline: time.sleep(.05)\n'
                'if not ready.exists(): raise RuntimeError("private descendant startup deadline expired")\n'
                'print("harmless metadata parent established its descendant")\n')
            return run_control([sys.executable,'-c',parent],env=env_extra,
                capture_output=True,text=True,timeout=35,stdin=subprocess.DEVNULL)
        code,value=run('next','fix')
        assert code==0,value
        return value
    monkeypatch.setattr(OwnedProcess,'start',start)
    monkeypatch.setattr(insights,'_tool_preflight',control)
    caught=None
    try:
        try:worker.execute_task(task)
        except worker.ProviderOwnershipError as exc:caught=exc
        saved=database.get_task(task.id)
        # Readiness precedes the injected cleanup refusal. Hosted Windows can
        # cold-start a child more slowly than the old two-second poll allowed.
        assert ready.exists(),'harmless owned descendant was not established'
        (tmp_path/'cleanup-observed.json').write_text(json.dumps(dict(
            task_status=saved.status.value,ownership_error_propagated=caught is not None,
            retained_controls=len(held),descendant_established=True,
            reservation_count=len(database.list_pipeline_target_reservations()))),encoding='utf-8')
        assert held and saved.status.value=='running', 'terminal task with unclosed original control: '+saved.status.value
        assert caught is not None
        assert len(database.list_pipeline_target_reservations())==1
    finally:
        permit['close']=True
        close_controls(task.id)
        for tree,close in held:close();tree.process.wait(timeout=10)
