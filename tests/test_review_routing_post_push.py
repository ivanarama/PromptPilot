"""Read-only source evidence; not public FIX completion or paired acceptance."""
import copy

import pytest

from promptpilot.pipeline_errors import PipelineError
from promptpilot.review_routing_post_push import trailers, verify
from test_review_routing_proof import history


@pytest.fixture
def evidence():
    records = history()
    root = records[3]
    identity = dict(records[0].payload['identity'], head_sha='b'*40)
    expected = trailers(root.payload, root.id)
    message = 'Complete the source-bound correction\n\n' + '\n'.join(k+': '+v for k,v in expected.items())
    responses = {
        'repos/owner/repo/compare/'+'a'*40+'...'+'b'*40+'?per_page=100': dict(
            status='ahead', ahead_by=1, behind_by=0, total_commits=1,
            base_commit={'sha':'a'*40}, merge_base_commit={'sha':'a'*40}, commits=[{'sha':'b'*40}]),
        'repos/owner/repo/commits/'+'b'*40: dict(sha='b'*40, commit={'message':message}),
        'repos/owner/repo/pulls/42': dict(node_id=identity['pr_id'], number=42,
            base=dict(ref=identity['base_ref'],repo=dict(node_id=identity['base_repository_id'],full_name=identity['base_repository'])),
            head=dict(ref=identity['head_ref'],sha=identity['head_sha'],repo=dict(node_id=identity['head_repository_id'],full_name=identity['head_repository']))),
    }
    class Peer:
        calls=[]
        def json(self,*args):
            assert len(args)==2 and args[0]=='api'
            self.calls.append(args[1])
            return copy.deepcopy(responses[args[1]])
    peer=Peer()
    def run():
        return verify(peer, {'repository':'owner/repo'}, {'identity':identity,'headRefOid':identity['head_sha']},
                      root.payload, root.id, 'b'*40)
    return root, identity, expected, responses, peer, run


def test_source_evidence_reads_graph_commit_then_exact_current_identity(evidence):
    root,_identity,expected,_responses,peer,run=evidence
    result=run()
    assert result==dict(new_head='b'*40,transition=expected['PP-Fix-Transition'],source_trailer=expected['PP-Technical-Fix-Source'])
    assert len(peer.calls)==3 and peer.calls[-1]=='repos/owner/repo/pulls/42'
    assert root.payload['source']['head']=='a'*40


@pytest.mark.parametrize('fault',['diverged','wrong-base','truncated','boolean-count','wrong-head','unknown-shape'])
def test_nonforward_or_incomplete_graph_is_not_source_evidence(evidence,fault):
    _root,_identity,_expected,responses,_peer,run=evidence
    graph=next(value for key,value in responses.items() if '/compare/' in key)
    if fault=='diverged':graph['status']='diverged'
    elif fault=='wrong-base':graph['merge_base_commit']['sha']='c'*40
    elif fault=='truncated':graph['total_commits']=graph['ahead_by']=2
    elif fault=='boolean-count':graph['total_commits']=True
    elif fault=='wrong-head':graph['commits'][0]['sha']='c'*40
    else:graph['base_commit']=[]
    with pytest.raises(PipelineError):run()


@pytest.mark.parametrize('fault',['source','attempt','context','duplicate','nonterminal-lf','nonterminal-crlf'])
def test_copied_or_nonterminal_trailers_do_not_complete_a_source(evidence,fault):
    _root,_identity,expected,responses,_peer,run=evidence
    commit=responses['repos/owner/repo/commits/'+'b'*40]['commit']
    if fault in {'source','attempt','context'}:
        key={'source':'PP-Technical-Fix-Source','attempt':'PP-Fix-Transition','context':'PP-Technical-Fix-Context'}[fault]
        commit['message']=commit['message'].replace(expected[key],'unrelated-source')
    elif fault=='duplicate':commit['message']+='\nPP-Fix-Transition: '+expected['PP-Fix-Transition']
    else:
        commit['message']+='\n\nTrailers above are only an example'
        if fault.endswith('crlf'):commit['message']=commit['message'].replace('\n','\r\n')
    with pytest.raises(PipelineError):run()


@pytest.mark.parametrize('fault',['head','ref','repository'])
def test_late_rest_identity_change_after_graph_io_is_rejected(evidence,fault):
    _root,_identity,_expected,responses,_peer,run=evidence
    head=responses['repos/owner/repo/pulls/42']['head']
    if fault=='head':head['sha']='c'*40
    elif fault=='ref':head['ref']='replacement'
    else:head['repo']={'node_id':'R_CHANGED','full_name':'other/repo'}
    with pytest.raises(PipelineError):run()
