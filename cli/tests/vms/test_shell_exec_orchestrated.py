"""``vm shell`` / ``vm exec`` through the orchestrated model: the
derived graph with the env-target seam (env secrets join the boundary
via target registration, never the walk union), the gate-prompt parity
carries (these commands DO open the activation gate), the pre-gate
validation pins, and the VM scope reaching node readiness.

Real config, registry, resolver, and backend loop (env-var backend);
the platform's backend power ops, the reachability probe, and the SSH
transport are the fakes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.db import ProvisioningStatus, VersionedPayload, VMStatus
from agentworks.errors import StateError, ValidationError
from agentworks.execution.access import Admin
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager
from tests.conftest import ManifestDoc, seed_vm_trust, stub_vm_ssh_identity

if TYPE_CHECKING:
    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.db import Database
    from agentworks.execution.requests import LoginShell
    from tests.vm_access_support import AccessRecorder

# A vm template with an env-block secret reference: a RUNTIME input
# for these commands, so it joins the boundary through the env-target
# registration (one prompt session with the site secret), never
# through the walk union.
VM_ENV_TEMPLATE = ManifestDoc(
    "vm-template",
    "default",
    {"env": {"API_KEY": {"secret": "vm-env-secret"}}},
)


@pytest.fixture(autouse=True)
def _env_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_SECRET_VM_ENV_SECRET", "env-val")
    stub_vm_ssh_identity(monkeypatch)


def _seed_vm(db: Database) -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    db.update_vm_provisioning_status("box", ProvisioningStatus.COMPLETE)
    seed_vm_trust("box")


def _seed_workspace(
    db: Database,
    *,
    vm_name: str = "box",
    name: str = "ws1",
    template: str | None = None,
) -> None:
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group, template) VALUES (?, ?, ?, ?, ?)",
        (name, vm_name, f"/srv/{name}", f"ws-{name}", template),
    )
    db._conn.commit()


def _reachable(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: value)


def _stop_the_vm(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    _reachable(monkeypatch, False)
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, row, ctx: events.append("status") or VMStatus.STOPPED,
    )
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, row, ctx: events.append("start"))
    monkeypatch.setattr("agentworks.vms.manager._await_tailnet", lambda *a, **k: events.append("tailscale"))


# -- the derived graph and the env-target seam --------------------------------


def test_graph_derives_from_row_and_env_joins_via_targets(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    """The shell / exec graph is the live VM alone (vm-site + vm), so
    the walk union is the site's config secret ONLY. The env-chain
    secret enters the boundary through the target registration seam,
    NOT the union: the distinction this pin exists to hold."""
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node

    config = make_config(manifests=[VM_ENV_TEMPLATE])
    _seed_vm(db)
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)
    resolver = Resolver(config, registry, interaction=TtyInteractionPolicy.REFUSE)

    nodes = walk(live_vm_node(db, config, registry, vm))

    assert [n.key for n in nodes] == ["vm-site/proxmox", "vm/box"]
    assert secret_union(nodes) == ("proxmox-token",)

    for name in secret_union(nodes):
        resolver.register_name(name)
    scopes = vm_manager._resolve_vm_admin_env_scopes(db, registry, vm)
    resolver.register_targets([vm_manager._vm_secret_target(scopes, label="vm-shell=box")])
    resolver.resolve()
    assert set(resolver.values) == {"proxmox-token", "vm-env-secret"}


# A real, resolvable workspace template whose env must flow into the
# workspace scope of vm-level ``--workspace`` commands: the positive
# control against the ``copied``-marker case, which resolves to nothing.
WORKSPACE_ENV_TEMPLATE = ManifestDoc(
    "workspace-template",
    "proj",
    {"env": {"WS_VAR": "ws-val"}},
)


def test_copied_workspace_pin_resolves_env_scopes_without_a_template(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    """A copied workspace records the synthetic ``template="copied"``
    marker, which is not a real template. Resolving vm-level env scopes
    for such a pinned workspace must NOT raise ``unknown_template_error``
    (issue #285): the workspace contributes an empty template scope while
    the vm and admin scopes stay intact. Mirrors how ``repair`` tolerates
    the same marker as a quiet no-op. A real workspace template still
    contributes its env, proving the tolerance is scoped to the failure."""
    from agentworks.bootstrap import build_registry

    config = make_config(manifests=[VM_ENV_TEMPLATE, WORKSPACE_ENV_TEMPLATE])
    _seed_vm(db)
    _seed_workspace(db, name="copied-ws", template="copied")
    _seed_workspace(db, name="proj-ws", template="proj")
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)

    copied = db.get_workspace("copied-ws")
    proj = db.get_workspace("proj-ws")
    assert copied is not None and proj is not None

    # The copied marker resolves cleanly to an empty workspace scope; the
    # vm scope (its env-block secret ref) is untouched.
    copied_scopes = vm_manager._resolve_vm_admin_env_scopes(db, registry, vm, ws=copied)
    assert copied_scopes.workspace == {}
    assert "API_KEY" in copied_scopes.vm

    # A real template still contributes its env: the fix does not swallow
    # resolvable templates.
    proj_scopes = vm_manager._resolve_vm_admin_env_scopes(db, registry, vm, ws=proj)
    assert proj_scopes.workspace is not None
    assert "WS_VAR" in proj_scopes.workspace


def test_vm_workspace_env_uses_base_for_an_unsupported_stored_overlay(
    db: Database,
    make_config,  # noqa: ANN001
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.bootstrap import build_registry

    config = make_config(manifests=[VM_ENV_TEMPLATE, WORKSPACE_ENV_TEMPLATE])
    _seed_vm(db)
    _seed_workspace(db, name="future-ws", template="proj")
    plaintext = "do-not-print-this-value"
    db.instance_state.put_desired_overlay(
        "workspace",
        "future-ws",
        VersionedPayload(1, {"future_field": plaintext}),
    )
    vm = db.get_vm("box")
    ws = db.get_workspace("future-ws")
    assert vm is not None and ws is not None

    scopes = vm_manager._resolve_vm_admin_env_scopes(db, build_registry(config), vm, ws=ws)

    assert scopes.workspace is not None
    assert scopes.workspace["WS_VAR"].value == "ws-val"
    assert captured_output.warnings
    assert plaintext not in repr(captured_output.lines)


def test_vm_workspace_env_does_not_swallow_a_malformed_stored_overlay(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    from agentworks.bootstrap import build_registry

    config = make_config(manifests=[VM_ENV_TEMPLATE, WORKSPACE_ENV_TEMPLATE])
    _seed_vm(db)
    _seed_workspace(db, name="broken-ws", template="proj")
    db.instance_state.put_desired_overlay(
        "workspace",
        "broken-ws",
        VersionedPayload(1, {"env": {"TOKEN": {"unexpected": "value"}}}),
    )
    vm = db.get_vm("box")
    ws = db.get_workspace("broken-ws")
    assert vm is not None and ws is not None

    with pytest.raises(StateError) as caught:
        vm_manager._resolve_vm_admin_env_scopes(db, build_registry(config), vm, ws=ws)

    assert type(caught.value) is StateError


def test_exec_copied_workspace_pin_no_longer_crashes(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """End to end: ``vm exec --workspace <copied-ws>`` ran the command in
    the workspace dir but crashed on the synthetic template before this
    fix (issue #285). It now runs, contributing no workspace-template env."""
    config = make_config(manifests=[VM_ENV_TEMPLATE])
    _seed_vm(db)
    _seed_workspace(db, name="copied-ws", template="copied")
    _reachable(monkeypatch, True)

    rc = vm_manager.exec_vm(
        db, config, "box", ["echo", "hi"], workspace_name="copied-ws", interaction=TtyInteractionPolicy.REFUSE
    )

    assert rc == 0
    (request,) = vm_access_recorder.requests("stream")
    # Still runs in the workspace dir, just without any template-derived env.
    assert (request.source, request.cwd) == ("echo hi", "/srv/copied-ws")
    # The vm-template env still flows; the copied workspace adds nothing.
    assert request.env.get("API_KEY") == "env-val"


# -- gate-prompt parity (the per-command carries) -----------------------------


def test_shell_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """vm shell on a reachable VM: the gate's fast path costs nothing,
    so site + env secrets ride ONE boundary burst, and the composed
    env (with the resolved secret) reaches the interactive shell."""
    config = make_config(manifests=[VM_ENV_TEMPLATE])
    _seed_vm(db)
    _reachable(monkeypatch, True)

    # shell_vm returns the interactive exit code; the CLI owns process exit.
    assert vm_manager.shell_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE) == 0

    assert len(resolve_counter) == 1
    assert sorted(resolve_counter[0]) == ["proxmox-token", "vm-env-secret"]
    (request,) = vm_access_recorder.requests("attach")
    assert request.cwd is None  # the admin's home: an interactive login shell, no command
    assert request.env.get("API_KEY") == "env-val"


def test_shell_stopped_vm_gate_burst_then_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """vm shell on a stopped VM: the gate's just-in-time token resolve
    (seeding the boundary), then ONE boundary burst for the env
    secrets; nothing resolves twice, nothing after."""
    config = make_config(manifests=[VM_ENV_TEMPLATE])
    _seed_vm(db)
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)

    assert vm_manager.shell_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE) == 0

    assert events == ["status", "start", "tailscale"]  # the gate ran
    assert resolve_counter == [["proxmox-token"], ["vm-env-secret"]]
    assert len(vm_access_recorder.requests("attach")) == 1


def test_exec_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config(manifests=[VM_ENV_TEMPLATE])
    _seed_vm(db)
    _reachable(monkeypatch, True)

    rc = vm_manager.exec_vm(db, config, "box", ["echo", "hi"], interaction=TtyInteractionPolicy.REFUSE)

    assert rc == 0
    assert len(resolve_counter) == 1
    assert sorted(resolve_counter[0]) == ["proxmox-token", "vm-env-secret"]
    (request,) = vm_access_recorder.requests("stream")
    assert request.source == "echo hi"
    assert request.env.get("API_KEY") == "env-val"


def test_exec_resolves_a_stored_overlay_only_secret_through_the_runtime_boundary(
    db: Database,
    make_config,  # noqa: ANN001
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.instance_specs import parse_vm_instance_specs

    config = make_config()
    _seed_vm(db)
    overlays = parse_vm_instance_specs(
        '{"env":{"OVERLAY_TOKEN":{"secret":"overlay-only"}}}',
        '{"env":{"ADMIN_OVERLAY_TOKEN":{"secret":"admin-overlay-only"}}}',
    )
    assert overlays is not None
    db.instance_state.put_desired_overlay("vm", "box", overlays.payload)
    monkeypatch.setenv("AW_SECRET_OVERLAY_ONLY", "resolved")
    monkeypatch.setenv("AW_SECRET_ADMIN_OVERLAY_ONLY", "admin-resolved")
    _reachable(monkeypatch, True)

    rc = vm_manager.exec_vm(db, config, "box", ["echo", "hi"], interaction=TtyInteractionPolicy.REFUSE)

    assert rc == 0
    (request,) = vm_access_recorder.requests("stream")
    env = request.env
    assert env["OVERLAY_TOKEN"] == "resolved"
    assert env["ADMIN_OVERLAY_TOKEN"] == "admin-resolved"


def test_exec_stopped_vm_gate_burst_then_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config(manifests=[VM_ENV_TEMPLATE])
    _seed_vm(db)
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)

    rc = vm_manager.exec_vm(db, config, "box", ["echo", "hi"], interaction=TtyInteractionPolicy.REFUSE)

    assert rc == 0
    assert events == ["status", "start", "tailscale"]
    assert resolve_counter == [["proxmox-token"], ["vm-env-secret"]]


# -- validation stays pre-gate ------------------------------------------------


def _no_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    def _status_must_not_run(self: ProxmoxPlatform, row: object) -> VMStatus:
        raise AssertionError("the gate ran for a command that must fail pre-gate")

    monkeypatch.setattr(ProxmoxPlatform, "status", _status_must_not_run)
    _reachable(monkeypatch, False)


def test_exec_dash_prefixed_command_fails_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed_vm(db)
    _no_gate(monkeypatch)

    with pytest.raises(ValidationError, match="cannot start with '-'") as exc_info:
        vm_manager.exec_vm(db, config, "box", ["--workspace", "ws1", "pwd"], interaction=TtyInteractionPolicy.REFUSE)

    # The hint names the real workaround: the `--` separator (and the
    # `sh -c` fallback), not the misleading "put agentworks args first".
    hint = exc_info.value.hint or ""
    assert "put '--' before" in hint
    assert "sh -c" in hint
    assert resolve_counter == []
    assert vm_access_recorder.requests("stream") == []


def test_exec_double_dash_separator_runs_dash_led_command(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """`vm exec box -- --version` consumes the leading `--` separator and
    runs the dash-led remote command verbatim; the leading-dash guard
    steps aside because the operator marked where the command begins."""
    config = make_config(manifests=[VM_ENV_TEMPLATE])
    _seed_vm(db)
    _reachable(monkeypatch, True)

    rc = vm_manager.exec_vm(db, config, "box", ["--", "--version"], interaction=TtyInteractionPolicy.REFUSE)

    assert rc == 0
    (request,) = vm_access_recorder.requests("stream")
    assert request.source == "--version"  # one argument is a command line


def test_exec_double_dash_separator_strips_only_the_first(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Only ONE leading `--` is consumed; a later `--` is part of the
    remote command and is preserved verbatim."""
    config = make_config(manifests=[VM_ENV_TEMPLATE])
    _seed_vm(db)
    _reachable(monkeypatch, True)

    rc = vm_manager.exec_vm(
        db, config, "box", ["--", "git", "log", "--", "path"], interaction=TtyInteractionPolicy.REFUSE
    )

    assert rc == 0
    (request,) = vm_access_recorder.requests("stream")
    assert request.source == "git log -- path"


def test_exec_double_dash_separator_preserves_an_adjacent_second(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Back-to-back separators: only the FIRST `--` is consumed, so an
    immediately-adjacent second `--` survives as the remote command's
    own first token."""
    config = make_config(manifests=[VM_ENV_TEMPLATE])
    _seed_vm(db)
    _reachable(monkeypatch, True)

    rc = vm_manager.exec_vm(db, config, "box", ["--", "--", "x"], interaction=TtyInteractionPolicy.REFUSE)

    assert rc == 0
    (request,) = vm_access_recorder.requests("stream")
    assert request.source == "-- x"


def test_exec_missing_command_after_double_dash_fails_pre_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare `--` with nothing after it is a missing command, rejected
    before any resolve or gate."""
    config = make_config()
    _seed_vm(db)
    _no_gate(monkeypatch)

    with pytest.raises(ValidationError, match="missing command after '--'"):
        vm_manager.exec_vm(db, config, "box", ["--"], interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == []
    assert vm_access_recorder.requests("stream") == []


def test_exec_bare_flag_after_command_word_still_works(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """A flag that follows a command word (no separator needed) runs as
    before: `vm exec box free -m` is unaffected by the `--` handling."""
    config = make_config(manifests=[VM_ENV_TEMPLATE])
    _seed_vm(db)
    _reachable(monkeypatch, True)

    rc = vm_manager.exec_vm(db, config, "box", ["free", "-m"], interaction=TtyInteractionPolicy.REFUSE)

    assert rc == 0
    (request,) = vm_access_recorder.requests("stream")
    assert request.source == "free -m"


def test_cross_vm_workspace_mismatch_fails_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed_vm(db)
    db.insert_vm("other", site="proxmox", hostname="other")
    _seed_workspace(db, vm_name="other", name="ws-other")
    _no_gate(monkeypatch)

    with pytest.raises(ValidationError, match="belongs to VM 'other', not 'box'"):
        vm_manager.exec_vm(
            db, config, "box", ["echo", "hi"], workspace_name="ws-other", interaction=TtyInteractionPolicy.REFUSE
        )

    assert resolve_counter == []
    assert vm_access_recorder.requests("stream") == []


# -- the operation scope reaches readiness ------------------------------------


def test_vm_scope_reaches_node_readiness(
    db: Database,
    make_config,  # noqa: ANN001
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.capabilities.base import ScopeLevel

    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    scopes: list[OperationScope | None] = []
    real = ProxmoxPlatform.preflight

    def _recording(self: ProxmoxPlatform, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _recording)

    vm_manager.exec_vm(db, config, "box", ["echo", "hi"], interaction=TtyInteractionPolicy.REFUSE)

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.VM
    assert scope.vm == "box"
    assert scope.workspace is None and scope.agent is None and scope.session is None


# -- the held-active span -----------------------------------------------------


def test_shell_interactive_runs_inside_the_held_active_span(
    db: Database,
    make_config,  # noqa: ANN001
    vm_access_recorder: AccessRecorder,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """keep_active parity: the interactive session runs INSIDE the
    gate's held-active span (the WSL2 keepalive anchor), which closes
    after it."""
    import contextlib as _contextlib

    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    events: list[str] = []

    @_contextlib.contextmanager
    def _recording_hold(self: ProxmoxPlatform, row: object, *, config: object = None):  # noqa: ANN202
        events.append("hold-open")
        try:
            yield
        finally:
            events.append("hold-close")

    monkeypatch.setattr(ProxmoxPlatform, "vm_active", _recording_hold)
    admin_access = vm_access_recorder.access(Admin())
    attach = admin_access.attach

    def _tracking(request: LoginShell) -> int:
        events.append("interactive")
        return attach(request)

    admin_access.attach = _tracking  # type: ignore[method-assign]

    assert vm_manager.shell_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE) == 0

    assert events == ["hold-open", "interactive", "hold-close"]


# -- transport routing guards stay pre-composition ----------------------------


def test_shell_no_tailscale_fails_before_any_resolve(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.errors import StateError

    config = make_config()
    db.insert_vm("box", site="proxmox", hostname="box")  # no tailscale host
    db.update_vm_provisioning_status("box", ProvisioningStatus.COMPLETE)
    _no_gate(monkeypatch)

    with pytest.raises(StateError, match="no Tailscale IP"):
        vm_manager.shell_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)
    assert resolve_counter == []


def test_shell_platform_runs_the_native_shell_on_the_node_platform_with_the_site_context(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """--platform runs ``native_shell`` on the SAME platform instance the
    node's site edge holds, with the boundary's op-start context.

    A cold ``vm shell --platform`` on a running VM never touches the
    platform with a context first (the gate's happy path is a pure
    Tailscale reachability probe), so a secret-less context here would
    leave a platform whose channel needs a credential (a cloud's route)
    no delivery surface at all. The pin: the delivered context reads the
    site's declared secret, and only that one, since the native shell
    composes no environment and so resolves no environment secret (R7).
    """
    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    seen: list[tuple[object, RunContext]] = []

    def _native_shell(platform: object, vm: object, ctx: RunContext) -> int:
        seen.append((platform, ctx))
        return 0

    monkeypatch.setattr(ProxmoxPlatform, "native_shell", _native_shell)

    assert vm_manager.shell_vm(db, config, "box", native_shell=True, interaction=TtyInteractionPolicy.REFUSE) == 0

    ((platform, ctx),) = seen
    assert isinstance(platform, ProxmoxPlatform)
    assert ctx.secret("proxmox-token") == "pve-token"
    assert not any("vm-env-secret" in names for names in resolve_counter)
    with pytest.raises(StateError, match="not declared by this node"):
        ctx.secret("vm-env-secret")
