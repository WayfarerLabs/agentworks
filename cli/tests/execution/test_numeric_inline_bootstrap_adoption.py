"""Private inline routing and packed execution with mocked privileged admission."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._execution_operation import OwnedInlineOutcome
from agentworks.execution._execution_result import reduce_owned_inline_result
from agentworks.execution._helper_bundle import RootGuestDelivery
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._inline import execute_inline_candidate, prepare_inline_candidate
from agentworks.execution._inline_bundle import FIXED_SOURCE, ROOT_PROGRAM
from agentworks.execution._inline_control import StreamRetention, WaitFact, WaitKind
from agentworks.execution._inline_request import MAX_MANIFEST_BYTES, decode_manifest
from agentworks.execution._runtime_prerequisite import (
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
    _NumericGuestBootstrap,
    build_root_guest_bootstrap_argv,
    build_runtime_identity_helper_argv,
)
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.execution.carriers.proxmox import ProxmoxCarrier
from agentworks.execution.models import Command, Script, Shell
from agentworks.execution.result import ApplicationState, ExitCode

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="inline bootstrap requires Linux")
_GUEST = VMGuestIdentity("a" * 32, "123e4567-e89b-12d3-a456-426614174000", 1234)
_ROOT = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.DIRECT)
_CONTEXT = _NumericGuestBootstrap(_ROOT, _GUEST)
_RUNTIME = RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3")

# Execute the delivered compressed program. Only privileged admission and the
# guest's external observations are mocked; the checkpoint and body run intact.
_MOCK_ADMISSION = """
import builtins,contextlib,json,os,sys
real_exec,real_read=builtins.exec,os.read
events=[]
observed=json.loads(sys.argv[3])
@contextlib.contextmanager
def admit(uid,gid,groups):
 assert (uid,gid,groups)==(os.geteuid(),os.getegid(),tuple(sorted(set(os.getgroups())|{os.getegid()})))
 events.append('admit')
 yield lambda:b'1 (init) S '+b'0 '*18+str(observed[2]).encode()+b'\\n'
def read(fd,count):
 if fd==0:events.append('request')
 return real_read(fd,count)
def enter(frame,event,arg):
 if event=='call' and frame.f_code.co_filename=='<agentworks-root-bootstrap>' and frame.f_code.co_name=='main':
  frame.f_globals['_admit']=admit
  sys.setprofile(None)
def execute(source,scope=None,local=None):
 if scope is None:scope=sys._getframe(1).f_globals
 real_exec(source,scope,local)
 name=scope.get('__name__','')
 if name=='_agw_inline._vm_guest_identity_guest':
  scope['_read_marker']=lambda *args:observed[0]
  scope['_read_boot_id']=lambda *args:observed[1]
 elif name.startswith('_agw_inline.') and not name.endswith('_vm_guest_identity_protocol'):
  events.append('remaining')
wrapper=sys.argv[2]
sys.argv=['fixed-helper',sys.argv[1]]
builtins.exec=execute
os.read=read
sys.setprofile(enter)
try:real_exec(compile(wrapper,'<delivered-wrapper>','exec'),{'__name__':'__main__'})
finally:sys.stderr.write(json.dumps(events))
"""


def _plan() -> IdentityPlan:
    return IdentityPlan(
        IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
        IdentityMode.DIRECT,
    )


class _PackedCarrier:
    features = ChannelFeatures()

    def __init__(self) -> None:
        self.events: list[str] = []
        self.observed = _GUEST
        self.damage: str | None = None

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        pass

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody | None = None,
    ) -> CarrierReport:
        assert isinstance(io.input, FiniteInput) and isinstance(io.output, SinkOutput)
        nonce = invocation.argv[invocation.argv.index("agentworks-runtime-prerequisite") + 1]
        guest = self.observed
        completed = subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                "-S",
                "-B",
                "-c",
                _MOCK_ADMISSION,
                nonce,
                invocation.argv[-2],
                json.dumps((guest.instance_marker, guest.boot_id, guest.init_start_ticks)),
            ],
            input=io.input.data,
            capture_output=True,
            timeout=deadline.remaining(),
            check=False,
        )
        self.events = json.loads(completed.stderr)
        data = completed.stdout
        if self.damage == "lost":
            data = b""
        elif self.damage == "invalid":
            data += f"AGWE1 {nonce} invalid frame\n".encode()
        ready = f"AGW_RUNTIME_1:{nonce}:ready:0\n".encode()
        io.output.stdout.try_write(memoryview(ready + data))
        output = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        return CarrierReport(Dispatch.SENT, ExitStatus(code=completed.returncode), 0, output, output, None)


@pytest.mark.parametrize("mode", [None, IdentityMode.DIRECT, IdentityMode.SUDO_ROOT])
def test_root_entry_is_distinct_from_body_and_manifest_is_unchanged(mode: IdentityMode | None) -> None:
    plan = IdentityPlan(IdentityExpectation(1001, 1002, (1002, 1003)), IdentityMode.DIRECT)
    context = None if mode is None else _NumericGuestBootstrap(replace(_ROOT, mode=mode), _GUEST)
    secret = "private payload $();'\\n"
    prepared = prepare_inline_candidate(
        Script(secret, Shell.SH),
        plan=plan,
        bootstrap=context,
        runtime_selection=_RUNTIME,
        stdin=b"\0\xff",
        env={"PRIVATE": secret},
        cwd="/tmp",
        sensitive=True,
    )
    if context is None:
        argv, _, _ = build_runtime_identity_helper_argv(
            plan,
            selection=_RUNTIME,
            fixed_source=FIXED_SOURCE,
            nonce=prepared.nonce,
        )
    else:
        argv, _, _ = build_root_guest_bootstrap_argv(
            context.root_entry,
            plan.expected,
            selection=_RUNTIME,
            program=ROOT_PROGRAM,
            nonce=prepared.nonce,
            expected_guest=context.guest,
        )
    assert prepared.invocation.argv == argv
    assert ROOT_PROGRAM.delivery is RootGuestDelivery.INLINE and ROOT_PROGRAM.prefix == b""
    assert secret not in " ".join(argv)
    assert isinstance(prepared.io.input, FiniteInput)
    manifest = decode_manifest(prepared.io.input.data)
    assert manifest.identity == plan.expected
    assert manifest.source == secret.encode() and manifest.stdin == b"\0\xff"
    assert manifest.env == (("PRIVATE", secret),) and manifest.cwd == "/tmp"
    legacy = prepare_inline_candidate(
        Script(secret, Shell.SH),
        plan=plan,
        runtime_selection=_RUNTIME,
        stdin=b"\0\xff",
        env={"PRIVATE": secret},
        cwd="/tmp",
        sensitive=True,
    )
    assert isinstance(legacy.io.input, FiniteInput)
    assert replace(decode_manifest(legacy.io.input.data), nonce=prepared.nonce) == manifest


@pytest.mark.parametrize(
    "runtime", [RuntimeSelection(RuntimeTargetOS.DARWIN), RuntimeSelection(RuntimeTargetOS.LINUX, "/tmp/python3")]
)
def test_wrong_runtime_refuses_preparation(runtime: RuntimeSelection) -> None:
    with pytest.raises(ValidationError):
        prepare_inline_candidate(Command(["true"]), plan=_plan(), bootstrap=_CONTEXT, runtime_selection=runtime)


@pytest.mark.parametrize("status", [0, 1, 255])
@pytest.mark.parametrize("script", [False, True])
def test_packed_program_preserves_literal_request_and_exact_wait(
    status: int,
    script: bool,
    tmp_path: Path,
) -> None:
    payload = bytes(range(256))
    argument = "literal ' $(); space\n"
    code = (
        "import os,sys;"
        f"assert sys.argv[1:]=={[argument]!r};"
        f"assert os.environ['INLINE_VALUE']=={argument!r};"
        f"assert os.getcwd()=={str(tmp_path)!r};"
        f"assert sys.stdin.buffer.read()=={payload!r};"
        "sys.stdout.buffer.write(b'out\\x00\\xff');sys.stderr.buffer.write(b'err\\x80');"
        f"raise SystemExit({status})"
    )
    argv = ["/usr/bin/python3", "-I", "-S", "-B", "-c", code, argument]
    request = Script(shlex.join(argv), Shell.SH) if script else Command(argv)
    prepared = prepare_inline_candidate(
        request,
        plan=_plan(),
        bootstrap=_CONTEXT,
        runtime_selection=_RUNTIME,
        stdin=payload,
        env={"INLINE_VALUE": argument},
        cwd=str(tmp_path),
    )
    carrier = _PackedCarrier()
    result = execute_inline_candidate(carrier, prepared, deadline=Deadline.after(10))
    observation = result.observation
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    assert result.carrier_completion == ExitStatus(code=0)
    assert observation is not None and observation.trusted_terminal
    assert observation.wait == WaitFact(WaitKind.EXIT, status)
    assert observation.stdout is not None and observation.stdout.data == b"out\0\xff"
    assert observation.stderr is not None and observation.stderr.data == b"err\x80"
    assert carrier.events.index("admit") < carrier.events.index("remaining") < carrier.events.index("request")
    public = reduce_owned_inline_result(OwnedInlineOutcome(candidate=result))
    assert public.application_state is ApplicationState.COMPLETED and public.status == ExitCode(status)


@pytest.mark.parametrize(
    "sensitive,capture_limit,retention",
    [
        (False, None, StreamRetention.DISCARDED),
        (True, 4096, StreamRetention.SUPPRESSED),
    ],
)
def test_packed_program_discards_or_suppresses_payload(
    sensitive: bool,
    capture_limit: int | None,
    retention: StreamRetention,
) -> None:
    prepared = prepare_inline_candidate(
        Command(["/bin/cat"]),
        plan=_plan(),
        bootstrap=_CONTEXT,
        runtime_selection=_RUNTIME,
        stdin=b"secret\0\xff",
        sensitive=sensitive,
        capture_limit=capture_limit,
    )
    assert isinstance(prepared.io.input, FiniteInput) and prepared.io.input.sensitive is sensitive
    result = execute_inline_candidate(_PackedCarrier(), prepared, deadline=Deadline.after(10))
    assert result.observation is not None and result.observation.trusted_terminal
    assert result.observation.stdout is not None and result.observation.stdout.data == b""
    assert result.observation.stdout.retention is retention
    assert result.observation.stderr is not None and result.observation.stderr.data == b""
    assert result.observation.stderr.retention is retention


@pytest.mark.parametrize("damage", ["lost", "invalid"])
def test_damaged_observation_keeps_application_uncertain(damage: str) -> None:
    prepared = prepare_inline_candidate(
        Command(["/bin/true"]),
        plan=_plan(),
        bootstrap=_CONTEXT,
        runtime_selection=_RUNTIME,
    )
    carrier = _PackedCarrier()
    carrier.damage = damage
    result = execute_inline_candidate(carrier, prepared, deadline=Deadline.after(10))
    assert result.carrier_completion == ExitStatus(code=0)
    assert result.observation is not None and not result.observation.trusted_terminal
    if damage == "invalid":
        assert result.observation.wait == WaitFact(WaitKind.EXIT, 0)
    public = reduce_owned_inline_result(OwnedInlineOutcome(candidate=result, requires_owner_retention=True))
    assert public.application_state is ApplicationState.UNKNOWN and public.status is None


@pytest.mark.parametrize(
    "field,value",
    [("instance_marker", "b" * 32), ("boot_id", "223e4567-e89b-12d3-a456-426614174000"), ("init_start_ticks", 1235)],
)
def test_guest_mismatch_refuses_before_request_and_body(field: str, value: str | int) -> None:
    prepared = prepare_inline_candidate(
        Command(["/bin/false"]),
        plan=_plan(),
        bootstrap=_CONTEXT,
        runtime_selection=_RUNTIME,
    )
    carrier = _PackedCarrier()
    carrier.observed = replace(_GUEST, **{field: value})
    result = execute_inline_candidate(carrier, prepared, deadline=Deadline.after(10))
    assert result.carrier_completion == ExitStatus(code=125)
    assert carrier.events == ["admit"]
    public = reduce_owned_inline_result(OwnedInlineOutcome(candidate=result, requires_owner_retention=True))
    assert public.application_state is ApplicationState.UNKNOWN and public.status is None


def test_real_nonroot_entry_refuses_without_reading_manifest_or_launching(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("requires real nonroot entry")
    side_effect = tmp_path / "launched"
    prepared = prepare_inline_candidate(
        Command(["/usr/bin/touch", str(side_effect)]),
        plan=_plan(),
        bootstrap=_CONTEXT,
        runtime_selection=_RUNTIME,
    )
    assert isinstance(prepared.io.input, FiniteInput)
    with subprocess.Popen(
        ["/usr/bin/python3", "-I", "-S", "-B", "-c", prepared.invocation.argv[-2], prepared.nonce],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        # Keeping stdin open proves admission refuses before the manifest EOF.
        assert process.wait(timeout=5) == 125
        assert process.stdout is not None and process.stdout.read() == b""
        assert process.stderr is not None and process.stderr.read() == b""
    assert not side_effect.exists()


@pytest.mark.parametrize("mode", [IdentityMode.DIRECT, IdentityMode.SUDO_ROOT])
def test_complete_provider_envelope_with_maximum_valid_manifest(mode: IdentityMode) -> None:
    context = _NumericGuestBootstrap(replace(_ROOT, mode=mode), _GUEST)
    initial = prepare_inline_candidate(Command(["x"]), plan=_plan(), bootstrap=context, runtime_selection=_RUNTIME)
    assert isinstance(initial.io.input, FiniteInput)
    # Base64 text grows in multiples of four; fill the remaining valid space.
    count = (MAX_MANIFEST_BYTES - len(initial.io.input.data)) // 4 * 3 + 1
    prepared = prepare_inline_candidate(
        Command(["x" * count]),
        plan=_plan(),
        bootstrap=context,
        runtime_selection=_RUNTIME,
    )
    assert isinstance(prepared.io.input, FiniteInput)
    assert MAX_MANIFEST_BYTES - 3 <= len(prepared.io.input.data) <= MAX_MANIFEST_BYTES
    body = ProxmoxCarrier._request_body(prepared.invocation, prepared.io)
    assert len(body) <= 65_536
    assert len(subprocess.list2cmdline(prepared.invocation.argv)) < 32_767
    groups = tuple(sorted({1001} | {int.from_bytes(hashlib.sha256(str(i).encode()).digest()[:4]) for i in range(2000)}))
    oversized = replace(_plan(), expected=IdentityExpectation(1001, 1001, groups))
    base = prepare_inline_candidate(Command(["x"]), plan=oversized, bootstrap=context, runtime_selection=_RUNTIME)
    assert isinstance(base.io.input, FiniteInput)
    count = (MAX_MANIFEST_BYTES - len(base.io.input.data)) // 4 * 3 + 1
    prepared_large = prepare_inline_candidate(
        Command(["x" * count]),
        plan=oversized,
        bootstrap=context,
        runtime_selection=_RUNTIME,
    )
    with pytest.raises(ValidationError):
        ProxmoxCarrier._request_body(prepared_large.invocation, prepared_large.io)
