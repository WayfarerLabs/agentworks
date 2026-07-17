"""Spike scenario tests (FRD R11): `vm create` and `session create
--new-agent`, asserted against the behavior the imperative code at HEAD
exhibits (citations inline; the expected values are read off the
manager code, since the spike cannot run real provisioning).

Run from `cli/`:  uv run pytest ../docs/sdd/2026-07-16-orchestration-layer/spike/ -q
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from textwrap import dedent

import pytest
from spike import (
    AgentTemplateNode,
    CapabilityInstanceNode,
    HarnessStubNode,
    LiveVMNode,
    Node,
    PendingAgentNode,
    PendingVMNode,
    PendingWorkspaceNode,
    RealizationLog,
    SpikeError,
    VMTemplateNode,
    preflight_all,
    secret_union,
    walk,
)

from agentworks.bootstrap import build_registry
from agentworks.capabilities.base import RunContext
from agentworks.capabilities.git_credential.github import GitHubCredentialProvider
from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
from agentworks.config import load_config
from agentworks.db import VMRow
from agentworks.errors import ConfigError
from agentworks.secrets.resolver import Resolver
from agentworks.vms.templates import resolve_template

_PROXMOX_CFG = {
    "api_url": "https://pve.example:8006",
    "node": "n1",
    "token_id": "agw@pam!ci",
    "template_vmid": 900,
}


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Real config, registry, and resolver (no stubs), with every secret
    the scenarios declare resolvable via the env backend."""
    pub = tmp_path / "k.pub"
    priv = tmp_path / "k"
    pub.write_text("ssh-ed25519 AAAA spike")
    priv.write_text("key")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        [git_credentials.gh]
        provider = "github"

        # env-only chain: prediction is deterministic (the prompt backend
        # reports every secret resolvable without probing, which is right
        # for operators and wrong for a spike oracle).
        [secret_config]
        backends = ["env-var"]
        """)
    )
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "ts-key")
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "px-token")
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GH", "gh-token")
    config = load_config(cfg_path, warn_issues=False)
    registry = build_registry(config)
    resolver = Resolver(config, registry)
    return config, registry, resolver


def _vm_row(name: str = "box") -> VMRow:
    return VMRow(
        name=name,
        site="px",
        template="default",
        extra_packages=[],
        provisioning_status="provisioned",
        init_status="initialized",
        tailscale_host=f"{name}.tail.example",
        cpus=4,
        memory_gib=8,
        disk_gib=50,
        swap_gib=4,
        admin_username="awadmin",
        hostname=name,
        created_at="2026-07-17T00:00:00Z",
        last_seen_at=None,
    )


# -- Scenario A: vm create ----------------------------------------------------


def _scenario_a(world):
    config, registry, resolver = world
    template = VMTemplateNode(resolve_template(registry), resolver)
    platform = CapabilityInstanceNode(ProxmoxPlatform("px", _PROXMOX_CFG, resolver))
    gh = CapabilityInstanceNode(GitHubCredentialProvider("gh", {}, resolver))
    vm = PendingVMNode("demo", _deps=(template, platform, gh))
    return config, resolver, template, platform, gh, vm


def test_a_walk_reproduces_create_vm_preflight_set(world) -> None:
    """Oracle: create_vm preflights the vm-template, the site's platform,
    and each git-credential provider before the resolve
    (vms/manager.py, the Preflight phase). The walk derives the same
    set from declared edges, with the pending VM last (deps first)."""
    config, resolver, template, platform, gh, vm = _scenario_a(world)
    order = walk(vm)
    assert [n.key for n in order] == [
        "vm-template/default",
        "vm-site/px",
        "git-credential/gh",
        "vm/demo",
    ]
    assert all(isinstance(n, Node) for n in order)  # protocol conformance
    preflight_all(order, RunContext(config=config))  # no error, no prompt


def test_a_secret_union_matches_the_one_resolve_pass(world) -> None:
    """Oracle: create_vm's single resolve covers the template's Tailscale
    key, the platform's API token, and each provider's PAT (one prompt
    session). Central derivation over the plan yields the same union."""
    _, _, template, platform, gh, vm = _scenario_a(world)
    assert secret_union(walk(vm)) == {
        "tailscale-auth-key",
        "proxmox-token",
        "git-token-gh",
    }


def test_a_template_readiness_is_real(world, monkeypatch) -> None:
    """The template node's preflight is today's preflight_vm_template
    doing real prediction: an unresolvable key still fails loudly."""
    config, resolver, template, *_ = _scenario_a(world)
    monkeypatch.delenv("AW_SECRET_TAILSCALE_AUTH_KEY")
    with pytest.raises(ConfigError, match="tailscale-auth-key"):
        template.preflight(RunContext(config=config))


def test_a_unwind_matches_create_vm_rollback(world) -> None:
    """Oracle: create_vm's rollback deletes the VM's DB record on a
    provisioning failure (vms/manager.py, best-effort). Realize the VM,
    inject a failure, unwind: just the VM, torn down."""
    *_, vm = _scenario_a(world)
    log = RealizationLog()
    log.realize(vm)
    assert log.unwind() == ["vm/demo"]
    assert vm.torn_down


def test_a_shared_platform_node_is_walked_once(world) -> None:
    """Oracle: bind_platforms dedupes by site so one platform instance
    serves every VM on that site. Memoization gives the same property:
    two pending VMs, one platform visit."""
    _, resolver, template, platform, gh, _ = _scenario_a(world)
    vm1 = PendingVMNode("a", _deps=(template, platform))
    vm2 = PendingVMNode("b", _deps=(template, platform))
    keys = [n.key for n in walk(vm1, vm2)]
    assert keys.count("vm-site/px") == 1


# -- Scenario B: session create --new-agent -----------------------------------


def _scenario_b(world):
    config, registry, resolver = world
    platform = CapabilityInstanceNode(ProxmoxPlatform("px", _PROXMOX_CFG, resolver))
    live_vm = LiveVMNode(_vm_row("box"), platform)
    gh = CapabilityInstanceNode(GitHubCredentialProvider("gh", {}, resolver))
    agent_tmpl = AgentTemplateNode("t-dev", providers=(gh,))
    workspace = PendingWorkspaceNode("ws1", _deps=(live_vm,))
    agent = PendingAgentNode(
        "dev", _deps=(agent_tmpl, live_vm), vm_name="box", workspace_name="ws1"
    )
    harness = HarnessStubNode(
        session_name="s1", target=agent, required_commands=("claude", "git")
    )
    session = PendingVMNode("s1", _deps=(harness, agent, workspace, live_vm))
    return config, resolver, live_vm, gh, agent_tmpl, workspace, agent, harness, session


def test_b_walk_pulls_agent_git_credentials_through_the_edge(world) -> None:
    """Oracle: create_session hand-constructs the ephemeral agent's git
    providers and preflights them (sessions/manager.py, the fold that
    threads git_tokens into create_agent). Here they enter the plan
    only through agent -> agent-template -> provider edges; the command
    names none of them."""
    config, _, live_vm, gh, agent_tmpl, workspace, agent, harness, session = _scenario_b(world)
    keys = [n.key for n in walk(session)]
    assert "git-credential/gh" in keys
    assert keys.index("git-credential/gh") < keys.index("agent/dev")
    assert secret_union(walk(session)) == {"git-token-gh", "proxmox-token"}


def test_b_harness_defers_at_preflight_and_fires_once_at_runup(world) -> None:
    """Oracle: create_session probes required_commands AFTER the
    ephemeral agent exists (sessions/manager.py, post-create_agent);
    the harness SDD wanted the same check pre-resolve for existing
    targets. Pending-ness gives both: defer while the agent node is
    pending, fire exactly once when realized, no to_create field."""
    config, resolver, live_vm, gh, agent_tmpl, workspace, agent, harness, session = _scenario_b(world)
    order = walk(session)
    preflight_all(order, RunContext(config=config))
    assert harness.deferred and not harness.probes  # pending -> deferred

    log = RealizationLog()
    log.realize(workspace)
    log.realize(agent)
    resolver.resolve()  # env-backed, promptless: the one resolve pass
    runup_ctx = RunContext(config=config, secrets=resolver)
    harness.runup(runup_ctx)
    harness.runup(runup_ctx)  # idempotent: still exactly one probe
    assert len(harness.probes) == 1

    probe = harness.probes[0]
    # Injected identity (session) + intrinsic identity (agent chain):
    assert probe.session_name == "s1"
    assert (probe.agent_name, probe.vm_name, probe.workspace_name) == (
        "dev",
        "box",
        "ws1",
    )


def test_b_existing_target_probes_at_preflight(world) -> None:
    """The other half of the float: an EXISTING agent probes at
    preflight, pre-resolve (the harness SDD's earlier-failure win)."""
    config, *_ = _scenario_b(world)
    agent = PendingAgentNode("dev", vm_name="box", workspace_name="ws1")
    agent.realize()
    harness = HarnessStubNode("s2", target=agent, required_commands=("claude",))
    harness.preflight(RunContext(config=config))
    assert not harness.deferred and len(harness.probes) == 1


def test_b_missing_target_is_loud_never_a_skip(world) -> None:
    """Anti-silent-skip (FRD R3): absent-for-another-reason raises."""
    config, *_ = _scenario_b(world)
    harness = HarnessStubNode("s3", target=None, required_commands=("claude",))
    with pytest.raises(SpikeError, match="refusing to skip"):
        harness.preflight(RunContext(config=config))


def test_b_unwind_matches_rollback_ephemerals(world) -> None:
    """Oracle: _rollback_ephemerals deletes the ephemeral agent, then
    the workspace (sessions/manager.py), the reverse of their creation
    order (workspace first, then agent). Reverse realization order
    reproduces it with no hand-rolled function."""
    *_, workspace, agent, harness, session = _scenario_b(world)
    log = RealizationLog()
    log.realize(workspace)
    log.realize(agent)
    # Failure at harness runup / session mutation: session never realized.
    assert log.unwind() == ["agent/dev", "workspace/ws1"]
    assert agent.torn_down and workspace.torn_down


def test_cycles_are_loud(world) -> None:
    a = PendingVMNode("a")
    b = PendingVMNode("b", _deps=(a,))
    a._deps = (b,)
    with pytest.raises(SpikeError, match="cycle"):
        walk(a)
