"""A harness for Phase A over a real database row and the recording ``VMAccess``.

``PhaseAHarness`` holds what ``phase_a.bootstrap`` is given and what it did:
the platform's hook calls, the log's steps, and the recorder's requests. The
admin's access answers the VM contract cleanly unless a test scripts otherwise.
"""

from __future__ import annotations

import signal
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from agentworks.capabilities.base import RunContext
from agentworks.debian import DebianRelease
from agentworks.execution.results import Completed, Result
from agentworks.vms.initializer import phase_a

if TYPE_CHECKING:
    from agentworks.db import Database
    from tests.vm_access_support import AccessRecorder

VM = "hookvm"
ADDRESS = "100.64.0.8"
TRIXIE_OS_RELEASE = b'ID=debian\nVERSION_ID="13"\nVERSION_CODENAME=trixie\n'
BOOKWORM_OS_RELEASE = b'ID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n'
CLEAN_SCRIPT = b"AGW-CHECK-END\n"


def contract_outcomes(
    *, os_release: bytes = TRIXIE_OS_RELEASE, script: bytes = CLEAN_SCRIPT, sudo: bytes = b"0\n"
) -> list[Result | BaseException]:
    """The VM's answers to the base checks: the release, the script, then the elevated sudo check."""
    return [Result(Completed(0), os_release), Result(Completed(0), script), Result(Completed(0), sudo)]


class SpyPlatform:
    """Records the provisioning status of the row each close was given; ``close_error`` makes the close fail.

    ``signal_during_close`` delivers that signal as the close begins, as an
    operator's would arrive while the provider call is in flight;
    ``close_finished`` then says whether the close still ran to its end.
    """

    name = "stub"

    def __init__(self) -> None:
        self.closed: list[str] = []
        self.close_error: BaseException | None = None
        self.signal_during_close: int | None = None
        self.close_finished = False

    def close_provisioning_access(self, vm: Any, ctx: object) -> None:
        self.closed.append(vm.provisioning_status)
        if self.signal_during_close is not None:
            signal.raise_signal(self.signal_during_close)
        if self.close_error is not None:
            raise self.close_error
        self.close_finished = True


class SpyLog:
    """The operation log Phase A writes: steps, output lines and request records."""

    display_path = "~/hookvm-vm-create.log"

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.lines: list[str] = []
        self.records: list[tuple[object, object]] = []

    def step(self, name: str) -> None:
        self.steps.append(name)

    def output(self, text: str) -> None:
        self.lines.append(text)

    def record(self, request: object, result: object) -> None:
        self.records.append((request, result))


@dataclass
class PhaseAHarness:
    db: Database
    recorder: AccessRecorder
    platform: SpyPlatform = field(default_factory=SpyPlatform)
    log: SpyLog = field(default_factory=SpyLog)
    config: Any = field(
        default_factory=lambda: SimpleNamespace(operator=SimpleNamespace(ssh_private_key=Path("/nonexistent/key")))
    )

    def __post_init__(self) -> None:
        self.db.insert_vm(VM, site="stub", hostname=VM)
        self.admin.run_outcomes = contract_outcomes()

    @property
    def admin(self) -> Any:
        return self.recorder.access(vm_name=VM)

    @property
    def enrollment(self) -> Any:
        return self.recorder.enrollment(vm_name=VM)

    def bootstrap(
        self,
        *,
        tailnet_address: str = ADDRESS,
        expected_release: DebianRelease = DebianRelease.TRIXIE,
    ) -> tuple[str, DebianRelease]:
        return phase_a.bootstrap(
            self.db,
            self.config,
            VM,
            self.platform,  # type: ignore[arg-type]
            RunContext(),
            self.log,
            admin_username="agentworks",
            tailnet_address=tailnet_address,
            expected_release=expected_release,
        )

    def row(self) -> Any:
        row = self.db.get_vm(VM)
        assert row is not None
        return row

    def events(self) -> list[str]:
        return [event.event for event in self.db.list_vm_events(VM)]
