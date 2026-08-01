"""``_tailscale_logout`` operator-facing message ordering on ``vm delete``.

The deregistration dispatch happens INSIDE the native-transport route
window: on Azure, between the transient route's "Opening SSH route..."
and "Closing SSH route..." lines. The success message must print at the
point of the action, inside that window, so the transcript keeps real
order (#350: it used to print after the ExitStack unwound, reading as if
the node deregistered after the route closed, when it actually happened
before). Platforms whose ``transient_route`` is a nullcontext emit no
route lines, so for them the placement is observationally unchanged; a
test pins that too. The platform stand-ins below are hand-rolled (the
``test_ensure_tailscale_wording.py`` pattern): the Azure-shaped one
mirrors the real ``transient_route``'s output lines without touching the
SDK.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from agentworks import output
from agentworks.capabilities.base import RunContext
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.conftest import CapturedOutput

_OPEN_LINE = "Opening SSH route (allow scoped to 198.51.100.7/32)..."
_CLOSE_LINE = "Closing SSH route (removing allow rule 'allow-ssh-transient-cafe0001')..."


class _RecordingTransport:
    """Stand-in transport: the factory's reachability probe and the
    logout dispatch both succeed, and every command is recorded."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str, **kwargs: object) -> object:
        self.commands.append(command)
        return None


class _AzureShapedPlatform:
    """A platform whose ``transient_route`` emits the real Azure route
    lines around the window, so the ordering pin observes the same
    transcript shape ``vm delete`` produces on Azure."""

    name = "azure-vm"
    probe_failure_hint = None

    def __init__(self, target: _RecordingTransport) -> None:
        self._target = target

    @contextlib.contextmanager
    def transient_route(self, vm: object, ctx: object, *, config: object | None = None) -> Iterator[None]:
        output.info(_OPEN_LINE)
        try:
            yield
        finally:
            output.info(_CLOSE_LINE)

    def native_transport(self, vm: object, ctx: object, *, config: object | None = None) -> _RecordingTransport:
        return self._target


class _NullRoutePlatform:
    """The lima/wsl2/proxmox shape: no transient route state, no lines."""

    name = "lima"
    probe_failure_hint = None

    def __init__(self, target: _RecordingTransport) -> None:
        self._target = target

    def transient_route(self, vm: object, ctx: object, *, config: object | None = None) -> Any:
        return contextlib.nullcontext()

    def native_transport(self, vm: object, ctx: object, *, config: object | None = None) -> _RecordingTransport:
        return self._target


def _fake_vm() -> Any:
    return SimpleNamespace(name="box", admin_username="agentworks", tailscale_host="100.64.0.9")


def _fake_config() -> Any:
    """A config stand-in: the logout path only threads it through to the
    platform stand-ins, which ignore it."""
    return SimpleNamespace()


def test_deregistered_prints_inside_the_route_window(captured_output: CapturedOutput) -> None:
    """The full info-level transcript, in order: the deregistration
    announcement, the route open, the success message, THEN the route
    close. The success message printing after the close (the pre-#350
    ordering) would misstate when the deregistration happened."""
    target = _RecordingTransport()
    platform: Any = _AzureShapedPlatform(target)

    vm_manager._tailscale_logout(_fake_vm(), _fake_config(), platform, RunContext())

    assert captured_output.info == [
        "Deregistering from Tailscale...",
        _OPEN_LINE,
        "Tailscale node deregistered",
        _CLOSE_LINE,
    ]
    assert any("tailscale down && tailscale logout" in cmd for cmd in target.commands)


def test_nullcontext_route_platform_output_is_unchanged(captured_output: CapturedOutput) -> None:
    """Platforms without transient route state see the same two lines as
    before the reorder: the window is invisible, so moving the success
    message inside it changes nothing for them."""
    target = _RecordingTransport()
    platform: Any = _NullRoutePlatform(target)

    vm_manager._tailscale_logout(_fake_vm(), _fake_config(), platform, RunContext())

    assert captured_output.info == [
        "Deregistering from Tailscale...",
        "Tailscale node deregistered",
    ]
    assert any("tailscale down && tailscale logout" in cmd for cmd in target.commands)


def test_failed_dispatch_still_warns_without_success_line(captured_output: CapturedOutput) -> None:
    """The failure path is unchanged by the reorder: a dispatch that
    raises warns and never prints the success message."""

    class _FailingTransport(_RecordingTransport):
        def run(self, command: str, **kwargs: object) -> object:
            if "tailscale down" in command:
                raise RuntimeError("ssh died mid-logout")
            return super().run(command, **kwargs)

    platform: Any = _AzureShapedPlatform(_FailingTransport())
    vm_manager._tailscale_logout(_fake_vm(), _fake_config(), platform, RunContext())

    assert "Tailscale node deregistered" not in captured_output.info
    assert any("Tailscale logout failed" in w for w in captured_output.warnings)
    # The route still closed on the unwind: the finally is not the
    # reorder's to break.
    assert _CLOSE_LINE in captured_output.info
