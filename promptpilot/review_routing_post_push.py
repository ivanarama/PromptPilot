"""Read-only Git graph and source-trailer evidence for a technical FIX.

This is not a completion/launch permit. Its caller must independently prove the
canonical REVIEW/attempt, executor, full timeline, task, ownership and live gates,
then repeat them immediately before any publication. No text is executed here.
"""
from .pipeline_errors import PipelineError
from .pr_identity import PRIdentityV1, identity_from_rest
from .review_routing_lease import decimal_id
from .review_routing_wire import context, head40, pr_key, sha, validate


def trailers(attempt, attempt_id):
    validate(attempt)
    if attempt['kind'] != 'attempt':
        raise PipelineError('post-push evidence requires a technical attempt')
    decimal_id(attempt_id)
    return {
        'PP-Fix-Transition': 'review-routing-v1/' + attempt_id,
        'PP-Technical-Fix-Source': sha(attempt['source']),
        'PP-Technical-Fix-Context': context(attempt),
    }


def verify(gh, config, snapshot, attempt, attempt_id, new_head):
    """Verify a finite forward graph, exact current REST identity and trailers.

    This observes a push; it does not attest that an outside author's push used
    CAS. The future pipeline push writer still needs its own expected-HEAD CAS.
    """
    expected = trailers(attempt, attempt_id)
    head40(new_head)
    old_head = attempt['source']['head']
    current = snapshot['identity']
    original = dict(current, head_sha=old_head)
    if (new_head == old_head or current['head_sha'] != new_head
            or snapshot['headRefOid'] != new_head
            or pr_key(PRIdentityV1(**current)) != attempt['pr']
            or PRIdentityV1(**original).sha256 != attempt['source']['identity_sha256']):
        raise PipelineError('post-push current HEAD or lifetime PR identity changed')
    repository = config['repository']
    comparison = gh.json('api', f'repos/{repository}/compare/{old_head}...{new_head}?per_page=100')
    if not isinstance(comparison, dict):
        raise PipelineError('post-push graph response is incomplete')
    total = comparison.get('total_commits')
    commits = comparison.get('commits')
    base, merge_base = comparison.get('base_commit'), comparison.get('merge_base_commit')
    if (type(total) is not int or not 1 <= total <= 100
            or type(comparison.get('ahead_by')) is not int or comparison['ahead_by'] != total
            or type(comparison.get('behind_by')) is not int or comparison['behind_by'] != 0
            or comparison.get('status') != 'ahead'
            or not isinstance(base, dict) or base.get('sha') != old_head
            or not isinstance(merge_base, dict) or merge_base.get('sha') != old_head
            or not isinstance(commits, list) or len(commits) != total
            or not all(isinstance(item, dict) for item in commits)
            or commits[-1].get('sha') != new_head):
        raise PipelineError('post-push requires a complete finite forward graph from its exact source')
    for item in commits:
        head40(item.get('sha'))
    if len({item['sha'] for item in commits}) != total:
        raise PipelineError('post-push graph duplicates commits')
    commit = gh.json('api', f'repos/{repository}/commits/{new_head}')
    if not isinstance(commit, dict) or commit.get('sha') != new_head:
        raise PipelineError('post-push commit response belongs to another HEAD')
    content = commit.get('commit')
    message = content.get('message') if isinstance(content, dict) else None
    if not isinstance(message, str) or len(message.encode('utf-8')) > 65536:
        raise PipelineError('post-push commit message is missing or oversized')
    lines = message.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    last_break = max((index for index, line in enumerate(lines) if not line.strip()), default=-1)
    block = lines[last_break+1:]
    for key, value in expected.items():
        matches = [line for line in lines if line.startswith(key + ':')]
        if matches != [key + ': ' + value] or matches[0] not in block:
            raise PipelineError('post-push requires unique terminal source/attempt trailers')
    # A metadata read after graph/commit I/O detects ordinary concurrent pushes
    # and ref/repository swaps. The eventual mutation guard must repeat its own
    # complete stable timeline and clock checks after every intervening wait.
    pull = gh.json('api', f'repos/{repository}/pulls/{attempt["pr"]["pr_number"]}')
    if identity_from_rest(pull, repository=repository,
                          repository_id=current['base_repository_id']).to_dict() != current:
        raise PipelineError('post-push PR identity changed during graph verification')
    return dict(new_head=new_head, transition=expected['PP-Fix-Transition'],
                source_trailer=expected['PP-Technical-Fix-Source'])
