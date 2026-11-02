"""Incident row I10 on every platform's create: the Tailscale auth key reaches one root script and nothing else.

Each create runs with its channel faked at the lowest seam it uses
(``root_script.run_process`` for Lima, WSL2 and the clouds, the QGA client
for Proxmox), so every other step is the platform's own code. The key
must appear in exactly one ``run_as_root`` script, the join's, and in no
argv, progress-sink call, console line, exception (message, ``repr`` or
chain) or retained file. A failed address read or an unknown join never
sends it a second time.
"""

from __future__ import annotations

import base64
import gzip
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest, ssh_exposure, wsl2
from agentworks.capabilities.vm_platform.bootstrap_script import REBOOT_SENTINEL_PATH
from agentworks.capabilities.vm_platform.lima import _REBOOT_CLEAR_MARKER, LimaPlatform
from agentworks.capabilities.vm_platform.lima_host import LimaHost
from agentworks.capabilities.vm_platform.tailscale_join import CREATE_RERUN_HINT, JOIN_FRAGMENT
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.debian import DebianRelease
from agentworks.errors import AgentworksError
from agentworks.plugins.aws.platform import EC2Platform
from agentworks.plugins.azure.platform import AzureVMPlatform
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from tests._aws_fakes import install_fakes as install_aws_fakes
from tests._azure_platform_support import _install_fakes as install_azure_fakes
from tests._root_channel_fakes import Answer, FakeGuest, answer_by_payload, install_at_run_process
from tests.conftest import provisioning_trust_for_tests
from tests.plugins.gcp.test_platform import _ctx as gcp_ctx
from tests.plugins.gcp.test_platform import _platform as gcp_platform
from tests.plugins.test_proxmox_create_interrupt import _CONFIG as PROXMOX_CONFIG
from tests.plugins.test_proxmox_create_interrupt import FakeProxmoxAPI

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.capabilities.vm_platform import ProvisionResult
    from tests.conftest import CapturedOutput

KEY = "tskey-auth-i10-'sentinel"
READ = "tailscale ip -4"
PLATFORMS = ["lima", "wsl2", "proxmox", "aws-ec2", "azure-vm", "gcp-gce"]


@dataclass
class Create:
    """One platform's create, ready to run, with everything it handed outside the guest."""

    run: Callable[[ProvisionRequest], ProvisionResult]
    guest: FakeGuest
    argvs: Callable[[], list[list[str]]]
    retained: Callable[[], str]
    """What the provider keeps of the create (user-data, a template), or ``""``."""


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(ssh_exposure, "_egress_ip_cache", None)
    monkeypatch.setattr(ssh_exposure, "detect_egress_ip", lambda: "198.18.0.7")
    monkeypatch.setattr("agentworks.capabilities.vm_platform.tailscale_join.time.sleep", lambda seconds: None)


def request() -> ProvisionRequest:
    return ProvisionRequest(
        vm_name="vm1",
        debian_release=DebianRelease.TRIXIE,
        hostname="lab-vm1",
        system_slug=None,
        admin_username="agentworks",
        ssh_public_key="ssh-ed25519 AAAA test",
        ssh_private_key=Path("/dev/null"),
        provisioning_trust=provisioning_trust_for_tests("vm1"),
        tailscale_auth_key=KEY,
        progress=MagicMock(),
        cpus=2,
        memory_gib=8,
        disk_gib=40,
        swap_gib=4,
    )


def lima(monkeypatch: pytest.MonkeyPatch, guest: FakeGuest) -> Create:
    install_at_run_process(monkeypatch, guest)
    templates: list[str] = []
    commands: list[list[str]] = []
    monkeypatch.setattr(LimaPlatform, "_ensure_limactl", lambda self: None)
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)
    monkeypatch.setattr(LimaPlatform, "_create_local", lambda self, name, rendered: templates.append(rendered))
    monkeypatch.setattr(LimaPlatform, "_transport_for", lambda self, name: SimpleNamespace())

    def run(self: LimaHost, command: str, **kwargs: object) -> str:
        commands.append([command, repr(kwargs)])
        return f"{_REBOOT_CLEAR_MARKER}\n" if REBOOT_SENTINEL_PATH in command else ""

    monkeypatch.setattr(LimaHost, "run", run)
    platform = LimaPlatform("lima", {"placement": {"mode": "local"}})
    return Create(
        lambda req: platform.create(req, RunContext()),
        guest,
        lambda: guest.argvs + commands,
        lambda: "\n".join(templates),
    )


def wsl2_create(monkeypatch: pytest.MonkeyPatch, guest: FakeGuest) -> Create:
    install_at_run_process(monkeypatch, guest)
    commands: list[list[str]] = []

    def wsl(args: list[str], *, check: bool = True, timeout: int = 300) -> str:
        commands.append(list(args))
        return ""

    monkeypatch.setattr(wsl2, "_wsl", wsl)
    monkeypatch.setattr(wsl2, "_powershell", lambda script, **kwargs: commands.append([script]) or "")
    monkeypatch.setattr(wsl2, "_download_debian_rootfs", lambda tarball, *, tag: None)
    monkeypatch.setattr(WSL2Platform, "_distro_exists", staticmethod(lambda name: False))
    platform = WSL2Platform("wsl2", {})
    return Create(lambda req: platform.create(req, RunContext()), guest, lambda: guest.argvs + commands, str)


def proxmox(monkeypatch: pytest.MonkeyPatch, guest: FakeGuest) -> Create:
    fake = FakeProxmoxAPI()
    fake.guest = guest
    monkeypatch.setattr(ProxmoxPlatform, "_api", lambda self, ctx: fake)
    platform = ProxmoxPlatform("pve-site", dict(PROXMOX_CONFIG))
    return Create(
        lambda req: platform.create(req, RunContext()),
        guest,
        lambda: [[repr(call)] for call in fake.calls],
        str,
    )


def aws(monkeypatch: pytest.MonkeyPatch, guest: FakeGuest) -> Create:
    install_at_run_process(monkeypatch, guest)
    recorder = install_aws_fakes(monkeypatch)
    platform = EC2Platform("aws-site", {"region": "us-east-1", "auth": {"mode": "ambient"}})
    ctx = RunContext(config=SimpleNamespace(operator=SimpleNamespace(ssh_allow_cidrs=[])))
    return Create(
        lambda req: platform.create(req, ctx),
        guest,
        lambda: guest.argvs,
        lambda: gzip.decompress(recorder.kwargs_for("run_instances")["UserData"]).decode(),
    )


def azure(monkeypatch: pytest.MonkeyPatch, guest: FakeGuest) -> Create:
    install_at_run_process(monkeypatch, guest)
    fakes = install_azure_fakes(monkeypatch, vm_exists_lookup=False)
    config = {"subscription_id": "sub-A", "resource_group": "rg1", "region": "eastus", "auth": {"mode": "ambient"}}
    platform = AzureVMPlatform("az-site", config)

    def retained() -> str:
        [(_group, _name, final)] = fakes.compute.virtual_machines.created
        return base64.b64decode(final.os_profile.custom_data).decode()

    return Create(lambda req: platform.create(req, RunContext()), guest, lambda: guest.argvs, retained)


def gcp(monkeypatch: pytest.MonkeyPatch, guest: FakeGuest) -> Create:
    platform, cache = gcp_platform(monkeypatch, guest)

    def retained() -> str:
        return "\n".join(repr(insert.instance_resource) for insert in cache.instances.insert_requests)

    return Create(lambda req: platform.create(req, gcp_ctx()), guest, lambda: guest.argvs, retained)


BUILDERS: dict[str, Callable[[pytest.MonkeyPatch, FakeGuest], Create]] = {
    "lima": lima,
    "wsl2": wsl2_create,
    "proxmox": proxmox,
    "aws-ec2": aws,
    "azure-vm": azure,
    "gcp-gce": gcp,
}


def holds_key(text: str | bytes) -> bool:
    data = text.encode() if isinstance(text, str) else text
    return any(form.encode() in data for form in (KEY, shlex.quote(KEY)))


def assert_key_only_in_the_join(create: Create, req: ProvisionRequest, tmp_path: Path, output: CapturedOutput) -> None:
    scripts = zip(create.guest.payloads, create.guest.scripts, strict=True)
    holders = [payload for payload, script in scripts if holds_key(script)]
    assert holders == [JOIN_FRAGMENT] * create.guest.joins
    assert not any(holds_key(arg) for argv in create.argvs() for arg in argv)
    assert not holds_key(repr(req.progress.mock_calls))  # type: ignore[attr-defined]
    assert not holds_key(repr(output.lines))
    assert not holds_key(create.retained())
    assert not [path for path in tmp_path.rglob("*") if path.is_file() and holds_key(path.read_bytes())]


def assert_key_free(error: BaseException) -> None:
    pending: list[BaseException | None] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        assert not holds_key(str(current))
        assert not holds_key(repr(current))
        pending += [current.__cause__, current.__context__]


@pytest.mark.parametrize("name", PLATFORMS)
def test_the_key_reaches_only_the_join_script(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, captured_output: CapturedOutput
) -> None:
    create = BUILDERS[name](monkeypatch, FakeGuest())
    req = request()

    result = create.run(req)

    assert result.tailnet_address == "100.64.0.9"
    assert create.guest.joins == 1
    assert create.guest.payloads[-2:] == [JOIN_FRAGMENT, READ]
    assert_key_only_in_the_join(create, req, tmp_path, captured_output)


@pytest.mark.parametrize("name", PLATFORMS)
def test_exhausted_address_reads_log_out_without_a_second_join(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, captured_output: CapturedOutput
) -> None:
    create = BUILDERS[name](monkeypatch, FakeGuest(answer_by_payload(answers={READ: Answer(status=None)})))
    req = request()

    with pytest.raises(AgentworksError) as caught:
        create.run(req)

    assert create.guest.joins == 1
    assert create.guest.count("tailscale logout") == 1
    assert create.guest.payloads[-1] == "tailscale logout"
    assert caught.value.hint == CREATE_RERUN_HINT  # through every platform's own error wrapping
    assert_key_free(caught.value)
    assert_key_only_in_the_join(create, req, tmp_path, captured_output)


@pytest.mark.parametrize("name", PLATFORMS)
def test_an_unknown_join_is_never_retried(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, captured_output: CapturedOutput
) -> None:
    create = BUILDERS[name](monkeypatch, FakeGuest(answer_by_payload(answers={JOIN_FRAGMENT: Answer(status=None)})))
    req = request()

    with pytest.raises(AgentworksError) as caught:
        create.run(req)

    assert create.guest.joins == 1
    assert create.guest.payloads[-1] == JOIN_FRAGMENT
    assert "lab-vm1" in str(caught.value)
    assert_key_free(caught.value)
    assert_key_only_in_the_join(create, req, tmp_path, captured_output)
