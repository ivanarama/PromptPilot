"""Owned control CLI lifetime, including retained cleanup after OS errors."""
import os
from datetime import timezone
from pathlib import Path
import subprocess
import threading

from . import db
from .pipeline_errors import PipelineError
from .process_tree import OwnedProcess

_CONTROLS={}
_LOCK=threading.Lock()


def _key(task_id):return os.path.realpath(str(db.DB_PATH)),task_id


def close_controls(task_id):
    key=_key(task_id)
    with _LOCK:trees=list(_CONTROLS.get(key,()))
    errors=[]
    for tree in trees:
        try:
            tree.close();tree.process.wait(timeout=10)
            for stream in [tree.process.stdin,tree.process.stdout,tree.process.stderr]:
                if stream is not None:stream.close()
        except Exception as exc:
            errors.append(type(exc).__name__+': '+str(exc));continue
        with _LOCK:
            current=_CONTROLS.get(key,[])
            if tree in current:current.remove(tree)
            if not current:_CONTROLS.pop(key,None)
    return '; '.join(errors)


def run(args,*,timeout=None,capture_output=False,**kwargs):
    from .worker import ProviderOwnershipError
    env=kwargs.get('env') or {}
    raw=env.get('PP_TASK_ID','')
    if not isinstance(raw,str) or not raw.isdecimal() or not 0<int(raw)<=9223372036854775807 or str(int(raw))!=raw:
        raise PipelineError('owned technical control lacks its exact scheduler task')
    task_id=int(raw);task=db.get_task(task_id)
    if (task is None or task.status.value!='running' or task.started_at is None
            or task.started_at.astimezone(timezone.utc).isoformat()!=env.get('PP_TASK_STARTED_AT')
            or env.get('PP_PROVIDER_OWNERSHIP_KIND')!='headless' or task.machine or task.detached
            or task.herdr_target or task.worktree
            or os.path.realpath(env.get('PP_DATA_DIR',''))!=os.path.realpath(str(Path(db.DB_PATH).parent))):
        raise PipelineError('owned technical control task occurrence/transport changed')
    if capture_output:
        kwargs['stdout']=subprocess.PIPE;kwargs['stderr']=subprocess.PIPE
    with _LOCK:
        if _CONTROLS.get(_key(task_id)):
            raise ProviderOwnershipError('technical task retains another original control boundary',lambda:close_controls(task_id))
        tree=OwnedProcess.start(args,**kwargs)
        _CONTROLS.setdefault(_key(task_id),[]).append(tree)
    try:
        try:stdout,stderr=tree.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            tree.close()
            tree.process.communicate(timeout=10)
            raise
        return subprocess.CompletedProcess(args,tree.process.returncode,stdout,stderr)
    finally:
        try:
            tree.close();tree.process.wait(timeout=10)
            for stream in [tree.process.stdin,tree.process.stdout,tree.process.stderr]:
                if stream is not None:stream.close()
        except Exception as exc:
            raise ProviderOwnershipError('technical control retains its original owned boundary',
                lambda:close_controls(task_id)) from exc
        with _LOCK:
            current=_CONTROLS.get(_key(task_id),[])
            if tree in current:current.remove(tree)
            if not current:_CONTROLS.pop(_key(task_id),None)
