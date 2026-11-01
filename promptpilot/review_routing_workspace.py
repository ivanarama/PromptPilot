"""Private deterministic source-bound FIX worktree; never edits the base checkout.

This is not a sandbox. The eventual worker must retain the workspace on unknown
process outcomes and must use the actual owned factory and CAS coordinator.
"""
import hashlib
import json
import os
from pathlib import Path
import stat

from .pipeline_errors import PipelineError
from .process_tree import run_owned
from .review_routing_execution import freeze_scope
from .review_routing_wire import canonical,sha


PROTOCOL='technical-fix-worktree-v1'


def _plain(path,*,directory=False):
    try:info=path.lstat()
    except OSError as exc:raise PipelineError('FIX workspace path is unavailable') from exc
    if (stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400
            or directory and not stat.S_ISDIR(info.st_mode)
            or not directory and not stat.S_ISREG(info.st_mode)):
        raise PipelineError('FIX workspace path is linked or has an unexpected type')
    return info


def _remote(url,repository):
    expected=repository.lower()
    valid={f'https://github.com/{expected}',f'https://github.com/{expected}.git',
           f'git@github.com:{expected}',f'git@github.com:{expected}.git',
           f'ssh://git@github.com/{expected}',f'ssh://git@github.com/{expected}.git'}
    if not isinstance(url,str) or url.lower() not in valid:
        raise PipelineError('FIX workspace requires the exact credential-free GitHub repository URL')


class Workspace:
    def __init__(self,database,scope,source):
        self.scope=freeze_scope(scope)
        database=Path(database)
        _plain(database)
        self.source=Path(source).resolve()
        _plain(self.source,directory=True)
        self.root=database.resolve().parent/'technical-fix-workspaces'
        self.folder=self.root/self.scope['attempt_uuid']
        self.path=self.folder/'checkout'
        self.receipt=self.folder/'receipt.json'
        self.hooks=self.folder/'empty-hooks'

    def _directories(self,*,create=False):
        for directory in [self.root,self.folder,self.hooks]:
            if create:directory.mkdir(mode=0o700,exist_ok=True)
            _plain(directory,directory=True)
        if any(self.hooks.iterdir()):raise PipelineError('FIX workspace hook directory is not empty')
        if self.path.resolve().parent!=self.folder.resolve():
            raise PipelineError('FIX workspace escaped its deterministic owned directory')

    def _git(self,root,*args,allow_failure=False,binary=False):
        env=dict(os.environ,GIT_NO_REPLACE_OBJECTS='1',GIT_GRAFT_FILE=os.devnull,GIT_TERMINAL_PROMPT='0')
        result=run_owned(['git','-c','maintenance.auto=false','-c','gc.auto=0',
            '-c','core.autocrlf=false','-c','core.eol=lf',
            '-c','core.fsmonitor=false','-c','core.hooksPath='+str(self.hooks),'-C',str(root),*args],
            capture_output=True,env=env,timeout=120,
            **({} if binary else dict(text=True,encoding='utf-8',errors='strict')))
        if result.returncode and not allow_failure:
            raise PipelineError('FIX workspace Git operation failed; preserve its artifacts')
        if len(result.stdout)>1048576 or len(result.stderr)>1048576:
            raise PipelineError('FIX workspace Git metadata exceeds its finite bound')
        return result

    def _raw_tree(self):
        # Git status/hash-object with filters can normalize unreviewed bytes
        # back to the committed blob. Hash regular working files directly,
        # independently of the index, attributes and clean/smudge drivers.
        records=self._git(self.path,'ls-tree','-r','-z','HEAD').stdout.split('\0')
        total=0;tracked=set()
        for record in records:
            if not record:continue
            try:
                metadata,name=record.split('\t',1)
                mode,kind,oid=metadata.split(' ')
            except ValueError as exc:raise PipelineError('FIX tracked tree metadata is malformed') from exc
            relative=Path(name)
            if (mode not in {'100644','100755'} or kind!='blob' or len(oid)!=40
                    or any(char not in '0123456789abcdef' for char in oid)
                    or relative.is_absolute() or any(part in {'.','..','.git'} for part in relative.parts)):
                raise PipelineError('FIX raw source requires regular tracked files in its bound tree')
            tracked.add(relative.as_posix())
            parent=self.path
            for part in relative.parts[:-1]:
                parent=parent/part;_plain(parent,directory=True)
            path=self.path/relative;before=_plain(path)
            if before.st_size>134217728:raise PipelineError('FIX tracked source file exceeds its finite raw-byte bound')
            total+=before.st_size
            if total>536870912:raise PipelineError('FIX tracked source tree exceeds its finite raw-byte bound')
            flags=os.O_RDONLY|getattr(os,'O_BINARY',0)|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)
            descriptor=os.open(path,flags)
            with os.fdopen(descriptor,'rb') as stream:
                opened=os.fstat(stream.fileno())
                if (not stat.S_ISREG(opened.st_mode) or (before.st_dev,before.st_ino)!=(opened.st_dev,opened.st_ino)
                        or opened.st_size!=before.st_size
                        or os.name!='nt' and (mode=='100755')!=bool(opened.st_mode&0o111)):
                    raise PipelineError('FIX tracked source changed while opening')
                digest=hashlib.sha1(usedforsecurity=False)
                digest.update(('blob '+str(opened.st_size)+'\0').encode('ascii'));read=0
                while True:
                    block=stream.read(min(1048576,opened.st_size-read+1))
                    if not block:break
                    read+=len(block)
                    if read>opened.st_size:raise PipelineError('FIX tracked source grew during raw verification')
                    digest.update(block)
                after=os.fstat(stream.fileno())
                if (read!=opened.st_size or digest.hexdigest()!=oid
                        or (opened.st_size,opened.st_mtime_ns,opened.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns)):
                    raise PipelineError('FIX working bytes differ from its bound Git tree; filtered status is not source proof')
        return tracked

    def _pristine_tree(self,tracked):
        # Enumerate the physical checkout, independently of Git excludes.
        # In particular an ignored AGENTS.md/.env cannot influence a provider
        # whose source commit does not contain that file. Preserve artifacts.
        expected=set(tracked)|{'.git'}
        directories={parent.as_posix() for name in tracked for parent in Path(name).parents if parent!=Path('.')}
        pending=[self.path];seen=set()
        while pending:
            directory=pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path=Path(entry.path);relative=path.relative_to(self.path).as_posix()
                    if relative in directories:
                        _plain(path,directory=True);pending.append(path)
                    elif relative in expected:
                        _plain(path);seen.add(relative)
                    else:
                        raise PipelineError('FIX checkout has unexpected uncommitted/ignored artifacts; preserve them outside its bound source')
        if seen!=expected:raise PipelineError('FIX physical checkout is missing a bound source file')

    def _forward(self,head,old):
        # Read and hash actual commit objects: rev-list/merge-base can honor
        # grafts or other local reachability metadata. A non-forward CAS is
        # forbidden BEFORE the only push, not diagnosed by later readback.
        from .review_routing_wire import head40
        pending=[head];seen=set();found=False
        while pending:
            oid=pending.pop()
            if oid in seen:continue
            if len(seen)>=101:raise PipelineError('FIX output exceeds its finite actual-parent graph')
            raw=self._git(self.path,'cat-file','commit',head40(oid),binary=True).stdout
            digest=hashlib.sha1(b'commit '+str(len(raw)).encode('ascii')+b'\0'+raw,usedforsecurity=False).hexdigest()
            if digest!=oid:raise PipelineError('FIX commit object differs from its bound Git identity')
            seen.add(oid)
            if oid==old:found=True;continue
            parents=[]
            for line in raw.split(b'\n\n',1)[0].split(b'\n'):
                if line.startswith(b'parent '):
                    try:parent=head40(line[7:].decode('ascii'))
                    except UnicodeError as exc:raise PipelineError('FIX actual parent is malformed') from exc
                    if parent in parents or len(parents)>=16:raise PipelineError('FIX actual parent graph is ambiguous or oversized')
                    parents.append(parent)
            pending.extend(parents)
        if not found or head==old or len(seen)-1>100:
            raise PipelineError('FIX output must move forward from its actual reviewed source objects')

    def _repository(self,root):
        top=Path(self._git(root,'rev-parse','--show-toplevel').stdout.strip()).resolve()
        common=Path(self._git(root,'rev-parse','--path-format=absolute','--git-common-dir').stdout.strip()).resolve()
        urls=self._git(root,'config','--get-all','remote.origin.url').stdout.strip().splitlines()
        if len(urls)!=1:raise PipelineError('FIX workspace origin is missing or ambiguous')
        _remote(urls[0],self.scope['repository'])
        return top,common

    def _expected(self,common):
        return dict(protocol=PROTOCOL,scope_sha256=sha(self.scope),path=str(self.path.resolve()),
                    source=str(self.source),common=str(common),repository=self.scope['repository'],
                    head=self.scope['source']['head'])

    def prepare(self):
        self._directories(create=True)
        top,common=self._repository(self.source)
        if top!=self.source:raise PipelineError('FIX source must be the configured repository root')
        if self.path.exists() or self.receipt.exists():
            return self.verify()
        head=self.scope['source']['head']
        if self._git(self.source,'cat-file','-e',head+'^{commit}',allow_failure=True).returncode:
            self._git(self.source,'fetch','--no-tags','--no-write-fetch-head','origin',head)
        self._git(self.source,'cat-file','-e',head+'^{commit}')
        self._git(self.source,'worktree','add','--detach',str(self.path),head)
        # No overwrite/reconstruction after interrupted creation. Existing
        # checkout without its receipt remains an engineering artifact.
        value=self._expected(common)
        descriptor=os.open(self.receipt,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(canonical(value));stream.flush();os.fsync(stream.fileno())
        return self.verify()

    def _verify(self,*,head):
        self._directories()
        _plain(self.path,directory=True);_plain(self.receipt)
        if self.receipt.stat().st_size>65536:raise PipelineError('FIX workspace receipt is oversized')
        try:value=json.loads(self.receipt.read_text(encoding='utf-8'))
        except (OSError,ValueError,UnicodeError) as exc:raise PipelineError('FIX workspace receipt is unreadable') from exc
        top,common=self._repository(self.source)
        actual,workspace_common=self._repository(self.path)
        if (top!=self.source or actual!=self.path.resolve() or workspace_common!=common
                or value!=self._expected(common)):
            raise PipelineError('FIX workspace receipt, root or shared repository changed')
        if head is not None and self._git(self.path,'rev-parse','HEAD').stdout.strip()!=head:
            raise PipelineError('FIX workspace HEAD differs from the bound reviewed source')
        branch=self._git(self.path,'symbolic-ref','-q','HEAD',allow_failure=True)
        if branch.returncode!=1:raise PipelineError('FIX workspace must remain a detached private checkout')
        entries=self._git(self.path,'ls-files','-v','-z').stdout.split('\0')
        if any(len(entry)<3 or entry[:2]!='H ' for entry in entries if entry):
            raise PipelineError('FIX workspace index conceals tracked source or has unresolved/sparse state')
        sparse=self._git(self.path,'config','--type=bool','--get','core.sparseCheckout',allow_failure=True)
        if sparse.returncode not in {0,1} or sparse.stdout.strip() not in {'','false'}:
            raise PipelineError('FIX workspace cannot use sparse checkout to conceal source')
        self._pristine_tree(self._raw_tree())
        if self._git(self.path,'status','--porcelain=v1','--untracked-files=normal').stdout:
            raise PipelineError('FIX workspace changed before its one owned factory')
        return self.path

    def verify(self):return self._verify(head=self.scope['source']['head'])

    def output(self,attempt):
        """A clean, finite forward commit with exact source trailers, never a push."""
        from .review_routing_post_push import trailers,verify_message
        from .review_routing_wire import context,head40
        if (attempt['source']!=self.scope['source'] or attempt['pr']!=self.scope['pr']
                or attempt['owner']!=self.scope['owner'] or context(attempt)!=self.scope['context_sha256']):
            raise PipelineError('FIX output differs from its immutable technical attempt')
        self._verify(head=None)
        head=head40(self._git(self.path,'rev-parse','HEAD').stdout.strip())
        old=self.scope['source']['head']
        self._forward(head,old)
        verify_message(self._git(self.path,'show','-s','--format=%B',head).stdout,
                       trailers(attempt,self.scope['attempt_id']))
        tree=head40(self._git(self.path,'rev-parse',head+'^{tree}').stdout.strip())
        self._verify(head=head)
        return dict(new_head=head,tree=tree)
