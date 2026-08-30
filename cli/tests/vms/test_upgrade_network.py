from __future__ import annotations

from dataclasses import dataclass

import pytest

from agentworks.errors import StateError
from agentworks.vms.upgrade.network import (
    predict_interface_names,
    require_stable_interface_names,
    snapshot_provider_interface_names,
    verify_interface_names,
)


@dataclass(frozen=True)
class _Result:
    stdout: str
    returncode: int = 0

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class _Target:
    def __init__(self, result: _Result) -> None:
        self.result = result

    def run(self, command: str, **kwargs: object) -> _Result:
        del command, kwargs
        return self.result


def test_prediction_parses_stable_installed_udev_result() -> None:
    assert predict_interface_names(_Target(_Result("eth0\teth0\ntailscale0\ttailscale0\n"))) == {
        "eth0": "eth0",
        "tailscale0": "tailscale0",
    }


def test_predicted_rename_blocks_reboot() -> None:
    with pytest.raises(StateError):
        require_stable_interface_names({"eth0": "enp1s0"})


def test_provider_managed_interface_snapshot_keeps_observed_names() -> None:
    assert snapshot_provider_interface_names(_Target(_Result("eth0\ntailscale0\n"))) == {
        "eth0": "eth0",
        "tailscale0": "tailscale0",
    }


def test_post_reboot_interface_verification_requires_predicted_names() -> None:
    with pytest.raises(StateError):
        verify_interface_names(_Target(_Result("enp2s0\n")), {"eth0": "eth0"})
