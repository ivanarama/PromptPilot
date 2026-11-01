"""Retirement uses actual original cleanup receipts, not task/PID/TTL inference."""
import pytest

from promptpilot import worker
from promptpilot.pipeline_errors import PipelineError
from promptpilot.review_routing_execution import OneUseStart
from promptpilot.review_routing_push import Push
from promptpilot.review_routing_retirement import Retirement
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401
from test_review_routing_fix import author_bridge as author_bridge,ledger  # noqa: F401
from test_review_routing_pipeline_fix import pipeline_case as pipeline_case  # noqa: F401
from test_review_routing_technical_worker import technical_worker as technical_worker  # noqa: F401
from test_review_routing_push import push_case as push_case  # noqa: F401


@pytest.mark.parametrize('author_bridge',['real-source'],indirect=True)
@pytest.mark.parametrize('fault',['rate-limit','ambiguous-spawn'])
def test_terminal_task_alone_cannot_settle_unknown_but_original_stop_can_be_retired(technical_worker,fault):
    peer,path,task,database,_calls,factories,pushes,_remote,_ref,mode=technical_worker
    mode['fault']=fault
    worker._execute_task_body(task)
    assert database.get_task(task.id).status.value=='failed' and len(factories)==1 and not pushes
    state=ledger(peer,path);root=state.attempts[0]
    retirement=Retirement(database.DB_PATH,root,state.attempt_phases(root))
    if fault=='ambiguous-spawn':
        with pytest.raises(PipelineError,match='unknown'):retirement.observe()
        return
    observed=retirement.observe()
    assert observed['evidence']['state']=='stopped' and not observed['retired']
    assert observed['evidence']['publication']['outcome']=='not-issued'
    digest=retirement.retire(observed['evidence'])
    assert len(digest)==64 and retirement.observe()['retired']
    assert retirement.retire(observed['evidence'])==digest
    with pytest.raises(PipelineError,match='retired'):
        OneUseStart(database.DB_PATH,create=False,allow_new=False).invoke(observed['scope'],
            final_guard=lambda _scope:True,factory=lambda:pytest.fail('retired executor relaunched'))
    with pytest.raises(PipelineError,match='retired'):
        OneUseStart(database.DB_PATH,create=False,allow_new=False).verify_stopped(observed['scope'])


@pytest.mark.parametrize('author_bridge',['real-source'],indirect=True)
def test_unknown_push_blocks_retirement_until_independent_readback_confirms_actual_delivery(push_case,pipeline_case):
    peer,_health,path,task,database,_config=pipeline_case
    publish,push,readback,guard,_workspace,_request,_database,pushes,_remote,_ref=push_case
    with pytest.raises(PipelineError,match='unknown'):
        publish.publish(final_guard=guard,push=push,readback=lambda:False)
    assert readback() and len(pushes)==1 and publish.status()=='sending'
    state=ledger(peer,path);root=state.attempts[0]
    retirement=Retirement(database.DB_PATH,root,state.attempt_phases(root))
    with pytest.raises(PipelineError,match='terminal'):retirement.observe()
    database.mark_failed(task.id,'controlled publication response/readback interruption',exit_code=1)
    observed=retirement.observe()
    with pytest.raises(PipelineError,match='unknown'):retirement.retire(observed['evidence'])
    with pytest.raises(PipelineError,match='unknown'):retirement.confirm_publication(lambda _value:False)
    observed=retirement.confirm_publication(lambda value:value==publish.value and readback())
    assert observed['evidence']['publication']['outcome']=='confirmed' and len(pushes)==1
    retirement.retire(observed['evidence'])
    with pytest.raises(PipelineError,match='retired'):
        Push(database.DB_PATH,publish.value).prepare()
