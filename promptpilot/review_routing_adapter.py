"""Typed REVIEW adapter, gated by explicit opt-in and the complete paired readers.

Compatibility alone grants no execution or publication authority. FIX and the
positive MERGE/TAIL consumers retain their separate source-bound stage gates.
"""
from __future__ import annotations

import copy
import json
import sqlite3

from . import project_pipeline as pp
from . import review_routing_request as requests
from .pipeline_errors import PipelineError
from .review_routing import VERSION,inspect_file
from .review_routing_baseline import verify
from .review_routing_history import verified_depth
from .review_routing_journal import Journal
from .review_routing_policy import preflight
from .review_routing_publication import Publication
from .review_routing_snapshot import stable_timeline
from .review_routing_stage_guard import StageGuard
from .review_routing_wire import context,outcome,parse,records,render


def journal_directory(config):
    identity=pp.digest({'repository':config['repository'],'trusted_account':config['trusted_account']})
    return pp._merge_comment_index_path(config).parent/('routing-'+identity)


def _journal(config):
    try:return Journal(journal_directory(config))
    except sqlite3.Error as exc:
        raise PipelineError('typed REVIEW journal is unavailable; preserve pending send barriers') from exc


def _wait(item,reason,**details):
    return {'action':'wait','stage':'review','protocol':VERSION,'verdict':'ПУСТО',
            'number':item['number'],'reason':reason,**details}


def _recover_visible_pending(gh,config,snapshot,history,journal,pending):
    """Confirm local receipts from authenticated history; never POST as owner.

    The old signed task/request may be lost or expired. Exact read-back needs
    no new WRITE authority and must not strand a completed older HEAD forever.
    An invisible body or conflicting returned ID remains a durable barrier.
    """
    recovered=[]
    for row in pending:
        if (row['repository'],row['pr_number'])!=(config['repository'],snapshot['pr_key']['pr_number']):
            raise PipelineError('pending REVIEW receipt belongs to another repository/PR')
        payload=parse(row['body'])
        if payload['pr']!=snapshot['pr_key']:
            raise PipelineError('pending REVIEW receipt changes lifetime PR identity')
        matches=[record for record in history if render(record.payload)==row['body']]
        if not matches:return pending
        if len(matches)!=1 or row['returned_id'] is not None and row['returned_id']!=matches[0].id:
            raise PipelineError('pending REVIEW receipt has conflicting exact body or returned ID')
        recovered.append((row,matches[0]))
    if recovered:
        pp.ensure_identity(gh,config)
        try:
            for row,record in recovered:
                journal.confirm(owner=row['owner'],phase=row['phase'],returned_id=record.id,body=row['body'])
        except sqlite3.Error as exc:
            raise PipelineError('REVIEW receipt journal unavailable; preserve unresolved barriers') from exc
    return []


def _admit_audit(ledger,head):
    if ledger.resuming_publication_id is not None:
        raise PipelineError('an unfinished publication already consumed the engineering resume')
    resume=None
    if ledger.parking_id is not None:
        grant=next((item for item in ledger.resumes if item.id==ledger.resume_id),None)
        if grant is None or grant.payload['head']!=head:
            raise PipelineError('engineering parking requires its exact current HEAD resume')
        resume=grant.id
    if ledger.attempts:
        previous=ledger.attempts[-1]
        done=ledger.attempt_phases(previous).get('fix-done')
        settled = ledger.attempt_settlement(previous)
        if (done is None and (settled is None or resume is None)
                or done is not None and done.payload['details']['new_head']!=head and resume is None):
            raise PipelineError('new REVIEW must wait for completed post-push provenance')
    if ledger.proofs:
        last=ledger.proofs[-1]
        if last.outcome=='technical-handoff' and not any(item.payload['source']==last.source for item in ledger.attempts):
            raise PipelineError('technical REVIEW source still awaits its single FIX attempt')
    return resume


def next_review(gh,config,*,config_path,item,reservation,health):
    """Return None for the unchanged first two ordinary legacy rounds."""
    policy=preflight(config,health)
    if not policy['enabled']:return None
    if not isinstance(config_path,str) or not config_path:
        raise PipelineError('typed REVIEW requires its effective configuration path')
    snapshot=stable_timeline(gh,config,int(item['number']))
    pp.validate_common(snapshot,config,item)
    info=pp.epoch(snapshot,config['trusted_account'])
    pp.review_gate(snapshot,config,dict(head=item['head'],epoch=info['hash'],anchor=info['anchor_id']))
    verified=verified_depth(snapshot,trusted=config['trusted_account'])
    history,_deleted=records(snapshot,trusted=config['trusted_account'],expected_pr=snapshot['pr_key'])
    if verified['depth']<2 and not history:return None
    ledger=verified['ledger']
    current=(item['head'],info['hash'])
    roots={}
    for record in history:
        if record.payload['kind']=='publication':
            key=(record.payload['identity']['head_sha'],record.payload['epoch'])
            roots.setdefault(key,record)
    root=roots.get(current)
    completed={proof.publication.id for proof in ledger.proofs}
    directory=journal_directory(config)
    pending=[]
    if (directory/'review-routing-journal.sqlite3').exists():
        journal=_journal(config)
        try:pending=journal.pending_for(snapshot['pr_key'])
        except sqlite3.Error as exc:raise PipelineError('REVIEW receipt journal unavailable') from exc
        pending=_recover_visible_pending(gh,config,snapshot,history,journal,pending)
    if root is None and pending:
        return _wait(item,'unresolved local POST requires exact receipt recovery; no new audit')
    if root is not None:
        if root.id in completed:
            proof=next(proof for proof in ledger.proofs if proof.publication.id==root.id)
            return _wait(item,'this HEAD/epoch already has a canonical completed REVIEW',
                         outcome=proof.outcome,source=proof.source)
        # Recovery preserves the old audit/report/budget and gets a NEW UUID.
        verify(snapshot,root.payload['audit'],publication_context=context(root.payload),trusted=config['trusted_account'])
        request=requests.build(config,snapshot,policy,depth=root.payload['depth'],
            budget_used=root.payload['budget_used'],resume_id=root.payload['resume_id'],reservation=reservation,
            recovery={'publication_id':root.id,'context_sha256':context(root.payload),'audit':root.payload['audit']})
        action='recover'
    else:
        if any(record.id not in completed for record in roots.values()):
            return _wait(item,'an earlier HEAD has an unresolved publication; no replacement audit')
        if any((entry['head'],entry['epoch'])==current for entry in verified['completed']):
            return _wait(item,'this HEAD/epoch already has a canonical legacy REVIEW')
        if 'changes-requested' in snapshot['labels']:
            raise PipelineError('typed REVIEW target entered the FIX route')
        resume=_admit_audit(ledger,item['head'])
        request=requests.build(config,snapshot,policy,depth=verified['depth'],budget_used=ledger.budget_used,
                               resume_id=resume,reservation=reservation)
        action='audit'
    if not pp.content_review_elected(health,{'number':request['number'],'head':request['head'],
                                                'review_depth':request['depth']}):
        raise PipelineError('typed REVIEW verified depth/target changed after health election')
    # Authenticate and renew before issuing even the model's bounded request.
    pp.ensure_identity(gh,config)
    pp.renew_lease_target_reservation(request,config)
    requests.require_current(request,config,write=True)
    response={'action':action,'stage':'review','protocol':VERSION,'target':dict(item),
              'lease':requests.encode(request),'mutation_authorized':False}
    if action=='recover':
        response.update(publication_id=root.id,reason='recover the existing immutable report without invoking a model')
    else:
        response.update(inspect=[f"gh pr diff {item['number']} --repo {config['repository']}"],
            complete='write review-routing-v1 report JSON, then complete review --lease-file <file> --report <file>',
            report_protocol=VERSION,review_depth=verified['depth'])
    return response


def complete_review(gh,config,token,report_path,*,config_path):
    request=requests.decode(token)
    if request['purpose']==requests.AUDIT:
        if not report_path:raise PipelineError('typed audit completion requires --report')
        inspection=inspect_file(report_path,request['head'])
        report=json.loads(inspection['canonical_record'])
        seed={'protocol':VERSION,'kind':'publication','pr':request['pr_key'],'owner':request['owner_uuid'],
              'identity':request['identity'],'author':request['author'],'epoch':request['epoch'],
              'anchor':request['anchor'],'audit':request['audit'],'depth':request['depth'],
              'budget_used':request['budget_used'],'max_attempts':request['current_limit'],
              'report':report,'report_sha256':inspection['report_sha256'],'resume_id':request['resume_id'],
              'outcome':outcome(report,depth=request['depth'],budget_used=request['budget_used'],max_attempts=request['current_limit'])}
    else:
        if report_path is not None:raise PipelineError('recovery cannot substitute a new report')
        health=pp.run_health(config,config_path=config_path);preflight(config,health)
        requests.require_current(request,config,write=False)
        snapshot=stable_timeline(gh,config,request['number'])
        verified_depth(snapshot,trusted=config['trusted_account'])
        history,_deleted=records(snapshot,trusted=config['trusted_account'],expected_pr=request['pr_key'])
        root=next((item for item in history if item.id==request['recovery']['publication_id'] and item.payload['kind']=='publication'),None)
        if root is None or context(root.payload)!=request['recovery']['context_sha256']:
            raise PipelineError('recovery canonical immutable report is missing or changed')
        seed=copy.deepcopy(root.payload);seed['owner']=request['owner_uuid']
    seed=parse(render(seed))
    guard=StageGuard(gh,config_path,token,seed)
    # No journal/schema creation until signature, report and first READ gate pass.
    guard(seed,write=False)
    result=Publication(gh,config,_journal(config),guard,seed).run()
    return dict(result,stage='review',number=request['number'],head=request['head'],protocol=VERSION)
