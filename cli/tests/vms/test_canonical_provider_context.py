"""Canonical contexts over real owned adapters with scripted provider reads.

SSH delivery and provider observations are synthetic. SQLite claims, common
preparation, account composition and packed application helpers remain intact.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator, VMPlatform
from agentworks.db import VMStatus
from agentworks.execution.binding import _IndependentJobAvailability
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.proxmox import _ProxmoxWire
from agentworks.execution.carriers.ssh import SSHCarrier
from agentworks.execution.models import Command
from agentworks.execution.profiles import Protection
from agentworks.execution.target import ExecutionRoute
from agentworks.plugins.aws._native_access import EC2OwnedAccess
from agentworks.plugins.aws.platform import EC2Platform
from agentworks.plugins.azure._native_access import AzureOwnedReadAccess
from agentworks.plugins.azure.platform import AzureVMPlatform
from agentworks.plugins.gcp._native_access import GCEInstanceObservation, GCEOwnedRead
from agentworks.plugins.gcp.platform import GCEPlatform
from agentworks.vms._native_operation import vm_execution_context
from tests.vms.canonical_support import ADDRESS, canonical_config, install_canonical_delivery
from tests.vms.test_native_operation import _root, _RouteCarrier, _scope
from tests.vms.test_native_operation import database as database

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="packed Linux helper fixture")


@pytest.mark.parametrize("provider", ["aws", "azure", "gcp"])
def test_canonical_cloud_context_requires_no_public_topology_or_native_route(database, tmp_path, monkeypatch, provider):
    route = _RouteCarrier(database)
    connections = install_canonical_delivery(monkeypatch, route)
    deadline = Deadline.after(30)
    accesses: list[Any] = []
    reads = []
    platform: VMPlatform

    def deny(*args, **kwargs):
        pytest.fail("canonical cloud context selected native route or endpoint")

    def read() -> None:
        claim = database.operations.inspect(_scope())
        assert claim is not None and claim.ownership == accesses[0]._owner.ownership
        reads.append(provider)

    if provider == "aws":
        platform = EC2Platform("wsl2", {"region": "us-east-1", "auth": {"mode": "ambient"}})
        metadata = {"instance_id": "i-0123456789abcdef0", "account_id": "123456789012", "region": "us-east-1"}
        locator = ProviderLocator("aws-ec2:123456789012:us-east-1:i-0123456789abcdef0")

        def ec2(access, budget):
            assert budget is deadline and access is accesses[0]
            read()
            return locator, {"State": {"Name": "running"}}

        monkeypatch.setattr(EC2OwnedAccess, "_read_exact_instance", ec2)
        monkeypatch.setattr(EC2OwnedAccess, "observe_public_endpoint", deny)
    elif provider == "azure":
        platform = AzureVMPlatform("wsl2", {"subscription_id": "sub", "resource_group": "group", "region": "eastus"})
        resource = "/subscriptions/sub/resourceGroups/group/providers/Microsoft.Compute/virtualMachines/vm"
        metadata = {"resource_id": resource, "vm_id": "00112233-4455-6677-8899-aabbccddeeff"}
        current = SimpleNamespace(instance_view=SimpleNamespace(statuses=[SimpleNamespace(code="PowerState/running")]))

        def azure(reader, budget, kind):
            assert reader is accesses[0]._reader and budget is deadline
            assert kind in {"power", "vm"}
            read()
            return VMStatus.RUNNING if kind == "power" else current

        monkeypatch.setattr(AzureOwnedReadAccess, "_observe", azure)
        monkeypatch.setattr(AzureOwnedReadAccess, "observe_public_endpoint", deny)
    else:
        platform = GCEPlatform("wsl2", {"project_id": "project", "zone": "us-central1-a"})
        metadata = {"project_id": "project", "zone": "us-central1-a", "instance_name": "vm", "instance_id": "123"}
        locator = ProviderLocator("gcp-gce:project:us-central1-a:123")

        def gce(reader, budget, *, identity=None):
            assert reader is accesses[0]._reader and budget is deadline and identity is None
            read()
            return GCEInstanceObservation(locator, "RUNNING", VMStatus.RUNNING, None, (), ())

        monkeypatch.setattr(GCEOwnedRead, "_read_exact_instance", gce)
        monkeypatch.setattr(GCEOwnedRead, "observe_instance", deny)
    database.update_vm_platform_metadata("box", metadata)
    database.update_vm_tailscale("box", ADDRESS)
    config = canonical_config(tmp_path)
    original = platform.build_native_execution_access

    def build(*args, **kwargs):
        access = original(*args, **kwargs)
        accesses.append(access)
        monkeypatch.setattr(access, "prepare_native_route", deny)
        monkeypatch.setattr(type(access), "native_configuration_check", property(lambda _: deny))
        return access

    monkeypatch.setattr(platform, "build_native_execution_access", build)
    monkeypatch.setattr(platform, "resolve_native_execution_binding", deny)
    monkeypatch.setattr("agentworks.vms._ssh_native_exposure.OwnedSSHExposure.resolve", deny)
    root = _root(tmp_path)
    with vm_execution_context(
        database,
        "box",
        platform,
        RunContext(config=config),
        route=ExecutionRoute.CANONICAL,
        deadline=deadline,
        trusted_root=root,
    ) as ctx:
        target = ctx.admin_execution_target()
        assert target is not None and target.route is ExecutionRoute.CANONICAL
        execution, files = target.execution(), target.files()
        assert execution is not None and files is not None
        binding = execution._operation._native_binding
        assert binding is not None and isinstance(binding.carrier, SSHCarrier)
        assert (
            binding._independent_availability
            is accesses[0].independent_availability
            is _IndependentJobAvailability.NO_IDLE_STOP
        )
        assert len(accesses) == 1 and accesses[0].binding is None
        assert execution.run(Command(["/bin/true"]), profile=Protection.DIRECT).ok
        assert files.stat(root / "missing") is None
    assert reads and connections and route.guest.calls == 1
    assert database.operations.inspect(_scope()) is None


def test_stopped_proxmox_canonical_context_activates_without_qga_guest_info(database, tmp_path, monkeypatch):
    from tests.vms.test_proxmox_stopped_operation import INFO, Startup, task

    startup = Startup(database, monkeypatch)
    database._conn.execute("UPDATE vms SET site = ? WHERE name = ?", ("pve", "box"))
    database._conn.commit()
    startup.tasks, startup.infos = [task()], [INFO]
    route = _RouteCarrier(database)
    connections = install_canonical_delivery(monkeypatch, route)
    database.update_vm_tailscale("box", ADDRESS)

    def deny(*args, **kwargs):
        pytest.fail("canonical Proxmox context selected QGA guest delivery")

    monkeypatch.setattr(_ProxmoxWire, "request_guest_info", deny)
    monkeypatch.setattr("agentworks.plugins.proxmox._native_access.proxmox_native_binding", deny)
    monkeypatch.setattr(
        startup.platform,
        "observe_execution_power",
        lambda *args, **kwargs: VMStatus.RUNNING if "power" in startup.events else VMStatus.STOPPED,
    )
    with vm_execution_context(
        database,
        "box",
        startup.platform,
        RunContext(config=canonical_config(tmp_path)),
        route=ExecutionRoute.CANONICAL,
        deadline=Deadline.after(30),
        trusted_root=_root(tmp_path),
    ) as ctx:
        target = ctx.admin_execution_target()
        assert target is not None
        execution = target.execution()
        assert execution is not None
        assert execution.run(Command(["/bin/true"]), profile=Protection.DIRECT).ok
        binding = execution._operation._native_binding
        assert binding is not None
        assert binding._independent_availability is _IndependentJobAvailability.NO_IDLE_STOP
    assert startup.events == ["start", "task", "power"]
    assert connections and route.guest.calls == 1 and startup.route.guest.calls == startup.route.resolutions == 0
    assert database.operations.inspect(_scope()) is None
