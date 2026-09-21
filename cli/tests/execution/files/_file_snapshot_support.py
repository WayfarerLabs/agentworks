"""Test-only helper composition for private snapshot exchanges."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.execution import _file_snapshot_exchange
from agentworks.execution._file_snapshot_bundle import _MODULE_NAMES, _PACKAGE
from agentworks.execution._helper_bundle import FixedFileHelperBundle
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
from tests.execution.files._fixed_bundle_support import fixture_file_bundle

if TYPE_CHECKING:
    import pytest


def fixture_source(scratch_root: Path, guest_patch: str = "") -> FixedFileHelperBundle:
    setup = f"""
import os
root=sys.modules[{(_PACKAGE + "._scratch_root")!r}]
root._LINUX_SCRATCH_ROOT={str(scratch_root)!r}
root._EXPECTED_OWNER_UID=os.geteuid()
"""
    return fixture_file_bundle(_PACKAGE, _MODULE_NAMES, "_file_snapshot_guest", setup + textwrap.dedent(guest_patch))


def install_fixture_bundle(
    monkeypatch: pytest.MonkeyPatch,
    scratch_root: Path,
    guest_patch: str = "",
) -> FixedFileHelperBundle:
    bundle = fixture_source(scratch_root, guest_patch)
    monkeypatch.setattr(_file_snapshot_exchange, "FIXED_BUNDLE", bundle)
    return bundle


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
