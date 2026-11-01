"""Public heartbeat with real scheduler, owned child and canonical peer ledger."""
from contextlib import redirect_stdout
from datetime import timedelta
from io import StringIO
import json
import subprocess
import sys

import pytest

from promptpilot import project_pipeline as pp
from promptpilot import review_routing_heartbeat as heartbeat
from promptpilot import review_routing_start_request as starts
from promptpilot.pipeline_errors import PipelineError
from promptpilot.process_tree import OwnedProcess
from promptpilot.review_routing_execution import OneUseStart,TABLE
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401
from test_review_routing_fix import author_bridge as author_bridge,ledger  # noqa: F401
from test_review_routing_pipeline_fix import pipeline_case as pipeline_case,selected,issue  # noqa: F401
from test_review_routing_start import metadata_launch
from test_review_routing_execution import update


@pytest.fixture
def producer(pipeline_case,monkeypatch):
    peer,_health,path,_task,database,_config=pipeline_case
    prepared=issue(pipeline_case,selected(pipeline_case))
    request=starts.decode(prepared['lease']);scope=request['scope']
    request['expires_at']=request['issued_at']+300
    prepared['lease']=starts.encode(request)
    store=OneUseStart(database.DB_PATH,create=False,allow_new=False)
    tree=metadata_launch(peer,str(path),prepared['lease'],factory=lambda:OwnedProcess.start(
        [sys.executable,'-c','import time; time.sleep(120)'],stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
    nonce='3'*64
    store.observe_started(scope,tree.process.pid,live_nonce=nonce)
    monkeypatch.setenv(heartbeat.NONCE_ENV,nonce)
    monkeypatch.setattr(heartbeat,'_now',lambda:peer.now)
    def cli(command,token):
        stream=StringIO()
        with redirect_stdout(stream):code=pp.run(['--config',str(path),command,'fix','--lease',token])
        return code,json.loads(stream.getvalue())
    try:yield peer,path,database,scope,store,tree,prepared['lease'],cli
    finally:
        store.confirm_stopped(scope,tree)
        tree.close();tree.process.wait(timeout=10)


def advance(case,minutes):
    peer,_path,database,_scope,_store,_tree,_start,_cli=case
    peer.now+=timedelta(minutes=minutes)
    update(database.DB_PATH,'UPDATE pipeline_target_reservations SET expires_at=?',
           ((peer.now+timedelta(hours=2)).isoformat(),))


def test_public_heartbeat_renews_only_original_owner_after_factory_token_expiry(producer):
    peer,path,_database,scope,store,_tree,start,cli=producer
    code,result=cli('prepare-fix-heartbeat',start)
    assert code==0 and result['action']=='heartbeat-prepared',result
    with pytest.raises(PipelineError):starts.decode(result['lease'])
    code,unchanged=cli('renew-fix-heartbeat',result['lease'])
    assert code==0 and unchanged['action']=='heartbeat-unchanged',unchanged
    before=list(peer.posts)
    advance(producer,26)
    # Expired START is only a lookup; this cannot give it factory authority.
    with pytest.raises(PipelineError):starts.require_current(starts.decode(start),pp.load_config(str(path)))
    code,result=cli('prepare-fix-heartbeat',start)
    assert code==0,result
    code,renewed=cli('renew-fix-heartbeat',result['lease'])
    assert code==0 and renewed['action']=='heartbeat-renewed',renewed
    assert len(peer.posts)==len(before)+1
    state=ledger(peer,path)
    assert state.budget_used==len(state.attempts)==1
    assert state.attempt_phases(state.attempts[0]).keys()>={'started','assigned','handoff-done'}
    with pytest.raises(PipelineError):store.invoke(scope,final_guard=lambda _scope:True,
        factory=lambda:pytest.fail('heartbeat reauthorized a second provider'))


@pytest.mark.parametrize('fault',['nonce','cancel','expired-owner','deadline'])
def test_public_heartbeat_refuses_lost_producer_and_never_revives_owner(producer,monkeypatch,fault):
    peer,_path,database,scope,_store,_tree,start,cli=producer
    advance(producer,31 if fault=='expired-owner' else 26)
    before=list(peer.posts)
    if fault=='nonce':monkeypatch.setenv(heartbeat.NONCE_ENV,'4'*64)
    elif fault=='cancel':database.request_cancel(scope['task_id'])
    elif fault=='deadline':monkeypatch.setattr(heartbeat,'deadline',lambda *_:int(peer.now.timestamp()))
    code,result=cli('prepare-fix-heartbeat',start)
    assert code==2 and result['action']=='error',result
    assert peer.posts==before


def test_heartbeat_rechecks_private_proof_after_journal_writer_wait(producer,monkeypatch):
    from promptpilot.review_routing_journal import Journal
    peer,_path,database,scope,_store,_tree,start,cli=producer
    advance(producer,26)
    code,result=cli('prepare-fix-heartbeat',start)
    assert code==0,result
    original=Journal.arm
    def arm(journal,*args,**kwargs):
        value=original(journal,*args,**kwargs)
        key=TABLE+':live:'+OneUseStart._keys(scope)[0]
        update(database.DB_PATH,'DELETE FROM settings WHERE key=?',(key,))
        return value
    monkeypatch.setattr(Journal,'arm',arm)
    before=list(peer.posts)
    code,result=cli('renew-fix-heartbeat',result['lease'])
    assert code==2 and result['action']=='error',result
    assert peer.posts==before
