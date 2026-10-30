"""Explicit opt-in and paired-reader preflight; never mutation authority."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat

from .pipeline_errors import PipelineError
from .review_routing import VERSION


ROLES={'review':'review-queue','fix':'fix-approved','merge':'merge-shepherd','tail':'tail-issues'}


def off_fence(gh, config, number):
    """OFF never erases an existing trusted v1 lifetime transaction.

    No readers, journal creation/migration or wire writes occur here. Edited
    trusted comments, deletions and existing local transactions retain the
    fence even when their old marker text is no longer visible. Unaltered
    ordinary no-v1 legacy bytes remain unchanged.
    """
    from . import project_pipeline as pp
    from .review_routing_adapter import journal_directory
    from .review_routing_journal import Journal
    if (not isinstance(config.get('repository'),str) or not config['repository']
            or not isinstance(config.get('trusted_account'),str) or not config['trusted_account']):
        raise PipelineError('OFF history fence requires repository and trusted account configuration')
    snapshot = pp.stable_timeline(gh, config, number)
    fenced=Journal.has_existing_history(journal_directory(config),config['repository'],number)
    for edge in snapshot['edges']:
        node = edge.get('node') or {}
        if (node.get('__typename')=='CommentDeletedEvent'
                or node.get('__typename') == 'IssueComment'
                and (node.get('author') or {}).get('login') == config['trusted_account']
                and ('<!-- pp:review-routing-' in (node.get('body') or '') or node.get('lastEditedAt') is not None)):
            fenced=True
    if fenced:
        return {'action': 'wait', 'verdict': 'ПУСТО', 'number': number,
                'reason': 'routing is OFF with existing or uncertain trusted history; read-only engineering fence'}
    return None


def _file_bytes(path):
    """Bound before reading; never block opening a FIFO or follow a link."""
    try:
        before=path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise PipelineError('paired routing consumer is not a regular file')
        if before.st_size>1048576:
            raise PipelineError('paired routing consumer file exceeds bounded size')
        flags=os.O_RDONLY|getattr(os,'O_BINARY',0)|getattr(os,'O_NONBLOCK',0)|getattr(os,'O_NOFOLLOW',0)
        descriptor=os.open(path,flags)
        try:
            opened=os.fstat(descriptor)
            if (not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev,opened.st_ino)!=(before.st_dev,before.st_ino)):
                raise PipelineError('paired routing consumer changed while opening')
            with os.fdopen(descriptor,'rb') as stream:
                descriptor=None
                data=stream.read(1048577)
            if len(data)>1048576:
                raise PipelineError('paired routing consumer file exceeds bounded size')
            return data
        finally:
            if descriptor is not None:
                os.close(descriptor)
    except OSError as exc:
        raise PipelineError('paired routing consumer file is unavailable') from exc


def settings(config):
    """Do not add defaults to legacy config: its signed digest must survive."""
    if 'technical_handoff' not in config:
        return None
    value=config['technical_handoff']
    if not isinstance(value,dict) or set(value)!={'enabled','protocol','max_extra_attempts'}:
        raise PipelineError('technical_handoff requires exactly enabled/protocol/max_extra_attempts')
    if type(value['enabled']) is not bool or value['protocol']!=VERSION:
        raise PipelineError('technical_handoff requires boolean enabled and review-routing-v1 protocol')
    limit=value['max_extra_attempts']
    if type(limit) is not int or not 1<=limit<=16:
        raise PipelineError('technical_handoff requires an explicit attempt limit from1 to16')
    return dict(value)


def consumer_tree(root,role):
    if role not in ROLES:
        raise PipelineError('unsupported routing consumer role')
    directory=Path(root)/ROLES[role]
    if not directory.is_dir() or directory.is_symlink():
        raise PipelineError('paired routing consumer directory is missing or linked')
    entry=directory/'SKILL.md'
    if entry.is_symlink():
        raise PipelineError('paired routing consumer entry cannot be linked')
    try:
        entry_bytes=_file_bytes(entry)
        text=entry_bytes.decode('utf-8')
    except UnicodeError as exc:
        raise PipelineError('paired routing consumer entry is unavailable') from exc
    marker=f'<!-- pp:review-routing-v1-consumer stage={role} -->'
    if text.splitlines().count(marker)!=1:
        raise PipelineError('paired routing consumer version marker is missing or ambiguous')
    files=[]
    for path in directory.rglob('*'):
        relative=path.relative_to(directory)
        if '__pycache__' in relative.parts or path.suffix in {'.pyc','.pyo'}:
            continue
        if path.is_symlink():
            raise PipelineError('paired routing consumer tree contains a symbolic link')
        if not path.is_dir():
            name=relative.as_posix()
            if any(ord(char)<32 or ord(char)==127 for char in name):
                raise PipelineError('paired routing consumer path contains a control character')
            files.append((name,path))
    if not 1<=len(files)<=256:
        raise PipelineError('paired routing consumer file set is missing or oversized')
    digest=hashlib.sha256(b'pp-review-routing-consumer-tree-v1\n')
    total=0
    for name,path in sorted(files,key=lambda item:item[0].encode('utf-8')):
        data=entry_bytes if path==entry else _file_bytes(path)
        total+=len(data)
        if len(data)>1048576 or total>4194304:
            raise PipelineError('paired routing consumer tree exceeds bounded size')
        digest.update(name.encode('utf-8')+b'\0'+hashlib.sha256(data).hexdigest().encode('ascii')+b'\n')
    return digest.hexdigest()


def preflight(config,health):
    """Compare all four actual local readers with a complete checker contract.

    A declared version in config or one skill marker alone cannot enable the
    protocol. Call again at each future stage gate; this is only compatibility
    and read-only admission, not proof/evidence/ownership verification.
    """
    value=settings(config)
    if value is None or not value['enabled']:
        return {'enabled':False,'mutation_authorized':False}
    if config.get('review_completion_gate')!='target-v1' or config.get('fallback_handoff')!='target-v1':
        raise PipelineError('technical_handoff requires both target-v1 gates')
    if (not isinstance(health,dict) or health.get('state') not in {'green','yellow'}
            or not isinstance(health.get('findings'),list)
            or any(not isinstance(item,dict) or item.get('severity')=='red' for item in health['findings'])):
        raise PipelineError('routing checker snapshot is incomplete or red')
    contract=health.get('review_routing_contract')
    if (not isinstance(contract,dict) or set(contract)!={'protocol','snapshot','reader_sha256'}
            or contract['protocol']!=VERSION or contract['snapshot']!='complete'
            or not isinstance(contract['reader_sha256'],dict)
            or set(contract['reader_sha256'])!=set(ROLES)):
        raise PipelineError('checker lacks the complete paired review-routing-v1 reader contract')
    command=config.get('health_command')
    if not isinstance(command,list) or command.count('-contract')!=1:
        raise PipelineError('routing checker requires one explicit pinned contract path')
    try:
        entry=Path(command[command.index('-contract')+1])
    except (IndexError,TypeError) as exc:
        raise PipelineError('routing checker contract path is invalid') from exc
    if entry.name!='SKILL.md' or entry.parent.name!=ROLES['review']:
        raise PipelineError('routing checker contract is not a canonical review entry')
    root=entry.parent.parent
    readers={role:consumer_tree(root,role) for role in ROLES}
    if readers!=contract['reader_sha256']:
        raise PipelineError('routing checker/local consumer tree hashes differ')
    return {'enabled':True,'protocol':VERSION,'current_limit':value['max_extra_attempts'],
            'consumer_sha256':readers,'mutation_authorized':False}
