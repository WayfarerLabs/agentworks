"""Actual packed start/controller and real children under a synthetic boundary."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

import pytest

from agentworks.execution import _managed_lease_guest as lease_guest
from agentworks.execution import _managed_lease_store as lease_store
from agentworks.execution import _managed_service_bundle as service_bundle
from agentworks.execution._helper_bundle import build_helper_modules
from agentworks.execution._managed_job_store import FactName, StoreError
from agentworks.execution._managed_lease_protocol import LeaseRequest
from agentworks.execution._managed_lease_store import publish_lease, read_lease
from agentworks.execution._managed_lease_wire import WINDOW_NS, sampled_lease
from agentworks.execution._managed_start_protocol import ManagedStartRequest, encode_request

from .test_managed_operation_lease import job, launch
from .test_managed_service_guest import _store
from .test_managed_start import GUEST, NONCE, ROOT

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux packed controller and process groups")


def _wait_file(path: Path) -> None:
    until = time.monotonic() + 5
    while not path.exists() and time.monotonic() < until:
        time.sleep(0.01)
    assert path.exists()


def _set_clock(path: Path, value: int) -> None:
    stage = path.with_suffix(".stage")
    stage.write_text(str(value))
    stage.replace(path)


def _program() -> str:
    package = "_agw_lease_process"
    modules = (
        *service_bundle._MODULES,
        "_file_wire",
        "_vm_guest_identity_protocol",
        "_vm_guest_identity_guest",
        "_managed_start_protocol",
    )
    source = build_helper_modules(package, modules)
    start_source = files("agentworks.execution").joinpath("_managed_start_guest.py").read_text(encoding="utf-8")
    source += (
        f"m=types.ModuleType('{package}._managed_service_bundle');m.FIXED_SOURCE={service_bundle.FIXED_SOURCE!r}\n"
        f"sys.modules[m.__name__]=m\n"
        f"m=types.ModuleType('{package}._managed_start_guest');m.__package__={package!r};sys.modules[m.__name__]=m\n"
        f"exec(compile({start_source!r},'<packed-start>','exec'),m.__dict__)\n"
    )
    source += r"""
import os, signal, time
from pathlib import Path
root=Path(sys.argv[1])
phase=sys.argv[2]
p=sys.modules['_agw_lease_process._managed_start_protocol']
s=sys.modules['_agw_lease_process._managed_start_guest']
g=sys.modules['_agw_lease_process._managed_service_guest']
c=sys.modules['_agw_lease_process._managed_lease_controller']
w=sys.modules['_agw_lease_process._managed_lease_wire']
st=sys.modules['_agw_lease_process._managed_job_store']
w.time.clock_gettime_ns=lambda clock:int((root/'clock').read_text())
original=c.LeaseControl.stop_due
def tick(self,store):
    due=original(self,store)
    if self.expires_ns>60000000000:(root/'renewed').touch()
    if int((root/'clock').read_text())>=60000000000 and not due:(root/'past-initial').touch()
    if self.closed:(root/'closed').touch()
    return due
c.LeaseControl.stop_due=tick
g._STOP_GRACE_SECONDS=0.05
g._CLEANUP_SECONDS=0.5
class Boundary:
    def __init__(self):self.pid=None;self.killed=False
    def place(self,pid):
        self.pid=pid
        (root/'placed').write_text(str(pid))
        os.setpgid(pid,pid)
        if phase=='placement':(root/'clock').write_text('60000000000')
    def kill(self):
        self.killed=True
        try:os.killpg(self.pid,signal.SIGKILL)
        except ProcessLookupError:pass
    def empty(self):return self.killed
request=p.decode_request(sys.stdin.buffer.read())
fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
store=st.ManagedJobStore(request.job.operation_lease.run_id,_namespace='managed',_owner_uid=os.getuid(),_anchor_fd=fd)
os.close(fd)
boundary=Boundary()
if phase in {'release-read','release-stop-read'}:
    real_read=store.read_stop_request
    calls=0
    def delayed_stop_read():
        global calls
        calls+=1
        result=real_read()
        (root/'stop-reads').write_text(str(calls))
        if calls==1:(root/'clock').write_text('60000000001')
        elif phase=='release-read':
            until=time.monotonic()+1
            while not (root/'entered').exists() and time.monotonic()<until:time.sleep(0.001)
        return result
def notify():
    if phase=='release':(root/'clock').write_text('60000000000')
    if phase=='release-read':(root/'clock').write_text('59000000000')
    if phase=='release-stop-read':
        (root/'clock').write_text('59000000000')
        store.publish_stop_request(request.job.launch)
def runner(argv):
    assert argv[-2]==request.job.operation_lease.run_id
    if phase in {'release-read','release-stop-read'}:store.read_stop_request=delayed_stop_read
    return g.run(request.job.operation_lease.run_id,_store=store,_boundary=boundary,
                 _notify=notify,_identity_check=False,_apply_identity=False)
try:
    prepared=s._prepare_start(request,store,python=sys.executable,runner=runner)
    assert prepared.result.exit_code== (125 if phase in {'placement','release','release-read'} else 0)
finally:store.close()
"""
    return source


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
@pytest.mark.parametrize(
    "phase", ["placement", "release", "release-read", "release-stop-read", "expiry", "renewal", "stop"]
)
def test_packed_operation_start_lease_and_existing_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interpreter: str, phase: str
) -> None:
    if not Path(interpreter).is_file():
        pytest.skip("interpreter unavailable")
    tmp_path.chmod(0o700)
    clock = tmp_path / "clock"
    clock.write_text("0")
    marker = tmp_path / "entered"
    child = (
        "import os,signal,time\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "p=os.fork()\n"
        "if p:\n"
        f" with open({str(marker.with_suffix('.stage'))!r},'w') as f:f.write(str(os.getpid())+' '+str(p))\n"
        f" os.replace({str(marker.with_suffix('.stage'))!r},{str(marker)!r})\n"
        "while True: time.sleep(0.01)\n"
    )
    request = ManagedStartRequest(NONCE, ROOT, replace(job(0), argv=(interpreter, "-c", child)), GUEST)
    worker = subprocess.Popen(
        [interpreter, "-I", "-S", "-B", "-c", _program(), str(tmp_path), phase],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert worker.stdin is not None
    worker.stdin.write(encode_request(request))
    worker.stdin.close()
    worker.stdin = None
    parent_pid = None
    descendant_pid = None
    try:
        if phase not in {"placement", "release", "release-read", "release-stop-read"}:
            _wait_file(marker)
            parent_pid, descendant_pid = map(int, marker.read_text().split())
            with _store(tmp_path) as store:
                initial = read_lease(store, launch())
                assert initial is not None and initial.expires_ns == WINDOW_NS
                if phase == "renewal":
                    _set_clock(clock, WINDOW_NS // 2)
                    monkeypatch.setattr(lease_store, "boottime_ns", lambda: int(clock.read_text()))
                    renewal = sampled_lease(launch(), WINDOW_NS // 2)
                    lease_guest._publish(LeaseRequest(NONCE, ROOT, GUEST, launch(), renewal), store)
                    _wait_file(tmp_path / "renewed")
                    _set_clock(clock, WINDOW_NS)
                    _wait_file(tmp_path / "past-initial")
                    assert worker.poll() is None
                    os.kill(parent_pid, 0)
                    _set_clock(clock, renewal.expires_ns)
                elif phase == "stop":
                    store.publish_stop_request(launch())
                    with pytest.raises(StoreError):
                        publish_lease(store, launch(), sampled_lease(launch(), 1))
                else:
                    _set_clock(clock, WINDOW_NS)
        stdout, stderr = worker.communicate(timeout=5)
        if phase == "release-read":
            assert not marker.exists(), marker.read_text()
            assert (tmp_path / "stop-reads").read_text() == "1"
            assert clock.read_text() == str(WINDOW_NS + 1)
        if phase == "release-stop-read":
            assert not marker.exists()
            assert int((tmp_path / "stop-reads").read_text()) >= 1
            assert clock.read_text() == str(WINDOW_NS + 1)
        assert worker.returncode == 0, stderr
        assert stdout == b"" and stderr == b""
        with _store(tmp_path) as store:
            if phase in {"placement", "release", "release-read", "release-stop-read"}:
                assert not marker.exists()
                assert not Path(f"/proc/{int((tmp_path / 'placed').read_text())}").exists()
                assert store.read_fact(FactName.LAUNCH) == (None if phase == "placement" else launch())
                assert (store.read_fact(FactName.BOUNDARY_EMPTY) is not None) == (phase == "release-stop-read")
            else:
                assert store.read_fact(FactName.LAUNCH) == launch()
                assert store.read_fact(FactName.BOUNDARY_EMPTY) is not None
                assert store.read_fact(FactName.WAIT) is None
                assert store.read_fact(FactName.STDOUT_END) is not None
                assert store.read_fact(FactName.STDERR_END) is not None
                assert not Path(f"/proc/{parent_pid}").exists()
                descendant = Path(f"/proc/{descendant_pid}/stat")
                assert not descendant.exists() or descendant.read_text().split()[2] == "Z"
    finally:
        if parent_pid is None and (tmp_path / "placed").exists():
            parent_pid = int((tmp_path / "placed").read_text())
        if worker.poll() is None:
            worker.kill()
            worker.communicate(timeout=5)
        if parent_pid is not None:
            with suppress(ProcessLookupError):
                os.killpg(parent_pid, signal.SIGKILL)
