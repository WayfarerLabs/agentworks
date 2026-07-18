"""``agent create`` / ``agent reinit`` through the orchestrated model:
the derived graph, the gate-prompt parity carry (the tracer's mirror
shape), the banner and failure parity with the imperative commands, and
the grant-all reconciliation riding the realization body.

Real config, registry, resolver, and backend loop (env-var backend);
the platform's backend ops, the reachability probe, the transports,
and the on-VM mutation (``_create_agent_on_vm``) are the fakes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from agentworks.agents import manager as agent_manager
from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
from agentworks.config import load_config
from agentworks.errors import ExternalError
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.db import Database

PROXMOX_SECTION = """
[proxmox]
api_url = "https://pve:8006"
node = "pve1"
token_id = "agw@pam!agw"
template_vmid = 9000
"""

AGENT_SECTION = """
[git_credentials.gh]
provider = "github"

[agent_templates.default]
git_credentials = ["gh"]
"""


@pytest.fixture
def make_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    key = tmp_path / "id_ed25519"
    key.write_text("private")
    (tmp_path / "id_ed25519.pub").write_text("public")
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GH", "ghtok")

    def _make():  # noqa: ANN202
        path = tmp_path / "config.toml"
        path.write_text(
            f'[operator]\nssh_public_key = "{key}.pub"\nssh_private_key = "{key}"\n'
            + PROXMOX_SECTION
            + AGENT_SECTION
        )
        return load_config(path, warn_issues=False, warn_deprecations=False)

    return _make


@pytest.fixture
def resolve_counter(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every backend-loop pass (the prompt-session oracle)."""
    from agentworks.secrets import resolve as secrets_resolve

    calls: list[list[str]] = []
    real = secrets_resolve.resolve_secrets

    def _counting(secrets: list[object], *args: object, **kwargs: object) -> dict[str, str]:
        calls.append([getattr(s, "name", str(s)) for s in secrets])
        return real(secrets, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(secrets_resolve, "resolve_secrets", _counting)
    return calls


@pytest.fixture
def mutation(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake the on-VM mutation and the SSH-config refresh; capture what
    the orchestrator hands the body."""
    captured: dict[str, Any] = {}

    def _fake_mutation(*args: Any, **kwargs: Any) -> None:
        captured["git_tokens"] = kwargs["git_tokens"]
        captured["agent_name"] = kwargs["agent_name"]

    monkeypatch.setattr(agent_manager, "_create_agent_on_vm", _fake_mutation)
    monkeypatch.setattr(
        "agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None
    )
    return captured


def _seed_vm(db: Database) -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")


def _reachable(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: value)


def _stop_the_vm(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    from agentworks.db import VMStatus as _VMStatus

    _reachable(monkeypatch, False)
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, row: events.append("status") or _VMStatus.STOPPED,
    )
    monkeypatch.setattr(
        ProxmoxPlatform, "start", lambda self, row: events.append("start")
    )
    monkeypatch.setattr(
        vm_manager, "_ensure_tailscale", lambda *a, **k: events.append("tailscale")
    )


# -- the derived graph --------------------------------------------------------


def test_create_graph_derives_from_template_and_row(
    db: Database, make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node

    config = make_config()
    _seed_vm(db)
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)
    resolver = Resolver(config, registry)

    vm_node = live_vm_node(db, config, registry, vm, resolver)
    tmpl_node = agent_template_node(
        registry, resolve_template(registry, None), resolver
    )
    pending = pending_agent_node(db, config, "dev", tmpl_node, vm_node)
    nodes = walk(pending)

    assert [n.key for n in nodes] == [
        "git-credential/gh",
        "agent-template/default",
        "vm-site/proxmox",
        "vm/box",
        "agent/dev",
    ]
    assert secret_union(nodes) == ("git-token-gh", "proxmox-token")


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

    agent_manager.create_agent(db, config, name="dev", vm_name="box")

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
    assert "Checking agent-template/default..." in captured_output.detail
    assert "Checking git-credential/gh..." in captured_output.detail


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

    agent_manager.reinit_agent(db, config, name="dev")

    assert resolve_counter == [["proxmox-token"], ["git-token-gh"]]
    assert events == ["status", "start", "tailscale"]
    assert mutation["git_tokens"] == {"gh": "ghtok"}
    assert mutation["agent_name"] == "dev"
    assert any("reinitialized" in m for m in captured_output.info)


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
    monkeypatch.setattr(
        "agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None
    )

    def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("ssh exploded")

    monkeypatch.setattr(agent_manager, "_create_agent_on_vm", _boom)
    deletes: list[str] = []
    monkeypatch.setattr(
        agent_manager,
        "_delete_agent_on_vm",
        lambda vm, config_, linux_user, **k: deletes.append(linux_user),
    )

    with pytest.raises(ExternalError, match="creating agent: ssh exploded"):
        agent_manager.create_agent(db, config, name="dev", vm_name="box")

    assert deletes == ["agt-dev"]  # the body's partial-state cleanup ran
    assert db.get_agent("dev") is None


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

    monkeypatch.setattr(agent_manager, "_create_agent_on_vm", _boom)

    with pytest.raises(ExternalError, match="reinitializing agent: ssh exploded"):
        agent_manager.reinit_agent(db, config, name="dev")

    assert db.get_agent("dev") is not None  # re-runnable, as before


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

    agent_manager.create_agent(db, config, name="dev", vm_name="box")

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
        agent_manager,
        "_add_to_workspace_group",
        lambda vm, config_, db_, linux_user, ws, **k: group_adds.append(
            (linux_user, ws)
        ),
    )

    agent_manager.create_agent(
        db, config, name="dev", vm_name="box", grant_all_workspaces=True
    )

    row = db.get_agent("dev")
    assert row is not None and row.grant_all
    assert group_adds == [("agt-dev", "ws1")]
    assert db.has_any_grant("dev", "ws1")
