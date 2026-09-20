"""Test-only fixed-lock helper composition for private stage exchanges."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

from agentworks.execution import _file_stage_exchange
from agentworks.execution._file_stage_bundle import FIXED_LOADER
from agentworks.execution.carrier import (
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    PreparedInvocation,
)
from agentworks.execution.carriers._subprocess import run_process

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_PACKAGE = "_agw_file_stage"


def fixed_lock_source(root: Path, guest_patch: str = "") -> str:
    entry = f"""
import contextlib,os,sys
@contextlib.contextmanager
def fixed_test_lock(*,expires_at):
 root_fd=os.open({str(root)!r},os.O_PATH|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
 try:
  with sys.modules[{(_PACKAGE + "._file_lock")!r}]._file_lock_at_root(root_fd,os.getuid(),expires_at=expires_at):
   yield
 finally:
  os.close(root_fd)
guest=sys.modules[{(_PACKAGE + "._file_stage_guest")!r}]
guest.system_file_lock=fixed_test_lock
{textwrap.dedent(guest_patch)}
raise SystemExit(guest.main(sys.argv[1]))
"""
    return FIXED_LOADER + textwrap.dedent(entry)


def install_fixed_lock_bundle(root: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    root.mkdir(mode=0o700)
    parent = root
    for component in ("var", "lib", "agentworks", "execution"):
        parent = parent / component
        parent.mkdir(mode=0o700)
    lock = parent / "files.lock"
    lock.write_bytes(b"")
    lock.chmod(0o444)
    source = fixed_lock_source(root)
    monkeypatch.setattr(_file_stage_exchange, "FIXED_SOURCE", source)
    return source


class LocalCarrier:
    def __init__(self, *, dispatch_deadline: Deadline | None = None) -> None:
        self.calls = 0
        self.invocation: PreparedInvocation | None = None
        self.io: CarrierIO | None = None
        self.dispatch_deadline = dispatch_deadline

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.calls += 1
        self.invocation = invocation
        self.io = io
        result = run_process(list(invocation.argv), io=io, deadline=self.dispatch_deadline or deadline)
        completion = None
        if result.exit_status is not None:
            completion = (
                ExitStatus(signal=-result.exit_status)
                if result.exit_status < 0
                else ExitStatus(code=result.exit_status)
            )
        return CarrierReport(
            Dispatch.SENT if result.started else Dispatch.NOT_SENT,
            completion,
            result.local_status,
            result.stdout,
            result.stderr,
            result.failure,
        )
