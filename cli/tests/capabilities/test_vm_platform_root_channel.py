"""The platform surface's root channel: the default host-key read goes through ``run_as_root``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.errors import StateError
from agentworks.execution import trust
from agentworks.execution.root_script import RawRun
from tests.plugins._fixtures import ConformingVMPlatform

ED25519 = "AAAAC3NzaC1lZDI1NTE5AAAAIOU3HUqrGubKZio8MVx9WOFPYHGIHl5tIyF3skwoAxv0"


class _Platform(ConformingVMPlatform):
    """Answers every root script with ``listing`` on stdout and status 0, as the real wrapper would."""

    name = "root-channel-fixture"
    description = "answers root scripts from a fixed listing"
    listing = b""

    def run_as_root(self, vm: Any, ctx: Any, script: bytes, deadline: float) -> RawRun:
        type(self).calls.append((vm, ctx, deadline))
        nonce = script.split(b"\\036", 1)[1][:32]
        prefix = b"\x1e" + nonce + b" "
        return RawRun(
            prefix + b"start\n" + self.listing + prefix + b"end\n",
            prefix + b"start\n" + prefix + b"exit 0\n",
        )

    calls: list[tuple[Any, Any, float]] = []


def test_the_default_read_goes_through_run_as_root_and_keeps_only_valid_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_Platform, "calls", [])
    monkeypatch.setattr(_Platform, "listing", f"garbage line\nssh-ed25519 {ED25519} root@vm\n".encode())
    vm, ctx = SimpleNamespace(name="box"), RunContext()

    keys = _Platform("site", {"name": "root-channel-fixture"}).read_host_keys(vm, ctx)  # type: ignore[arg-type]

    assert keys == (trust.HostKey("ssh-ed25519", ED25519),)
    ((called_vm, called_ctx, _deadline),) = _Platform.calls
    assert (called_vm, called_ctx) == (vm, ctx)


def test_the_default_read_refuses_a_vm_that_reports_no_valid_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_Platform, "calls", [])
    monkeypatch.setattr(_Platform, "listing", b"not a key\n")

    with pytest.raises(StateError):
        _Platform("site", {"name": "root-channel-fixture"}).read_host_keys(SimpleNamespace(name="box"), RunContext())  # type: ignore[arg-type]
