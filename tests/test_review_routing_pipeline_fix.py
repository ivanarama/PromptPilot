"""Private issuer composition with actual scheduler and authenticated peer.

This is not installed public CLI/worker or accepted OneBase checker evidence.
"""
import copy
import json
import subprocess
import sys

import pytest

from promptpilot import project_pipeline as pp
from promptpilot import review_routing_pipeline_fix as pipeline
from promptpilot import review_routing_pipeline_policy as policy
from promptpilot import review_routing_start_request as requests
from promptpilot.pipeline_errors import PipelineError
from promptpilot.process_tree import OwnedProcess
from promptpilot.review_routing_execution import OneUseStart,TABLE
from promptpilot.review_routing_wire import parse
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401
from test_review_routing_fix import author_bridge as author_bridge,ledger  # noqa: F401
from test_review_routing_start import metadata_launch


@pytest.fixture
def pipeline_case(author_bridge,monkeypatch):
    peer,health,path,task,_run,database=author_bridge
    raw=json.loads(path.read_text(encoding='utf-8'))
    raw['technical_fix_execution']=dict(enabled=True,protocol='review-routing-v1',
        executor=dict(kind='pipeline',login='owner'))
    path.write_text(json.dumps(raw),encoding='utf-8')
    health['fix_execution_contract']=copy.deepcopy(policy.CONTRACT)
    original=pp.run_health
    def checker(*args,**kwargs):
        result=original(*args,**kwargs)
        result['technical_fix_start_candidates']=[]
        state=ledger(peer,path)
        for root in state.attempts:
            started=state.attempt_phases(root).get('started')
            if started is not None:
                result['technical_fix_start_candidates'].append(dict(stage='fix-pr',number=42,
                    head=root.payload['source']['head'],source=root.payload['source'],attempt_id=root.id,
                    started_id=started.id,**started.payload['details']))
        return result
    monkeypatch.setattr(pp,'run_health',checker)
    config=pp.load_config(str(path))
    return peer,health,path,task,database,config


def selected(case):
    peer,health,path,_task,_database,config=case
    result=pipeline.select(peer,config,config_path=str(path),health=health)
    assert result['action']=='pipeline-fix'
    return result


def issue(case,selection):
    peer,_health,path,_task,_database,config=case
    return pipeline.issue(peer,config,selection['lease'],config_path=str(path))


@pytest.mark.parametrize('lost_response',[False,True])
def test_private_new_issuer_to_actual_owned_factory_has_one_canonical_attempt(pipeline_case,lost_response):
    peer,_health,path,_task,database,_config=pipeline_case
    selection=selected(pipeline_case)
    if lost_response:
        original=peer.json
        def lost(*args,input_value=None):
            result=original(*args,input_value=input_value)
            if input_value is not None:
                value=parse(input_value['body'])
                if value['kind']=='phase' and value['phase']=='started':raise OSError('response lost after actual POST')
            return result
        peer.json=lost
    issued=issue(pipeline_case,selection)
    assert issued['action']=='pipeline-start' and issued['provider_started'] is False
    before=list(peer.posts)
    repeated=issue(pipeline_case,selection)
    assert repeated['target']==issued['target'] and peer.posts==before
    request=requests.decode(issued['lease']);calls=[]
    def factory():
        calls.append(True)
        return OwnedProcess.start([sys.executable,'-c','print("one-factory")'],
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    tree=metadata_launch(peer,str(path),issued['lease'],factory=factory)
    try:
        OneUseStart(database.DB_PATH).observe_started(request['scope'],tree.process.pid)
        output,_error=tree.process.communicate(timeout=10)
        assert b'one-factory' in output and len(calls)==1
        with pytest.raises(PipelineError):issue(pipeline_case,selection)
        assert peer.posts==before
        assert len(ledger(peer,path).attempts)==ledger(peer,path).budget_used==1
        assert not peer.assignment_posts
    finally:
        OneUseStart(database.DB_PATH).confirm_stopped(request['scope'],tree)
        tree.close()


def test_existing_start_does_not_reconstruct_lost_local_issuance(pipeline_case):
    from contextlib import closing
    import sqlite3
    peer,_health,path,_task,database,_config=pipeline_case
    selection=selected(pipeline_case);issue(pipeline_case,selection)
    before=list(peer.posts)
    with closing(sqlite3.connect(database.DB_PATH)) as connection:
        with connection:connection.execute('DELETE FROM '+TABLE)
    with pytest.raises(PipelineError,match='cannot reconstruct missing local issuance'):
        issue(pipeline_case,selection)
    assert peer.posts==before and ledger(peer,path).budget_used==1


def test_unknown_started_post_never_repeats_or_prepares_a_launch(pipeline_case):
    peer,_health,path,_task,_database,_config=pipeline_case
    selection=selected(pipeline_case)
    original=peer.json;attempts=[]
    def unknown(*args,input_value=None):
        if input_value is not None:
            value=parse(input_value['body'])
            if value['kind']=='phase' and value['phase']=='started':
                attempts.append(True);raise OSError('unknown network outcome, absent visibility')
        return original(*args,input_value=input_value)
    peer.json=unknown
    for _ in range(2):
        with pytest.raises(PipelineError):issue(pipeline_case,selection)
    assert len(attempts)==1 and ledger(peer,path).budget_used==1
    assert 'started' not in ledger(peer,path).attempt_phases(ledger(peer,path).attempts[0])


@pytest.mark.parametrize('fault',['actor','cancel'])
def test_late_pre_admission_change_spends_no_attempt_and_starts_nothing(pipeline_case,fault):
    peer,_health,path,task,database,_config=pipeline_case
    selection=selected(pipeline_case);before=list(peer.posts)
    if fault=='actor':peer.actor='wrong'
    else:database.request_cancel(task.id)
    with pytest.raises(PipelineError):issue(pipeline_case,selection)
    assert peer.posts==before and ledger(peer,path).budget_used==0



@pytest.mark.parametrize('author_bridge',['real-source'],indirect=True)
def test_public_cli_admission_prepare_and_consumed_start_gate(pipeline_case):
    from contextlib import redirect_stdout
    from io import StringIO
    from promptpilot.review_routing_wire import sha
    peer,_health,path,_task,database,_config=pipeline_case
    def cli(*args):
        stream=StringIO()
        with redirect_stdout(stream):code=pp.run(['--config',str(path),*args])
        return code,json.loads(stream.getvalue())
    code,selection=cli('next','fix')
    assert code==0 and selection['action']=='pipeline-fix',selection
    code,prepared=cli('prepare-fix-start','fix','--lease',selection['lease'])
    assert code==0 and prepared['action']=='pipeline-start',prepared
    value=requests.decode(prepared['lease']);calls=[]
    from promptpilot.review_routing_workspace import Workspace
    Workspace(database.DB_PATH,value['scope'],database.get_task(value['scope']['task_id']).working_dir).prepare()
    code,refused=cli('gate-fix-start','fix','--lease',prepared['lease'])
    assert code==2 and refused['action']=='error','unconsumed token acquired factory permission'
    assert 'consumed' in refused['error']
    def final(scope):
        code,result=cli('gate-fix-start','fix','--lease',prepared['lease'])
        assert code==0 and result['action']=='start-allowed',result
        assert result['scope_sha256']==sha(scope) and result['owner']==scope['owner']
        return True
    OneUseStart(database.DB_PATH,create=False,allow_new=False).invoke(value['scope'],
        final_guard=final,factory=lambda:calls.append(True))
    assert calls==[True] and ledger(peer,path).budget_used==1
    with pytest.raises(PipelineError):issue(pipeline_case,selection)



@pytest.mark.parametrize('author_bridge',['fork'],indirect=True)
def test_public_pipeline_policy_keeps_fork_execution_with_the_author(pipeline_case):
    from contextlib import redirect_stdout
    from io import StringIO
    peer,_health,path,_task,_database,_config=pipeline_case
    stream=StringIO()
    with redirect_stdout(stream):code=pp.run(['--config',str(path),'next','fix'])
    value=json.loads(stream.getvalue())
    assert code==0 and value['action']=='author-handoff',value
    assert value['mutation_authorized'] is False
    assert ledger(peer,path).budget_used==0



@pytest.mark.parametrize('author_bridge',['real-source'],indirect=True)
@pytest.mark.parametrize('fault',['bytes','receipt'])
def test_public_factory_gate_refuses_changed_deterministic_checkout(pipeline_case,fault):
    from contextlib import redirect_stdout
    from io import StringIO
    from promptpilot.review_routing_workspace import Workspace
    peer,_health,path,task,database,_config=pipeline_case
    selection=selected(pipeline_case);prepared=issue(pipeline_case,selection)
    value=requests.decode(prepared['lease'])
    workspace=Workspace(database.DB_PATH,value['scope'],task.working_dir)
    workspace.prepare()
    if fault=='bytes':(workspace.path/'app.py').write_text('unexpected checkout change',encoding='utf-8')
    else:
        record=json.loads(workspace.receipt.read_bytes());record['scope_sha256']='f'*64
        workspace.receipt.write_text(json.dumps(record),encoding='utf-8')
    calls=[]
    def final(_scope):
        stream=StringIO()
        with redirect_stdout(stream):code=pp.run(['--config',str(path),'gate-fix-start','fix','--lease',prepared['lease']])
        result=json.loads(stream.getvalue())
        assert code==2 and result['action']=='error',result
        raise PipelineError('checkout gate refused before actual factory')
    store=OneUseStart(database.DB_PATH,create=False,allow_new=False)
    with pytest.raises(PipelineError):store.invoke(value['scope'],final_guard=final,factory=lambda:calls.append(True))
    assert not calls and store.status(value['scope'])['state']=='not-started'
    assert workspace.path.exists() and ledger(peer,path).budget_used==1
