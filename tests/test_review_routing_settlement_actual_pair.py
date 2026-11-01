"""Administrative settlement/resume keeps the real Go allowlist through readback."""
import json

import pytest

from promptpilot import worker
from test_review_routing_actual_onebase_pair import adapter as adapter, synthetic_adapter as synthetic_adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401
from test_review_routing_fix import author_bridge as author_bridge, ledger  # noqa: F401
from test_review_routing_pipeline_fix import pipeline_case as pipeline_case  # noqa: F401
from test_review_routing_technical_worker import technical_worker as technical_worker  # noqa: F401
from test_review_routing_settlement_public import settlement_case as settlement_case  # noqa: F401


@pytest.mark.parametrize('author_bridge', ['real-source'], indirect=True)
def test_actual_checker_settlement_and_resume_remain_confirmable(settlement_case):
    case=settlement_case
    peer=case['peer'];database=case['database'];task=case['task']
    before=list(peer.posts)
    worker._execute_task_body(task)
    saved=database.get_task(task.id)
    assert saved.status.value=='completed' and saved.verdict=='ГОТОВО', saved.error or saved.result
    assert peer.posts[len(before):]==['lease','settlement','resume']
    state=ledger(peer,case['path'])
    assert state.budget_used==len(state.attempts)==1 and state.resume_id is not None
    assert len(case['factories'])==len(case['pushes'])==1
    actual=peer.actual_pair
    matching=[item for snapshot in actual['evidence']
              for item in snapshot.get('technical_fix_settlement_candidates',[])
              if item['number']==42 and item['head']==case['new_head']
              and item['attempt_id']==case['root'].id]
    assert matching, 'real checker did not retain the administrative target'
    receipt=dict(binary_sha256=actual['binary_sha256'],budget_used=1,
        factories=len(case['factories']),cas_sends=len(case['pushes']),
        administrative_posts=peer.posts[len(before):],resume_id=state.resume_id,
        current_head=case['new_head'],old_attempt_id=case['root'].id,
        old_source=case['root'].payload['source'],actual_checker=True,
        provider_for_settlement=False,production_activation=False)
    (actual['fixture_root']/'actual-settlement-pair-receipt.json').write_text(
        json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
