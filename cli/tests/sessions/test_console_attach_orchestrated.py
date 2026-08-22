"""The named-console attach path through the orchestrated model.

Pins the lazily-decided console-node ruling
(there is NO console node: attach provisions nothing, so the graph is
the live VM alone), the gate-prompt parity carries, the gate's
held-active span covering the interactive attach (keep-active parity
with the imperative hold), the pre-gate no-Tailscale bail, and the VM
scope reaching node readiness.

Real config, registry, resolver, and backend loop (env-var backend);
the platform's backend power ops, the reachability probe, and the SSH
transport are the fakes.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.db import VMStatus
from agentworks.errors import StateError, ValidationError
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import multi_console
from agentworks.vms import manager as vm_manager
from tests.conftest import ManifestDoc

if TYPE_CHECKING:
    from agentworks.db import Database


@pytest.fixture(autouse=True)
def _outside_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)


def _seed_vm(db: Database) -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")


def _seed_named_console(db: Database) -> None:
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('c1', 'box')")
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
    monkeypatch.setattr(vm_manager, "_tailscale_rejoin_required", lambda *a, **k: True)
    monkeypatch.setattr(vm_manager, "_ensure_tailscale", lambda *a, **k: events.append("tailscale"))


class _FakeTarget:
    """Transport double: ``run`` reports the console tmux session as
    already existing (the plain-attach path), ``interactive`` records
    the attach command."""

    def __init__(self) -> None:
        self.interactive_calls: list[str] = []

    def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(ok=True, returncode=0, stdout="", stderr="")

    def interactive(self, cmd: str, **kwargs: object) -> int:
        self.interactive_calls.append(cmd)
        return 0


@pytest.fixture
def target(monkeypatch: pytest.MonkeyPatch) -> _FakeTarget:
    fake = _FakeTarget()
    monkeypatch.setattr("agentworks.transports.transport", lambda vm, config, **kwargs: fake)
    return fake


# -- the derived graph (the console-node ruling) ------------------------------


def test_attach_graph_is_the_live_vm_alone(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    """The lazily-decided console-node ruling: attach provisions
    nothing console-shaped, so no console node exists and the graph is
    the live VM (vm-site + vm), union = the site's config secret."""
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.vms.nodes import live_vm_node

    config = make_config()
    _seed_vm(db)
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)

    nodes = walk(live_vm_node(db, config, registry, vm))

    assert [n.key for n in nodes] == ["vm-site/proxmox", "vm/box"]
    assert secret_union(nodes) == ("proxmox-token",)


def test_named_console_attach_holds_across_the_interactive_attach(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Hold parity with the imperative caller-opened ``vm_active``
    span: the gate's held-active span covers the tmux probe and the
    interactive attach, closing after them."""
    config = make_config()
    _seed_vm(db)
    _seed_named_console(db)
    _reachable(monkeypatch, True)
    events: list[str] = []

    @contextlib.contextmanager
    def _recording_hold(self: ProxmoxPlatform, row: object, *, config: object = None):  # noqa: ANN202
        events.append("hold-open")
        try:
            yield
        finally:
            events.append("hold-close")

    monkeypatch.setattr(ProxmoxPlatform, "vm_active", _recording_hold)
    interactive = target.interactive

    def _tracking(cmd: str, **kwargs: object) -> int:
        events.append("interactive")
        return interactive(cmd, **kwargs)

    target.interactive = _tracking  # type: ignore[method-assign]

    multi_console.attach_console(db, config, name="c1", interaction=TtyInteractionPolicy.REFUSE)

    assert events == ["hold-open", "interactive", "hold-close"]
    assert any("Attaching to running console 'c1'" in m for m in captured_output.info)


def test_console_recreate_multiline_environment_secret_refuses_before_rebuild(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_value = "console-recreate-sentinel\nsecond\n"
    monkeypatch.setenv("AW_SECRET_CONSOLE_ENV", environment_value)
    config = make_config(
        manifests=[
            ManifestDoc(
                "vm-template",
                "default",
                {"env": {"CONSOLE_ENV": {"secret": "console-env"}}},
            ),
            ManifestDoc("secret", "console-env", description="console environment"),
        ]
    )
    _seed_vm(db)
    db.insert_console("c1", "box", admin_shell=True)
    _reachable(monkeypatch, True)
    monkeypatch.setattr(multi_console, "_console_tmux_exists", lambda *args: True)
    rebuilds: list[str] = []
    monkeypatch.setattr(
        "agentworks.sessions.multi_console.attach._build_console_tmux",
        lambda *args, **kwargs: rebuilds.append("rebuild"),
    )

    with pytest.raises(ValidationError) as caught:
        multi_console.attach_console(
            db,
            config,
            name="c1",
            recreate=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert rebuilds == []
    assert target.interactive_calls == []
    assert db.get_console("c1") is not None
    assert "console-recreate-sentinel" not in repr((caught.value.args, vars(caught.value)))
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_named_console_stopped_vm_gate_burst_seeds_the_boundary(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed_vm(db)
    _seed_named_console(db)
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)

    assert multi_console.attach_console(db, config, name="c1", interaction=TtyInteractionPolicy.REFUSE) == 0

    assert events == ["status", "start", "tailscale"]
    assert resolve_counter == [["proxmox-token"]]
    assert target.interactive_calls == ["tmux attach -t aw-console-c1"]


def test_named_console_no_tailscale_fails_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    db.insert_vm("box", site="proxmox", hostname="box")  # no tailscale host
    _seed_named_console(db)
    _reachable(monkeypatch, False)

    def _no_status(self: ProxmoxPlatform, row: object) -> VMStatus:
        raise AssertionError("the gate ran for an unattachable VM")

    monkeypatch.setattr(ProxmoxPlatform, "status", _no_status)

    with pytest.raises(StateError, match="no Tailscale address"):
        multi_console.attach_console(db, config, name="c1", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == []


class _FakeRestoreTarget:
    """Transport double for the restore path: the console tmux session
    exists, the session window is present, and the window carries only
    its session pane (no shell panes), so a zero-shell member is
    already intact."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
        self.commands.append(cmd)
        if "list-windows" in cmd:
            return SimpleNamespace(ok=True, returncode=0, stdout="s1\n", stderr="")
        if "list-panes" in cmd:
            # Pane 0 is the session pane; no tagged shell panes.
            return SimpleNamespace(ok=True, returncode=0, stdout="%0|0|\n", stderr="")
        return SimpleNamespace(ok=True, returncode=0, stdout="", stderr="")


def test_restore_session_stopped_vm_drives_the_real_gated_composition(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """restore_session end to end through the real orchestrated helper
    on a stopped VM: the gate's just-in-time token resolve fully seeds
    the union (one backend pass total), the gate starts the VM, and
    the tmux reconciliation (probe, window check, pane check, landing
    focus) runs inside the gate span; the already-intact member is a
    reported no-op."""
    config = make_config()
    _seed_vm(db)
    _seed_named_console(db)
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'box', '/srv/ws1', 'ws-ws1')"
    )
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, socket_path) "
        "VALUES ('s1', 'ws1', 'default', 'admin', '/tmp/s1.sock')"
    )
    db._conn.execute(
        "INSERT INTO console_sessions (console_name, session_name, shells, position) VALUES ('c1', 's1', '[]', 0)"
    )
    db._conn.commit()
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)
    fake = _FakeRestoreTarget()
    monkeypatch.setattr("agentworks.transports.transport", lambda vm, config, **kwargs: fake)

    multi_console.restore_session(
        db, config, console_name="c1", session_name="s1", interaction=TtyInteractionPolicy.REFUSE
    )

    assert events == ["status", "start", "tailscale"]  # the gate ran
    assert resolve_counter == [["proxmox-token"]]
    assert any("has-session -t aw-console-c1" in c for c in fake.commands)
    assert any("list-windows -t aw-console-c1" in c for c in fake.commands)
    # The no-op landing focus still fires (post-restore focus parity).
    assert any("select-pane -t aw-console-c1:s1.0" in c for c in fake.commands)
    assert any("already matches config" in m for m in captured_output.info)
