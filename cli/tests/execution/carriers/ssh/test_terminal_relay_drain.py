"""Fixed post-exit collection preserves one frozen prefix under the original deadline."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from tests.execution.carriers.ssh._terminal_modes import assert_preserved_terminal_mode, with_terminal_mode_assertion

if TYPE_CHECKING:
    from typing import IO

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Owned probe uses Linux child subreaping")


@pytest.mark.parametrize("flood", [False, True])
def test_alternating_sink_stalls_preserve_bounded_inherited_output(tmp_path: Path, flood: bool) -> None:
    code = r"""
import ctypes,json,os,pathlib,signal,subprocess,sys,termios,threading,time
from agentworks.execution._process import LocalProcessOwner
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import CarrierIO,Deadline,Failure,SinkOutput,TerminalInput
from agentworks.execution.carriers.ssh._terminal_relay import run_terminal_relay_candidate, _Stream
# Restrict adoption to this disposable probe, so every known descendant is reaped.
libc=ctypes.CDLL(None,use_errno=True)
assert libc.prctl(36,1,0,0,0)==0
pidfile=pathlib.Path(sys.argv[1])
flood=sys.argv[2]=='True'
master,slave=os.openpty()
mode=termios.tcgetattr(slave)
children=[]
original_launch=subprocess.Popen
def launch(*args,**kwargs):
    child=original_launch(*args,**kwargs)
    children.append(child)
    return child
subprocess.Popen=launch
exited=threading.Event()
exit_seen=[]
original_snapshot=LocalProcessOwner.snapshot
def snapshot(owner):
    result=original_snapshot(owner)
    if result.exit_status is not None:
        if not exit_seen: exit_seen.append(time.monotonic())
        exited.set()
    return result
LocalProcessOwner.snapshot=snapshot
class Source:
    def try_read(self,limit): return b''
stalls=[]
blocked_until=0
monotonic=time.monotonic
advanced=0.0
# Charge controlled time to accepted partial writes, independently of real stalls.
time.monotonic=lambda:monotonic()+advanced
class Sink:
    def __init__(self,name,byte):
        self.name=name
        self.byte=byte
        self.count=0
        self.stalled=False
    def try_write(self,data):
        global blocked_until,advanced
        if not exited.is_set(): return None
        now=monotonic()
        if now<blocked_until: return None
        if not self.stalled:
            self.stalled=True
            stalls.append(self.name)
            blocked_until=now+.16
            return None
        delivered=min(len(data),1024)
        assert bytes(data[:delivered])==self.byte*delivered
        self.count+=delivered
        if flood: advanced+=.01
        return delivered
stdout,stderr=Sink('stdout',b'S'),Sink('stderr',b'E')
frozen={}
native_freeze=_Stream.freeze
native_read=os.read
def freeze(stream,pipe):
    fd=pipe.fileno()
    assert fd not in frozen
    pending=0 if stream.pending is None else len(stream.pending)
    native_freeze(stream,pipe)
    frozen[fd]={'name':stream.sink.name,'quota':stream.quota,'pending':pending,
        'before':stream.sink.count,'at':time.monotonic()-exit_seen[0],'reads':[]}
def read(fd,limit):
    try: chunk=native_read(fd,limit)
    except BlockingIOError:
        if fd in frozen: frozen[fd]['reads'].append([limit,None])
        raise
    if fd in frozen: frozen[fd]['reads'].append([limit,len(chunk)])
    return chunk
_Stream.freeze=freeze
os.read=read
custody=LocalDeliveryCustody()
io=CarrierIO(TerminalInput(slave,slave,'fixture',Source()),SinkOutput(stdout,stderr))
child=r'''
import os,pathlib,sys,threading,time
pidfile=pathlib.Path(sys.argv[1])
probe=int(sys.argv[2])
flood=sys.argv[3]=='True'
read_fd,write_fd=os.pipe()
descendant=os.fork()
if descendant:
    os.close(write_fd)
    assert os.read(read_fd,1)==b'R'
    os.close(read_fd)
    os.write(1,b'S'*4096)
    os.write(2,b'E'*4096)
    pidfile.write_text(str(descendant))
    os._exit(0)
os.close(read_fd)
# The inherited writer holds output only, never the lent terminal slave.
os.close(0)
os.write(write_fd,b'R')
os.close(write_fd)
def watch_probe():
    until=time.monotonic()+5
    while time.monotonic()<until:
        try: os.kill(probe,0)
        except ProcessLookupError: os._exit(94)
        time.sleep(.01)
    os._exit(95)
threading.Thread(target=watch_probe,daemon=True).start()
while not pidfile.exists(): time.sleep(.001)
try:
    while True:
        if flood:
            os.write(1,b'S'*65536)
            os.write(2,b'E'*65536)
        else: time.sleep(.01)
except BrokenPipeError: pass
os._exit(0)
'''
try:
    started=monotonic()
    result=run_terminal_relay_candidate(
        [sys.executable,'-c',child,str(pidfile),str(os.getpid()),str(flood)],
        io=io,deadline=Deadline.after(2),custody=custody)
    elapsed=monotonic()-started
    print(json.dumps({'elapsed':elapsed,'counts':[stdout.count,stderr.count],'stalls':stalls,'delivery_clock':advanced,
        'frozen':list(frozen.values()),'result':{'started':result.started,'local_status':result.local_status,'exit_status':result.exit_status,
            'failure':None if result.failure is None else result.failure.value,
            'complete':[result.stdout.complete,result.stderr.complete]}}),flush=True)
    assert result.started and result.local_status==result.exit_status==0
    assert result.failure in ((Failure.OUTPUT,Failure.DEADLINE) if flood else (Failure.OUTPUT,))
    assert not result.stdout.complete and not result.stderr.complete
    assert result.stdout.data==result.stderr.data==b''
    assert sorted(stalls)==['stderr','stdout']
    assert stdout.count>=4096 and stderr.count>=4096
    assert .32<=elapsed<1.5
    assert len(frozen)==2
    for state in frozen.values():
        assert .1<=state['at']<.16
        quota=state['quota']
        probes=0
        collected=0
        probe_bytes=0
        for limit,count in state['reads']:
            if quota:
                assert 0<limit<=min(quota,65536) and count is not None
                quota-=count
                collected+=count
            else:
                probes+=1
                assert limit==1 and probes==1
                probe_bytes=count or 0
        delivered=stdout.count if state['name']=='stdout' else stderr.count
        prefix=state['before']+state['pending']+state['quota']
        assert delivered<=prefix+1
        if result.failure is Failure.OUTPUT:
            assert quota==0 and probes==1
            assert delivered==prefix+probe_bytes
    assert advanced<=2.01
    if result.failure is Failure.DEADLINE: assert elapsed+advanced>=2
    assert_preserved_terminal_mode(termios.tcgetattr(slave), mode)
    assert len(children)==1
    client=children[0]
    assert client.returncode==0 and client.stdout.closed and client.stderr.closed
    try: os.waitpid(client.pid,os.WNOHANG)
    except ChildProcessError: pass
    else: raise AssertionError('Client was not reaped')
finally:
    assert custody.close(Deadline.after(3))
    for client in children:
        if client.poll() is None: client.kill()
        client.wait(timeout=2)
        for pipe in (client.stdout,client.stderr):
            if pipe is not None: pipe.close()
    if pidfile.exists():
        descendant=int(pidfile.read_text())
        try: os.kill(descendant,signal.SIGKILL)
        except ProcessLookupError: pass
        assert os.waitpid(descendant,0)[0]==descendant
        try: os.waitpid(descendant,os.WNOHANG)
        except ChildProcessError: pass
        else: raise AssertionError('Inherited writer was not reaped')
    assert_preserved_terminal_mode(termios.tcgetattr(slave), mode)
    os.close(slave)
    os.close(master)
"""
    code = with_terminal_mode_assertion(code)
    completed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "descendant.pid"), str(flood)],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, (
        f"Owned drain probe exited {completed.returncode}\n"
        f"stdout: {completed.stdout[-4096:].decode(errors='replace')}\n"
        f"stderr: {completed.stderr[-4096:].decode(errors='replace')}"
    )
    proof = json.loads(completed.stdout)
    assert proof["counts"][0] >= 4096 and proof["counts"][1] >= 4096
    assert sorted(proof["stalls"]) == ["stderr", "stdout"]
    assert completed.stderr == b""


@pytest.mark.parametrize(
    "size,capacity,seconds",
    [
        (65_536, None, 5),
        (262_144, None, 5),
        (1_048_576, None, 5),
        (262_144, 4096, 5),
        (262_144, 262_144, 5),
        (65_536, None, None),
    ],
)
def test_finite_slow_output_preserves_native_frozen_prefix(
    size: int, capacity: int | None, seconds: float | None
) -> None:
    # The disposable process owns all PTYs and children even if an assertion fails.
    code = r"""
import fcntl,json,os,subprocess,sys,termios,threading,time
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import CarrierIO,Deadline,SinkOutput,TerminalInput
from agentworks.execution.carriers.ssh._terminal_relay import run_terminal_relay_candidate,_Stream
size=int(sys.argv[1])
capacity=None if sys.argv[2]=='None' else int(sys.argv[2])
seconds=None if sys.argv[3]=='None' else float(sys.argv[3])
master,slave=os.openpty()
mode=termios.tcgetattr(slave)
custody=LocalDeliveryCustody()
children=[]
ready=threading.Event()
frozen_stdout=threading.Event()
held_drain=capacity is not None and capacity>=size
pipe_capacity=None
native_launch=subprocess.Popen
native_freeze=_Stream.freeze
native_read=os.read
frozen={}
class Source:
    sent=False
    def try_read(self,limit):
        if not ready.is_set(): return None
        if not self.sent:
            self.sent=True
            return b'G'
        return b''
class Sink:
    data=bytearray()
    def try_write(self,data):
        if not ready.is_set():
            assert bytes(data)==b'READY'
            ready.set()
            return len(data)
        # The enlarged pipe holds the entire finite payload. Retain pending
        # output until its actual native snapshot, independent of scheduling.
        if held_drain and not frozen_stdout.is_set(): return None
        time.sleep(.002)
        count=min(1024,len(data))
        self.data.extend(data[:count])
        return count
class Diagnostics:
    def try_write(self,data): raise AssertionError('Unexpected child diagnostics')
sink=Sink()
def launch(*args,**kwargs):
    global pipe_capacity
    child=native_launch(*args,**kwargs)
    children.append(child)
    if capacity is not None:
        assert fcntl.fcntl(child.stdout.fileno(),fcntl.F_SETPIPE_SZ,capacity)>=capacity
    pipe_capacity=fcntl.fcntl(child.stdout.fileno(),fcntl.F_GETPIPE_SZ)
    return child
def freeze(stream,pipe):
    if stream.eof: return native_freeze(stream,pipe)
    fd=pipe.fileno()
    assert fd not in frozen
    pending=0 if stream.pending is None else len(stream.pending)
    native_freeze(stream,pipe)
    is_stdout=stream.sink is sink
    frozen[fd]={'name':'stdout' if is_stdout else 'stderr',
        'before':len(sink.data) if is_stdout else 0,'pending':pending,'quota':stream.quota,'reads':[]}
    if is_stdout: frozen_stdout.set()
def read(fd,limit):
    chunk=native_read(fd,limit)
    if fd in frozen: frozen[fd]['reads'].append([limit,len(chunk)])
    return chunk
subprocess.Popen=launch
_Stream.freeze=freeze
os.read=read
child='''import os,tty,sys
size=int(sys.argv[1])
tty.setraw(0)
os.write(1,b'READY')
assert os.read(0,1)==b'G'
pending=bytes(range(256))*(size//256)
while pending: pending=pending[os.write(1,pending):]
'''
try:
    result=run_terminal_relay_candidate([sys.executable,'-c',child,str(size)],
        io=CarrierIO(TerminalInput(slave,slave,'fixture',Source()),SinkOutput(sink,Diagnostics())),
        deadline=Deadline.after(seconds),custody=custody)
    assert result.started and result.exit_status==result.local_status==0 and result.failure is None
    assert result.stdout.complete and result.stderr.complete
    assert sink.data==bytes(range(256))*(size//256)
    # Finite output can reach both EOFs before the cutoff. Any actual frozen
    # stream still owes its entire snapshot plus exactly one EOF probe.
    for state in frozen.values():
        expected=size if state['name']=='stdout' else 0
        assert state['before']+state['pending']+state['quota']==expected
        quota=state['quota']
        probes=0
        for limit,count in state['reads']:
            if quota:
                assert 0<limit<=min(quota,65536) and 0<count<=limit
                quota-=count
            else:
                probes+=1
                assert probes==1 and limit==1 and count==0
        assert quota==0 and probes==1
    if held_drain:
        assert frozen_stdout.is_set()
        state=next(state for state in frozen.values() if state['name']=='stdout')
        assert state['quota']>65536
    print(json.dumps({'bytes':len(sink.data),'pipe_capacity':pipe_capacity,
        'frozen':list(frozen.values()),'complete':True}),flush=True)
finally:
    assert custody.close(Deadline.after(3))
    assert_preserved_terminal_mode(termios.tcgetattr(slave), mode)
    for child in children:
        assert child.returncode==0 and child.stdout.closed and child.stderr.closed
        try: os.waitpid(child.pid,os.WNOHANG)
        except ChildProcessError: pass
        else: raise AssertionError('Client not reaped')
    os.close(slave)
    os.close(master)
"""
    code = with_terminal_mode_assertion(code)
    completed = subprocess.run(
        [sys.executable, "-c", code, str(size), str(capacity), str(seconds)],
        capture_output=True,
        timeout=12,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout[-4096:], completed.stderr[-4096:])
    proof = json.loads(completed.stdout)
    assert proof["bytes"] == size and proof["complete"]
    assert completed.stderr == b""


@pytest.mark.parametrize("boundary", ["starvation", "control", "query"])
def test_frozen_prefix_respects_original_deadline_and_control(boundary: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import termios
    import time
    from threading import Event

    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution.carrier import CarrierIO, Deadline, Failure, SinkOutput, TerminalInput
    from agentworks.execution.carriers.ssh._terminal_relay import _Attempt, _Stream

    master, slave = os.openpty()
    mode = termios.tcgetattr(slave)
    custody = LocalDeliveryCustody()
    frozen = Event()
    primary = KeyboardInterrupt("owned-post-cutoff-control")
    native_freeze, native_launch = _Stream.freeze, subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []
    reads: list[int] = []
    failed_fd: int | None = None
    delivered = bytearray()
    native_read = os.read

    class Source:
        def try_read(self, limit: int) -> bytes:
            return b""

    class Sink:
        def try_write(self, data: memoryview) -> int | None:
            assert bytes(data) == b"X" * len(data)
            if frozen.is_set() and boundary == "control":
                raise primary
            if frozen.is_set() and boundary == "query":
                delivered.extend(data)
                return len(data)
            return None

    def freeze(stream: _Stream, pipe: IO[bytes]) -> None:
        nonlocal failed_fd
        if stream.eof:
            return
        frozen.set()
        if boundary == "query":
            failed_fd = pipe.fileno()
            raise OSError("private-native-queue-canary")
        native_freeze(stream, pipe)

    def launch(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = cast("subprocess.Popen[bytes]", native_launch(*args, **kwargs))
        children.append(child)
        return child

    def read(fd: int, limit: int) -> bytes:
        if frozen.is_set() and (failed_fd is None or fd == failed_fd):
            reads.append(limit)
        return native_read(fd, limit)

    monkeypatch.setattr(_Stream, "freeze", freeze)
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(os, "read", read)
    io = CarrierIO(TerminalInput(slave, slave, "fixture", Source()), SinkOutput(Sink(), Sink()))
    attempt = _Attempt([sys.executable, "-c", "import os; os.write(1,b'X'*4096)"], io, Deadline.after(0.35), custody)
    started = time.monotonic()
    try:
        if boundary == "control":
            with pytest.raises(KeyboardInterrupt) as raised:
                attempt.run()
            assert raised.value is primary and primary.__cause__ is None
        else:
            result = attempt.run()
            assert result.failure is (Failure.DEADLINE if boundary == "starvation" else Failure.OUTPUT)
            assert result.started and result.exit_status == result.local_status == 0
            assert not result.stdout.complete
            assert "private-native-queue-canary" not in repr(result)
        assert frozen.is_set()
        assert reads == []  # Pending bytes must precede any EOF or quota read.
        if boundary == "query":
            assert delivered == b"X" * 4096
        assert time.monotonic() - started < 0.8
    finally:
        assert custody.close(Deadline.after(3))
        assert_preserved_terminal_mode(termios.tcgetattr(slave), mode)
        for child in children:
            assert child.returncode == 0
            assert child.stdout is not None and child.stdout.closed
            assert child.stderr is not None and child.stderr.closed
            with pytest.raises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)
        os.close(slave)
        os.close(master)
