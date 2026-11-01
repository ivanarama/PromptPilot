"""Durable checkpoint readiness barrier; only retained owned resources can retry.

The source checkpoint is accepted with its index/ref fence still held. Workflow
readiness is released separately after cleanup, never from PID absence, timeout,
agent text or a newly started process. Lost ownership remains an explicit barrier.
"""
import json
import threading
import uuid

from . import db
from .models import WorkflowEventCreate, WorkflowStatus

PROTOCOL='checkpoint-cleanup-v1'
PENDING='checkpoint.cleanup.v1.pending'
DONE='checkpoint.cleanup.v1.done'
_RESOURCES={}
_MUTEX=threading.Lock()


def _payload(raw):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('duplicate cleanup metadata key')
            result[key]=value
        return result
    def reject_constant(_value):raise ValueError('non-JSON cleanup metadata')
    if not isinstance(raw,str) or len(raw)>65536:return None
    try:return json.loads(raw,object_pairs_hook=unique,parse_constant=reject_constant)
    except (ValueError,TypeError,RecursionError):return None


def active(conn, workflow_id):
    row=conn.execute('SELECT seq,idempotency_key,payload_json FROM workflow_events WHERE workflow_id=? AND event_type=? ORDER BY seq DESC LIMIT 1',
                     (workflow_id,PENDING)).fetchone()
    if row is None:return None
    value=_payload(row['payload_json'])
    required={'protocol','generation','pending_seq','sha','stage_id','round_no','target','memory_stage','accepted','version'}
    if (not isinstance(value,dict) or set(value)!=required or value.get('protocol')!=PROTOCOL
            or not isinstance(value.get('generation'),str) or type(value.get('pending_seq')) is not int
            or value['pending_seq']<1 or type(value.get('round_no')) is not int or value['round_no']<1
            or type(value.get('version')) is not int or value['version']<1
            or type(value.get('accepted')) is not bool
            or not isinstance(value.get('target'),str)
            or value['target'] not in {'awaiting_human','queued','completed','revision_required'}
            or value['accepted'] and value['target'] not in {'queued','completed'}
            or not value['accepted'] and value['target'] not in {'awaiting_human','revision_required'}
            or any(value[name] is not None and not isinstance(value[name],str) for name in ['stage_id','memory_stage'])
            or value['sha'] is not None and (not isinstance(value['sha'],str) or len(value['sha'])!=40
                or any(char not in '0123456789abcdef' for char in value['sha']))):
        return {'invalid':True}
    generation=value['generation']
    if row['idempotency_key']!=PENDING+':'+generation:return {'invalid':True}
    try:
        parsed=uuid.UUID(generation)
        if parsed.version!=4 or str(parsed)!=generation:return {'invalid':True}
    except ValueError:return {'invalid':True}
    receipt=conn.execute('SELECT seq,event_type,payload_json FROM workflow_events WHERE workflow_id=? AND idempotency_key=?',
                         (workflow_id,DONE+':'+generation)).fetchone()
    if receipt is None:return value
    done=_payload(receipt['payload_json'])
    required={'protocol','generation','terminal','release_seq','version','status'}
    if (receipt['event_type']!=DONE or receipt['seq']<=row['seq']
            or not isinstance(done,dict) or set(done)!=required or done['protocol']!=PROTOCOL
            or done['generation']!=generation or type(done['terminal']) is not bool
            or type(done['release_seq']) is not int or not row['seq']<done['release_seq']<receipt['seq']
            or type(done['version']) is not int or done['version']<1
            or not isinstance(done['status'],str)
            or done['terminal'] and done['status'] not in {'cancelled','completed','failed'}
            or not done['terminal'] and done['status']!=value['target']):
        return {'invalid':True}
    workflow=conn.execute('SELECT status,state_version FROM workflows WHERE id=?',(workflow_id,)).fetchone()
    if (workflow is None or not value['version']<done['version']<=workflow['state_version']
            or workflow['state_version']==done['version'] and workflow['status']!=done['status']):
        return {'invalid':True}
    from . import workflows
    release=conn.execute('SELECT event_type,idempotency_key,payload_json FROM workflow_events WHERE workflow_id=? AND seq=?',
                         (workflow_id,done['release_seq'])).fetchone()
    expected=dict(version=done['version'],generation=generation,terminal=done['terminal'],target=done['status'])
    if not done['terminal'] and done['status']!='awaiting_human':
        expected.update({'from':'awaiting_human','to':done['status']})
    if (release is None or release['event_type']!='checkpoint.cleanup.released'
            or release['idempotency_key']!=workflows._event_key('checkpoint.cleanup.released',workflow_id,done['version'])
            or _canonical(_payload(release['payload_json']))!=_canonical(expected)):
        return {'invalid':True}
    suffix=f":{workflow_id}:v{done['version']}"
    versions=conn.execute('SELECT seq FROM workflow_events WHERE workflow_id=? AND substr(idempotency_key,-length(?))=? LIMIT 2',
                          (workflow_id,suffix,suffix)).fetchall()
    if len(versions)!=1 or versions[0]['seq']!=done['release_seq']:return {'invalid':True}
    return None


def hold(conn, workflow, generation, pending, sha, *, target, memory_stage=None, accepted=True):
    if WorkflowStatus(workflow['status']) is not WorkflowStatus.AWAITING_HUMAN:
        raise db.WorkflowConflictError('checkpoint cleanup must retain a non-runnable workflow')
    value=dict(protocol=PROTOCOL,generation=generation,pending_seq=pending['pending_seq'],sha=sha,
               stage_id=workflow['current_stage_id'],round_no=workflow['current_round'],
               target=target,memory_stage=memory_stage,accepted=accepted,version=workflow['state_version'])
    event=db._append_workflow_event(conn,WorkflowEventCreate(workflow_id=workflow['id'],event_type=PENDING,
        idempotency_key=PENDING+':'+generation,payload=value))
    return dict(workflow_id=workflow['id'],seq=event.seq,key=event.idempotency_key,payload=_canonical(value))


def new_generation():return str(uuid.uuid4())


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)


def _require_binding(conn,workflow_id,pending,resource):
    row=conn.execute('SELECT seq,idempotency_key,payload_json FROM workflow_events WHERE workflow_id=? AND idempotency_key=?',
                    (workflow_id,PENDING+':'+pending['generation'])).fetchone()
    if row is None:raise db.WorkflowConflictError('checkpoint cleanup original receipt disappeared')
    actual=dict(workflow_id=workflow_id,seq=row['seq'],key=row['idempotency_key'],payload=_canonical(_payload(row['payload_json'])))
    if actual!=resource['binding'] or actual['payload']!=_canonical(pending):
        raise db.WorkflowConflictError('checkpoint cleanup original receipt context changed')


def register(generation, cleanup, ready, *, binding):
    if not callable(cleanup) or not callable(ready):
        raise db.WorkflowConflictError('checkpoint cleanup requires retained owned callbacks')
    if (not isinstance(binding,dict) or set(binding)!={'workflow_id','seq','key','payload'}
            or binding['key']!=PENDING+':'+generation or type(binding['seq']) is not int
            or binding['seq']<1 or not isinstance(binding['workflow_id'],str)
            or not isinstance(binding['payload'],str)):
        raise db.WorkflowConflictError('checkpoint cleanup requires its original immutable receipt')
    with _MUTEX:
        if generation in _RESOURCES:
            raise db.WorkflowConflictError('checkpoint cleanup owner already registered')
        _RESOURCES[generation]=dict(cleanup=cleanup,ready=ready,binding=dict(binding),lock=threading.Lock(),auto_tried=False)


def failed(workflow_id,generation,error):
    from . import workflows
    with db._connect(immediate=True) as conn:
        current=workflows._workflow_row(conn,workflow_id)
        pending=active(conn,workflow_id)
        if not pending or pending.get('generation')!=generation:
            raise db.WorkflowConflictError('checkpoint cleanup binding changed')
        if WorkflowStatus(current['status']) in workflows.TERMINAL_STATES:
            return db._row_to_workflow(current)
        current=workflows._touch(conn,current,'checkpoint.cleanup.failed',
            dict(protocol=PROTOCOL,generation=generation,reason=str(error)[:800]))
        return db._row_to_workflow(current)


def finish(workflow_id,generation,*,expected_version=None,said=None):
    """Trusted caller has confirmed its original resources closed successfully."""
    from . import workflows,stage_checkpoint
    with db._connect(immediate=True) as conn:
        workflow=workflows._workflow_row(conn,workflow_id)
        pending=active(conn,workflow_id)
        if pending is None:return db._row_to_workflow(workflow)
        if pending.get('invalid') or pending.get('generation')!=generation:
            raise db.WorkflowConflictError('checkpoint cleanup history is unknown or changed')
        with _MUTEX:resource=_RESOURCES.get(generation)
        if resource is None:raise db.WorkflowConflictError('checkpoint cleanup owner is unavailable')
        _require_binding(conn,workflow_id,pending,resource)
        terminal=WorkflowStatus(workflow['status']) in workflows.TERMINAL_STATES
        if terminal:
            target=WorkflowStatus(workflow['status'])
        else:
            if expected_version is not None:
                workflows._require_version(conn,workflow,expected_version,allow_checkpoint_cleanup=True)
            if (workflow['status']!='awaiting_human' or pending['stage_id']!=workflow['current_stage_id']
                    or pending['round_no']!=workflow['current_round']):
                raise db.WorkflowConflictError('checkpoint cleanup lifecycle binding changed')
            if pending['accepted']:
                row=conn.execute("SELECT payload_json FROM workflow_events WHERE workflow_id=? AND seq=? AND event_type='stage.checkpoint_pending'",
                                 (workflow_id,pending['pending_seq'])).fetchone()
                if row is None:raise db.WorkflowConflictError('checkpoint cleanup source receipt disappeared')
                source=db._json_load(row['payload_json'],{})
                if not isinstance(source,dict):
                    raise db.WorkflowConflictError('checkpoint cleanup source receipt is malformed')
                source['pending_seq']=pending['pending_seq']
                stage_checkpoint.confirm(source,pending['sha'],workflows._file_sig)
            if said:
                workflow=workflows._touch(conn,workflow,'human.input',dict(said,resume=True,checkpoint_cleanup=True))
            if pending.get('memory_stage'):
                conn.execute('DELETE FROM workflow_memory WHERE workflow_id=? AND scope_stage=?',
                             (workflow_id,pending['memory_stage']))
            target=WorkflowStatus(pending['target'])
        payload=dict(generation=generation,terminal=terminal,target=target.value)
        if terminal or target is WorkflowStatus.AWAITING_HUMAN:
            workflow=workflows._touch(conn,workflow,'checkpoint.cleanup.released',payload)
        elif target in {WorkflowStatus.QUEUED,WorkflowStatus.COMPLETED,WorkflowStatus.REVISION_REQUIRED}:
            workflow=workflows._transition(conn,workflow,target,'checkpoint.cleanup.released',payload)
        else:raise db.WorkflowConflictError('checkpoint cleanup cannot release an unsupported lifecycle')
        release=conn.execute('SELECT seq FROM workflow_events WHERE workflow_id=? AND idempotency_key=?',
            (workflow_id,workflows._event_key('checkpoint.cleanup.released',workflow_id,workflow['state_version']))).fetchone()
        if release is None:raise db.WorkflowConflictError('checkpoint cleanup release receipt disappeared')
        db._append_workflow_event(conn,WorkflowEventCreate(workflow_id=workflow_id,event_type=DONE,
            idempotency_key=DONE+':'+generation,payload=dict(protocol=PROTOCOL,generation=generation,
                terminal=terminal,release_seq=release['seq'],version=workflow['state_version'],status=workflow['status'])))
        result=db._row_to_workflow(workflow)
    with _MUTEX:_RESOURCES.pop(generation,None)
    return result


def retry(workflow_id,expected_version,*,manual=False,said=None):
    from . import workflows
    with db._connect() as conn:
        workflow=workflows._workflow_row(conn,workflow_id)
        workflows._require_version(conn,workflow,expected_version,allow_checkpoint_cleanup=True)
        pending=active(conn,workflow_id)
        if pending is None:return db._row_to_workflow(workflow)
        if pending.get('invalid'):
            if manual:raise db.WorkflowConflictError('checkpoint cleanup history is unknown; preserve the barrier')
            return db._row_to_workflow(workflow)
        generation=pending['generation']
    with _MUTEX:resource=_RESOURCES.get(generation)
    if resource is None:
        if manual:raise db.WorkflowConflictError('checkpoint cleanup owner is unavailable; engineering verification required')
        return db.get_workflow(workflow_id)
    if not resource['lock'].acquire(blocking=False):
        if manual:raise db.WorkflowConflictError('checkpoint cleanup is already owned by another caller')
        return db.get_workflow(workflow_id)
    try:
        with db._connect() as conn:
            current=workflows._workflow_row(conn,workflow_id)
            workflows._require_version(conn,current,expected_version,allow_checkpoint_cleanup=True)
            current_pending=active(conn,workflow_id)
            if not current_pending or current_pending.get('invalid'):
                raise db.WorkflowConflictError('checkpoint cleanup original history changed')
            _require_binding(conn,workflow_id,current_pending,resource)
        if not manual and (resource['auto_tried'] or resource['ready']() is not True):
            return db.get_workflow(workflow_id)
        if not manual:resource['auto_tried']=True
        try:
            if resource['cleanup']() is not None:
                raise db.WorkflowConflictError('checkpoint cleanup did not confirm owned resource closure')
        except Exception as exc:
            return failed(workflow_id,generation,exc)
        try:
            return finish(workflow_id,generation,expected_version=expected_version,said=said)
        except (OSError,RuntimeError) as exc:
            return failed(workflow_id,generation,exc)
    finally:resource['lock'].release()
