"""Unproved platform bindings refuse RESOURCE launch before any new effect."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.errors import StateError
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh import SSHSettings
from agentworks.execution.models import Command, Lifetime, Output, Script, Shell
from agentworks.execution.profiles import Protection
from agentworks.vms._ssh_native_binding import ssh_native_binding
from tests.execution.test_independent_execution_reads import observer as observer


@pytest.mark.parametrize("platform", ["default", "ssh", "wsl2"])
@pytest.mark.parametrize("user_default", [False, True])
def test_unproved_binding_refuses_before_shell_reservation_and_dispatch(
    observer, monkeypatch, tmp_path, platform, user_default
):
    _, repository, operation, access, main, workflow = observer
    vm = SimpleNamespace(name="vm-one", admin_username="root", platform_metadata={"distro_name": "recorded"})
    if platform == "ssh":
        binding = ssh_native_binding(vm, "198.51.100.2", SSHSettings(tmp_path / "trust", tmp_path / "key"))
    elif platform == "wsl2":
        binding = WSL2Platform("wsl2", {}).resolve_native_execution_binding(
            vm, RunContext(), deadline=Deadline.after(5)
        )
    else:
        binding = operation._native_binding
    assert binding._independent_availability is None
    # Substitute only delivery so the real platform's facts reach admission
    # under the fixture's otherwise matching RESOURCE execution context.
    operation._native_binding = replace(binding, carrier=main)
    monkeypatch.setattr(repository, "reserve", lambda *a, **k: pytest.fail("reservation"))
    monkeypatch.setattr(workflow.owner, "borrow", lambda: pytest.fail("shell lookup or dispatch borrow"))
    invocation = Script("true", shell=Shell.USER_DEFAULT) if user_default else Command(["/bin/true"])
    with pytest.raises(StateError):
        access.start(invocation, profile=Protection.MANAGED, lifetime=Lifetime.INDEPENDENT, output=Output.discard())
    assert operation.active_inline_calls == () and main.start.calls == 0
