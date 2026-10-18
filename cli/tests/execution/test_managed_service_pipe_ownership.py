"""Packed controller pipe ownership with real descriptor reuse and child cleanup."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.execution import _managed_service_bundle as bundle
from agentworks.execution._helper_bundle import build_helper_modules
from tests.execution.test_managed_service_guest import _launch

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux controller")

_SCRIPT = r"""
import os,selectors,signal,sys
from contextlib import suppress
from _test_pipe_ownership import _managed_service_guest as guest
from _test_pipe_ownership._managed_job_request import ManagedJobRequest
from _test_pipe_ownership._managed_job_store import ManagedJobStore,FactName
from _test_pipe_ownership._managed_job_wire import decode_fact
root,mode,launch=sys.argv[1],sys.argv[2],bytes.fromhex(sys.argv[3])
anchor=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
store=ManagedJobStore('a'*32,_anchor_fd=anchor,_namespace='managed',_owner_uid=os.getuid())
os.close(anchor)
initialization=mode in ('selector','blocking','register')
argv=('/bin/sleep','30') if initialization else ('/bin/true',)
store.publish_request(ManagedJobRequest(launch,'command',argv,None,'discard',None,(),b'',b''))
class Boundary:
    pid=None
    killed=False
    def place(self,pid):self.pid=pid
    def kill(self):
        self.killed=True
        if self.pid is not None:
            with suppress(ProcessLookupError):os.kill(self.pid,signal.SIGKILL)
    def empty(self):return True
boundary=Boundary()
real_observe=guest._observe
real_close=os.close
real_selector=selectors.DefaultSelector
real_set_blocking=os.set_blocking
reused=[]
fillers=[]
failure=RuntimeError('observation initialization failure')
selector_closed=[]
def descriptors():
    result={}
    for name in os.listdir('/proc/self/fd'):
        try:result[int(name)]=os.readlink('/proc/self/fd/'+name)
        except FileNotFoundError:pass
    return result
before=descriptors()
class FailingSelector(real_selector):
    def register(self,*args,**kwargs):
        if mode=='register':raise failure
        return super().register(*args,**kwargs)
    def close(self):
        super().close()
        selector_closed.append(True)
        raise OSError('selector cleanup failure')
def fail_selector():raise failure
def fail_blocking(*args,**kwargs):raise failure
def observe(*args,**kwargs):
    if initialization:
        guest.selectors.DefaultSelector=fail_selector if mode=='selector' else FailingSelector
        if mode=='blocking':os.set_blocking=fail_blocking
        return real_observe(*args,**kwargs)
    target=args[6] if mode in ('input','uncertain-close') else args[8][mode=='stderr'].fd
    if mode=='uncertain-close':
        interrupted=[]
        def interrupted_close(fd):
            real_close(fd)
            if fd==target and not interrupted:
                interrupted.append(True)
                raise OSError('close result unavailable')
        os.close=interrupted_close
    try:result=real_observe(*args,**kwargs)
    finally:os.close=real_close
    try:os.fstat(target)
    except OSError as exc:assert exc.errno==9
    else:raise AssertionError('observation did not close the selected pipe')
    # Occupy lower free numbers so the real store walk returns this released number.
    while True:
        candidate=os.dup(store._anchor_fd)
        if candidate>=target:
            real_close(candidate)
            assert candidate==target
            break
        fillers.append(candidate)
    directory=store.open_live_directory()
    reused.append(directory)
    assert directory==target
    os.fstat(directory)
    return result
guest._observe=observe
try:
    if initialization:
        caught=None
        try:guest._serve('a'*32,store,boundary,lambda:None,False)
        except BaseException as exc:caught=exc
        assert caught is failure,repr(caught)
        # Initialization failure does not transfer child lifetime to this fixture.
        # Reap only the real child created above, independently of pipe cleanup.
        boundary.kill()
        os.waitpid(boundary.pid,0)
        assert descriptors()==before,(before,descriptors())
        assert bool(selector_closed)==(mode!='selector')
    else:
        guest._serve('a'*32,store,boundary,lambda:None,False)
        # The caller now owns a real directory, even though its number was a pipe.
        metadata=os.fstat(reused[0])
        assert metadata.st_ino==os.stat(root+'/managed/'+'a'*32).st_ino
        assert decode_fact(store.read_fact(FactName.WAIT))['exit_code']==0
        for fact in (FactName.STDOUT_END,FactName.STDERR_END):assert store.read_fact(fact) is not None
    try:os.waitpid(boundary.pid,os.WNOHANG)
    except ChildProcessError:pass
    else:raise AssertionError('controller left its child unreaped')
finally:
    guest.selectors.DefaultSelector=real_selector
    os.set_blocking=real_set_blocking
    os.close=real_close
    if boundary.pid is not None:
        # Never signal an already reaped PID during test cleanup.
        try:waited,_=os.waitpid(boundary.pid,os.WNOHANG)
        except ChildProcessError:pass
        else:
            if waited==0:
                boundary.kill()
                os.waitpid(boundary.pid,0)
    for fd in (*reused,*fillers):
        with suppress(OSError):real_close(fd)
    store.close()
"""


@pytest.mark.parametrize("runtime", [Path(sys.executable), Path("/usr/bin/python3.11")], ids=["current", "python311"])
@pytest.mark.parametrize("mode", ["input", "stdout", "stderr", "uncertain-close", "selector", "blocking", "register"])
def test_packed_controller_closes_only_its_owned_pipes(tmp_path: Path, runtime: Path, mode: str) -> None:
    if not runtime.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {runtime}")
    source = build_helper_modules("_test_pipe_ownership", bundle._MODULES) + _SCRIPT
    completed = subprocess.run(
        [str(runtime), "-I", "-S", "-B", "-c", source, str(tmp_path), mode, _launch().hex()],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert completed.stdout == completed.stderr == b""
