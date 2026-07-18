"""The tracer bullet: ``vm add-git-credential``, the first command
migrated onto the orchestrated model.

Real config, registry, resolver, and backend loop (env-var backend);
the platform's backend API, the network probes, and the SSH transport
are the only fakes. The proof points pinned for the first migrated
command:

(a) the DERIVED graph reproduces the imperative preflight set and
    secret union, with zero hand-wired edges;
(b) a runup rejection is FATAL, matching HEAD;
(c) SCOPED DELIVERY: a node reads only its declared secret names;
(d) the operation scope reaches a node's readiness;
(e) GATE-PROMPT parity: nothing resolves or prompts twice, the gate's
    just-in-time resolve seeds the boundary (proxmox's ``status``
    reads the token pre-boundary), and the operator-stopped refusal
    covers the re-read race guard.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.git_credential.github import GitHubCredentialProvider
from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
from agentworks.db import VMStatus
from agentworks.errors import StateError, TokenRejectedError, ValidationError
from agentworks.vms import manager as vm_manager
from tests.orchestrated_fixtures import PROXMOX_SECTION, write_operator_config

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.db import Database, VMRow

GIT_CRED_SECTION = """
[git_credentials.gh]
provider = "github"
"""


@pytest.fixture
def make_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """This suite's ``make_config`` delta from the shared fixture: the
    git token in the env, and the default body REPLACED per test (the
    proxmox section is part of the default, not baked in)."""
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GH", "ghtok")

    def _make(extra: str = PROXMOX_SECTION + GIT_CRED_SECTION):
        return write_operator_config(tmp_path, extra)

    return _make


class _FakeTarget:
    def __init__(self, existing: str = "") -> None:
        self.runs: list[str] = []
        self.writes: list[tuple[str, str, str | None]] = []
        self._existing = existing

    def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
        self.runs.append(cmd)
        if cmd.startswith("cat"):
            return SimpleNamespace(stdout=self._existing)
        return SimpleNamespace(stdout="")

    def write_file(self, path: str, content: str, mode: str | None = None) -> None:
        self.writes.append((path, content, mode))


@pytest.fixture
def target(monkeypatch: pytest.MonkeyPatch) -> _FakeTarget:
    import agentworks.transports as transports

    fake = _FakeTarget()
    monkeypatch.setattr(transports, "transport", lambda vm, config: fake)
    return fake


@pytest.fixture
def verified_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The github runup probe answers 200 (token verified)."""

    def _probe(url: str, headers: dict[str, str], *, timeout: float = 5.0):
        return (200, b'{"login": "x"}', {})

    monkeypatch.setattr(
        "agentworks.capabilities.git_credential.base._http_probe", _probe
    )


def _seed_vm(db: Database, *, tailscale: str | None = "100.64.0.9") -> VMRow:
    db.insert_vm("box", site="proxmox", hostname="box")
    if tailscale:
        db.update_vm_tailscale("box", tailscale)
    vm = db.get_vm("box")
    assert vm is not None
    return vm


def _reachable(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(
        vm_manager, "_is_tailscale_reachable", lambda host: value
    )


# -- the command end to end --------------------------------------------------


def test_orchestrated_add_writes_the_credential(
    db: Database,
    make_config,
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    verified_token: None,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    """The happy path on a running, reachable VM: one boundary resolve
    pass covering the whole union (gate fast path costs nothing), the
    store line written, the helper slot preserved."""
    config = make_config()
    vm = _seed_vm(db)
    _reachable(monkeypatch, True)

    vm_manager.add_git_credential(db, config, vm.name, "gh")

    assert len(resolve_counter) == 1
    assert sorted(resolve_counter[0]) == ["git-token-gh", "proxmox-token"]
    ((path, content, mode),) = target.writes
    assert path == "~/.git-credentials"
    assert content.splitlines()[0] == "https://x-access-token:ghtok@github.com"
    assert mode == "600"
    helper_cmd = target.runs[-1]
    assert "if [ -x ~/.agentworks-git-cred-helper.sh ]" in helper_cmd
    assert "--replace-all credential.helper" in helper_cmd


def test_scoped_credential_refused_before_any_resolve_or_gate(
    db: Database,
    make_config,
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oracle guard preserved: a scoped credential is refused toward
    reinit (the single-line merge cannot rebuild the helper's selection
    map) before any prompt, secret, or VM start. The VM deliberately
    sits on an UNDECLARED site: at HEAD the site bound only after this
    guard, so the scoped refusal must still win over the site error."""
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "creds.yaml").write_text(
        "apiVersion: agentworks/v1\n"
        "kind: git-credential\n"
        "metadata:\n"
        "  name: widgets-bot\n"
        "spec:\n"
        "  provider: github\n"
        "  provider_config:\n"
        "    repos: [acme/widgets]\n"
    )
    config = make_config(PROXMOX_SECTION)
    db.insert_vm("box", site="ghost-site", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")

    def _no_status(self: object, row: object) -> VMStatus:
        raise AssertionError("gate ran for a refused scoped credential")

    monkeypatch.setattr(ProxmoxPlatform, "status", _no_status)
    with pytest.raises(ValidationError, match="scoped"):
        vm_manager.add_git_credential(db, config, "box", "widgets-bot")
    assert resolve_counter == []
    assert target.writes == []


# -- proof point (a): the derived graph --------------------------------------


def test_graph_derives_from_row_and_declared_references(
    db: Database, make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The translation rule end to end: the VM row's ``site`` field
    becomes the edge to the ``vm-site`` node (which HOLDS the platform
    instance), the credential decl's provider reference becomes the
    HELD provider instance, secret references become ``secret_refs``,
    and nothing is hand-wired. The walk's set and the secret union
    reproduce what the imperative composition covered (site config
    secrets plus the credential token)."""
    from agentworks.bootstrap import build_registry
    from agentworks.git_credentials.nodes import git_credential_node
    from agentworks.orchestration.node import Node
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.vms.nodes import live_vm_node

    config = make_config()
    vm = _seed_vm(db)
    registry = build_registry(config)

    cred = git_credential_node(registry, "gh")
    vm_node = live_vm_node(db, config, registry, vm)
    nodes = walk(vm_node, cred)

    assert [n.key for n in nodes] == ["vm-site/proxmox", "vm/box", "git-credential/gh"]
    assert all(isinstance(n, Node) for n in nodes)
    # Row field -> live edge, one object per node (the site node the VM
    # depends on IS the walked one).
    assert vm_node.deps()[0] is nodes[0]
    # Held instances, not nodes: the platform and provider are off the
    # graph, composed by their holders.
    assert isinstance(nodes[0].platform, ProxmoxPlatform)  # type: ignore[attr-defined]
    assert isinstance(cred.provider, GitHubCredentialProvider)
    # The union the one resolve pass must cover, from declarations.
    assert secret_union(nodes) == ("proxmox-token", "git-token-gh")
    # The gate's eager needs are exactly the site's declared config
    # secrets; the credential token is boundary-only.
    assert vm_node.gate_secret_refs() == ("proxmox-token",)


# -- proof point (b): fatal runup --------------------------------------------


def test_runup_rejection_is_fatal_and_writes_nothing(
    db: Database,
    make_config,
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    config = make_config()
    vm = _seed_vm(db)
    _reachable(monkeypatch, True)

    def _probe(url: str, headers: dict[str, str], *, timeout: float = 5.0):
        return (401, b"", {})

    monkeypatch.setattr(
        "agentworks.capabilities.git_credential.base._http_probe", _probe
    )
    with pytest.raises(TokenRejectedError, match="rejected the token"):
        vm_manager.add_git_credential(db, config, vm.name, "gh")
    assert target.writes == []
    assert target.runs == []


# -- proof point (c): scoped delivery ----------------------------------------


def test_nodes_receive_only_their_declared_secrets(
    db: Database,
    make_config,
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    """The credential node's runup context serves its own token and
    REFUSES the site's secret: delivery is scoped to declared names on
    the real command path (guarding the whole-cache fallback from
    quietly becoming permanent)."""
    config = make_config()
    vm = _seed_vm(db)
    _reachable(monkeypatch, True)
    seen: dict[str, str] = {}

    def _probing_runup(self: GitHubCredentialProvider, ctx: RunContext) -> None:
        with pytest.raises(StateError, match="not declared"):
            ctx.secret("proxmox-token")
        seen["token"] = ctx.secret(self.secret_name)

    monkeypatch.setattr(GitHubCredentialProvider, "runup", _probing_runup)
    vm_manager.add_git_credential(db, config, vm.name, "gh")
    assert seen["token"] == "ghtok"


# -- proof point (d): the operation scope reaches readiness ------------------


def test_operation_scope_reaches_node_readiness(
    db: Database,
    make_config,
    target: _FakeTarget,
    verified_token: None,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    from agentworks.capabilities.base import ScopeLevel

    config = make_config()
    vm = _seed_vm(db)
    _reachable(monkeypatch, True)
    scopes: list[OperationScope | None] = []
    real = GitHubCredentialProvider.preflight

    def _recording(self: GitHubCredentialProvider, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(GitHubCredentialProvider, "preflight", _recording)
    vm_manager.add_git_credential(db, config, vm.name, "gh")

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.VM
    assert scope.vm == "box"
    assert scope.workspace is None and scope.agent is None


# -- proof point (e): gate-prompt parity -------------------------------------


def test_stopped_vm_gate_resolves_once_and_seeds_the_boundary(
    db: Database,
    make_config,
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    verified_token: None,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    """The seam the tracer exists to prove: a stopped VM's status probe
    needs the API token BEFORE the boundary. The gate resolves it
    just-in-time and the platform's op reads it through the CONTEXT
    (``ctx.secret``, the gate's scoped reader: the op-client bridge is
    dead), the boundary pass covers only the remainder, and no secret
    resolves twice. Both resolutions precede the walk-away point. The
    reader is SCOPED: an undeclared name (the credential's token,
    which is boundary-only) is refused at the op."""
    config = make_config()
    vm = _seed_vm(db)
    _reachable(monkeypatch, False)
    events: list[str] = []

    def _status(self: ProxmoxPlatform, row: VMRow, ctx: RunContext) -> VMStatus:
        # The real proxmox status builds its API client via ctx.secret;
        # prove that read works pre-boundary, and that delivery is
        # scoped: a name outside the gate's declared set refuses.
        with pytest.raises(StateError, match="not declared"):
            ctx.secret("git-token-gh")
        events.append(f"status-token:{ctx.secret('proxmox-token')}")
        return VMStatus.STOPPED

    monkeypatch.setattr(ProxmoxPlatform, "status", _status)
    monkeypatch.setattr(
        ProxmoxPlatform, "start", lambda self, row, ctx: events.append("start")
    )
    monkeypatch.setattr(
        vm_manager, "_ensure_tailscale", lambda *a, **k: events.append("tailscale")
    )

    vm_manager.add_git_credential(db, config, vm.name, "gh")

    assert events == ["status-token:pve-token", "start", "tailscale"]
    # Exactly two backend passes (gate, then boundary), no name twice,
    # nothing after the boundary (a post-boundary read would be a third).
    assert resolve_counter == [["proxmox-token"], ["git-token-gh"]]
    assert target.writes  # the command completed


def test_operator_stopped_vm_refuses_via_the_reread_race_guard(
    db: Database,
    make_config,
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    """The refusal's re-read race guard, through the real command: the
    row is loaded un-stopped, a concurrent `vm stop` lands during the
    status probe, and the gate refuses on the RE-READ flag rather than
    the stale row. The refusal is the node's typed error with the
    explicit-start hint; nothing starts, nothing is written."""
    config = make_config()
    vm = _seed_vm(db)
    _reachable(monkeypatch, False)
    started: list[str] = []

    def _status(self: ProxmoxPlatform, row: VMRow, ctx: RunContext) -> VMStatus:
        db.set_operator_stopped("box", True)  # the concurrent `vm stop`
        return VMStatus.STOPPED

    monkeypatch.setattr(ProxmoxPlatform, "status", _status)
    monkeypatch.setattr(
        ProxmoxPlatform, "start", lambda self, row, ctx: started.append("start")
    )

    with pytest.raises(StateError, match="manually stopped") as exc:
        vm_manager.add_git_credential(db, config, vm.name, "gh")
    assert "agw vm start box" in (exc.value.hint or "")
    assert started == []
    assert target.writes == []
    # The gate's just-in-time resolve ran (the status probe needed the
    # token); the boundary never did (refusal precedes it).
    assert resolve_counter == [["proxmox-token"]]
