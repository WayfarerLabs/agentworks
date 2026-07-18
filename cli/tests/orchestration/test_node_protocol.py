"""The ``Readiness`` / ``Node`` split.

Capability instances satisfy ``Readiness`` ONLY: no ``key``, no
``deps``, so they are structurally not nodes and cannot be walked.
Only consuming and live resources implement ``Node``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from agentworks.capabilities import Capability
from agentworks.capabilities.base import RunContext
from agentworks.capabilities.git_credential.github import GitHubCredentialProvider
from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
from agentworks.orchestration.node import CreatableNode, Node, Readiness

_PROXMOX_CONFIG = {
    "api_url": "https://pve:8006",
    "node": "n",
    "token_id": "t",
    "template_vmid": 1,
}


@dataclass
class _FakeNode:
    """A minimal consuming-resource-shaped node."""

    key: str
    _deps: tuple[_FakeNode, ...] = ()
    _secret_refs: tuple[str, ...] = ()

    def deps(self) -> tuple[_FakeNode, ...]:
        return self._deps

    def secret_refs(self) -> tuple[str, ...]:
        return self._secret_refs

    def preflight(self, ctx: RunContext) -> None: ...

    def runup(self, ctx: RunContext) -> None: ...


@dataclass
class _FakeCreatableNode(_FakeNode):
    realized: bool = field(default=False)
    torn_down: bool = field(default=False)

    def mark_realized(self) -> None:
        self.realized = True

    def teardown(self) -> None:
        self.torn_down = True


class _PlainCap(Capability):
    name: ClassVar[str] = "plain"
    description: ClassVar[str] = "no config"
    owner_kind: ClassVar[str] = "thing"


def test_capability_base_is_readiness_only() -> None:
    cap = _PlainCap("t1", {})
    assert isinstance(cap, Readiness)
    assert not isinstance(cap, Node)


def test_real_capability_instances_are_readiness_not_nodes() -> None:
    """The shipped capability implementations gained nothing
    node-shaped: a provider or platform instance is HELD by its
    consuming-resource node and composed, never walked."""
    provider = GitHubCredentialProvider("gh", {})
    platform = ProxmoxPlatform("px", _PROXMOX_CONFIG)
    for instance in (provider, platform):
        assert isinstance(instance, Readiness)
        assert not isinstance(instance, Node)
        assert not hasattr(instance, "key")
        assert not hasattr(instance, "deps")


def test_a_node_satisfies_both_contracts() -> None:
    node = _FakeNode(key="git-credential/gh")
    assert isinstance(node, Readiness)
    assert isinstance(node, Node)
    assert not isinstance(node, CreatableNode)


def test_creatable_node_adds_the_teardown_surface() -> None:
    node = _FakeCreatableNode(key="vm/box")
    assert isinstance(node, Node)
    assert isinstance(node, CreatableNode)
    node.teardown()
    assert node.torn_down
