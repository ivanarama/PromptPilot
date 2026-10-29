"""Transport uncertainty is recovered by exact reads, never a second POST."""
import copy
import json
from pathlib import Path

import pytest

from promptpilot.pipeline_errors import PipelineError
from promptpilot.review_routing_journal import Journal
from promptpilot.review_routing_transport import Transport
from promptpilot.review_routing_wire import render


PAYLOAD=json.loads((Path(__file__).parent/'fixtures/review-routing-wire-v1.json').read_text(encoding='utf-8'))['records'][0]['payload']
OWNER=PAYLOAD['owner']
CONFIG={'repository':'owner/repo','trusted_account':'owner'}


class Server:
    def __init__(self,*,lose_response=False,visible=True,returned_id='1000',actor='owner',edited=None,hold_at_guard=None):
        self.posts=0
        self.guards=0
        self.lose_response=lose_response
        self.visible=visible
        self.returned_id=returned_id
        self.actor=actor
        self.edited=edited
        self.hold_at_guard=hold_at_guard
        self.body=None

    def guard(self,payload):
        self.guards+=1
        if self.hold_at_guard and self.guards>=self.hold_at_guard:
            raise PipelineError('phase gate closed by late hold')
        edges=[]
        if self.body is not None and self.visible:
            edges=[{'cursor':'COMMENT','node':{'__typename':'IssueComment','fullDatabaseId':'1000',
                    'createdAt':'2026-10-29T00:00:00Z','lastEditedAt':self.edited,
                    'author':{'login':self.actor},'body':self.body}}]
        return {'pr_key':payload['pr'],'edges':edges}

    def json(self,*args,input_value=None):
        assert args==('api','repos/owner/repo/issues/42/comments','--method','POST','--input','-')
        self.posts+=1
        self.body=input_value['body']
        if self.lose_response:
            raise PipelineError('connection lost after acceptance')
        return {'id':self.returned_id}


def invoke(server,tmp_path):
    return Transport(server,CONFIG,Journal(tmp_path),server.guard).publish(PAYLOAD,owner=OWNER,phase='publication')


def test_normal_publication_and_lost_response_with_visible_receipt_have_one_post(tmp_path):
    for index,server in enumerate([Server(),Server(lose_response=True)]):
        directory=tmp_path/str(index)
        assert invoke(server,directory).id=='1000'
        assert invoke(server,directory).id=='1000'
        assert server.posts==1


def test_accepted_but_not_yet_visible_request_stays_ambiguous_across_process_restarts(tmp_path):
    server=Server(lose_response=True,visible=False)
    for _attempt in range(2):
        with pytest.raises(PipelineError,match='ambiguous|unique authenticated'):
            invoke(server,tmp_path)
    assert server.posts==1
    server.visible=True
    assert invoke(server,tmp_path).id=='1000'
    assert server.posts==1


@pytest.mark.parametrize('hold_at', [1,2])
def test_gate_failure_before_post_never_sends_the_prepared_body(tmp_path,hold_at):
    server=Server(hold_at_guard=hold_at)
    with pytest.raises(PipelineError,match='late hold'):
        invoke(server,tmp_path)
    assert server.posts==0


def test_late_hold_after_post_stops_following_phases_and_does_not_erase_intent(tmp_path):
    server=Server(hold_at_guard=3)
    with pytest.raises(PipelineError,match='late hold'):
        invoke(server,tmp_path)
    assert server.posts==1
    assert Journal(tmp_path).get(owner=OWNER,phase='publication')['state']=='pending'


@pytest.mark.parametrize('actor,edited', [('contributor',None),('owner','2026-10-29T00:01:00Z')])
def test_untrusted_or_edited_server_record_cannot_complete_local_request(tmp_path,actor,edited):
    server=Server(actor=actor,edited=edited)
    with pytest.raises(PipelineError):
        invoke(server,tmp_path)
    assert server.posts==1
    assert Journal(tmp_path).get(owner=OWNER,phase='publication')['state']=='pending'


def test_contradicting_returned_id_cannot_be_forgotten_on_a_later_recovery(tmp_path):
    server=Server(returned_id='1001')
    for _attempt in range(2):
        with pytest.raises(PipelineError,match='returned ID differs'):
            invoke(server,tmp_path)
    assert server.posts==1
    saved=Journal(tmp_path).get(owner=OWNER,phase='publication')
    assert saved['state']=='pending' and saved['returned_id']=='1001'


def test_missing_stage_guard_or_wrong_pr_identity_cannot_publish(tmp_path):
    server=Server()
    with pytest.raises(PipelineError,match='fresh stage guard'):
        Transport(server,CONFIG,Journal(tmp_path),None)
    def wrong(payload):
        changed=copy.deepcopy(payload['pr']);changed['pr_id']='OTHER'
        return {'pr_key':changed,'edges':[]}
    with pytest.raises(PipelineError,match='another PR identity'):
        Transport(server,CONFIG,Journal(tmp_path),wrong).publish(PAYLOAD,owner=OWNER,phase='publication')
    assert server.posts==0


def test_oversized_evidence_is_rejected_before_guard_or_journal_or_post(tmp_path):
    from promptpilot.review_routing import inspect_report
    payload=copy.deepcopy(PAYLOAD)
    payload['report']['change']='x'*60000
    payload['report_sha256']=inspect_report(payload['report'],payload['identity']['head_sha'])['report_sha256']
    server=Server()
    with pytest.raises(PipelineError,match='not truncated'):
        Transport(server,CONFIG,Journal(tmp_path),server.guard).publish(payload,owner=OWNER,phase='publication')
    assert server.guards==0 and server.posts==0
    assert Journal(tmp_path).get(owner=OWNER,phase='publication') is None
    assert render(PAYLOAD)


def test_real_github_client_supplies_json_stdin_to_explicit_api_input(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from promptpilot import project_pipeline as pp
    server=Server()
    def command(args,**kwargs):
        # Exercise the real GitHub.json/run boundary. input_value alone does
        # not instruct gh api to read stdin; --input - is mandatory.
        assert args[0]=='fixture-gh' and args[1]=='api'
        assert '--input' in args and args[args.index('--input')+1]=='-'
        assert any(value.endswith('/issues/42/comments') for value in args)
        sent=json.loads(kwargs['input'])
        server.body=sent['body'];server.posts+=1
        return SimpleNamespace(returncode=0,stdout=json.dumps({'id':'1000'}),stderr='')
    monkeypatch.setattr(pp.subprocess,'run',command)
    github=pp.GitHub(executable='fixture-gh')
    record=Transport(github,CONFIG,Journal(tmp_path),server.guard).publish(PAYLOAD,owner=OWNER,phase='publication')
    assert record.id=='1000' and server.posts==1
