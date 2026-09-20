"""Test-only helper composition for private stage exchanges."""

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
    import pytest

_PACKAGE = "_agw_file_stage"


def fixture_source(guest_patch: str = "") -> str:
    entry = f"""
import sys
guest=sys.modules[{(_PACKAGE + "._file_stage_guest")!r}]
{textwrap.dedent(guest_patch)}
raise SystemExit(guest.main(sys.argv[1]))
"""
    return FIXED_LOADER + textwrap.dedent(entry)


def install_fixture_bundle(monkeypatch: pytest.MonkeyPatch) -> str:
    source = fixture_source()
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
