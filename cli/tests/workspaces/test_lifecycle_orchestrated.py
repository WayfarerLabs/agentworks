"""``workspace reinit`` / ``workspace rehome`` / ``workspace delete`` /
``workspace copy`` through the orchestrated model: the shared derived
graph (the live VM alone; deliberately NO live workspace node, the
workspace has no capability instances and nothing realization-shaped),
the gate-prompt parity carries (all four DO open the activation gate,
at WORKSPACE scope), the pre-gate validation pins (refusals cost zero
prompts, zero resolves, zero gate events), rehome's inherently
post-gate directory checks and confirm (they need SSH), delete's two
paths (its own ``gated_vm_boundary`` composition when standalone; the
caller's bound platform held verbatim on the nested-teardown path; no
boundary at all without a VM row), and copy's sequential two-boundary
composition (one per VM, nested holds; exactly one on the same-VM
path).

Real config, registry, resolver, and backend loop (env-var backend);
the platform's backend power ops, the reachability probe, the admin
SSH transport, and (for copy's pack step) ``subprocess.run`` are the
fakes.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
from agentworks.db import InitStatus, VMStatus
from agentworks.errors import (
    NotFoundError,
    StateError,
    UserAbort,
    ValidationError,
)
from agentworks.vms import manager as vm_manager
from agentworks.workspaces import manager as workspace_manager

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.db import Database
    from tests.conftest import CapturedOutput


@pytest.fixture
def make_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """The shared ``make_config`` shape, plus a tmp ``[paths]`` section
    so delete's ``.code-workspace`` unlink and copy's VS Code stub
    never touch the operator's real directories."""
    from tests.orchestrated_fixtures import PROXMOX_SECTION, write_operator_config

    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")
    paths_section = (
        f'[paths]\nvscode_workspaces = "{tmp_path / "vscode"}"\n'
    )

    def _make(extra: str = ""):  # noqa: ANN202
        return write_operator_config(
            tmp_path, PROXMOX_SECTION + paths_section + extra
        )

    return _make


def _seed(db: Database, *, ws: str = "ws1") -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    # rehome and copy guard on init status pre-gate; the seeded row
    # must be COMPLETE for them (reinit and delete never guarded).
    db.update_vm_init_status("box", InitStatus.COMPLETE)
    _seed_workspace(db, vm_name="box", name=ws)


def _seed_workspace(db: Database, *, vm_name: str, name: str) -> None:
    db.insert_workspace(
        name,
        vm_name=vm_name,
        workspace_path=f"/srv/{name}",
        template="default",
        linux_group=f"ws-{name}",
    )


def _seed_live_session(db: Database, *, name: str, ws: str) -> None:
    """A session row that reads as alive (pid + boot_id + socket), so
    delete's status-aware kill loop probes and kills it."""
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, "
        "socket_path, pid, boot_id) VALUES (?, ?, 'default', 'admin', "
        "?, 4242, 'boot-1')",
        (name, ws, f"/tmp/{name}.sock"),
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
    monkeypatch.setattr(
        ProxmoxPlatform, "start", lambda self, row, ctx: events.append("start")
    )
    monkeypatch.setattr(
        vm_manager, "_ensure_tailscale", lambda *a, **k: events.append("tailscale")
    )


def _no_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_status(self: ProxmoxPlatform, row: object) -> VMStatus:
        raise AssertionError("the gate ran for a command that must fail pre-gate")

    monkeypatch.setattr(ProxmoxPlatform, "status", _no_status)
    _reachable(monkeypatch, False)


class _FakeAdminTarget:
    """Admin transport double: every command is recorded (optionally
    into a shared event log) and answers ok unless a substring matches
    the per-test response map."""

    def __init__(
        self,
        *,
        events: list[str] | None = None,
        failing: tuple[str, ...] = (),
    ) -> None:
        self.commands: list[str] = []
        self.written: list[tuple[str, str]] = []
        self._events = events
        self._failing = failing

    def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
        self.commands.append(cmd)
        if self._events is not None:
            self._events.append(f"run:{cmd}")
        ok = not any(needle in cmd for needle in self._failing)
        return SimpleNamespace(
            ok=ok, returncode=0 if ok else 1, stdout="", stderr=""
        )

    def write_file(self, remote_path: str, content: str, **kwargs: object) -> None:
        self.written.append((remote_path, content))

    def copy_to(self, local_path: object, remote_path: str, **kwargs: object) -> None:
        self.commands.append(f"copy_to:{remote_path}")


@pytest.fixture
def target(monkeypatch: pytest.MonkeyPatch) -> _FakeAdminTarget:
    """One recording admin target behind the canonical transport
    factory (the lifecycle bodies import ``transport``
    function-locally) AND the workspace VM backend's eager module
    import (pre-imported before patching so the module can never
    first-import mid-patch and capture the fake as its original)."""
    import agentworks.workspaces.backends.vm  # noqa: F401

    fake = _FakeAdminTarget()
    factory = lambda vm, config, **kwargs: fake  # noqa: E731
    monkeypatch.setattr("agentworks.transports.transport", factory)
    monkeypatch.setattr("agentworks.workspaces.backends.vm.transport", factory)
    return fake


# -- the derived graph (stated once for the lifecycle ops) --------------------


def test_graph_is_the_live_vm_alone_no_workspace_node(
    db: Database, make_config  # noqa: ANN001
) -> None:
    """reinit / rehome / delete / copy share one graph per VM: the live
    VM from its row (vm-site + vm), union = the site's config secret
    only. Deliberately NO live workspace node: the workspace here has
    no capability instances, no secret refs, no readiness, and nothing
    realization-shaped (delete unwinds nothing, reinit converges,
    rehome / copy mutate through the VM transport), so introducing one
    would be over-orchestration."""
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node

    config = make_config()
    _seed(db)
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)
    resolver = Resolver(config, registry)

    nodes = walk(live_vm_node(db, config, registry, vm, resolver))

    assert [n.key for n in nodes] == ["vm-site/proxmox", "vm/box"]
    assert secret_union(nodes) == ("proxmox-token",)

    resolver.resolve()
    assert set(resolver.values) == {"proxmox-token"}


# -- gate-prompt parity (the per-command carries) -----------------------------


def test_reinit_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)

    workspace_manager.reinit_workspace(db, config, "ws1")

    assert resolve_counter == [["proxmox-token"]]
    assert any("chmod 2770 /srv/ws1" in c for c in target.commands)
    assert "Reinitializing workspace 'ws1' on VM 'box'..." in captured_output.info


def test_reinit_stopped_vm_gate_burst_seeds_the_whole_union(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """No env targets, so the gate's just-in-time resolve covers the
    entire union: one burst, nothing twice, nothing after."""
    config = make_config()
    _seed(db)
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)

    workspace_manager.reinit_workspace(db, config, "ws1")

    assert events == ["status", "start", "tailscale"]  # the gate ran
    assert resolve_counter == [["proxmox-token"]]
    assert any("chmod 2770 /srv/ws1" in c for c in target.commands)


def test_delete_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)

    workspace_manager.delete_workspace(db, config, "ws1", yes=True)

    assert resolve_counter == [["proxmox-token"]]
    assert any("rm -rf /srv/ws1" in c for c in target.commands)
    assert db.get_workspace("ws1") is None
    assert "Workspace 'ws1' deleted" in captured_output.info


def test_delete_stopped_vm_gate_burst_seeds_the_whole_union(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed(db)
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)

    workspace_manager.delete_workspace(db, config, "ws1", yes=True)

    assert events == ["status", "start", "tailscale"]
    assert resolve_counter == [["proxmox-token"]]
    assert db.get_workspace("ws1") is None


# -- the operation scope reaches readiness ------------------------------------


def test_workspace_scope_reaches_node_readiness(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.capabilities.base import ScopeLevel

    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)
    scopes: list[OperationScope | None] = []
    real = ProxmoxPlatform.preflight

    def _recording(self: ProxmoxPlatform, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _recording)

    workspace_manager.reinit_workspace(db, config, "ws1")

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.WORKSPACE
    assert scope.vm == "box" and scope.workspace == "ws1"
    assert scope.agent is None and scope.session is None


# -- validation stays pre-gate ------------------------------------------------


def test_delete_sessions_guard_refuses_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed(db)
    _seed_live_session(db, name="s1", ws="ws1")
    _no_gate(monkeypatch)

    with pytest.raises(StateError, match="has 1 session"):
        workspace_manager.delete_workspace(db, config, "ws1")

    assert resolve_counter == []
    assert target.commands == []
    assert db.get_workspace("ws1") is not None


def test_delete_declined_confirm_aborts_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    config = make_config()
    _seed(db)
    _no_gate(monkeypatch)
    captured_output.confirm_response = False

    with pytest.raises(UserAbort, match="delete cancelled"):
        workspace_manager.delete_workspace(db, config, "ws1")

    assert resolve_counter == []
    assert target.commands == []
    assert db.get_workspace("ws1") is not None


def test_rehome_overlapping_paths_fail_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed(db)
    _no_gate(monkeypatch)

    with pytest.raises(ValidationError, match="paths overlap"):
        workspace_manager.rehome_workspace(
            db, config, "ws1", target_path="/srv/ws1/nested"
        )

    assert resolve_counter == []
    assert target.commands == []


def test_reinit_unknown_workspace_fails_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed(db)
    _no_gate(monkeypatch)

    with pytest.raises(NotFoundError, match="workspace 'ghost' not found"):
        workspace_manager.reinit_workspace(db, config, "ghost")

    assert resolve_counter == []
    assert target.commands == []


# -- delete's two paths -------------------------------------------------------


def test_delete_nested_platform_path_reuses_the_callers_composition(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The nested-teardown seam: a handed-in bound platform means the
    caller's composition already resolved and holds its gate open, so
    delete performs ZERO additional resolves and composes no second
    boundary (a status probe would be one); only the hold is
    re-entered."""

    class _BoundPlatformStub:
        def __init__(self) -> None:
            self.holds = 0

        def vm_active(
            self, row: object, *, config: object | None = None
        ) -> contextlib.AbstractContextManager[None]:
            self.holds += 1
            return contextlib.nullcontext()

        def status(self, row: object, ctx: object) -> VMStatus:
            raise AssertionError("nested delete must not probe status")

    config = make_config()
    _seed(db)
    _no_gate(monkeypatch)  # any gate composition would probe status and fail
    _reachable(monkeypatch, True)  # keep_active's fast path
    bound = _BoundPlatformStub()

    workspace_manager.delete_workspace(
        db,
        config,
        "ws1",
        force=True,
        yes=True,
        platform=bound,  # type: ignore[arg-type]
        platform_ctx=RunContext(),
    )

    assert resolve_counter == []  # nothing resolved beyond the caller's pass
    assert bound.holds == 1  # the hold was re-entered, nothing else
    assert db.get_workspace("ws1") is None
    assert any("rm -rf /srv/ws1" in c for c in target.commands)


def test_delete_without_vm_row_is_db_only_with_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The imperative special case, preserved: a workspace whose VM row
    is gone (drift) deletes DB-side only, with no boundary, no gate,
    and no SSH."""
    config = make_config()
    _seed(db)
    # Fabricate the drift the special case defends against: drop the VM
    # row out from under the workspace (FKs off for the surgery only).
    db._conn.execute("PRAGMA foreign_keys = OFF")
    db._conn.execute("DELETE FROM vms WHERE name = 'box'")
    db._conn.execute("PRAGMA foreign_keys = ON")
    db._conn.commit()
    _no_gate(monkeypatch)

    workspace_manager.delete_workspace(db, config, "ws1", yes=True)

    assert resolve_counter == []
    assert target.commands == []
    assert db.get_workspace("ws1") is None


# -- rehome: the confirm is inherently post-gate ------------------------------


def test_rehome_confirm_sits_inside_the_span_after_the_dir_checks(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The order pin, which doubles as rehome's gate-prompt parity
    carry: on a stopped VM the gate's just-in-time token resolve is
    the ONLY backend pass (the boundary is fully seeded), then the
    gate events, then the SSH directory existence checks, then the
    confirm (which renders their results). A declined confirm raises
    UserAbort and leaves the DB path unchanged; by then the gate has
    already run, because the checks the prompt reports on need SSH
    (inherently post-gate)."""
    from agentworks import output as output_mod

    config = make_config()
    _seed(db)
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)
    fake = _FakeAdminTarget(events=events, failing=("test -d /dst/ws1",))
    monkeypatch.setattr(
        "agentworks.transports.transport", lambda vm, config_, **kwargs: fake
    )

    def _decline(message: str, default: bool = False) -> bool:
        events.append("confirm")
        return False

    monkeypatch.setattr(output_mod, "confirm", _decline)

    with pytest.raises(UserAbort, match="rehome cancelled"):
        workspace_manager.rehome_workspace(
            db, config, "ws1", target_path="/dst/ws1"
        )

    assert events == [
        "status",
        "start",
        "tailscale",
        "run:test -d /srv/ws1",
        "run:test -d /dst/ws1",
        "confirm",
    ]
    assert resolve_counter == [["proxmox-token"]]
    ws = db.get_workspace("ws1")
    assert ws is not None and ws.workspace_path == "/srv/ws1"


# -- copy: the sequential two-boundary composition ----------------------------


def _wire_copy_fakes(
    monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> _FakeAdminTarget:
    """The copy command's fakes: a transport double that IS an
    SSHTransport (the pack step asserts the concrete type to read the
    raw ssh argv off it), a recording ``subprocess.run`` for the tar
    pipe, and hold-span recording on the platform's ``vm_active``."""
    import subprocess as subprocess_mod

    from agentworks.transports import SSHTransport

    # The fake FIRST in the MRO so its recording run / write_file /
    # copy_to win; SSHTransport supplies the concrete type (and the
    # host / user / identity_file attributes the pack step reads).
    class _FakeSSHTarget(_FakeAdminTarget, SSHTransport):  # type: ignore[misc]  # the fake's recording run deliberately shadows the real signature
        def __init__(self) -> None:
            SSHTransport.__init__(self, "100.64.0.9", user="admin")
            _FakeAdminTarget.__init__(self, events=events)

    fake = _FakeSSHTarget()
    monkeypatch.setattr(
        "agentworks.transports.transport", lambda vm, config, **kwargs: fake
    )

    def _fake_pack(args: object, **kwargs: object) -> SimpleNamespace:
        events.append("pack")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(subprocess_mod, "run", _fake_pack)

    real_vm_active = ProxmoxPlatform.vm_active

    @contextlib.contextmanager
    def _recording_hold(self: ProxmoxPlatform, row, *, config=None):  # noqa: ANN001, ANN202
        events.append(f"hold-enter:{row.name}")
        with real_vm_active(self, row, config=config):
            yield
        events.append(f"hold-exit:{row.name}")

    monkeypatch.setattr(ProxmoxPlatform, "vm_active", _recording_hold)
    return fake


def test_copy_cross_vm_runs_two_sequential_boundaries_with_nested_holds(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Cross-VM copy: TWO boundary bursts in order (source first, then
    dest; one shared site config here, so both bursts name the site's
    secret), the dest boundary only entered after the pack (the dest
    VM is resolved mid-command, as at HEAD), and BOTH holds open
    concurrently (the dest span nests inside the source span)."""
    config = make_config()
    _seed(db)
    db.insert_vm("box2", site="proxmox", hostname="box2")
    db.update_vm_tailscale("box2", "100.64.0.10")
    db.update_vm_init_status("box2", InitStatus.COMPLETE)
    _reachable(monkeypatch, True)
    events: list[str] = []
    _wire_copy_fakes(monkeypatch, events)

    workspace_manager.copy_workspace(
        db, config, "ws1", dest_name="ws2", vm_name="box2"
    )

    # Two sequential compositions, one boundary resolve each.
    assert resolve_counter == [["proxmox-token"], ["proxmox-token"]]
    # Source held before the pack; dest boundary only after it; the
    # dest hold exits before the source hold (nested spans, both open
    # across the unpack).
    assert events.index("hold-enter:box") < events.index("pack")
    assert events.index("pack") < events.index("hold-enter:box2")
    assert events.index("hold-exit:box2") < events.index("hold-exit:box")
    row = db.get_workspace("ws2")
    assert row is not None and row.vm_name == "box2" and row.template == "copied"
    assert any("tar xzf" in e for e in events if e.startswith("run:"))
    assert "Workspace 'ws1' copied to 'ws2'" in captured_output.info


def test_copy_same_vm_reuses_the_source_composition(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Same-VM copy: exactly ONE boundary (no second resolve, no
    second hold); the source composition already gates and holds the
    one VM."""
    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)
    events: list[str] = []
    _wire_copy_fakes(monkeypatch, events)

    workspace_manager.copy_workspace(
        db, config, "ws1", dest_name="ws2", vm_name="box"
    )

    assert resolve_counter == [["proxmox-token"]]
    assert events.count("hold-enter:box") == 1
    row = db.get_workspace("ws2")
    assert row is not None and row.vm_name == "box"
    assert "Workspace 'ws1' copied to 'ws2'" in captured_output.info


def test_copy_refusals_fail_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copy's cheap row refusals stay pre-everything: an unknown
    source workspace and an already-existing destination both fail
    before any prompt, resolve, or gate event."""
    from agentworks.errors import AlreadyExistsError

    config = make_config()
    _seed(db)
    _no_gate(monkeypatch)

    with pytest.raises(NotFoundError, match="workspace 'nope' not found"):
        workspace_manager.copy_workspace(
            db, config, "nope", dest_name="ws2", vm_name="box"
        )

    _seed_workspace(db, vm_name="box", name="ws2")
    with pytest.raises(AlreadyExistsError, match="workspace 'ws2' already exists"):
        workspace_manager.copy_workspace(
            db, config, "ws1", dest_name="ws2", vm_name="box"
        )

    assert resolve_counter == []
