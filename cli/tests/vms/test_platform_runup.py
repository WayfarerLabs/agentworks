"""Provisioning-phase runup for vm-platform.

The proxmox platform authenticates its API token with a cheap read
(next available VMID) before ``create`` mutates anything: a 401/403 is
a definitive rejection (fatal, before any VM exists); a transient error
or unreachable host warns and continues unverified. lima/wsl2 have no
token to check, so their runup is the base no-op.
"""

from __future__ import annotations

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
from agentworks.capabilities.vm_platform.proxmox_api import (
    ProxmoxAPI,
    ProxmoxAPIError,
)
from agentworks.errors import TokenRejectedError

_CONFIG = {
    "api_url": "https://pve:8006",
    "node": "n",
    "token_id": "t",
    "template_vmid": 1,
}


class _StubResolver:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def register_name(self, name: str) -> str:
        return name

    def get(self, name: str) -> str:
        return self._values[name]


def _platform() -> ProxmoxPlatform:
    return ProxmoxPlatform("px", _CONFIG, _StubResolver({"proxmox-token": "tok"}))  # type: ignore[arg-type]


def test_proxmox_runup_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ProxmoxAPI, "next_id", lambda self: 100)
    _platform().runup(RunContext())  # no error


@pytest.mark.parametrize("code", [401, 403])
def test_proxmox_runup_rejection_is_fatal(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    def _boom(self: object) -> int:
        err = ProxmoxAPIError(f"failed ({code})")
        err.code = code
        raise err

    monkeypatch.setattr(ProxmoxAPI, "next_id", _boom)
    with pytest.raises(TokenRejectedError, match="Proxmox rejected"):
        _platform().runup(RunContext())


def test_proxmox_runup_other_status_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(self: object) -> int:
        err = ProxmoxAPIError("failed (500)")
        err.code = 500
        raise err

    monkeypatch.setattr(ProxmoxAPI, "next_id", _boom)
    _platform().runup(RunContext())  # no raise
    assert "could not verify" in capsys.readouterr().err


def test_proxmox_runup_network_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(self: object) -> int:
        raise OSError("unreachable")

    monkeypatch.setattr(ProxmoxAPI, "next_id", _boom)
    _platform().runup(RunContext())  # no raise
    assert "could not reach Proxmox" in capsys.readouterr().err


def test_proxmox_runup_without_resolver_is_noop() -> None:
    """Inspection construction (no resolver) has no resolved token to
    check; runup is a silent no-op, not a crash."""
    ProxmoxPlatform("px", _CONFIG).runup(RunContext())
