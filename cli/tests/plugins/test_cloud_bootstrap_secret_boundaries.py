"""Azure and EC2 provider-retained bootstrap credential boundaries."""

from __future__ import annotations

import base64
import gzip
from types import SimpleNamespace

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest, ssh_exposure
from agentworks.capabilities.vm_platform.tailscale_join import TAILSCALE_JOIN_STDIN_COMMAND
from agentworks.plugins.aws.network import EC2Error
from agentworks.plugins.aws.platform import EC2Platform
from agentworks.plugins.azure.network import AzureError
from agentworks.plugins.azure.platform import AzureVMPlatform
from agentworks.ssh import SSHError, SSHResult
from agentworks.transports import SSHTransport
from tests._aws_fakes import install_fakes as install_aws_fakes
from tests._azure_platform_support import _install_fakes as install_azure_fakes

_SENTINEL = "tskey-boundary-'swordfish"
_AZURE_CONFIG = {
    "subscription_id": "sub-A",
    "resource_group": "rg1",
    "region": "eastus",
    "auth": {"mode": "ambient"},
}


@pytest.fixture(autouse=True)
def _stub_egress_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_exposure, "_egress_ip_cache", None)
    monkeypatch.setattr(ssh_exposure, "detect_egress_ip", lambda: "198.18.0.7")


def _request() -> ProvisionRequest:
    return ProvisionRequest(
        vm_name="vm1",
        hostname="vm1",
        system_slug=None,
        admin_username="agentworks",
        ssh_public_key="ssh-ed25519 AAAA test",
        ssh_private_key=None,
        tailscale_auth_key=_SENTINEL,
        cpus=4,
        memory_gib=8,
        disk_gib=50,
        swap_gib=4,
    )


def _successful_transport(calls: list[tuple[str, dict[str, object]]]):
    def _run(self: SSHTransport, command: str, **kwargs: object) -> SSHResult:
        del self
        calls.append((command, kwargs))
        stdout = "100.64.0.5\n" if command == "tailscale ip -4" else ""
        return SSHResult(returncode=0, stdout=stdout, stderr="")

    return _run


def _assert_one_ephemeral_join(calls: list[tuple[str, dict[str, object]]]) -> None:
    sensitive = [(command, kwargs) for command, kwargs in calls if kwargs.get("input_text") is not None]
    assert sensitive == [
        (
            TAILSCALE_JOIN_STDIN_COMMAND,
            {"sudo": True, "timeout": 30, "input_text": f"{_SENTINEL}\n"},
        )
    ]
    assert all(_SENTINEL not in command for command, _kwargs in calls)


def _assert_exception_objects_are_secret_free(exc: BaseException) -> None:
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        assert _SENTINEL not in str(current)
        assert _SENTINEL not in repr(current)
        pending.extend([current.__cause__, current.__context__])


def test_azure_final_custom_data_is_key_free_then_joins_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = install_azure_fakes(monkeypatch, vm_exists_lookup=False)
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(SSHTransport, "run", _successful_transport(calls))

    result = AzureVMPlatform("az-site", dict(_AZURE_CONFIG)).create(_request(), RunContext())

    [(_rg, _name, final_request)] = fakes.compute.virtual_machines.created
    custom_data = base64.b64decode(final_request.os_profile.custom_data).decode()
    assert _SENTINEL not in custom_data
    assert "TAILSCALE_AUTH_KEY=''" in custom_data
    _assert_one_ephemeral_join(calls)
    assert result.bootstrap_complete is True
    assert result.tailscale_ip == "100.64.0.5"


def test_ec2_final_run_instances_user_data_is_key_free_then_joins_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = install_aws_fakes(monkeypatch)
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(SSHTransport, "run", _successful_transport(calls))

    result = EC2Platform("aws-site", {"region": "us-east-1", "auth": {"mode": "ambient"}}).create(
        _request(),
        RunContext(config=SimpleNamespace(operator=SimpleNamespace(ssh_allow_cidrs=[]))),
    )

    final_request = recorder.kwargs_for("run_instances")
    user_data = gzip.decompress(final_request["UserData"]).decode()
    assert _SENTINEL not in user_data
    assert "TAILSCALE_AUTH_KEY=''" in user_data
    _assert_one_ephemeral_join(calls)
    assert result.bootstrap_complete is True
    assert result.tailscale_ip == "100.64.0.5"


def test_azure_join_failure_rolls_back_without_secret_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = install_azure_fakes(monkeypatch, vm_exists_lookup=False)
    calls: list[tuple[str, dict[str, object]]] = []

    def _run(self: SSHTransport, command: str, **kwargs: object) -> SSHResult:
        del self
        calls.append((command, kwargs))
        if kwargs.get("input_text") is not None:
            raise SSHError("fixed stdin join failed")
        return SSHResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(SSHTransport, "run", _run)

    with pytest.raises(AzureError) as caught:
        AzureVMPlatform("az-site", dict(_AZURE_CONFIG)).create(_request(), RunContext())

    _assert_one_ephemeral_join(calls)
    _assert_exception_objects_are_secret_free(caught.value)
    assert fakes.compute.virtual_machines.deleted == [("rg1", "vm1")]
    assert fakes.network.network_interfaces.deleted == [("rg1", "vm1-nic")]


def test_ec2_join_failure_rolls_back_without_secret_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = install_aws_fakes(monkeypatch)
    calls: list[tuple[str, dict[str, object]]] = []

    def _run(self: SSHTransport, command: str, **kwargs: object) -> SSHResult:
        del self
        calls.append((command, kwargs))
        if kwargs.get("input_text") is not None:
            raise SSHError("fixed stdin join failed")
        return SSHResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(SSHTransport, "run", _run)

    with pytest.raises(EC2Error) as caught:
        EC2Platform("aws-site", {"region": "us-east-1", "auth": {"mode": "ambient"}}).create(
            _request(),
            RunContext(config=SimpleNamespace(operator=SimpleNamespace(ssh_allow_cidrs=[]))),
        )

    _assert_one_ephemeral_join(calls)
    _assert_exception_objects_are_secret_free(caught.value)
    methods = recorder.methods("ec2")
    assert "terminate_instances" in methods
    assert "delete_security_group" in methods
