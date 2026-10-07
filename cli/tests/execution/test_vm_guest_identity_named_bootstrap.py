"""Canonical guest facts after private named admission, without root effects."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.execution import _guest_bootstrap
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState, RuntimeSelection, RuntimeTargetOS
from agentworks.execution._vm_guest_identity import VMGuestIdentityObservationError, observe_vm_guest_identity
from agentworks.execution._vm_guest_identity_bundle import _NAMED_BODY_SOURCE
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, decode_vm_guest_identity_response
from agentworks.execution.binding import _EarlyGuestFactsRoute
from agentworks.execution.carrier import Deadline, Dispatch, EndOfInput, ExitStatus
from tests.execution.files._file_snapshot_support import LocalCarrier

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux named guest admission")
_ROOT = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.DIRECT)
_NONCE = "a" * 32
_MARKER = "b" * 32
_BOOT = "12345678-1234-1234-1234-123456789abc"


@pytest.mark.parametrize("runtime", [Path(sys.executable), Path("/usr/bin/python3.11")])
@pytest.mark.parametrize("uid", [1001, 0])
def test_exact_named_body_loads_canonical_modules_after_admission_and_binds_reader_once(
    tmp_path: Path, runtime: Path, uid: int
) -> None:
    if not runtime.is_file():
        pytest.skip("selected interpreter unavailable")
    stat = tmp_path / "stat"
    stat.write_bytes(("1 (init) S " + " ".join(["0"] * 18 + ["1234"]) + "\n").encode())
    source = Path(_guest_bootstrap.__file__).read_text(encoding="utf-8")
    # The child runs unchanged packaged sources; only credentials and guest
    # paths are fixtures. This proves composition, not native privileged entry.
    program = f"""
import builtins,os,pwd,sys
scope={{'__name__':'__main__'}}
exec(compile({source!r},'<bootstrap>','exec'),scope)
scope['_INIT_PATH']={str(stat)!r}
scope['_capabilities_zero']=lambda **kwargs: True
credentials={{'uid':(0,0,0),'gid':(0,0,0),'groups':(0,)}}
os.getresuid=lambda: credentials['uid']
os.getresgid=lambda: credentials['gid']
os.getegid=lambda: credentials['gid'][1]
os.getgroups=lambda: list(credentials['groups'])
os.setgroups=lambda value: credentials.update(groups=tuple(value))
os.setresgid=lambda *value: credentials.update(gid=value)
os.setresuid=lambda *value: credentials.update(uid=value)
os.getgrouplist=lambda account,gid: [gid,1003]
pwd.getpwnam=lambda account: pwd.struct_passwd((account,'x',{uid},1002,'','/unused','/unused'))
original_exec=builtins.exec
events=[]
def execute(code,namespace,*local):
 if namespace.get('__name__')=='agentworks.execution._vm_guest_identity_guest':
  assert credentials['uid']==({uid},)*3
  assert credentials['gid']==(1002,)*3
  assert credentials['groups']==(1002,1003)
  events.append('load')
 original_exec(code,namespace,*local)
 if namespace.get('__name__')=='agentworks.execution._vm_guest_identity_guest':
  namespace['_read_marker']=lambda *args: {_MARKER!r}
  namespace['_read_boot_id']=lambda *args: {_BOOT!r}
  canonical_main=namespace['main']
  def main(nonce):
   assert callable(namespace['_INIT_READER'])
   assert nonce=={_NONCE!r}
   events.append('main')
   return canonical_main(nonce)
  namespace['main']=main
builtins.exec=execute
sys.argv=['fixed-helper',{_NONCE!r}]
assert scope['main_named']('configured-admin',{_NAMED_BODY_SOURCE!r})==0
assert events==['load','main']
"""
    result = subprocess.run(
        [str(runtime), "-I", "-S", "-B", "-c", program],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stderr == b""
    decoded = decode_vm_guest_identity_response(result.stdout, _NONCE)
    assert decoded.identity == VMGuestIdentity(_MARKER, _BOOT, 1234)


def test_actual_nonroot_admission_has_runtime_evidence_without_guest_evidence() -> None:
    if os.geteuid() == 0 or not Path("/usr/bin/python3").is_file():
        pytest.skip("requires nonroot Linux and system Python")
    carrier = LocalCarrier()
    route = _EarlyGuestFactsRoute(carrier, _ROOT, "configured-admin")
    result = observe_vm_guest_identity(
        carrier,
        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX),
        deadline=Deadline.after(10),
        _bootstrap_route=route,
    )
    assert carrier.calls == 1
    assert carrier.io is not None and isinstance(carrier.io.input, EndOfInput)
    assert result.dispatch is Dispatch.SENT
    assert result.carrier_completion == ExitStatus(code=125)
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    assert result.runtime_prerequisite.selected_path == "/usr/bin/python3"
    assert result.observation is not None
    assert result.observation.identity is None
    assert result.observation.error is VMGuestIdentityObservationError.MISSING
