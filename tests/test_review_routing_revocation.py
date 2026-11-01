"""Controlled DELETE and authenticated synthetic readback of exact author node."""
from contextlib import closing
import sqlite3

import pytest

from promptpilot import review_routing_fix_request as requests
from promptpilot.pipeline_errors import PipelineError
from promptpilot.review_routing_assignment import Assignment
from promptpilot.review_routing_revocation import Revocation,TABLE
from promptpilot.review_routing_snapshot import stable_timeline
from promptpilot import project_pipeline as pp
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401
from test_review_routing_fix import author_bridge as author_bridge,ledger  # noqa: F401


@pytest.fixture
def revocation_case(author_bridge):
    peer,_health,path,task,run,database=author_bridge
    code,selected=run('next','fix');assert code==0,selected
    code,done=run('complete','fix','--lease',selected['lease']);assert code==0,done
    request=requests.decode(selected['lease'])
    state=ledger(peer,path);root=state.attempts[0]
    observer=Revocation(database.DB_PATH,root,request['author'])
    database.mark_completed(task.id,'author handoff completed',exit_code=0)
    identity=stable_timeline(peer,pp.load_config(str(path)),42)['identity']
    original=peer.json;deletes=[];mode={'fault':None}
    def api(*args,input_value=None):
        if args==('api','repos/owner/repo/issues/42/assignees','--method','DELETE','--input','-'):
            assert input_value=={'assignees':[request['author']['login']]}
            deletes.append(input_value)
            if mode['fault']!='unknown':peer.assignees=[user for user in peer.assignees if user['node_id']!=request['author']['id']]
            if mode['fault'] in {'unknown','lost-response'}:raise OSError('DELETE response unavailable')
            return {'assignees':peer.assignees}
        return original(*args,input_value=input_value)
    peer.json=api
    return peer,database,observer,identity,deletes,mode,request,root


@pytest.mark.parametrize('fault',[None,'lost-response','already-unassigned'])
def test_revocation_seals_original_delivery_and_confirms_exact_node_once(revocation_case,fault):
    peer,database,observer,identity,deletes,mode,request,root=revocation_case
    mode['fault']=fault
    if fault=='already-unassigned':peer.assignees=[user for user in peer.assignees if user['node_id']!=request['author']['id']]
    before=observer.observe();assert before['confirmed'] is False
    digest=observer.revoke(peer,identity,final_guard=lambda:True)
    assert len(digest)==64 and len(deletes)==1 and observer.observe()['confirmed']
    assert observer.revoke(peer,identity,final_guard=lambda:pytest.fail('second DELETE guard'))==digest
    assert len(deletes)==1 and peer.assignees==[{'node_id':'U_OTHER','login':'other'}]
    with pytest.raises(PipelineError,match='revoked'):
        Assignment(database.DB_PATH).deliver(peer,request,root.id,None,allow_new=True)


def test_unknown_delete_is_never_repeated_from_task_end_or_lease_expiry(revocation_case):
    peer,_database,observer,identity,deletes,mode,_request,_root=revocation_case
    mode['fault']='unknown'
    for _ in range(2):
        with pytest.raises(PipelineError):observer.revoke(peer,identity,final_guard=lambda:True)
    assert len(deletes)==1 and observer.observe()['confirmed'] is False


def test_lost_revocation_table_preserves_unknown_and_never_reinitializes_delete(revocation_case):
    peer,database,observer,identity,deletes,_mode,_request,_root=revocation_case
    observer.revoke(peer,identity,final_guard=lambda:True)
    with closing(sqlite3.connect(database.DB_PATH)) as connection:
        with connection:connection.execute('DROP TABLE '+TABLE)
    with pytest.raises(PipelineError,match='disappeared'):observer.observe()
    with pytest.raises(PipelineError):observer.revoke(peer,identity,final_guard=lambda:True)
    assert len(deletes)==1
