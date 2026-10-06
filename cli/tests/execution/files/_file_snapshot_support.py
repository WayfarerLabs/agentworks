"""Test-only helper composition for private snapshot exchanges."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.execution import _file_effect_gate, _file_snapshot_exchange
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._file_snapshot_bundle import _MODULE_NAMES, _PACKAGE
from agentworks.execution._helper_bundle import FixedFileHelperBundle
from agentworks.execution.carrier import (
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    ExitStatus,
    PreparedInvocation,
)
from tests.execution._bound_carrier_support import fixture_dispatch, run_fixture_process
from tests.execution.files._fixed_bundle_support import fixture_file_bundle

if TYPE_CHECKING:
    import pytest


def fixture_source(scratch_root: Path, guest_patch: str = "") -> FixedFileHelperBundle:
    gate_namespace = _file_effect_gate._GATE_NAMESPACE
    setup = f"""
import os
root=sys.modules[{(_PACKAGE + "._scratch_root")!r}]
root._LINUX_SCRATCH_ROOT={str(scratch_root)!r}
root._EXPECTED_OWNER_UID=os.geteuid()
gate=sys.modules[{(_PACKAGE + "._file_effect_gate")!r}]
gate._GATE_NAMESPACE={gate_namespace!r}
gate._ROOT_UID=os.geteuid() if {gate_namespace != "/run/agentworks/file-gates-v1"!r} else 0
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
    def __init__(self, *, dispatch_deadline: Deadline | None = None, live_stdio: bool = False) -> None:
        self.local_delivery = LocalDeliveryCustody()
        self.calls = 0
        self.invocation: PreparedInvocation | None = None
        self.io: CarrierIO | None = None
        self.dispatch_deadline = dispatch_deadline
        self.live_stdio = live_stdio

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures(live_stdio=self.live_stdio)

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        del invocation, io

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody | None = None,
    ) -> CarrierReport:
        self.validate(invocation, io=io)
        self.calls += 1
        self.invocation = invocation
        self.io = io
        result = run_fixture_process(
            list(invocation.argv),
            io=io,
            deadline=self.dispatch_deadline or deadline,
            live_stdio=self.live_stdio,
            custody=custody,
            standalone_custody=self.local_delivery,
        )
        completion = None
        if result.exit_status is not None:
            completion = (
                ExitStatus(signal=-result.exit_status)
                if result.exit_status < 0
                else ExitStatus(code=result.exit_status)
            )
        return CarrierReport(
            fixture_dispatch(result),
            completion,
            result.local_status,
            result.stdout,
            result.stderr,
            result.failure,
        )
