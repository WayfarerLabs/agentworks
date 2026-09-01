"""The sessions machinery through the orchestrated node model: the
batch ops' coalesced composition (``_batch_vm_boundary`` under
``stop_all_sessions`` / ``start_all_sessions`` / ``restart_all_sessions`` / ``list_sessions``'
status pass) and the singular ops' ``_prepare_vm`` gate span.

Batch pins: the multi-root walk with ONE shared site-node object per
site (the ``live_vm_node`` ``site_nodes`` memo), boundary-then-gates
order with ZERO timing shift from the imperative batch, exactly one
backend burst even for two stopped VMs sharing a site (the gates serve
the boundary's cached values), the empty-set complete no-op, and the
operator-stopped abort propagating exactly as the imperative per-VM
gate loop did.

Singular pins: per-command one-burst parity, the gate-seeded
stopped-VM shape, describe's hold SUPERSET (it held nothing at HEAD),
the pre-gate refusals with zero resolves and zero gate events
(including the hoisted no-Tailscale row guard), and the SESSION-level
scope reaching node readiness.

Real config, registry, resolver, and backend loop (env-var backend);
the platform's backend power ops, the reachability probe, and the SSH
transports are the fakes.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from agentworks.db import PID_STOPPED, SessionMode, VMStatus
from agentworks.errors import NotFoundError, StateError
from agentworks.output import Role
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager as session_manager
from agentworks.sessions.manager._queries import session_listing_data
from agentworks.vms import manager as vm_manager
from tests.conftest import stub_vm_ssh_identity

if TYPE_CHECKING:
    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.db import Database


@pytest.fixture(autouse=True)
def _stub_ssh_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_vm_ssh_identity(monkeypatch)


def _seed_vm(db: Database, name: str, host: str | None, *, site: str = "proxmox") -> None:
    db.insert_vm(name, site=site, hostname=name)
    if host is not None:
        db.update_vm_tailscale(name, host)
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) VALUES (?, ?, ?, ?)",
        (f"ws-{name}", name, f"/srv/ws-{name}", f"ws-ws-{name}"),
    )
    db._conn.commit()


def _seed_session(db: Database, name: str, ws: str, *, agent: str | None = None) -> None:
    db.insert_session(
        name,
        ws,
        "default",
        SessionMode.AGENT if agent else SessionMode.ADMIN,
        agent_name=agent,
        socket_path=f"/tmp/{name}.sock",
    )
    db.update_session_runtime(
        name, socket_path=f"/tmp/{name}.sock", pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None
    )


def _reachable(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: value)


def _stop_the_vms(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    """Backend fakes for the stopped-VM gate path, recording per-VM."""
    _reachable(monkeypatch, False)
    monkeypatch.setattr(vm_manager, "_tailscale_rejoin_required", lambda *a, **k: True)
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, row, ctx: events.append(f"status:{row.name}") or VMStatus.STOPPED,
    )
    monkeypatch.setattr(
        ProxmoxPlatform,
        "start",
        lambda self, row, ctx: events.append(f"start:{row.name}"),
    )
    monkeypatch.setattr(
        vm_manager,
        "_ensure_tailscale",
        lambda db, config, vm, platform, ctx, **k: events.append(f"tailscale:{vm.name}"),
    )


def _record_holds(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    @contextlib.contextmanager
    def _recording_hold(self: ProxmoxPlatform, row: object, *, config: object = None):  # noqa: ANN202
        events.append(f"hold-open:{row.name}")  # type: ignore[attr-defined]
        try:
            yield
        finally:
            events.append(f"hold-close:{row.name}")  # type: ignore[attr-defined]

    monkeypatch.setattr(ProxmoxPlatform, "vm_active", _recording_hold)


def test_strict_batch_refuses_identity_before_any_activation(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_vm(db, "vm-a", "100.64.0.11")
    vm = db.get_vm("vm-a")
    assert vm is not None

    def refuse(*args: object, **kwargs: object) -> None:
        raise StateError("SSH identity drift")

    monkeypatch.setattr(vm_manager, "require_vm_ssh_boundary", refuse)
    monkeypatch.setattr(
        "agentworks.orchestration.activation.activation_gate",
        lambda *args, **kwargs: pytest.fail("activation started before SSH identity refusal"),
    )

    with (
        pytest.raises(StateError),
        session_manager._batch_vm_boundary(
            db,
            SimpleNamespace(),
            [vm],
            interaction=TtyInteractionPolicy.REFUSE,
        ),
    ):
        pass


def test_best_effort_batch_activates_only_usable_ssh_identities(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_vm(db, "vm-a", "100.64.0.11")
    _seed_vm(db, "vm-b", "100.64.0.12")
    vms = [vm for name in ("vm-a", "vm-b") if (vm := db.get_vm(name)) is not None]
    activated: list[str] = []

    def require(db: object, config: object, vm: object) -> None:
        if vm.name == "vm-b":  # type: ignore[attr-defined]
            raise StateError("SSH identity drift")

    @contextlib.contextmanager
    def fake_batch(
        db: object,
        config: object,
        candidates: list[object],
        *,
        interaction: TtyInteractionPolicy,
    ):  # noqa: ANN202
        activated.extend(candidate.name for candidate in candidates)  # type: ignore[attr-defined]
        yield

    monkeypatch.setattr(vm_manager, "require_vm_ssh_boundary", require)
    monkeypatch.setattr("agentworks.sessions.manager._scope._batch_vm_boundary_impl", fake_batch)

    with session_manager._best_effort_batch_vm_boundary(
        db,
        SimpleNamespace(),
        vms,
        interaction=TtyInteractionPolicy.REFUSE,
    ) as usable:
        assert usable == frozenset({"vm-a"})

    assert activated == ["vm-a"]


def test_best_effort_status_and_pid_repair_skip_identity_refusal_before_transport(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_vm(db, "vm-a", "100.64.0.11")
    db.insert_session("s-a", "ws-vm-a", "default", SessionMode.ADMIN, socket_path="/tmp/s-a.sock")
    session = db.get_session("s-a")
    assert session is not None

    def refuse(*args: object, **kwargs: object) -> None:
        raise StateError("SSH identity drift")

    monkeypatch.setattr(vm_manager, "require_vm_ssh_boundary", refuse)
    monkeypatch.setattr(
        session_manager,
        "transport",
        lambda *args, **kwargs: pytest.fail("transport constructed after identity refusal"),
    )

    assert session_manager.batch_check_all_sessions([session], db=db, config=SimpleNamespace()) == {}
    assert session_manager.ensure_pids_batch([session], db=db, config=SimpleNamespace()) == [session]


def test_list_status_reports_identity_refusal_as_unknown_without_transport(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    _seed_vm(db, "vm-a", "100.64.0.11")
    db.insert_session("s-a", "ws-vm-a", "default", SessionMode.ADMIN, socket_path="/tmp/s-a.sock")
    db.update_session_runtime(
        "s-a", socket_path="/tmp/s-a.sock", pid=4321, boot_id="boot-a", tmux_server_start_ticks=77
    )

    def refuse(*args: object, **kwargs: object) -> None:
        raise StateError("SSH identity drift")

    monkeypatch.setattr(vm_manager, "require_vm_ssh_boundary", refuse)
    monkeypatch.setattr(
        session_manager,
        "transport",
        lambda *args, **kwargs: pytest.fail("transport constructed after identity refusal"),
    )

    listing = session_manager.session_listing(
        db,
        make_config(),
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert listing.sessions[0].status == "unknown"
    json_rows = cast("list[dict[str, object]]", session_listing_data(listing)["sessions"])
    assert json_rows[0]["status"] == "unknown"

    session_manager.render_session_listing(listing)
    assert sum(role is Role.WARNING for role, _level, _message in captured_output.lines) == 1


class _FakeTarget:
    """Transport double for the probe / kill / regenerate helpers."""

    def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(ok=True, returncode=0, stdout="", stderr="")

    def interactive(self, cmd: str, **kwargs: object) -> int:
        return 0

    def write_file(self, path: str, content: str, **kwargs: object) -> None:
        return None


@pytest.fixture
def target(monkeypatch: pytest.MonkeyPatch) -> _FakeTarget:
    fake = _FakeTarget()
    factory = lambda vm, config, **kwargs: fake  # noqa: E731
    monkeypatch.setattr("agentworks.transports.transport", factory)
    # sessions.manager imports ``transport`` eagerly at module load.
    monkeypatch.setattr(session_manager, "transport", factory)
    return fake


# -- the batch graph (the shared site-node memo) ------------------------------


def test_batch_graph_two_vms_share_one_site_node(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    """Two live VM nodes on one site MUST share one ``VMSiteNode``
    object (the walk raises otherwise); the ``site_nodes`` memo is the
    sharing mechanism, and the batch union is the site's config secret
    once."""
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.vms.nodes import VMSiteNode, live_vm_node

    config = make_config()
    _seed_vm(db, "vm-a", "100.64.0.11")
    _seed_vm(db, "vm-b", "100.64.0.12")
    vm_a, vm_b = db.get_vm("vm-a"), db.get_vm("vm-b")
    assert vm_a is not None and vm_b is not None
    registry = build_registry(config)

    site_nodes: dict[str, VMSiteNode] = {}
    node_a = live_vm_node(db, config, registry, vm_a, site_nodes=site_nodes)
    node_b = live_vm_node(db, config, registry, vm_b, site_nodes=site_nodes)
    nodes = walk(node_a, node_b)

    assert [n.key for n in nodes] == ["vm-site/proxmox", "vm/vm-a", "vm/vm-b"]
    assert nodes[0] is node_a.deps()[0]
    assert nodes[0] is node_b.deps()[0]
    assert secret_union(nodes) == ("proxmox-token",)


# -- the batch composition ----------------------------------------------------


def test_stop_all_reachable_vms_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """A batch op over two reachable VMs = ONE boundary resolve for the
    whole batch (the coalesced parity of the imperative batch bind),
    nothing after."""
    config = make_config()
    _seed_vm(db, "vm-a", "100.64.0.11")
    _seed_vm(db, "vm-b", "100.64.0.12")
    _seed_session(db, "s-a", "ws-vm-a")
    _seed_session(db, "s-b", "ws-vm-b")
    _reachable(monkeypatch, True)

    session_manager.stop_all_sessions(db, config, interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"]]
    assert any("No running sessions to stop" in m for m in captured_output.info)


def test_stop_all_two_stopped_vms_one_site_one_backend_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The coalesced-is-parity headline: two STOPPED VMs sharing one
    site still cost exactly ONE backend burst (the boundary's; the
    gates SERVE its cached values rather than re-resolving), both
    gates fire in VM order, both holds open across the body and close
    in reverse."""
    config = make_config()
    _seed_vm(db, "vm-a", "100.64.0.11")
    _seed_vm(db, "vm-b", "100.64.0.12")
    _seed_session(db, "s-a", "ws-vm-a")
    _seed_session(db, "s-b", "ws-vm-b")
    events: list[str] = []
    _stop_the_vms(monkeypatch, events)
    _record_holds(monkeypatch, events)

    session_manager.stop_all_sessions(db, config, interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"]]
    assert events == [
        # gate vm-a: observe, start, rejoin (inside auto_start's own
        # hold), then the gate's held-active span opens
        "status:vm-a",
        "start:vm-a",
        "hold-open:vm-a",
        "tailscale:vm-a",
        "hold-close:vm-a",
        "hold-open:vm-a",
        # gate vm-b, same shape
        "status:vm-b",
        "start:vm-b",
        "hold-open:vm-b",
        "tailscale:vm-b",
        "hold-close:vm-b",
        "hold-open:vm-b",
        # the body ran inside both holds; the stack unwinds in reverse
        "hold-close:vm-b",
        "hold-close:vm-a",
    ]


def test_batch_empty_vm_set_is_a_complete_noop(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """An empty VM set costs zero registry builds, zero resolver work,
    and zero gate activity (the imperative lazy-bind property)."""
    import agentworks.bootstrap as bootstrap

    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("build_registry must not run for an empty VM set")

    monkeypatch.setattr(bootstrap, "build_registry", _boom)

    session_manager.stop_all_sessions(db, make_config(), interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == []
    assert any("No running sessions to stop" in m for m in captured_output.info)


def test_batch_operator_stopped_vm_aborts_before_the_probes(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Per-VM error semantics are the imperative gate loop's, verbatim:
    an operator-stopped VM's gate REFUSES with the typed error, which
    propagates and aborts the whole batch before any SSH probe runs."""
    config = make_config()
    _seed_vm(db, "vm-a", "100.64.0.11")
    _seed_session(db, "s-a", "ws-vm-a")
    db.set_operator_stopped("vm-a", True)
    _reachable(monkeypatch, False)
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, row, ctx: VMStatus.STOPPED)

    def _no_transport(*a: object, **k: object) -> object:
        raise AssertionError("SSH probes must not run after a refused gate")

    monkeypatch.setattr(session_manager, "transport", _no_transport)

    with pytest.raises(StateError, match="manually stopped"):
        session_manager.stop_all_sessions(db, config, interaction=TtyInteractionPolicy.REFUSE)


_SECOND_SITE_DOC = """\
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: proxmox-b
  description: Second proxmox site with its own token secret
spec:
  platform:
    name: proxmox
    api_url: "https://pve-b:8006"
    node: pve1
    token_id: "agw@pam!agw"
    template_vmid: 9000
    token_secret: proxmox-token-b
"""


def test_stop_all_mixed_site_batch_resolves_the_union_once(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
    tmp_path,  # noqa: ANN001
) -> None:
    """A mixed-site batch still resolves ONCE: the union of both sites'
    declared secrets goes through a single boundary pass (the relocated
    cross-site pin the deleted bind_platforms union test carried)."""
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "proxmox-b.yaml").write_text(_SECOND_SITE_DOC)
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN_B", "pve-token-b")
    config = make_config()
    _seed_vm(db, "vm-a", "100.64.0.11")
    _seed_vm(db, "vm-b", "100.64.0.12", site="proxmox-b")
    _seed_session(db, "s-a", "ws-vm-a")
    _seed_session(db, "s-b", "ws-vm-b")
    _reachable(monkeypatch, True)

    session_manager.stop_all_sessions(db, config, interaction=TtyInteractionPolicy.REFUSE)

    assert len(resolve_counter) == 1
    assert sorted(resolve_counter[0]) == ["proxmox-token", "proxmox-token-b"]
    assert any("No running sessions to stop" in m for m in captured_output.info)


def test_stop_all_two_sessions_one_vm_composes_one_node_one_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The everyday shape: two sessions on ONE VM dedupe to one node
    (a single walk entry for the VM), one gate sequence, one hold (the
    relocated by-VM dedup pin the deleted bind_platforms test
    carried)."""
    from agentworks.orchestration import walk as walk_mod

    config = make_config()
    _seed_vm(db, "box", "100.64.0.9")
    _seed_session(db, "s1", "ws-box")
    _seed_session(db, "s2", "ws-box")
    events: list[str] = []
    _stop_the_vms(monkeypatch, events)
    _record_holds(monkeypatch, events)

    real_walk = walk_mod.walk
    walks: list[list[str]] = []

    def _spy(*roots):  # noqa: ANN002, ANN202
        nodes = real_walk(*roots)
        walks.append([n.key for n in nodes])
        return nodes

    monkeypatch.setattr(walk_mod, "walk", _spy)

    session_manager.stop_all_sessions(db, config, interaction=TtyInteractionPolicy.REFUSE)

    assert walks == [["vm-site/proxmox", "vm/box"]]
    assert events == [
        "status:box",
        "start:box",
        "hold-open:box",
        "tailscale:box",
        "hold-close:box",
        "hold-open:box",
        "hold-close:box",
    ]
    assert resolve_counter == [["proxmox-token"]]


def test_batch_repair_path_resolves_the_rejoin_key_late(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The late-resolve branch of the batch gate callback, for real: a
    started VM fails to reconnect and the repair path reads the
    template's rejoin auth key through the gate reader, which resolves
    it LATE through the source chain (the boundary burst, then exactly
    one repair burst) with no seed error; the heal the imperative
    repair carried survives the batch composition."""
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "tskey-late")
    config = make_config()
    _seed_vm(db, "box", "100.64.0.9")
    _seed_session(db, "s1", "ws-box")
    _reachable(monkeypatch, False)
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, row, ctx: VMStatus.STOPPED)
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, row, ctx: None)
    keys: list[str] = []

    def _failing_reconnect(
        db_: object,
        config_: object,
        vm: object,
        platform: object,
        ctx: object,
        *,
        auth_keys: Any,
        auth_key_name: str,
    ) -> None:
        # The started VM fails to reconnect: the real repair reads the
        # rejoin key through the caller-supplied source at exactly this
        # point (see _ensure_tailscale), so the fake reads it the same
        # way and lets the gate reader's lazy branch run for real.
        keys.append(auth_keys.get(auth_key_name))

    monkeypatch.setattr(vm_manager, "_tailscale_rejoin_required", lambda *a, **k: True)
    monkeypatch.setattr(vm_manager, "_ensure_tailscale", _failing_reconnect)

    session_manager.stop_all_sessions(db, config, interaction=TtyInteractionPolicy.REFUSE)

    assert keys == ["tskey-late"]
    assert resolve_counter == [["proxmox-token"], ["tailscale-auth-key"]]


def test_batch_gate_refuses_an_undeclared_outside_union_name(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard on the late-resolve branch: a gate target that asks
    for a name outside the boundary union which it did NOT declare in
    repair_secret_refs is refused with the declare/receive error and
    nothing late-resolves. Driven through the real batch composition
    with a node-level gate_secret_refs patch (the callback is a
    closure of the composition root, so a synthetic handle would test
    a copy, not the real thing)."""
    from agentworks.vms.nodes import LiveVMNode

    config = make_config()
    _seed_vm(db, "box", "100.64.0.9")
    _seed_session(db, "s1", "ws-box")
    _reachable(monkeypatch, False)
    monkeypatch.setattr(LiveVMNode, "gate_secret_refs", lambda self: ("rogue-secret",))

    def _no_status(self: ProxmoxPlatform, row: object) -> VMStatus:
        raise AssertionError("observe must not run after the refused resolve")

    monkeypatch.setattr(ProxmoxPlatform, "status", _no_status)

    with pytest.raises(StateError, match="repair_secret_refs"):
        session_manager.stop_all_sessions(db, config, interaction=TtyInteractionPolicy.REFUSE)

    # The boundary burst only; the rogue name never resolved.
    assert resolve_counter == [["proxmox-token"]]


# -- the singular ops (_prepare_vm as a gate span) ----------------------------


def _seed_singular(db: Database, *, agent: str | None = None) -> None:
    _seed_vm(db, "box", "100.64.0.9")
    if agent is not None:
        db.insert_agent(agent, "box", f"aw-{agent}")
    _seed_session(db, "s1", "ws-box", agent=agent)


def test_stop_session_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed_singular(db)
    _reachable(monkeypatch, True)

    session_manager.stop_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"]]
    assert any("already stopped" in m for m in captured_output.info)


def test_stop_session_stopped_vm_gate_burst_seeds_the_boundary(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The gate opens before the boundary (the sanctioned pre-walk-away
    shift every gated seam carries): its just-in-time token resolve
    fully seeds the union, so one backend pass total, nothing twice."""
    config = make_config()
    _seed_singular(db)
    events: list[str] = []
    _stop_the_vms(monkeypatch, events)

    session_manager.stop_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)

    assert events == ["status:box", "start:box", "tailscale:box"]
    assert resolve_counter == [["proxmox-token"]]


def test_single_stop_ends_on_a_result_terminal_without_duplication(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """A single OK-status stop ends on a column-0 result() (parity with the
    force-stop sibling), and the shared _execute_stop helper's per-session
    'stopped' body line is suppressed so the terminal is not doubled."""
    from agentworks.db import SessionStatus
    from agentworks.output import Role

    config = make_config()
    _seed_singular(db)
    _reachable(monkeypatch, True)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    monkeypatch.setattr(session_manager, "_ensure_pid", lambda session, *, target, db: session)
    monkeypatch.setattr(
        session_manager,
        "check_session_status",
        lambda session, *, target: SessionStatus.OK,
    )
    monkeypatch.setattr(
        session_manager,
        "_build_session_target",
        lambda session, *, vm, config, db, admin_target: admin_target,
    )
    monkeypatch.setattr(
        session_manager,
        "batch_check_status",
        lambda sessions, *, target: {s.name: SessionStatus.OK for s in sessions},
    )
    monkeypatch.setattr("agentworks.sessions.manager._lifecycle._teardown_session", lambda *a, **k: None)

    session_manager.stop_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)

    # The terminal is exactly one RESULT line, not doubled by the helper's
    # per-session body line (announce_stopped=False suppresses it).
    stopped = [(role, msg) for role, _lvl, msg in captured_output.lines if msg == "Session 's1' stopped"]
    assert stopped == [(Role.RESULT, "Session 's1' stopped")]


def test_delete_session_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed_singular(db)
    _reachable(monkeypatch, True)

    session_manager.delete_session(db, config, name="s1", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"]]
    assert db.get_session("s1") is None


def test_attach_session_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed_singular(db)
    _reachable(monkeypatch, True)

    with pytest.raises(StateError):
        session_manager.attach_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"]]


def test_session_logs_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed_singular(db)
    _reachable(monkeypatch, True)

    with pytest.raises(StateError):
        session_manager.session_logs(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"]]


def test_residual_session_refuses_attach_without_interactive_entry(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.db import SessionStatus

    _seed_singular(db)
    _reachable(monkeypatch, True)
    session = db.get_session("s1")
    assert session is not None
    monkeypatch.setattr(session_manager, "_ensure_pid", lambda *args, **kwargs: session)
    monkeypatch.setattr(session_manager, "check_session_status", lambda *args, **kwargs: SessionStatus.RESIDUAL)
    monkeypatch.setattr(
        target,
        "interactive",
        lambda *args, **kwargs: pytest.fail("residual runtime must not be attached"),
    )

    with pytest.raises(StateError):
        session_manager.attach_session(
            db,
            make_config(),
            name="s1",
            interaction=TtyInteractionPolicy.REFUSE,
        )


def test_residual_session_refuses_logs_without_capture(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.db import SessionStatus

    _seed_singular(db)
    _reachable(monkeypatch, True)
    session = db.get_session("s1")
    assert session is not None
    monkeypatch.setattr(session_manager, "_ensure_pid", lambda *args, **kwargs: session)
    monkeypatch.setattr(session_manager, "check_session_status", lambda *args, **kwargs: SessionStatus.RESIDUAL)
    monkeypatch.setattr(
        "agentworks.sessions.tmux.capture_output",
        lambda *args, **kwargs: pytest.fail("residual runtime has no canonical pane to capture"),
    )

    with pytest.raises(StateError):
        session_manager.session_logs(
            db,
            make_config(),
            name="s1",
            interaction=TtyInteractionPolicy.REFUSE,
        )


def test_session_logs_writes_exact_utf8_bytes_on_a_legacy_console(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session scrollback is the raw data pipe ``_logs.py``'s module
    docstring describes, not a structured message: it can carry arbitrary
    Unicode captured verbatim from a workload. The CLI entrypoint's
    legacy-console policy degrades unencodable output to a literal ``?``
    for human display; routing the raw capture through that same
    text-mode stream would silently alias a genuine non-ASCII character
    onto the same byte as a literal ``?``, corrupting a redirected
    capture. The raw boundary instead writes exact UTF-8 bytes no matter
    what codepage the console reports."""
    import io
    import sys

    from agentworks.db import SessionStatus

    config = make_config()
    _seed_singular(db)
    _reachable(monkeypatch, True)
    monkeypatch.setattr(session_manager, "_ensure_pid", lambda session, *, target, db: session)
    monkeypatch.setattr(
        session_manager,
        "check_session_status",
        lambda session, *, target: SessionStatus.OK,
    )
    payload = "result=雪\n"
    monkeypatch.setattr("agentworks.sessions.tmux.capture_output", lambda *a, **k: payload)

    # A Windows-style legacy console: cp1252, strict, CRLF (matches the
    # stand-in ``cli/tests/test_console_streams.py`` uses for the same
    # field shape). ``errors="strict"`` makes any accidental fall-through
    # to the text path raise instead of silently degrading the assertion.
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="\r\n")
    monkeypatch.setattr(sys, "stdout", console)

    session_manager.session_logs(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)
    console.flush()

    assert raw.getvalue() == payload.encode("utf-8")
    # Through a text-mode legacy console, the genuine character and a
    # literal "?" would both degrade to this same byte string. The raw
    # boundary keeps them distinguishable.
    assert raw.getvalue() != b"result=?\n"


def test_session_logs_writes_the_whole_capture_through_a_short_writing_pipe(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under ``python -u`` stdout's buffer is a raw ``FileIO`` whose
    ``write`` can accept only part of a large capture against a
    non-blocking pipe and report the short count instead of raising.
    The raw boundary must loop until every byte is out (the
    ``write_all`` contract shared with the machine-output writer), or a
    redirected ``session logs`` silently truncates at exit code 0."""
    import sys

    from agentworks.db import SessionStatus

    class _ShortWritePipe:
        """A stdout stand-in whose buffer accepts a few bytes per call."""

        def __init__(self) -> None:
            self.raw = bytearray()

        @property
        def buffer(self):  # noqa: ANN202
            return self

        def write(self, data: bytes) -> int:
            accepted = min(7, len(data))
            self.raw.extend(data[:accepted])
            return accepted

        def flush(self) -> None:
            pass

    config = make_config()
    _seed_singular(db)
    _reachable(monkeypatch, True)
    monkeypatch.setattr(session_manager, "_ensure_pid", lambda session, *, target, db: session)
    monkeypatch.setattr(
        session_manager,
        "check_session_status",
        lambda session, *, target: SessionStatus.OK,
    )
    payload = ("line=雪\n" * 500)[:-1] + "\n"
    monkeypatch.setattr("agentworks.sessions.tmux.capture_output", lambda *a, **k: payload)

    pipe = _ShortWritePipe()
    monkeypatch.setattr(sys, "stdout", pipe)

    session_manager.session_logs(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)

    assert bytes(pipe.raw) == payload.encode("utf-8")


def test_describe_session_holds_across_the_probe(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Describe's hold SUPERSET, explicitly: the imperative body gated
    and DISCARDED the platform (no hold); the gate span now holds
    across the status probe (a no-op everywhere but WSL2, where it
    anchors the probe). Pinned as hold-open, probe, hold-close, with
    the one boundary burst."""
    config = make_config()
    _seed_singular(db)
    _reachable(monkeypatch, True)
    events: list[str] = []
    _record_holds(monkeypatch, events)

    def _probe(session: object, *, target: object) -> object:
        assert db._tx_depth == 0  # noqa: SLF001
        events.append("probe")
        from agentworks.db import SessionStatus

        return SessionStatus.STOPPED

    monkeypatch.setattr(session_manager, "check_session_status", _probe)

    session_manager.describe_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)

    assert events == ["hold-open:box", "probe", "hold-close:box"]
    assert resolve_counter == [["proxmox-token"]]
    assert any("Status:     stopped" in m for m in captured_output.info)


def test_describe_session_derives_vm_from_its_structural_snapshot(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed_singular(db)
    _seed_vm(db, "moved", "100.64.0.10")
    _reachable(monkeypatch, True)

    def move_before_snapshot(session: object, *, target: object) -> object:
        assert db._tx_depth == 0  # noqa: SLF001
        db._conn.execute("UPDATE workspaces SET vm_name = 'moved' WHERE name = 'ws-box'")  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
        from agentworks.db import SessionStatus

        return SessionStatus.STOPPED

    monkeypatch.setattr(session_manager, "check_session_status", move_before_snapshot)

    description = session_manager.session_description(
        db,
        config,
        name="s1",
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert description.vm_name == "moved"
    assert resolve_counter == [["proxmox-token"]]


def test_describe_session_shows_harness_integration_line(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Describe carries the resolved harness integration between Template and Mode."""
    config = make_config()
    _seed_singular(db)
    _reachable(monkeypatch, True)

    from agentworks.db import SessionStatus

    monkeypatch.setattr(
        session_manager,
        "check_session_status",
        lambda session, *, target: SessionStatus.STOPPED,
    )

    session_manager.describe_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)

    info = captured_output.info
    assert "Harness integration: shell" in info
    # The Harness integration line sits after Template and before Mode.
    template_idx = next(i for i, m in enumerate(info) if m.startswith("Template:"))
    harness_integration_idx = next(i for i, m in enumerate(info) if m.startswith("Harness integration:"))
    mode_idx = next(i for i, m in enumerate(info) if m.startswith("Mode:"))
    assert template_idx < harness_integration_idx < mode_idx


def test_describe_session_stopped_vm_gate_burst_seeds_the_boundary(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed_singular(db)
    events: list[str] = []
    _stop_the_vms(monkeypatch, events)

    session_manager.describe_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)

    assert events == ["status:box", "start:box", "tailscale:box"]
    assert resolve_counter == [["proxmox-token"]]


def test_unknown_session_refuses_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed_vm(db, "box", "100.64.0.9")
    _reachable(monkeypatch, False)

    def _no_status(self: ProxmoxPlatform, row: object) -> VMStatus:
        raise AssertionError("the gate ran for an unknown session")

    monkeypatch.setattr(ProxmoxPlatform, "status", _no_status)

    with pytest.raises(NotFoundError, match="session 'ghost' not found"):
        session_manager.stop_session(db, config, name="ghost", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == []


def test_unknown_workspace_refuses_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    # An orphan session row (its workspace is gone): the FK is relaxed
    # for the insert to reproduce the dangling-reference state.
    db._conn.execute("PRAGMA foreign_keys = OFF")
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, socket_path) "
        "VALUES ('orphan', 'ghost-ws', 'default', 'admin', '/tmp/orphan.sock')"
    )
    db._conn.commit()
    db._conn.execute("PRAGMA foreign_keys = ON")
    _reachable(monkeypatch, False)

    def _no_status(self: ProxmoxPlatform, row: object) -> VMStatus:
        raise AssertionError("the gate ran for an unknown workspace")

    monkeypatch.setattr(ProxmoxPlatform, "status", _no_status)

    with pytest.raises(NotFoundError, match="workspace 'ghost-ws' not found"):
        session_manager.stop_session(db, config, name="orphan", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == []


def test_no_tailscale_vm_fails_pre_gate_with_zero_resolves(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hoisted row guard: a VM with no Tailscale address refuses
    BEFORE any prompt and before any VM start (the imperative body
    checked it after its gate; the hoist forgoes the accidental heal
    where a post-gate start's rejoin repopulated the row)."""
    config = make_config()
    _seed_vm(db, "box", None)
    _seed_session(db, "s1", "ws-box")
    _reachable(monkeypatch, False)

    def _no_status(self: ProxmoxPlatform, row: object) -> VMStatus:
        raise AssertionError("the gate ran for a VM with no address")

    monkeypatch.setattr(ProxmoxPlatform, "status", _no_status)

    with pytest.raises(StateError, match="no Tailscale address"):
        session_manager.stop_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == []


def test_session_scope_reaches_node_readiness(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The singular ops run at SESSION level (the recorded
    pass-the-level-of-the-entity rule): the scope carries the session's
    full identity chain to every node preflight."""
    from agentworks.capabilities.base import ScopeLevel

    config = make_config()
    _seed_singular(db, agent="a1")
    _reachable(monkeypatch, True)
    scopes: list[OperationScope | None] = []
    real = ProxmoxPlatform.preflight

    def _recording(self: ProxmoxPlatform, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _recording)

    session_manager.describe_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.SESSION
    assert scope.vm == "box"
    assert scope.workspace == "ws-box"
    assert scope.session == "s1"
    assert scope.agent == "a1"
    assert scope.admin is False
