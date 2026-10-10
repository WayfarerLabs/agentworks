"""Real SIGINT is confined to an owned synthetic-process/PTY test subprocess."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from tests.execution.carriers.ssh._terminal_modes import with_terminal_mode_assertion

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Requires POSIX terminal descriptors and signals")


def test_real_repeated_sigint_retains_cleanup_worker() -> None:
    code = r"""
import fcntl,json,os,signal,subprocess,sys,termios,threading,time
from agentworks.execution.carrier import CarrierIO,Deadline,SinkOutput,TerminalInput
from agentworks.execution.carriers.ssh import _terminal_relay as relay
from agentworks.execution.carriers.ssh._terminal_posix import PosixTerminal
from agentworks.execution._process import LocalProcessOwner
from agentworks.execution._delivery_custody import LocalDeliveryCustody
master,slave=os.openpty()
mode=termios.tcgetattr(slave)
flags=fcntl.fcntl(slave,fcntl.F_GETFL),os.get_inheritable(slave)
ready=threading.Event()
waiting=threading.Event()
closing=threading.Event()
release_cleanup=threading.Event()
released=threading.Event()
errors=[]
owned=[]
settled=[]
children=[]
original_popen=subprocess.Popen
def launch(*args,**kwargs):
    child=original_popen(*args,**kwargs)
    children.append(child)
    return child
subprocess.Popen=launch
class Source:
    def try_read(self,limit): return None
class Sink:
    def try_write(self,data):
        ready.set()
        return len(data)
io=CarrierIO(TerminalInput(slave,slave,'fixture',Source(),True),SinkOutput(Sink(),Sink()))
child='''import os,tty,time,threading
parent=os.getppid()
def watch_parent():
    while os.getppid()==parent: time.sleep(.01)
    os._exit(92)
threading.Thread(target=watch_parent,daemon=True).start()
tty.setraw(0)
os.write(1,b'READY')
time.sleep(30)
'''
custody=LocalDeliveryCustody()
attempt=relay._Attempt([sys.executable,'-c',child],io,Deadline.after(5),custody)
original_wait=attempt._operation_done.wait
original_cleanup_wait=attempt._done.wait
original_close=LocalProcessOwner.close_bounded
original_acquire=PosixTerminal.acquire
original_release=PosixTerminal.release

def wait(timeout=None):
    waiting.set()
    try: return original_wait(timeout)
    except BaseException as error:
        errors.append(error)
        raise

def close(owner,deadline):
    closing.set()
    release_cleanup.wait()
    terminal=original_close(owner,deadline)
    settled.append(terminal.cleaned)
    return terminal

def acquire(terminal):
    original_acquire(terminal)
    owned.extend((terminal.master_fd,terminal.slave_fd))

def release(terminal):
    assert settled==[True]
    result=original_release(terminal)
    released.set()
    return result

def forbidden(*args,**kwargs):
    raise AssertionError('Thread status is not the completion fact')

def cleanup_wait(timeout=None):
    try: return original_cleanup_wait(timeout)
    except BaseException as error:
        errors.append(error)
        raise
attempt._operation_done.wait=wait
attempt._done.wait=cleanup_wait
attempt._worker.join=forbidden
attempt._worker.is_alive=forbidden
LocalProcessOwner.close_bounded=close
PosixTerminal.acquire=acquire
PosixTerminal.release=release

def interrupt():
    assert waiting.wait(2) and ready.wait(2)
    os.kill(os.getpid(),signal.SIGINT)
    assert closing.wait(2)
    time.sleep(.03)
    assert not released.is_set()
    os.kill(os.getpid(),signal.SIGINT)
    time.sleep(.03)
    release_cleanup.set()

interruptor=threading.Thread(target=interrupt)
interruptor.start()
try:
    attempt.run()
    raise AssertionError('SIGINT was lost')
except KeyboardInterrupt as error:
    assert len(errors)>=2 and error is errors[0]
    assert attempt._done.is_set() and released.is_set()
    assert_preserved_terminal_mode(termios.tcgetattr(slave), mode)
    assert (fcntl.fcntl(slave,fcntl.F_GETFL),os.get_inheritable(slave))==flags
    for fd in owned:
        try: os.fstat(fd)
        except OSError: pass
        else: raise AssertionError('Owned descriptor leaked')
    assert len(children)==1
    for child in children:
        assert child.returncode is not None
        try: os.waitpid(child.pid,os.WNOHANG)
        except ChildProcessError: pass
        else: raise AssertionError('Owned process not reaped')
    print(json.dumps({'interruptions':len(errors),'restored':True,'settled':settled,'closed':len(owned)}))
finally:
    release_cleanup.set()
    interruptor.join(3)
    assert custody.close(Deadline.after(3))
    os.close(slave)
    os.close(master)
"""
    code = with_terminal_mode_assertion(code)
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=15, check=False)
    assert completed.returncode == 0, (completed.stdout[-4096:], completed.stderr[-4096:])
    proof = json.loads(completed.stdout)
    assert proof["interruptions"] >= 2
    assert proof["restored"] is True
    assert proof["settled"] == [True]
    assert proof["closed"] == 2
    assert completed.stderr == b""
