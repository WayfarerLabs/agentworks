"""Hermetic packed DIRECT activation with explicit privileged observation seams."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from agentworks.errors import ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._file_stage_protocol import FileStageChunkRequest
from agentworks.execution._inline_request import REQUEST_MARKER
from agentworks.execution._staged_helper_protocol import decode_request
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    Deadline,
    Dispatch,
    ExitStatus,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.execution.carriers.proxmox import ProxmoxCarrier

from .test_prepared_helper_stage import GUEST, ROOT, Carrier

# Execute both delivered bootstraps and the application helper unchanged.
# Privileged credential transitions, root-owned scratch admission, and external
# guest observations are injected; this proves composition, not native root.
PACKED_ADMISSION = r"""
import builtins,contextlib,hashlib,json,os,sys
real_exec=builtins.exec
events=[]
wrapper,nonce,path,settings=sys.argv[1:]
settings=json.loads(settings)
guest=settings['guest']
target=tuple(settings['target'][:2])+(tuple(settings['target'][2]),)
@contextlib.contextmanager
def admit(uid,gid,groups):
 assert (uid,gid,groups) in ((0,0,(0,)),target)
 events.append(['admit',uid,gid,list(groups)])
 yield lambda:b'1 (init) S '+b'0 '*18+str(guest[2]).encode()+b'\n'
def enter(frame,event,arg):
 if event=='call' and frame.f_code.co_name=='main' and '_admit' in frame.f_globals:
  frame.f_globals['_admit']=admit
def verify(parent,reference,digest,**kwargs):
 data=open(path,'rb').read()
 assert len(data)==reference._ownership._length
 assert hashlib.sha256(data).digest()==digest
 events.append(['verified'])
 return path
@contextlib.contextmanager
def borrow(parent,ready,**kwargs):
 descriptor=os.open(ready,os.O_RDONLY)
 try:yield descriptor
 finally:os.close(descriptor)
def execute(source,scope=None,local=None):
 if scope is None:scope=sys._getframe(1).f_globals
 real_exec(source,scope,local)
 name=scope.get('__name__','')
 if getattr(sys.modules.get(name),'__dict__',None) is not scope:return
 if getattr(source,'co_filename',None)!=scope.get('__file__'):return
 if name.endswith('._vm_guest_identity_guest'):
  inner=name.startswith('_agw_direct_input.')
  scope['_read_marker']=lambda *args:('b'*32 if inner and settings.get('inner_guest_mismatch') else guest[0])
  scope['_read_boot_id']=lambda *args:guest[1]
 elif name=='_agw_staged_helper._staged_helper_guest':
  scope['matches_current_identity']=lambda expected:expected.euid==0
  scope['open_scratch_root']=lambda **kwargs:os.open('/tmp',os.O_RDONLY|os.O_DIRECTORY)
  scope['verify_scratch']=verify
  scope['borrow_ready_scratch']=borrow
 elif name=='_agw_direct_input._inline_guest':
  events.append(['application_modules'])
  scope['matches_current_identity']=lambda expected:(expected.euid,expected.egid,expected.groups)==target
sys.argv=['fixed-helper',nonce]
builtins.exec=execute
sys.setprofile(enter)
try:real_exec(compile(wrapper,'<delivered-wrapper>','exec'),{'__name__':'__main__'})
finally:sys.stderr.write(json.dumps(events))
"""


class DirectCarrier(Carrier):
    """Script stage exchanges, then run the actual packed staged activation."""

    def __init__(self, owner, tmp_path: Path) -> None:
        super().__init__(owner)
        self.tmp_path = tmp_path
        self.data = bytearray()
        self.activation_calls = 0
        self.validated_sizes: list[int] = []
        self.events: list[list[object]] = []
        self.force_stage = False
        self.refuse_activation = False
        self.fail_cleanup = False
        self.activation_control: BaseException | None = None
        self.activation_status: int | None = 0
        self.activation_unsettled = False
        self.inner_guest_mismatch = False
        self.damage_prefix = False
        self.interpreter = sys.executable
        self.target = (1001, 1002, (1002, 1003))

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        size = len(ProxmoxCarrier._request_body(invocation, io))
        self.validated_sizes.append(size)
        if isinstance(io.input, FiniteInput):
            if self.force_stage and io.input.data.startswith(REQUEST_MARKER):
                raise ValidationError("synthetic complete inline carrier refusal")
            if self.refuse_activation and b'"sha256"' in io.input.data:
                raise ValidationError("synthetic staged carrier refusal")

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody | None = None,
    ) -> CarrierReport:
        self.validate(invocation, io=io)
        assert isinstance(io.input, FiniteInput) and isinstance(io.output, SinkOutput)
        control = io.input.data[io.input.data.index(b"{") :]
        if b'"operation"' in control:
            if self.fail_cleanup and b'"stage_cleanup"' in control:
                return CarrierReport(Dispatch.NOT_SENT)
            stage_result = super().execute(invocation, io=io, deadline=deadline, custody=custody)
            stage_request = self.requests[-1]
            if isinstance(stage_request, FileStageChunkRequest):
                assert stage_request.offset == len(self.data)
                self.data.extend(stage_request.data)
            return stage_result
        request = decode_request(control)
        self.activation_calls += 1
        self.custody = custody
        self.deadlines.append(deadline)
        if self.activation_control is not None:
            raise self.activation_control
        assert request.identity == ROOT.expected
        assert request.sha256 == hashlib.sha256(self.data).digest()
        content = bytes(self.data)
        input_data = io.input.data
        if self.damage_prefix:
            content = b"!" + content[1:]
            request = replace(request, sha256=hashlib.sha256(content).digest())
            from agentworks.execution._staged_helper_protocol import encode_request

            input_data = input_data[: input_data.index(b"{")] + encode_request(request)
        path = self.tmp_path / "prepared-data"
        path.write_bytes(content)
        completed = subprocess.run(
            [
                self.interpreter,
                "-I",
                "-S",
                "-B",
                "-c",
                PACKED_ADMISSION,
                invocation.argv[-2],
                request.nonce,
                str(path),
                json.dumps(
                    {
                        "guest": (GUEST.instance_marker, GUEST.boot_id, GUEST.init_start_ticks),
                        "target": self.target,
                        "inner_guest_mismatch": self.inner_guest_mismatch,
                    }
                ),
            ],
            input=input_data,
            capture_output=True,
            timeout=deadline.remaining(),
            check=False,
        )
        self.events = json.loads(completed.stderr)
        remaining = memoryview(f"AGW_RUNTIME_1:{request.nonce}:ready:0\n".encode() + completed.stdout)
        while remaining:
            written = io.output.stdout.try_write(remaining)
            assert written is not None and written > 0
            remaining = remaining[written:]
        output = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        code = completed.returncode if self.activation_status == 0 else self.activation_status
        if self.activation_unsettled:
            assert custody is not None
            custody.begin_process()
        return CarrierReport(
            Dispatch.SENT, ExitStatus(code) if code is not None else None, stdout=output, stderr=output
        )
