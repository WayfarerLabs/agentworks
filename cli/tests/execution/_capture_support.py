"""Packed capture and snapshot helpers with explicit hermetic admission seams."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from agentworks.errors import ValidationError
from agentworks.execution._capture_obligation import CAPTURE_OBLIGATION_KIND
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._file_snapshot_protocol import (
    FileSnapshotChunkRequest,
    FileSnapshotCleanupRequest,
    FileSnapshotRequest,
    decode_file_snapshot_request,
)
from agentworks.execution._file_stage_protocol import FileStageChunkRequest
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._inline_request import decode_capture_manifest
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

from .test_prepared_helper_stage import GUEST, Carrier

PLAN = IdentityPlan(
    (
        IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()})))
        if sys.platform == "linux"
        else IdentityExpectation(1001, 1002, (1002, 1003))
    ),
    IdentityMode.DIRECT,
)

# The shipped bootstrap, pump, capture publisher and snapshot helpers all run.
# Privileged numeric admission and external guest observations are injected.
# Scratch operations themselves use real private files and the real workload UID.
PACKED = r"""
import builtins,contextlib,hashlib,json,os,sys
real_exec=builtins.exec
wrapper,nonce,stage_path,scratch,settings=sys.argv[1:]
settings=json.loads(settings)
guest=settings['guest']
@contextlib.contextmanager
def admit(uid,gid,groups):
 target=(os.geteuid(),os.getegid(),tuple(sorted(set(os.getgroups())|{os.getegid()})))
 assert (uid,gid,groups)==(0,0,(0,)) or (uid,gid,groups)==target
 yield lambda:b'1 (init) S '+b'0 '*18+str(guest[2]).encode()+b'\n'
def enter(frame,event,arg):
 if event=='call' and frame.f_code.co_name=='main' and '_admit' in frame.f_globals:frame.f_globals['_admit']=admit
def verify(parent,reference,digest,**kwargs):
 data=open(stage_path,'rb').read()
 assert len(data)==reference._ownership._length and hashlib.sha256(data).digest()==digest
 return stage_path
@contextlib.contextmanager
def borrow(parent,ready,**kwargs):
 fd=os.open(ready,os.O_RDONLY)
 try:yield fd
 finally:os.close(fd)
def execute(source,scope=None,local=None):
 if scope is None:scope=sys._getframe(1).f_globals
 real_exec(source,scope,local)
 name=scope.get('__name__','')
 if getattr(sys.modules.get(name),'__dict__',None) is not scope:return
 if getattr(source,'co_filename',None)!=scope.get('__file__'):return
 if name.endswith('._vm_guest_identity_guest'):
  scope['_read_marker']=lambda *args:guest[0]
  scope['_read_boot_id']=lambda *args:guest[1]
 elif name=='_agw_staged_helper._staged_helper_guest':
  scope['matches_current_identity']=lambda expected:expected.euid==0
  scope['open_scratch_root']=lambda **kw:os.open(scratch,os.O_RDONLY|os.O_DIRECTORY)
  scope['verify_scratch']=verify
  scope['borrow_ready_scratch']=borrow
 elif name in ('_agw_capture._capture_guest','_agw_file_snapshot._file_snapshot_guest'):
  scope['open_scratch_root']=lambda **kw:os.open(scratch,os.O_RDONLY|os.O_DIRECTORY)
  if name=='_agw_capture._capture_guest':
   real_begin=scope['begin_scratch']
   def begin(parent,length,token,context):
    if token.hex() in settings.get('fail_tokens',[]):raise OSError()
    return real_begin(parent,length,token,context)
   scope['begin_scratch']=begin
   if settings.get('fail_write'):
    def failed_write(*args,**kwargs):raise OSError()
    scope['write_scratch_chunk']=failed_write
   if settings.get('root_refusal'):
    def refused(**kw):raise OSError()
    scope['open_scratch_root']=refused
sys.argv=['fixed-helper',nonce]
builtins.exec=execute
sys.setprofile(enter)
real_exec(compile(wrapper,'<delivered-wrapper>','exec'),{'__name__':'__main__'})
"""


class CaptureCarrier(Carrier):
    def __init__(self, owner, tmp_path: Path) -> None:
        super().__init__(owner)
        self.tmp_path = tmp_path
        self.scratch = tmp_path / "scratch"
        self.scratch.mkdir(mode=0o700)
        self.stage_data = bytearray()
        self.activation_calls = 0
        self.snapshot_requests: list[FileSnapshotRequest] = []
        self.sizes: list[int] = []
        self.executed_sizes: list[int] = []
        self.interpreter = sys.executable
        self.fail_streams: tuple[int, ...] = ()
        self.root_refusal = False
        self.fail_cleanup = False
        self.fail_range: int | None = None
        self.activation_control: BaseException | None = None
        self.range_control: BaseException | None = None
        self.cleanup_control: BaseException | None = None
        self.helper_status: int | None = 0
        self.range_helper_status: int | None = 0
        self.unsettled_activation = False
        self.expire_after_activation = False
        self.fail_stage_cleanup = False
        self.fail_write = False

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        size = len(ProxmoxCarrier._request_body(invocation, io))
        self.sizes.append(size)
        if size > 65_536:
            raise ValidationError("Complete encoded carrier exceeds its fixed ceiling")

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody | None = None,
    ) -> CarrierReport:
        self.validate(invocation, io=io)
        self.executed_sizes.append(len(ProxmoxCarrier._request_body(invocation, io)))
        assert isinstance(io.input, FiniteInput) and isinstance(io.output, SinkOutput)
        control = io.input.data[io.input.data.index(b"{") :]
        if b'"operation":"stage_' in control:
            if self.fail_stage_cleanup and b'"operation":"stage_cleanup"' in control:
                return CarrierReport(Dispatch.NOT_SENT)
            report = super().execute(invocation, io=io, deadline=deadline, custody=custody)
            request = self.requests[-1]
            if isinstance(request, FileStageChunkRequest):
                assert request.offset == len(self.stage_data)
                self.stage_data.extend(request.data)
            return report
        self.deadlines.append(deadline)
        self.custody = custody
        if b'"operation":"snapshot_' in control:
            snapshot = decode_file_snapshot_request(control)
            self.snapshot_requests.append(snapshot)
            if isinstance(snapshot, FileSnapshotCleanupRequest) and self.fail_cleanup:
                return CarrierReport(Dispatch.NOT_SENT)
            if isinstance(snapshot, FileSnapshotCleanupRequest) and self.cleanup_control is not None:
                raise self.cleanup_control
            if isinstance(snapshot, FileSnapshotChunkRequest):
                if self.range_control is not None:
                    raise self.range_control
                if snapshot.offset == self.fail_range:
                    return CarrierReport(Dispatch.NOT_SENT)
            nonce = snapshot.nonce
        else:
            nonce = decode_capture_manifest(control).nonce if b'"kind"' in control else decode_request(control).nonce
            self.activation_calls += 1
            if self.activation_control is not None:
                raise self.activation_control
            rows = [
                row
                for row in self.owner.list_pending_lifecycle_obligations()
                if row.obligation_kind == CAPTURE_OBLIGATION_KIND
            ]
            assert len(rows) <= 1 and self.owner._active_borrow is not None
            if rows:
                assert json.loads(rows[0].payload)["workload"] == {
                    "mode": PLAN.mode.value,
                    "identity": {
                        "euid": PLAN.expected.euid,
                        "egid": PLAN.expected.egid,
                        "groups": list(PLAN.expected.groups),
                    },
                }
        path = self.tmp_path / "prepared-data"
        path.write_bytes(bytes(self.stage_data))
        rows = [
            row
            for row in self.owner.list_pending_lifecycle_obligations()
            if row.obligation_kind == CAPTURE_OBLIGATION_KIND
        ]
        tokens = json.loads(rows[0].payload)["tokens"] if rows else None
        completed = subprocess.run(
            [
                self.interpreter,
                "-I",
                "-S",
                "-B",
                "-c",
                PACKED,
                invocation.argv[-2],
                nonce,
                str(path),
                str(self.scratch),
                json.dumps(
                    {
                        "guest": (GUEST.instance_marker, GUEST.boot_id, GUEST.init_start_ticks),
                        "fail_tokens": []
                        if tokens is None
                        else [tokens[("stdout", "stderr")[index]] for index in self.fail_streams],
                        "root_refusal": self.root_refusal,
                        "fail_write": self.fail_write,
                    }
                ),
            ],
            input=io.input.data,
            capture_output=True,
            timeout=deadline.remaining(),
            check=False,
        )
        assert not completed.stderr, completed.stderr
        data = memoryview(f"AGW_RUNTIME_1:{nonce}:ready:0\n".encode() + completed.stdout)
        while data:
            amount = io.output.stdout.try_write(data)
            assert amount is not None and amount > 0
            data = data[amount:]
        channel = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        status = completed.returncode if self.helper_status == 0 else self.helper_status
        if self.snapshot_requests and isinstance(self.snapshot_requests[-1], FileSnapshotChunkRequest):
            status = completed.returncode if self.range_helper_status == 0 else self.range_helper_status
        if self.unsettled_activation and self.activation_calls == 1 and not self.snapshot_requests:
            assert custody is not None
            custody.begin_process()
        if self.expire_after_activation and self.activation_calls == 1 and not self.snapshot_requests:
            object.__setattr__(deadline, "expires_at", 0.0)
        return CarrierReport(
            Dispatch.SENT, None if status is None else ExitStatus(status), stdout=channel, stderr=channel
        )
