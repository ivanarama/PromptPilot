"""Positive typed REVIEW proofs for MERGE/TAIL; never legacy conversion.

This reader establishes source structure from a caller's complete snapshot.
Its caller still proves authenticated transport, paired readers, live policy,
identity, ship, CI, single-flight and mutation ownership at each write boundary.
"""
from __future__ import annotations

from .pipeline_errors import PipelineError
from .review_routing import VERSION
from .review_routing_baseline import _edges
from .review_routing_history import verified_depth
from .review_routing_wire import records

CONTRACT = 'positive-review-v1'
LIFECYCLE = {'PullRequestCommit', 'HeadRefForcePushedEvent',
             'HeadRefDeletedEvent', 'HeadRefRestoredEvent',
             'BaseRefChangedEvent', 'BaseRefForcePushedEvent',
             'BaseRefDeletedEvent', 'MergedEvent'}


def capability(config, health):
    from .review_routing_policy import preflight

    policy = preflight(config, health)
    expected = {'protocol': CONTRACT, 'snapshot': 'complete',
                'purposes': ['merge', 'tail']}
    if not policy['enabled'] or health.get('positive_review_contract') != expected:
        raise PipelineError('positive typed REVIEW requires its accepted paired consumer contract')
    return policy


def inspect(snapshot, *, trusted, merged=False):
    """Return an actual typed proof, or refuse; no synthetic legacy marker.

    For TAIL, the cut is the observed MergedEvent of this exact current HEAD.
    Historical audit bindings are verified against the full actual timeline;
    no former HEAD/epoch is reconstructed from today's commit timestamps.
    """
    from . import project_pipeline as pp

    if (snapshot.get('state') != ('MERGED' if merged else 'OPEN')
            or snapshot.get('baseRefName') != 'main'
            or snapshot.get('isDraft') is not False
            or snapshot.get('labelsComplete') is not True):
        raise PipelineError('positive typed REVIEW lifecycle/label snapshot is incomplete')
    edges = _edges(snapshot)
    verified = verified_depth(snapshot, trusted=trusted)
    ledger = verified['ledger']
    if not ledger.proofs or ledger.parking_id is not None:
        raise PipelineError('positive typed REVIEW has no completed unparked source')
    proof = ledger.proofs[-1]
    if (proof.outcome != 'reviewed' or proof.source['head'] != snapshot['headRefOid']
            or proof.publication.payload['identity'] != snapshot['identity']
            or proof.publication.payload['author'] != snapshot['author']):
        raise PipelineError('positive typed REVIEW source differs from current identity/author/HEAD')
    history, _ = records(snapshot, trusted=trusted, expected_pr=snapshot['pr_key'])
    completed = {item.publication.id for item in ledger.proofs}
    if any(item.payload['kind'] == 'publication'
           and item.id not in completed and item.id not in ledger.ignored_ids
           for item in history):
        raise PipelineError('positive typed REVIEW has an unfinished publication')
    merges = [i for i, edge in enumerate(edges)
              if edge['node']['__typename'] == 'MergedEvent']
    if (len(merges) != 1 if merged else bool(merges)):
        raise PipelineError('positive typed REVIEW requires exactly its observed merge lifecycle')
    cut = merges[0] if merged else len(edges)
    prefix = dict(snapshot, edges=edges[:cut])
    info = pp.epoch(prefix, trusted)
    pp.validate_epoch_safety(info, trusted)
    if (proof.source['epoch'], proof.publication.payload['anchor']) != (info['hash'], info['anchor_id']):
        raise PipelineError('positive typed REVIEW is not the current server epoch')
    by_id = {str(edge['node'].get('fullDatabaseId')): (i, edge)
             for i, edge in enumerate(edges)
             if edge['node']['__typename'] == 'IssueComment'}
    chain = [by_id[item.id] for item in (proof.publication, proof.claim, proof.completion)]
    if not info['anchor_index'] < chain[0][0] < chain[1][0] < chain[2][0] < cut:
        raise PipelineError('positive typed REVIEW proof does not precede the exact merge boundary')
    for edge in edges[chain[2][0] + 1:]:
        node = edge['node']
        if (node['__typename'] == 'IssueComment'
                and (node.get('author') or {}).get('login') == trusted
                and pp.has_review_again(node.get('body') or '')):
            raise PipelineError('positive typed REVIEW was superseded by a later override')
    merge_sha = None
    if merged:
        from .review_routing_lease import timestamp
        timestamp(edges[cut]['node'].get('createdAt'))
        merge_sha = (edges[cut]['node'].get('commit') or {}).get('oid')
        from .pr_identity import SHA
        if not isinstance(merge_sha, str) or not SHA.fullmatch(merge_sha):
            raise PipelineError('positive typed REVIEW merged commit is unavailable')
        lifecycle = [edge['node']['__typename'] for edge in edges[cut + 1:]
                     if edge['node']['__typename'] in LIFECYCLE]
        if lifecycle not in ([], ['HeadRefDeletedEvent']):
            raise PipelineError('positive typed REVIEW has unsupported post-merge lifecycle')
    descriptor = {'protocol': VERSION, 'kind': 'positive-review', 'outcome': 'reviewed',
                  'source': proof.source, 'identity': snapshot['identity'],
                  'author': snapshot['author'], 'anchor': info['anchor_id'],
                  'nodes': {name: edge['node']['id'] for name, (_i, edge)
                            in zip(('publication', 'claim', 'completion'), chain)}}
    return {'proof': proof, 'descriptor': descriptor, 'merge_sha': merge_sha,
            'merge_event': edges[cut]['node']['id'] if merged else None,
            'mutation_authorized': False}


def has_history(snapshot, trusted):
    """Unknown actors on protocol-looking comments also close the legacy path."""
    for edge in snapshot['edges']:
        node = edge['node']
        if node['__typename'] != 'IssueComment':
            continue
        login = (node.get('author') or {}).get('login')
        if ('<!-- pp:review-routing-' in (node.get('body') or '')
                and (not isinstance(login, str) or not login.strip() or login == trusted)):
            return True
    return False


def live(gh, config, number, *, purpose, health=None):
    """Authenticated read-only context, repeated by each actual write gate."""
    from . import project_pipeline as pp
    from .review_routing_snapshot import stable_timeline
    from .review_routing_wire import sha

    if purpose not in {'merge', 'tail', 'cleanup'}:
        raise PipelineError('unknown positive typed REVIEW consumer purpose')
    health = pp.run_health(config) if health is None else health
    policy = capability(config, health)
    snapshot = stable_timeline(gh, config, number)
    merged = purpose != 'merge'
    result = inspect(snapshot, trusted=config['trusted_account'], merged=merged)
    labels = set(snapshot['labels'])
    if labels & {'hold', 'manual', 'needs-decision', 'changes-requested'}:
        raise PipelineError('positive typed REVIEW entered a stopped route')
    if purpose == 'merge':
        pp.validate_common(snapshot, config, {'head': result['descriptor']['source']['head']})
        info = pp.epoch(snapshot, config['trusted_account'])
        if 'ship' not in labels or not pp.trusted_ship_authorized(info, config['trusted_account']):
            raise PipelineError('positive typed MERGE requires trusted current-epoch ship')
    else:
        pr = gh.json('api', f"repos/{config['repository']}/pulls/{number}")
        if not isinstance(pr, dict):
            raise PipelineError('positive typed REVIEW merged PR response is incomplete')
        obj = lambda value: value if isinstance(value, dict) else {}
        head, base, author = obj(pr.get('head')), obj(pr.get('base')), obj(pr.get('user'))
        head_repo, base_repo = obj(head.get('repo')), obj(base.get('repo'))
        identity = dict(base_repository_id=base_repo.get('node_id'), base_repository=base_repo.get('full_name'),
                        base_ref=base.get('ref'), pr_id=pr.get('node_id'), pr_number=pr.get('number'),
                        head_repository_id=head_repo.get('node_id'), head_repository=head_repo.get('full_name'),
                        head_ref=head.get('ref'), head_sha=head.get('sha'),
                        cross_repository=base_repo.get('node_id') != head_repo.get('node_id'))
        if (pr.get('merged') is not True or pr.get('state') != 'closed'
                or type(pr.get('number')) is not int or pr['number'] != number
                or identity != snapshot['identity']
                or dict(id=author.get('node_id'), login=author.get('login')) != snapshot['author']
                or (pr.get('head') or {}).get('sha') != snapshot['headRefOid']
                or (pr.get('base') or {}).get('ref') != config['base_branch']
                or pr.get('merge_commit_sha') != result['merge_sha']):
            raise PipelineError('positive typed REVIEW REST and observed merge disagree')
        from .review_routing_lease import timestamp
        merge_node = next(edge['node'] for edge in snapshot['edges'] if edge['node']['id'] == result['merge_event'])
        if timestamp(pr.get('merged_at')) != timestamp(merge_node.get('createdAt')):
            raise PipelineError('positive typed REVIEW REST and observed merge time disagree')
    if purpose == 'tail' and 'no-tail' in labels:
        raise PipelineError('positive typed TAIL was declined by the owner')
    proof = result['proof']
    items = []
    for index, item in enumerate(proof.publication.payload['report']['tail'], 1):
        items.append(dict(item, index=index, item_sha256=sha(item)))
    return {'protocol': VERSION, 'purpose': 'read-only-positive-' + purpose,
            'number': number, 'head': snapshot['headRefOid'],
            'source': result['descriptor'], 'source_sha256': sha(result['descriptor']),
            'publication_created_at': proof.publication.created_at,
            'merge_sha': result['merge_sha'], 'merge_event': result['merge_event'],
            'tail': items, 'reader_sha256': policy['consumer_sha256'],
            'mutation_authorized': False, 'snapshot': snapshot}
