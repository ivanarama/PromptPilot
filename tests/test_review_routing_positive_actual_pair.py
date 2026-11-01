"""Paired B acceptance with the actual pinned Go and four real consumer trees."""
import json

import pytest

from test_review_routing_actual_onebase_pair import adapter as adapter, synthetic_adapter as synthetic_adapter  # noqa: F401
from test_review_routing_positive import positive as positive  # noqa: F401
from test_review_routing_positive_merge import merge_case as merge_case  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401


@pytest.mark.parametrize('fault', [None, 'lost-merge-response', 'late-reader'])
def test_actual_go_positive_merge_and_tail_use_all_four_real_consumers(merge_case, fault):
    peer, _unused_synthetic_health, path, run = merge_case
    pair = peer.actual_pair
    code, selected = run('next', 'merge')
    assert code == 0 and selected['action'] == 'merge', selected
    if fault == 'lost-merge-response':
        peer.merge_failure = 'lost-accepted'
    elif fault == 'late-reader':
        peer.after_intent = lambda: (path.parent / '.claude/skills/tail-issues/SKILL.md').write_text(
            'incompatible late consumer', encoding='utf-8')
    code, completed = run('complete', 'merge', '--lease', selected['lease'])
    if fault == 'lost-merge-response':
        assert code == 2 and peer.merge_sends == 1, completed
        code, recovery = run('next', 'merge')
        assert code == 0 and recovery['action'] == 'cleanup', recovery
        code, completed = run('complete', 'merge-cleanup', '--lease', recovery['lease'])
    if fault == 'late-reader':
        assert code == 2 and peer.merge_sends == 0, completed
    else:
        assert code == 0 and completed['action'] == 'completed' and peer.merge_sends == 1, completed
        code, tail = run('inspect-routing-positive', '--number', '42', '--stage', 'tail')
        assert code == 0 and tail['merge_sha'] == 'b' * 40 and tail['tail'][0]['kind'] == 'issue', tail
    assert any(result.get('positive_review_contract', {}).get('protocol') == 'positive-review-v1'
               and result.get('positive_merge_candidates') for result in pair['evidence'])
    for result in pair['evidence']:
        contract = result.get('review_routing_contract')
        if contract is not None:
            assert contract['reader_sha256'] == pair['source_reader_sha256']
    receipt = dict(binary_sha256=pair['binary_sha256'], reader_sha256=pair['source_reader_sha256'],
                   fault=fault, actual_checker=True, github_transport='private executable GraphQL peer',
                   merge_sends=peer.merge_sends, native_posts=len(peer.native_posts),
                   provider_calls=0, production_activation=False)
    (pair['fixture_root'] / ('qualified-B-positive-' + str(fault) + '.json')).write_text(
        json.dumps(receipt, sort_keys=True), encoding='utf-8')
