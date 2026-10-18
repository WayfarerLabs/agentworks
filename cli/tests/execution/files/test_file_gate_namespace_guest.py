"""Real isolated helper effects with fixture ownership, not root privilege proof."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentworks.execution import _file_effect_gate as gate
from agentworks.execution import _file_gate_namespace_guest as guest
from agentworks.execution._file_gate_namespace_bundle import _MODULE_NAMES, _PACKAGE
from agentworks.execution._file_wire import FileRecord, FileRecordKind
from agentworks.execution._file_wire_reader import FileRecordReader
from tests.execution.files._fixed_bundle_support import fixture_file_bundle

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux namespace helper")

_SCRIPT = r"""
import json,os,stat,sys,time
from pathlib import Path
from dataclasses import replace
from agentworks.execution import _file_effect_gate as gate
from agentworks.execution import _file_gate_namespace_guest as guest
from agentworks.execution._file_gate_control import setup_file_effect_gate,advance_file_effect_gate
from agentworks.execution._file_gate_namespace_protocol import NamespaceRequest
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
root=Path(sys.argv[1]); mode=sys.argv[2]
uid=os.geteuid(); gid=os.getegid()
fence=root/'fence'/'run'/'agentworks'/'file-gates-v1'
(fence/str(uid)).mkdir(parents=True,mode=0o700)
gate._GATE_NAMESPACE=str(fence); gate._ROOT_UID=uid
epoch=VMGuestIdentity('a'*32,'123e4567-e89b-12d3-a456-426614174000',10)
binding=setup_file_effect_gate(str(fence/str(uid)/('b'*64+'.db')),epoch,uid,'box',lambda:epoch)
identity=IdentityExpectation(uid,gid,tuple(sorted(set(os.getgroups())|{gid})))
request=NamespaceRequest('c'*32,uid,gid,identity,binding,5.0)
namespace=root/'run'/'agentworks'/'file-gates-v1'
(namespace/'0').mkdir(parents=True,mode=0o700)
rules=root/'etc'/'tmpfiles.d'; rules.mkdir(parents=True)
guest._RULE_DIRECTORY=str(rules); guest._RULE_GID=gid
# Shared ancestors above root are outside this synthetic ownership fixture.
# Validate real fixture anchors; retain production leaf checks and direct effects.
def open_fixture_rule_directory():
 for anchor in (root,root/'etc',rules):
  metadata=anchor.lstat()
  if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid!=uid or metadata.st_mode&0o022:
   raise ValueError('unsafe fixture rule anchor')
  guest._no_acl(str(anchor))
 return os.open(rules,os.O_RDONLY|os.O_DIRECTORY|gate._open_flags())
guest._open_rule_directory=open_fixture_rule_directory
guest._identity=lambda:epoch
agent=namespace/str(uid)
rule=rules/f'agentworks-file-gates-uid-{uid}.conf'
if mode in ('unsafe-mode','unsafe-gid','unsafe-acl','unsafe-link','unsafe-rule','linked-rule','reuse'):
 agent.mkdir(mode=0o700)
if mode=='unsafe-mode':agent.chmod(0o755)
if mode=='unsafe-gid':request=replace(request,gid=gid+1)
if mode=='unsafe-acl':
 import subprocess
 subprocess.run(['/usr/bin/setfacl','-m','d:u::rwx,d:g::---,d:o::---',str(agent)],check=True)
if mode=='unsafe-link':
 agent.rmdir(); agent.symlink_to(rules,target_is_directory=True)
content=f'd {namespace}/{uid} 0700 {uid} {gid} - -\n'.encode().replace(b'\\n',b'\n')
if mode in ('unsafe-rule','partial-rule','linked-rule','reuse'):
 rule.write_bytes(b'wrong' if mode in ('unsafe-rule','partial-rule') else content);rule.chmod(0o644)
if mode=='temp-conflict':
 (rules/(f'.agentworks-file-gates-{uid}-'+ 'c'*32 +'.tmp')).write_bytes(b'unowned')
if mode=='linked-rule':os.link(rule,rules/'unexpected')
if mode=='absent-anchor':
 rules.rmdir()
if mode in ('stale','missing-root','unsafe-root'):
 if mode=='stale':advance_file_effect_gate(replace(binding,proposed_generation=bytes(range(16))),lambda:epoch)
 if mode=='missing-root':(fence/str(uid)).rename(fence/'retained-root')
 if mode=='unsafe-root':(fence/str(uid)).chmod(0o755)
 print(guest._operate(request,time.monotonic()+5).value);sys.exit(0)
real_write=os.write;real_unlink=os.unlink
def write(fd,data):
 result=real_write(fd,data)
 if mode=='interrupted':
  (root/'entered').touch()
  while True:time.sleep(.01)
 if mode=='partial':raise OSError('interrupted write')
 return result
def unlink(path,*args,**kwargs):
 if mode=='cleanup-uncertain' and str(path).endswith('.tmp'):raise OSError('cleanup uncertain')
 return real_unlink(path,*args,**kwargs)
os.write=write;os.unlink=unlink
def audit(event,args):
 if event in ('subprocess.Popen','os.system','os.fork','os.posix_spawn'):raise AssertionError('mutating child')
sys.addaudithook(audit)
with gate.hold_file_effect_gate(binding,lambda:epoch,expires_at=time.monotonic()+5):
 gate._GATE_NAMESPACE=str(namespace)
 if mode=='deadline':status=guest._prepare_locked(request,0.0)
 else:status=guest._prepare_locked(request,time.monotonic()+5)
temporary_count=len(list(rules.glob('*.tmp'))) if rules.exists() else 0
print(json.dumps({'status':status.value,'rule':rule.exists(),'temp':temporary_count}))
"""


@pytest.mark.parametrize("runtime", [Path(sys.executable), Path("/usr/bin/python3.11")], ids=["current", "python311"])
@pytest.mark.parametrize(
    ("mode", "status", "rule", "temporary"),
    [
        ("new", "prepared", True, 0),
        ("reuse", "prepared", True, 0),
        ("unsafe-mode", "refused", False, 0),
        ("unsafe-gid", "refused", False, 0),
        ("unsafe-acl", "refused", False, 0),
        ("unsafe-link", "refused", False, 0),
        ("unsafe-rule", "refused", True, 0),
        ("partial-rule", "uncertain", True, 0),
        ("temp-conflict", "uncertain", False, 1),
        ("linked-rule", "refused", True, 0),
        ("absent-anchor", "refused", False, 0),
        ("partial", "uncertain", False, 0),
        ("cleanup-uncertain", "uncertain", True, 1),
        ("deadline", "refused", False, 0),
    ],
)
def test_root_fenced_direct_effects(
    tmp_path: Path, runtime: Path, mode: str, status: str, rule: bool, temporary: int
) -> None:
    if not runtime.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {runtime}")
    completed = subprocess.run(
        [runtime, "-B", "-c", _SCRIPT, str(tmp_path), mode], capture_output=True, timeout=10, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"status": status, "rule": rule, "temp": temporary}
    if mode in {"new", "reuse"}:
        metadata = (tmp_path / "run" / "agentworks" / "file-gates-v1" / str(os.geteuid())).stat()
        assert metadata.st_uid == os.geteuid() and metadata.st_gid == os.getegid()
        assert metadata.st_mode & 0o7777 == 0o700


@pytest.mark.parametrize("mode", ["stale", "missing-root", "unsafe-root"])
def test_root_fence_refuses_before_namespace_effects(tmp_path: Path, mode: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-B", "-c", _SCRIPT, str(tmp_path), mode], capture_output=True, timeout=10, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == b"refused"
    assert not (tmp_path / "run" / "agentworks" / "file-gates-v1" / str(os.geteuid())).exists()


def test_killed_helper_never_exposes_partial_configuration(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", _SCRIPT, str(tmp_path), "interrupted"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        until = time.monotonic() + 5
        while not (tmp_path / "entered").exists() and process.poll() is None and time.monotonic() < until:
            time.sleep(0.01)
        assert (tmp_path / "entered").exists()
    finally:
        process.kill()
        process.communicate(timeout=5)
    assert process.returncode == -9
    rules = tmp_path / "etc" / "tmpfiles.d"
    assert not list(rules.glob("*.conf"))
    assert len(list(rules.glob("*.tmp"))) == 1


def test_separate_bundle_executes_in_bookworm_python() -> None:
    runtime = Path("/usr/bin/python3.11")
    if not runtime.is_file():
        pytest.skip("Bookworm Python 3.11 is unavailable on this host")
    modules = ("_vm_guest_identity_protocol", "_vm_guest_identity_guest", *_MODULE_NAMES)
    bundle = fixture_file_bundle(_PACKAGE, modules, "_file_gate_namespace_guest")
    # Exercise the shipped module closure, without host application imports.
    completed = subprocess.run(
        [str(runtime), "-I", "-S", "-B", "-c", bundle.bootstrap, "0" * 32],
        input=bundle.prefix + b"{}",
        capture_output=True,
        timeout=10,
        check=False,
    )
    records: list[FileRecord] = []
    reader = FileRecordReader("0" * 32, records.append)
    reader.try_write(memoryview(completed.stdout))
    reader.finish()
    assert completed.returncode == 0 and completed.stderr == b""
    assert reader.error is None
    assert [record.kind for record in records] == [FileRecordKind.RESULT, FileRecordKind.FINISHED]


@pytest.mark.parametrize("unsafe", ["foreign-owner", "writable", "symlink"])
def test_production_rule_opener_refuses_unsafe_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    metadata = list(tmp_path.stat())
    if unsafe == "foreign-owner":
        metadata[4] = os.geteuid() + 1
    elif unsafe == "writable":
        metadata[0] = stat.S_IFDIR | 0o1777
    else:
        metadata[0] = stat.S_IFLNK | 0o777
    observed: list[str] = []

    def first_ancestor(path: str) -> os.stat_result:
        observed.append(path)
        assert path == "/fixture-anchor"
        return os.stat_result(metadata)

    def unexpected_open(*args: object, **kwargs: object) -> int:
        pytest.fail("unsafe ancestor reached directory open")

    with monkeypatch.context() as patch:
        patch.setattr(gate, "_ROOT_UID", os.geteuid())
        patch.setattr(guest, "_RULE_DIRECTORY", "/fixture-anchor/etc/tmpfiles.d")
        patch.setattr(os, "lstat", first_ancestor)
        patch.setattr(os, "open", unexpected_open)
        with pytest.raises(ValueError):
            guest._open_rule_directory()
    assert observed == ["/fixture-anchor"]
