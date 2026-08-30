"""``agent create`` / ``agent reinit`` through the orchestrated model:
the derived graph, the gate-prompt parity carry (the tracer's mirror
shape), the banner and failure parity with the imperative commands, and
the grant-all reconciliation riding the realization body.

Real config, registry, resolver, and backend loop (env-var backend);
the platform's backend ops, the reachability probe, the transports,
and the on-VM mutation (``create_agent_on_vm``) are the fakes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from agentworks import db as db_module
from agentworks.agents import grants as agent_grants
from agentworks.agents import initializer as agent_initializer
from agentworks.agents import manager as agent_manager
from agentworks.capabilities.base import RunContext
from agentworks.db import VersionedPayload
from agentworks.errors import ExternalError, NotFoundError, StateError
from agentworks.output import Role
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager
from tests.conftest import ManifestDoc, stub_vm_ssh_identity
from tests.orchestrated_fixtures import PLUGINS_ENABLED, proxmox_site, write_operator_config

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.db import Database

AGENT_MANIFESTS = [
    ManifestDoc("git-credential", "gh", {"provider": {"name": "github"}}),
    ManifestDoc("agent-template", "default", {"git_credentials": ["gh"]}),
    ManifestDoc("agent-template", "other", {"git_credentials": ["gh"]}),
]


@pytest.fixture(autouse=True)
def _stub_ssh_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_vm_ssh_identity(monkeypatch)


@pytest.fixture
def make_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """This suite's ``make_config`` delta from the shared fixture: the
    git token in the env and the agent resources declared as manifests."""
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GH", "ghtok")

    def _make():  # noqa: ANN202
        return write_operator_config(tmp_path, PLUGINS_ENABLED, manifests=[proxmox_site(), *AGENT_MANIFESTS])

    return _make


@pytest.fixture
def mutation(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake the on-VM mutation and the SSH-config refresh; capture what
    the orchestrator hands the body."""
    captured: dict[str, Any] = {}

    def _fake_mutation(*args: Any, **kwargs: Any) -> None:
        captured["git_tokens"] = kwargs["git_tokens"]
        captured["agent_name"] = kwargs["agent_name"]

    monkeypatch.setattr(agent_initializer, "create_agent_on_vm", _fake_mutation)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)
    return captured


def _seed_vm(db: Database) -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")


def test_create_with_missing_vm_preserves_domain_error_and_state(
    db: Database,
    make_config,
) -> None:
    db.insert_vm("unrelated", site="proxmox", hostname="unrelated")
    db.instance_state.put_desired_overlay("vm", "unrelated", VersionedPayload(2, {"future": True}))

    with pytest.raises(NotFoundError) as caught:
        agent_manager.create_agent(
            db,
            make_config(),
            name="dev",
            vm_name="missing",
            spec='{"env":{"TOKEN":{"secret":"overlay-token"}}}',
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert caught.value.entity_kind == "vm"
    assert caught.value.entity_name == "missing"
    assert db.get_agent("dev") is None
    assert db.instance_state.get_desired_overlay("agent", "dev") is None


def _reachable(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: value)


def _stop_the_vm(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    from agentworks.db import VMStatus as _VMStatus

    _reachable(monkeypatch, False)
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, row, ctx: events.append("status") or _VMStatus.STOPPED,
    )
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, row, ctx: events.append("start"))
    monkeypatch.setattr(vm_manager, "_tailscale_rejoin_required", lambda *a, **k: True)
    monkeypatch.setattr(vm_manager, "_ensure_tailscale", lambda *a, **k: events.append("tailscale"))


@pytest.mark.parametrize("operation", ["create", "reinit"])
def test_agent_mutation_refuses_ssh_identity_before_activation(
    db: Database,
    make_config,
    mutation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    config = make_config()
    _seed_vm(db)
    if operation == "reinit":
        db.insert_agent("dev", "box", "agt-dev", template="default")

    def refuse(*args: object, **kwargs: object) -> None:
        raise StateError("SSH identity drift")

    monkeypatch.setattr(vm_manager, "require_vm_ssh_boundary", refuse)
    monkeypatch.setattr(
        "agentworks.orchestration.activation.activation_gate",
        lambda *args, **kwargs: pytest.fail("activation started before SSH identity refusal"),
    )

    with pytest.raises(StateError):
        if operation == "create":
            agent_manager.create_agent(
                db,
                config,
                name="dev",
                vm_name="box",
                interaction=TtyInteractionPolicy.REFUSE,
            )
        else:
            agent_manager.reinit_agent(
                db,
                config,
                name="dev",
                interaction=TtyInteractionPolicy.REFUSE,
            )

    assert mutation == {}


def test_create_and_reinit_loggers_receive_all_git_tokens(
    db: Database,
    make_config,
    mutation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both agent mutation roots bind the complete resolved credential set
    when constructing their incremental logger."""
    captured: list[tuple[str, str, tuple[str, ...]]] = []

    class _LoggerSpy:
        path = "/dev/null"

        def __init__(self, vm_name: str, command_stem: str, *, redactions: tuple[str, ...] = ()) -> None:
            captured.append((vm_name, command_stem, redactions))

        def close(self) -> None:
            pass

    assert mutation == {}
    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    monkeypatch.setattr("agentworks.ssh.SSHLogger", _LoggerSpy)

    agent_manager.create_agent(db, config, name="dev", vm_name="box", interaction=TtyInteractionPolicy.REFUSE)
    agent_manager.reinit_agent(db, config, name="dev", interaction=TtyInteractionPolicy.REFUSE)

    assert captured == [
        ("box", "agent-create", ("ghtok",)),
        ("box", "agent-reinit", ("ghtok",)),
    ]


# -- the derived graph --------------------------------------------------------


def test_create_graph_derives_from_template_and_row(db: Database, make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pending agent's graph: its edges are the resolved template
    (whose declared credentials become git-credential nodes) and the
    VM's row (whose site field is the vm-site edge); the union is the
    token plus the site's config secret, with the template's env-block
    secrets excluded (hermetic provisioning)."""
    from agentworks.agents.nodes import agent_template_node, pending_agent_node
    from agentworks.agents.templates import resolve_template
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.vms.nodes import live_vm_node

    config = make_config()
    _seed_vm(db)
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)

    vm_node = live_vm_node(db, config, registry, vm)
    tmpl_node = agent_template_node(registry, resolve_template(registry, None))
    pending = pending_agent_node(db, config, "dev", tmpl_node, vm_node, interaction=TtyInteractionPolicy.REFUSE)
    nodes = walk(pending)

    assert [n.key for n in nodes] == [
        "git-credential/gh",
        "agent-template/default",
        "vm-site/proxmox",
        "vm/box",
        "agent/dev",
    ]
    assert secret_union(nodes) == ("git-token-gh", "proxmox-token")


def test_reinit_graph_derives_from_row_and_stored_template(
    db: Database, make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reinit's two-root walk: the live agent (whose row carries the VM
    chain but no template edge) plus the stored template (whose declared
    credentials become edges, because the materials rewrite needs their
    tokens in the boundary union). One VM object serves both roots."""
    from agentworks.agents.nodes import agent_template_node, live_agent_node
    from agentworks.agents.templates import resolve_template
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.vms.nodes import live_vm_node

    config = make_config()
    _seed_vm(db)
    row = db.insert_agent("dev", "box", "agt-dev", template="default")
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)

    vm_node = live_vm_node(db, config, registry, vm)
    agent_node = live_agent_node(row, vm_node)
    tmpl_node = agent_template_node(registry, resolve_template(registry, row.template))
    nodes = walk(agent_node, tmpl_node)

    assert [n.key for n in nodes] == [
        "vm-site/proxmox",
        "vm/box",
        "agent/dev",
        "git-credential/gh",
        "agent-template/default",
    ]
    assert secret_union(nodes) == ("proxmox-token", "git-token-gh")


# -- gate-prompt parity (the per-command carry) -------------------------------


def test_create_stopped_vm_gate_resolves_once_and_seeds_the_boundary(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    mutation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """agent create on a stopped VM: the gate's just-in-time token
    resolve is the first backend pass, the boundary covers only the
    remainder (the seeded site token excluded), nothing resolves twice
    or after, and the scoped token reaches the mutation. The command
    frames its own phases (the banner parity the imperative root
    carried)."""
    config = make_config()
    _seed_vm(db)
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)

    agent_manager.create_agent(db, config, name="dev", vm_name="box", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"], ["git-token-gh"]]
    assert events == ["status", "start", "tailscale"]  # the gate ran
    assert mutation["git_tokens"] == {"gh": "ghtok"}
    row = db.get_agent("dev")
    assert row is not None and row.linux_user == "agt-dev"
    # Banner parity: the orchestrator frames the same phases the
    # imperative root did, and the checks announce the same lines.
    assert "=== Preflight ===" in captured_output.info
    assert "=== Resolving Secrets ===" in captured_output.info
    assert "=== Agent Initialization ===" in captured_output.info
    assert "Checking agent-template/default..." in captured_output.info
    assert "Checking git-credential/gh..." in captured_output.info
    # The phases are real sections now: headers at level 0, their body
    # step lines at level 1. Preflight's "Checking ..." lines are primary
    # steps, so they render as Role.BODY at the section level (2 spaces),
    # not de-emphasized detail one notch deeper.
    assert (Role.HEADER, 0, "Preflight") in captured_output.lines
    assert (Role.BODY, 1, "Checking agent-template/default...") in captured_output.lines


def test_reinit_stopped_vm_gate_resolves_once_and_seeds_the_boundary(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    mutation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """agent reinit, same invariant: gate burst then boundary burst,
    nothing twice, nothing after; the mutation runs against the STORED
    row (name and user), not a re-derivation."""
    config = make_config()
    _seed_vm(db)
    db.insert_agent("dev", "box", "agt-dev", template="default")
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)

    agent_manager.reinit_agent(db, config, name="dev", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"], ["git-token-gh"]]
    assert events == ["status", "start", "tailscale"]
    assert mutation["git_tokens"] == {"gh": "ghtok"}
    assert mutation["agent_name"] == "dev"
    assert any("reinitialized" in m for m in captured_output.info)
    # The terminal outcome routes through result(): RESULT role at level 0.
    assert (Role.RESULT, 0, "Agent 'dev' reinitialized") in captured_output.lines
    # No grants seeded, so the reconcile summary line (issue #280 item 5) is
    # suppressed: nothing was reconciled, so nothing is claimed.
    assert not any(m.startswith("Reconciled ") for m in captured_output.info)
    # Banner parity: reinit frames the same phases the imperative root
    # did, so a framing regression cannot pass.
    assert "=== Preflight ===" in captured_output.info
    assert "=== Resolving Secrets ===" in captured_output.info
    assert "=== Agent Initialization ===" in captured_output.info
    assert "Checking agent-template/default..." in captured_output.info


def test_create_reachable_vm_fast_path_costs_no_gate_resolve(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    mutation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The gate's fast path on a reachable VM: no just-in-time resolve
    at all, so the command's whole union rides ONE boundary burst."""
    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)

    agent_manager.create_agent(db, config, name="dev", vm_name="box", interaction=TtyInteractionPolicy.REFUSE)

    # One burst covering the whole union, in the walk's deterministic
    # first-encounter order (the union's only source since the
    # construct-time registration seam closed).
    assert resolve_counter == [["git-token-gh", "proxmox-token"]]
    assert mutation["git_tokens"] == {"gh": "ghtok"}


# -- failure parity -----------------------------------------------------------


def test_create_mutation_failure_cleans_up_and_leaves_no_row(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The imperative rollback window, reproduced: a mutation failure
    removes the half-configured user (the body's own cleanup), wraps in
    the same ExternalError, and no DB row ever exists; nothing else is
    unwound (there is nothing realized to unwind)."""
    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)

    def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("ssh exploded")

    monkeypatch.setattr(agent_initializer, "create_agent_on_vm", _boom)
    deletes: list[str] = []
    monkeypatch.setattr(
        agent_initializer,
        "delete_agent_on_vm",
        lambda vm, config_, linux_user, **k: deletes.append(linux_user),
    )

    with pytest.raises(ExternalError, match="creating agent: ssh exploded"):
        agent_manager.create_agent(db, config, name="dev", vm_name="box", interaction=TtyInteractionPolicy.REFUSE)

    assert deletes == ["agt-dev"]  # the body's partial-state cleanup ran
    assert db.get_agent("dev") is None


def test_create_overlay_persistence_failure_rolls_back_the_remote_agent_and_owner(
    db: Database,
    make_config,  # noqa: ANN001
    mutation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    deletes: list[str] = []
    monkeypatch.setattr(
        agent_initializer,
        "delete_agent_on_vm",
        lambda vm, config_, linux_user, **k: deletes.append(linux_user),
    )
    monkeypatch.setattr(
        "agentworks.instance_specs.persist_creation_overlay",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("overlay write failed")),
    )

    with pytest.raises(ExternalError) as caught:
        agent_manager.create_agent(
            db,
            config,
            name="dev",
            vm_name="box",
            spec='{"shell":"zsh"}',
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert caught.value.entity_kind == "agent"
    assert caught.value.entity_name == "dev"
    assert deletes == ["agt-dev"]
    assert db.get_agent("dev") is None
    assert db.instance_state.get_desired_overlay("agent", "dev") is None


def test_create_logger_close_failure_retains_owner_and_reports_once(
    db: Database,
    make_config,  # noqa: ANN001
    mutation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.instance_specs import OverlayDisposition, OverlayOutcome
    from agentworks.ssh import SSHLogger

    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    outcomes: list[OverlayOutcome] = []
    monkeypatch.setattr("agentworks.instance_specs.render_overlay_outcome", outcomes.append)
    monkeypatch.setattr(SSHLogger, "close", lambda self: (_ for _ in ()).throw(RuntimeError("close failed")))

    with pytest.raises(RuntimeError, match="close failed"):
        agent_manager.create_agent(
            db,
            config,
            name="dev",
            vm_name="box",
            spec='{"shell":"zsh"}',
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert db.get_agent("dev") is not None
    assert db.instance_state.get_desired_overlay("agent", "dev") is not None
    assert [(outcome.disposition, outcome.fields) for outcome in outcomes] == [(OverlayDisposition.SET, ("shell",))]


def test_reinit_mutation_failure_wraps_and_keeps_the_agent(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed_vm(db)
    db.insert_agent("dev", "box", "agt-dev", template="default")
    _reachable(monkeypatch, True)

    def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("ssh exploded")

    monkeypatch.setattr(agent_initializer, "create_agent_on_vm", _boom)

    with pytest.raises(ExternalError, match="reinitializing agent: ssh exploded"):
        agent_manager.reinit_agent(db, config, name="dev", interaction=TtyInteractionPolicy.REFUSE)

    assert db.get_agent("dev") is not None  # re-runnable, as before


def test_reinit_build_failure_reports_the_committed_overlay_once(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.instance_specs import OverlayDisposition, OverlayOutcome

    config = make_config()
    _seed_vm(db)
    db.insert_agent("dev", "box", "agt-dev", template="default")
    outcomes: list[OverlayOutcome] = []
    monkeypatch.setattr("agentworks.instance_specs.render_overlay_outcome", outcomes.append)
    monkeypatch.setattr(
        "agentworks.vms.nodes.live_vm_node",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("build failed")),
    )

    with pytest.raises(RuntimeError, match="build failed"):
        agent_manager.reinit_agent(
            db,
            config,
            name="dev",
            spec='{"shell":"zsh"}',
            interaction=TtyInteractionPolicy.REFUSE,
        )

    stored = db.instance_state.get_desired_overlay("agent", "dev")
    assert stored is not None and stored.payload.value == {"shell": "zsh"}
    assert [(outcome.disposition, outcome.fields) for outcome in outcomes] == [(OverlayDisposition.SET, ("shell",))]


@pytest.mark.parametrize("clear_spec", ["{}", ""])
def test_reinit_retains_replaces_and_clears_the_stored_overlay(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
    clear_spec: str,
) -> None:
    from agentworks.instance_specs import parse_instance_spec

    config = make_config()
    _seed_vm(db)
    db.insert_agent("dev", "box", "agt-dev", template="default")
    original = parse_instance_spec("agent", '{"shell":"bash"}')
    db.instance_state.put_desired_overlay("agent", "dev", original.payload)
    _reachable(monkeypatch, True)
    monkeypatch.setattr(agent_initializer, "create_agent_on_vm", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)

    agent_manager.reinit_agent(db, config, name="dev", interaction=TtyInteractionPolicy.REFUSE)
    retained = db.instance_state.get_desired_overlay("agent", "dev")
    assert retained is not None and retained.payload == original.payload

    agent_manager.reinit_agent(
        db,
        config,
        name="dev",
        spec='{"shell":"zsh"}',
        interaction=TtyInteractionPolicy.REFUSE,
    )
    replaced = db.instance_state.get_desired_overlay("agent", "dev")
    assert replaced is not None and replaced.payload.value == {"shell": "zsh"}

    agent_manager.reinit_agent(
        db,
        config,
        name="dev",
        spec=clear_spec,
        interaction=TtyInteractionPolicy.REFUSE,
    )
    assert db.instance_state.get_desired_overlay("agent", "dev") is None


@pytest.mark.parametrize(
    "stored_payload",
    [
        pytest.param(VersionedPayload(1, {"future_field": "do-not-print-this-value"}), id="future-field"),
        pytest.param(VersionedPayload(2, {"opaque": "do-not-print-this-value"}), id="future-version"),
    ],
)
def test_reinit_empty_spec_clears_an_unsupported_stored_overlay_without_exposing_it(
    db: Database,
    make_config,  # noqa: ANN001
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: dict[str, Any],
    captured_output,  # noqa: ANN001
    stored_payload: VersionedPayload,
) -> None:
    config = make_config()
    _seed_vm(db)
    db.insert_agent("dev", "box", "agt-dev", template="default")
    db.instance_state.put_desired_overlay("agent", "dev", stored_payload)
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    _reachable(monkeypatch, True)

    agent_manager.reinit_agent(
        db,
        config,
        name="dev",
        spec="",
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert db.instance_state.get_desired_overlay("agent", "dev") is None
    assert mutation["agent_name"] == "dev"
    assert "future_field" not in repr(captured_output.lines)
    assert "opaque" not in repr(captured_output.lines)
    assert "do-not-print-this-value" not in repr(captured_output.lines)


def test_reinit_whitespace_spec_remains_invalid_and_retains_the_stored_overlay(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.errors import ValidationError
    from agentworks.instance_specs import parse_instance_spec

    config = make_config()
    _seed_vm(db)
    db.insert_agent("dev", "box", "agt-dev", template="default")
    original = parse_instance_spec("agent", '{"shell":"bash"}')
    db.instance_state.put_desired_overlay("agent", "dev", original.payload)
    mutation_reached = False

    def _mutation(*args: object, **kwargs: object) -> None:
        nonlocal mutation_reached
        mutation_reached = True

    monkeypatch.setattr(agent_initializer, "create_agent_on_vm", _mutation)

    with pytest.raises(ValidationError):
        agent_manager.reinit_agent(
            db,
            config,
            name="dev",
            spec=" ",
            interaction=TtyInteractionPolicy.REFUSE,
        )

    retained = db.instance_state.get_desired_overlay("agent", "dev")
    assert retained is not None and retained.payload == original.payload
    assert not mutation_reached


# -- --update-template re-points before reinit --------------------------------


def test_reinit_update_template_repoints_and_resolves_the_new_template(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """``agent reinit --update-template other`` persists the new stored
    template and reinit resolves + sets up against it (not the old one)."""
    config = make_config()
    _seed_vm(db)
    db.insert_agent("dev", "box", "agt-dev", template="default")
    _reachable(monkeypatch, True)

    captured: dict[str, Any] = {}

    def _capture(vm: Any, config_: Any, registry: Any, agent_tmpl: Any, linux_user: str, **kwargs: Any) -> None:
        captured["template"] = agent_tmpl.name

    monkeypatch.setattr(agent_initializer, "create_agent_on_vm", _capture)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)

    agent_manager.reinit_agent(db, config, name="dev", update_template="other", interaction=TtyInteractionPolicy.REFUSE)

    assert captured["template"] == "other"  # setup ran against the NEW template
    row = db.get_agent("dev")
    assert row is not None and row.template == "other"  # persisted


def test_reinit_unknown_update_template_raises_and_keeps_the_row(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An undeclared ``--update-template`` name fails pre-boundary with a
    typed NotFoundError; nothing is persisted, nothing resolves, and the
    stored template is left as it was (validate BEFORE any side effect)."""
    from agentworks.errors import NotFoundError

    config = make_config()
    _seed_vm(db)
    db.insert_agent("dev", "box", "agt-dev", template="default")

    def _boom(*a: Any, **k: Any) -> None:
        raise AssertionError("setup must not run for an invalid template name")

    monkeypatch.setattr(agent_initializer, "create_agent_on_vm", _boom)

    with pytest.raises(NotFoundError, match="Unknown agent template"):
        agent_manager.reinit_agent(
            db, config, name="dev", update_template="ghost", interaction=TtyInteractionPolicy.REFUSE
        )

    assert resolve_counter == []  # refused before any secret resolve
    row = db.get_agent("dev")
    assert row is not None and row.template == "default"  # unchanged


def test_reinit_non_enum_policy_raises_before_the_repoint_is_persisted(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary resolver rejects a non-enum policy too, but only after the
    re-point is already persisted. ``reinit_agent`` checks first, so a refused
    reinit never leaves the row pointing at a template it never converged on."""
    config = make_config()
    _seed_vm(db)
    db.insert_agent("dev", "box", "agt-dev", template="default")

    def _boom(*a: Any, **k: Any) -> None:
        raise AssertionError("setup must not run for a rejected policy")

    monkeypatch.setattr(agent_initializer, "create_agent_on_vm", _boom)

    with pytest.raises(StateError):
        agent_manager.reinit_agent(
            db,
            config,
            name="dev",
            update_template="other",
            interaction="refuse",  # type: ignore[arg-type]
        )

    assert resolve_counter == []
    row = db.get_agent("dev")
    assert row is not None and row.template == "default"


def test_reinit_update_template_persists_before_convergence_so_a_mid_failure_keeps_the_new_binding(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The re-point is persisted BEFORE the on-VM convergence, so a setup
    failure mid-reinit leaves the agent bound to the NEW template (the op
    is non-atomic and re-runnable): the wrapped ExternalError propagates,
    but the row already points at 'other', so a plain `agent reinit` (no
    flag) re-converges toward it."""
    config = make_config()
    _seed_vm(db)
    db.insert_agent("dev", "box", "agt-dev", template="default")
    _reachable(monkeypatch, True)

    def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("ssh exploded")

    monkeypatch.setattr(agent_initializer, "create_agent_on_vm", _boom)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)

    with pytest.raises(ExternalError, match="reinitializing agent: ssh exploded"):
        agent_manager.reinit_agent(
            db, config, name="dev", update_template="other", interaction=TtyInteractionPolicy.REFUSE
        )

    row = db.get_agent("dev")
    assert row is not None and row.template == "other"  # persisted before the failed convergence


# -- Phase 7: the recipe use-gate fires on a live build -----------------------
#
# End-to-end proof (real config + real build_registry) that
# ``ensure_recipe_enabled`` actually refuses on a live registry with the right
# kind-string, before any DB / VM / mutation work. The fixture plugin ships a
# disabled agent-template (referencing a disabled user-install-command) via a
# bundled manifest; it is NOT in ``[plugins] system``, so its rows are
# present-but-disabled.

_DECLARABLE_ANCHOR = "tests.plugins._manifest_declarable_fixture"


def _install_disabled_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.plugins import SYSTEM_PLUGINS, Plugin

    plugin = Plugin(name="decl-plugin", description="a manifest-parity fixture", manifests=_DECLARABLE_ANCHOR)
    # Merge alongside the real shipped plugins (proxmox, which the shared config
    # enables and this suite's VM runs on, plus claude / onepassword) rather than
    # replacing them: replacing would drop proxmox's row and fail the enabled-name
    # check. decl-plugin ships present-but-disabled (not in [plugins] system), so
    # its bundled recipe refuses with the enable hint.
    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {**SYSTEM_PLUGINS, plugin.name: plugin})


def test_create_agent_on_disabled_plugin_recipe_refuses_before_any_work(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.errors import StateError

    config = make_config()
    _seed_vm(db)
    _install_disabled_fixture(monkeypatch)

    def _boom(*a: Any, **k: Any) -> None:
        raise AssertionError("the mutation must not run for a disabled-recipe template")

    monkeypatch.setattr(agent_initializer, "create_agent_on_vm", _boom)

    with pytest.raises(StateError) as caught:
        agent_manager.create_agent(
            db,
            config,
            name="dev",
            vm_name="box",
            template="fixture-agent-tmpl",
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert (caught.value.entity_kind, caught.value.entity_name) == ("agent-template", "fixture-agent-tmpl")
    assert db.get_agent("dev") is None  # refused before any DB write


def test_create_agent_overlay_only_disabled_reference_refuses_before_mutation(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed_vm(db)
    _install_disabled_fixture(monkeypatch)

    with pytest.raises(StateError) as caught:
        agent_manager.create_agent(
            db,
            config,
            name="dev",
            vm_name="box",
            spec='{"user_install_commands":["fixture-user-cmd"]}',
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert (caught.value.entity_kind, caught.value.entity_name) == ("user-install-command", "fixture-user-cmd")
    assert db.get_agent("dev") is None
    assert db.instance_state.get_desired_overlay("agent", "dev") is None


def test_reinit_agent_overlay_only_disabled_reference_refuses_before_persist(
    db: Database,
    make_config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed_vm(db)
    db.insert_agent("dev", "box", "dev")
    _install_disabled_fixture(monkeypatch)

    with pytest.raises(StateError) as caught:
        agent_manager.reinit_agent(
            db,
            config,
            name="dev",
            spec='{"user_install_commands":["fixture-user-cmd"]}',
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert (caught.value.entity_kind, caught.value.entity_name) == ("user-install-command", "fixture-user-cmd")
    assert db.instance_state.get_desired_overlay("agent", "dev") is None


def test_reinit_update_template_to_disabled_recipe_refuses_before_persist(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 1: a repoint to a real-but-disabled-recipe template is refused BEFORE
    ``db.update_agent_template`` persists it, so the stored template is
    unchanged (mirrors ``test_reinit_unknown_update_template_raises_and_keeps_the_row``)."""
    from agentworks.errors import StateError

    config = make_config()
    _seed_vm(db)
    db.insert_agent("dev", "box", "agt-dev", template="default")
    _install_disabled_fixture(monkeypatch)

    def _boom(*a: Any, **k: Any) -> None:
        raise AssertionError("setup must not run for a refused repoint")

    monkeypatch.setattr(agent_initializer, "create_agent_on_vm", _boom)

    with pytest.raises(StateError) as caught:
        agent_manager.reinit_agent(
            db, config, name="dev", update_template="fixture-agent-tmpl", interaction=TtyInteractionPolicy.REFUSE
        )

    assert (caught.value.entity_kind, caught.value.entity_name) == ("agent-template", "fixture-agent-tmpl")
    row = db.get_agent("dev")
    assert row is not None and row.template == "default"  # the refused repoint was NOT persisted


# -- the operation scope reaches readiness ------------------------------------


def test_agent_scope_reaches_node_readiness(
    db: Database,
    make_config,  # noqa: ANN001
    mutation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.capabilities.base import ScopeLevel
    from agentworks.capabilities.git_credential.github import (
        GitHubCredentialProvider,
    )

    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    scopes: list[OperationScope | None] = []
    real = GitHubCredentialProvider.preflight

    def _recording(self: GitHubCredentialProvider, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(GitHubCredentialProvider, "preflight", _recording)

    agent_manager.create_agent(db, config, name="dev", vm_name="box", interaction=TtyInteractionPolicy.REFUSE)

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.AGENT
    assert scope.vm == "box" and scope.agent == "dev"
    assert scope.workspace is None and scope.session is None


# -- grant-all rides the realization body -------------------------------------


def test_create_grant_all_reconciles_between_insert_and_sync(
    db: Database,
    make_config,  # noqa: ANN001
    mutation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """``--grant-all-workspaces`` keeps the imperative shape: the row
    carries grant_all, and each existing workspace on the VM gets the
    group add plus the explicit grant, before the SSH-config refresh."""
    config = make_config()
    _seed_vm(db)
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'box', '/srv/ws1', 'ws-ws1')"
    )
    db._conn.commit()
    _reachable(monkeypatch, True)
    group_adds: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agent_grants,
        "add_to_workspace_group",
        lambda vm, config_, db_, linux_user, ws, **k: group_adds.append((linux_user, ws)),
    )

    agent_manager.create_agent(
        db, config, name="dev", vm_name="box", grant_all_workspaces=True, interaction=TtyInteractionPolicy.REFUSE
    )

    row = db.get_agent("dev")
    assert row is not None and row.grant_all
    assert group_adds == [("agt-dev", "ws1")]
    assert db.has_any_grant("dev", "ws1")


# -- reinit reconciles recorded grants onto the VM (issue #280) ----------------
#
# These tests fake ``create_agent_on_vm`` wholesale (via the ``mutation``
# fixture, or a local recorder). The user step's two outcomes, the "#252
# truly-gone user" RECREATE and the already-exists no-op, both live INSIDE
# that faked function, so this seam does not distinguish them: whichever
# occurred, the reconcile that follows runs unconditionally. These tests
# therefore pin the reconcile itself (that it runs, over which workspaces,
# without re-inserting rows), NOT that the recreate branch specifically was
# taken. Driving the two user-step outcomes would require exercising the real
# ``create_agent_on_vm`` against a fake transport, which is out of scope here.


def _add_workspace(db: Database, name: str) -> None:
    """Insert a workspace row on 'box' with its canonical ws-<name> group."""
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) VALUES (?, 'box', ?, ?)",
        (name, f"/srv/{name}", f"ws-{name}"),
    )
    db._conn.commit()


def _seed_granted_agent(db: Database, workspaces: list[str]) -> None:
    """Seed the 'dev' agent on 'box' with an explicit grant for each name in
    ``workspaces`` (creating each workspace row too)."""
    db.insert_agent("dev", "box", "agt-dev", template="default")
    for ws in workspaces:
        _add_workspace(db, ws)
        db.insert_agent_grant("dev", ws, "explicit")


def _seed_grant_all_agent(db: Database, workspaces: list[str]) -> None:
    """Seed the 'dev' agent on 'box' as a grant_all agent with the rows that
    grant_all materializes.

    Faithful to the real grant_all shape: the agent row carries the
    grant_all flag, and each workspace has an 'explicit' grant row (the type
    grant_all writes, see agents/grants.py grant_all branch, realize.py, and
    workspaces/realize.py). There is no distinct grant_type for grant_all;
    the flag on the agent row is what marks these rows as blanket policy."""
    db.insert_agent("dev", "box", "agt-dev", template="default", grant_all=True)
    for ws in workspaces:
        _add_workspace(db, ws)
        db.insert_agent_grant("dev", ws, "explicit")


def test_reinit_reconciles_recorded_grants_onto_the_vm(
    db: Database,
    make_config,  # noqa: ANN001
    mutation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Regression for issue #280. reinit shares ``create_agent_on_vm`` with
    the create path but not realize's grant pass, so an agent's recorded
    grants were never reconciled onto the VM after reinit (which silently
    lost workspace access when the user step had recreated a gone user).
    This asserts the reconcile runs and adds the agent to its granted
    workspace's group. Fails without the fix, because reinit never called
    ``add_to_workspace_group``. (See the section note: this pins the
    reconcile, not which user-step branch ran.)"""
    config = make_config()
    _seed_vm(db)
    _seed_granted_agent(db, ["ws1"])
    _reachable(monkeypatch, True)
    group_adds: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agent_grants,
        "add_to_workspace_group",
        lambda vm, config_, db_, linux_user, ws, **k: group_adds.append((linux_user, ws)),
    )

    agent_manager.reinit_agent(db, config, name="dev", interaction=TtyInteractionPolicy.REFUSE)

    assert group_adds == [("agt-dev", "ws1")]


def test_reinit_reconcile_does_not_reinsert_grant_rows(
    db: Database,
    make_config,  # noqa: ANN001
    mutation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The reconcile touches ON-VM state only. The grant rows already exist
    on reinit (unlike create's initial grant_all materialization), so reinit
    must NOT re-insert or duplicate them: only ``add_to_workspace_group``
    runs, never ``insert_agent_grant``."""
    config = make_config()
    _seed_vm(db)
    _seed_granted_agent(db, ["ws1"])
    _reachable(monkeypatch, True)
    monkeypatch.setattr(agent_grants, "add_to_workspace_group", lambda *a, **k: None)

    before = len(db.list_agent_grants("dev"))
    agent_manager.reinit_agent(db, config, name="dev", interaction=TtyInteractionPolicy.REFUSE)

    assert len(db.list_agent_grants("dev")) == before == 1  # no new / duplicate rows
    assert db.has_any_grant("dev", "ws1")


def test_reinit_reconcile_is_idempotent_across_repeated_reinits(
    db: Database,
    make_config,  # noqa: ANN001
    mutation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The reconcile runs on every reinit and is safe to repeat because it is
    idempotent: ``add_to_workspace_group`` is ``getent || groupadd`` then
    ``usermod -aG``, a no-op once membership holds. A second reinit still
    invokes it, completes cleanly, and duplicates no grant rows."""
    config = make_config()
    _seed_vm(db)
    _seed_granted_agent(db, ["ws1"])
    _reachable(monkeypatch, True)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agent_grants,
        "add_to_workspace_group",
        lambda vm, config_, db_, linux_user, ws, **k: calls.append((linux_user, ws)),
    )

    agent_manager.reinit_agent(db, config, name="dev", interaction=TtyInteractionPolicy.REFUSE)
    agent_manager.reinit_agent(db, config, name="dev", interaction=TtyInteractionPolicy.REFUSE)

    assert calls == [("agt-dev", "ws1"), ("agt-dev", "ws1")]  # ran unconditionally both times
    assert len(db.list_agent_grants("dev")) == 1  # no duplication
    assert (Role.RESULT, 0, "Agent 'dev' reinitialized") in captured_output.lines


def test_reinit_skips_stale_grant_and_reconciles_the_rest(
    db: Database,
    make_config,  # noqa: ANN001
    mutation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """A grant row pointing at a since-deleted workspace is an invariant
    violation (workspace delete should sweep its grants), but reinit is a
    repair command and must not crash on stale DB state. ``_resolve_ws_group``
    raises NotFoundError, which reinit catches per workspace: it warns, skips
    that workspace, and reconciles the rest. Simulated by making
    ``add_to_workspace_group`` raise NotFoundError for the stale workspace
    while the healthy one succeeds. Reinit completes without raising."""
    config = make_config()
    _seed_vm(db)
    _seed_granted_agent(db, ["good", "stale"])
    _reachable(monkeypatch, True)
    done: list[str] = []

    def _fake(vm: Any, config_: Any, db_: Any, linux_user: str, ws: str, **k: Any) -> None:
        if ws == "stale":
            raise NotFoundError("workspace 'stale' not found", entity_kind="workspace", entity_name="stale")
        done.append(ws)

    monkeypatch.setattr(agent_grants, "add_to_workspace_group", _fake)

    agent_manager.reinit_agent(db, config, name="dev", interaction=TtyInteractionPolicy.REFUSE)  # must not raise

    assert done == ["good"]  # the healthy grant was still reconciled
    assert any("stale" in w and "skipping stale grant" in w for w in captured_output.warnings)
    assert (Role.RESULT, 0, "Agent 'dev' reinitialized") in captured_output.lines


def test_reinit_reconciles_grant_all_agent_via_materialized_rows(
    db: Database,
    make_config,  # noqa: ANN001
    mutation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """A grant_all agent IS reconciled on reinit, from the recorded grant rows
    (``db.list_granted_workspaces``). This seeds a real grant_all agent (flag
    set, one 'explicit' row per workspace, the shape grant_all materializes) and
    asserts the reconcile invoked ``add_to_workspace_group`` for that workspace.
    Fails without the reconcile (same discriminator as the explicit-grant
    regression test). Also asserts the success summary line names the reconciled
    count (issue #280 item 5).

    Note: with a single workspace this does not isolate "reconcile from rows"
    from a hypothetical "reconcile from the grant_all flag via
    db.list_workspaces": under the maintained rows/live-set sync both produce
    the same call. That distinction only bites when the two diverge (issue
    #321); the reconcile-from-rows design choice is documented at the call
    site."""
    config = make_config()
    _seed_vm(db)
    _seed_grant_all_agent(db, ["ws1"])
    _reachable(monkeypatch, True)
    group_adds: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agent_grants,
        "add_to_workspace_group",
        lambda vm, config_, db_, linux_user, ws, **k: group_adds.append((linux_user, ws)),
    )

    agent_manager.reinit_agent(db, config, name="dev", interaction=TtyInteractionPolicy.REFUSE)

    row = db.get_agent("dev")
    assert row is not None and row.grant_all  # the seed is a real grant_all agent
    assert group_adds == [("agt-dev", "ws1")]  # reconciled via the materialized row
    # Summary line present when grants were reconciled (N > 0).
    assert "Reconciled 1 workspace grant" in captured_output.info
