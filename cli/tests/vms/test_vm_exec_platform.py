"""Platform-native recovery execution for ``vm exec``."""

from __future__ import annotations

import shlex
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.db import VMStatus
from agentworks.errors import StateError, ValidationError
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.ssh import SSHResult
from agentworks.vms import manager as vm_manager
from tests.conftest import ManifestDoc
from tests.native_exec_support import ExecCall, ExecutionOnlyTransport

if TYPE_CHECKING:
    from agentworks.db import Database


VM_ENV_TEMPLATE = ManifestDoc(
    "vm-template",
    "default",
    {"env": {"API_KEY": {"secret": "vm-env-secret"}}},
)


def _seed_vm(db: Database, *, tailscale_host: str | None = "100.64.0.9") -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    if tailscale_host is not None:
        db.update_vm_tailscale("box", tailscale_host)


def test_platform_exec_uses_limited_native_transport_without_env_resolution(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config(manifests=[VM_ENV_TEMPLATE])
    _seed_vm(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda _host: True)
    monkeypatch.setattr(
        "agentworks.vms.manager.boundary.require_vm_ssh_boundary",
        lambda *_args, **_kwargs: pytest.fail("platform recovery must not require canonical SSH evidence"),
    )
    monkeypatch.setattr(
        "agentworks.transports.transport",
        lambda *_args, **_kwargs: pytest.fail("platform recovery must not use Tailscale SSH"),
    )

    events: list[str] = []

    @contextmanager
    def _hold_active(*_args: object, **_kwargs: object):  # noqa: ANN202
        events.append("hold-open")
        try:
            yield
        finally:
            events.append("hold-close")

    monkeypatch.setattr(ProxmoxPlatform, "vm_active", _hold_active)

    def _result(_call: ExecCall) -> SSHResult:
        events.append("run")
        return SSHResult(returncode=23, stdout="buffered-out", stderr="buffered-err")

    target = ExecutionOnlyTransport(_result)
    seen_contexts: list[RunContext] = []

    def _native_transport(
        vm: object,
        platform: object,
        cfg: object,
        *,
        ctx: RunContext,
        stack: object,
    ) -> ExecutionOnlyTransport:
        del vm, platform, cfg, stack
        events.append("native-transport")
        seen_contexts.append(ctx)
        return target

    monkeypatch.setattr("agentworks.transports.native_transport", _native_transport)

    command = ["printf", "%s", "hello world"]
    result = vm_manager.exec_vm_platform(
        db,
        config,
        "box",
        command,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert result == SSHResult(returncode=23, stdout="buffered-out", stderr="buffered-err")
    assert target.calls == [
        ExecCall(
            command=shlex.join(command),
            sudo=False,
            check=False,
            timeout=None,
            input_text=None,
        )
    ]
    assert resolve_counter == [["proxmox-token"]]
    assert events == ["hold-open", "native-transport", "run", "hold-close"]
    (ctx,) = seen_contexts
    assert ctx.secret("proxmox-token") == "pve-token"
    with pytest.raises(StateError):
        ctx.secret("vm-env-secret")


def test_platform_exec_rejects_workspace_before_composition(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(manifests=[VM_ENV_TEMPLATE])
    monkeypatch.setattr(
        "agentworks.transports.native_transport",
        lambda *_args, **_kwargs: pytest.fail("invalid options must not construct a transport"),
    )

    with pytest.raises(ValidationError) as exc_info:
        vm_manager.exec_vm_platform(
            db,
            config,
            "box",
            ["pwd"],
            workspace_name="ws1",
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert exc_info.value.entity_kind == "vm"
    assert exc_info.value.entity_name == "box"
    assert resolve_counter == []


@pytest.mark.parametrize("initial_status", [VMStatus.STOPPED, VMStatus.DEALLOCATED])
def test_platform_exec_starts_without_canonical_connectivity_repair(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
    initial_status: VMStatus,
) -> None:
    config = make_config(manifests=[VM_ENV_TEMPLATE])
    _seed_vm(db, tailscale_host=None)
    events: list[str] = []
    site_tokens: list[str] = []

    def _status(_self: object, _row: object, ctx: RunContext) -> VMStatus:
        events.append("status")
        site_tokens.append(ctx.secret("proxmox-token"))
        return initial_status

    def _start(_self: object, _row: object, ctx: RunContext) -> None:
        events.append("start")
        site_tokens.append(ctx.secret("proxmox-token"))

    @contextmanager
    def _hold_active(*_args: object, **_kwargs: object):  # noqa: ANN202
        events.append("hold-open")
        try:
            yield
        finally:
            events.append("hold-close")

    monkeypatch.setattr(ProxmoxPlatform, "status", _status)
    monkeypatch.setattr(ProxmoxPlatform, "start", _start)
    monkeypatch.setattr(ProxmoxPlatform, "vm_active", _hold_active)
    monkeypatch.setattr(
        vm_manager,
        "_is_tailscale_reachable",
        lambda *_args, **_kwargs: pytest.fail("platform recovery must not probe Tailscale"),
    )
    monkeypatch.setattr(
        vm_manager,
        "_tailscale_rejoin_required",
        lambda *_args, **_kwargs: pytest.fail("platform recovery must not evaluate Tailscale repair"),
    )
    monkeypatch.setattr(
        vm_manager,
        "_ensure_tailscale",
        lambda *_args, **_kwargs: pytest.fail("platform recovery must not rejoin Tailscale"),
    )
    monkeypatch.setattr(
        "agentworks.vms.manager.boundary.require_vm_ssh_boundary",
        lambda *_args, **_kwargs: pytest.fail("platform recovery must not require canonical SSH evidence"),
    )
    monkeypatch.setattr(
        "agentworks.transports.transport",
        lambda *_args, **_kwargs: pytest.fail("platform recovery must not use the canonical transport"),
    )

    def _result(_call: ExecCall) -> SSHResult:
        events.append("run")
        return SSHResult(returncode=0, stdout="", stderr="")

    target = ExecutionOnlyTransport(_result)
    seen_contexts: list[RunContext] = []

    def _native_transport(
        _vm: object,
        _platform: object,
        _config: object,
        *,
        ctx: RunContext,
        stack: object,
    ) -> ExecutionOnlyTransport:
        del stack
        events.append("native-transport")
        seen_contexts.append(ctx)
        return target

    monkeypatch.setattr("agentworks.transports.native_transport", _native_transport)

    result = vm_manager.exec_vm_platform(
        db,
        config,
        "box",
        ["true"],
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert result.returncode == 0
    assert target.calls == [ExecCall(command="true", sudo=False, check=False, timeout=None, input_text=None)]
    assert events == ["status", "start", "hold-open", "native-transport", "run", "hold-close"]
    assert site_tokens == ["pve-token", "pve-token"]
    assert resolve_counter == [["proxmox-token"]]
    vm = db.get_vm("box")
    assert vm is not None
    assert vm.last_started_at is not None
    (ctx,) = seen_contexts
    assert ctx.secret("proxmox-token") == "pve-token"
    with pytest.raises(StateError):
        ctx.secret("vm-env-secret")
