"""Conservative local observation of one persisted Windows controller identity."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution.carrier import Deadline

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.execution._wsl2_win32 import WindowsApi


@dataclass(frozen=True, slots=True)
class ControllerIdentity:
    """The already-running Windows Agentworks process, not the WSL client."""

    pid: int
    creation_ticks: int

    def __post_init__(self) -> None:
        if type(self.pid) is not int or not 0 < self.pid <= 0xFFFFFFFF:
            raise ValidationError("WSL2 controller PID is invalid")
        if type(self.creation_ticks) is not int or not 0 < self.creation_ticks <= 0xFFFFFFFFFFFFFFFF:
            raise ValidationError("WSL2 controller creation time is invalid")


class ControllerPresence(StrEnum):
    PRESENT = "present"
    ABSENT_CONFIRMED = "absent-confirmed"
    UNKNOWN = "unknown"


class WindowsControllerObserver:
    """Observe only the exact host process; this says nothing about guest drain."""

    def __init__(self, *, api_factory: Callable[[], WindowsApi] | None = None) -> None:
        from agentworks.execution._wsl2_win32 import WindowsApi

        self._api_factory = WindowsApi if api_factory is None else api_factory

    def observe(self, identity: ControllerIdentity, deadline: Deadline) -> ControllerPresence:
        """Observe validated persisted identity within a finite local deadline."""
        if type(identity) is not ControllerIdentity or type(deadline) is not Deadline or deadline.expires_at is None:
            raise ValidationError("WSL2 controller observation requires exact identity and finite deadline")
        if deadline.expired:
            return ControllerPresence.UNKNOWN
        try:
            api = self._api_factory()
            if deadline.expired:
                return ControllerPresence.UNKNOWN
            try:
                handle = api.open_process_for_observation(identity.pid)
            except OSError:
                return self._observe_snapshot(api, identity.pid, deadline)
            return self._observe_handle(api, handle, identity, deadline)
        except OSError:
            return ControllerPresence.UNKNOWN

    @staticmethod
    def _observe_handle(
        api: WindowsApi, handle: int, identity: ControllerIdentity, deadline: Deadline
    ) -> ControllerPresence:
        result = ControllerPresence.UNKNOWN
        try:
            if not deadline.expired:
                ticks = api.process_creation_ticks(handle)
                if not deadline.expired:
                    if ticks != identity.creation_ticks:
                        result = ControllerPresence.ABSENT_CONFIRMED
                    else:
                        wait = api.wait_process(handle, 0)
                        if not deadline.expired:
                            if wait == api.WAIT_TIMEOUT:
                                result = ControllerPresence.PRESENT
                            elif wait == api.WAIT_OBJECT_0:
                                result = ControllerPresence.ABSENT_CONFIRMED
        except OSError:
            pass
        try:
            api.close_handle(handle)
        except OSError:
            return ControllerPresence.UNKNOWN
        return ControllerPresence.UNKNOWN if deadline.expired else result

    @staticmethod
    def _observe_snapshot(api: WindowsApi, pid: int, deadline: Deadline) -> ControllerPresence:
        if deadline.expired:
            return ControllerPresence.UNKNOWN
        try:
            snapshot = api.snapshot_processes()
        except OSError:
            return ControllerPresence.UNKNOWN
        result = ControllerPresence.UNKNOWN
        try:
            if not deadline.expired:
                current = api.first_snapshot_pid(snapshot)
                while not deadline.expired:
                    if current is None:
                        result = ControllerPresence.ABSENT_CONFIRMED
                        break
                    if current == pid:
                        break
                    current = api.next_snapshot_pid(snapshot)
        except OSError:
            pass
        try:
            api.close_handle(snapshot)
        except OSError:
            return ControllerPresence.UNKNOWN
        return ControllerPresence.UNKNOWN if deadline.expired else result
