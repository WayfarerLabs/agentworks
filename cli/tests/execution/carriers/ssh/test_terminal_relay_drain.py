"""Post-exit collection pauses for pending sinks and bounds inherited writers."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Owned probe uses Linux child subreaping")


@pytest.mark.parametrize("flood", [False, True])
def test_alternating_sink_stalls_preserve_bounded_inherited_output(tmp_path: Path, flood: bool) -> None:
    code = r"""
import ctypes,json,os,pathlib,signal,subprocess,sys,termios,threading,time
from agentworks.execution._process import LocalProcessOwner
from agentworks.execution.carrier import CarrierIO,Deadline,Failure,SinkOutput,TerminalInput
from agentworks.execution.carriers.ssh._terminal_relay import run_terminal_relay_candidate
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
original_snapshot=LocalProcessOwner.snapshot
def snapshot(owner):
    result=original_snapshot(owner)
    if result.exit_status is not None: exited.set()
    return result
LocalProcessOwner.snapshot=snapshot
class Source:
    def try_read(self,limit): return b''
stalls=[]
blocked_until=0
class Sink:
    def __init__(self,name,byte):
        self.name=name
        self.byte=byte
        self.count=0
        self.stalled=False
    def try_write(self,data):
        global blocked_until
        if not exited.is_set(): return None
        now=time.monotonic()
        if now<blocked_until: return None
        if not self.stalled:
            self.stalled=True
            stalls.append(self.name)
            blocked_until=now+.16
            return None
        delivered=min(len(data),1024)
        assert bytes(data[:delivered])==self.byte*delivered
        self.count+=delivered
        return delivered
stdout,stderr=Sink('stdout',b'S'),Sink('stderr',b'E')
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
    started=time.monotonic()
    result=run_terminal_relay_candidate(
        [sys.executable,'-c',child,str(pidfile),str(os.getpid()),str(flood)],
        io=io,deadline=Deadline.after(2))
    elapsed=time.monotonic()-started
    assert result.started and result.local_status==result.exit_status==0
    assert result.failure is Failure.OUTPUT
    assert not result.stdout.complete and not result.stderr.complete
    assert result.stdout.data==result.stderr.data==b''
    assert sorted(stalls)==['stderr','stdout']
    assert stdout.count>=4096 and stderr.count>=4096
    assert .32<=elapsed<1.5
    assert termios.tcgetattr(slave)==mode
    assert len(children)==1
    client=children[0]
    assert client.returncode==0 and client.stdout.closed and client.stderr.closed
    try: os.waitpid(client.pid,os.WNOHANG)
    except ChildProcessError: pass
    else: raise AssertionError('Client was not reaped')
    print(json.dumps({'elapsed':elapsed,'counts':[stdout.count,stderr.count],'stalls':stalls}))
finally:
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
    assert termios.tcgetattr(slave)==mode
    os.close(slave)
    os.close(master)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "descendant.pid"), str(flood)],
        capture_output=True,
        timeout=10,
        check=True,
    )
    proof = json.loads(completed.stdout)
    assert proof["counts"][0] >= 4096 and proof["counts"][1] >= 4096
    assert sorted(proof["stalls"]) == ["stderr", "stdout"]
    assert completed.stderr == b""
