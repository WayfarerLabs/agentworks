"""FR17: an inheritance edge is source composition, not a runtime need.

The regression this file exists for, end to end over a real registry: a
child template that OVERRIDES the parent's default secret name must not,
by any runtime-need traversal, be reported as needing the parent's
default. Before the split it was, through the transitive walk across
``inherits``, and the failure mode was an operator prompted for a secret
nothing uses.

``vm-template`` is the fixture surface because its ``tailscale_auth_key``
IS an owner-defaulted secret name on a shipped, inheriting kind, so the
case is the real one rather than a constructed analogue.

The producing half (a child's edges coming off its merged declaration) is
pinned by ``tests/resources/test_effective_template_dependencies.py``; neither half
is correct alone, so both files assert absences.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import StateError
from agentworks.resources import Origin, Registry, collect_secrets_for
from agentworks.resources.access import ensure_recipe_enabled
from agentworks.resources.graph import DisabledMark, Enablement, EnablementSource
from agentworks.resources.reference import RefRelationship
from agentworks.vms.template import VMTemplate
from tests.conftest import ManifestDoc
from tests.resources.test_graph import _write_cfg

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def inheriting_registry(tmp_path: Path) -> Registry:
    """``base`` keeps the kind's default auth-key name and declares an env
    secret; ``kid`` inherits it and overrides the auth-key name only."""
    cfg = _write_cfg(
        tmp_path,
        "",
        ManifestDoc("vm-template", "base", {"env": {"BASE": {"secret": "base-env-secret"}}}),
        ManifestDoc("vm-template", "kid", {"inherits": ["base"], "tailscale_auth_key": "kid-auth-key"}),
    )
    return build_registry(load_config(cfg, warn_issues=False))


def test_the_child_declares_the_override_and_the_parent_keeps_its_own_default(
    inheriting_registry: Registry,
) -> None:
    graph = inheriting_registry.graph
    child_secrets = {ref.name for ref in graph.edges_of("vm-template", "kid") if ref.kind == "secret"}
    parent_secrets = {ref.name for ref in graph.edges_of("vm-template", "base") if ref.kind == "secret"}

    assert "kid-auth-key" in child_secrets
    assert "tailscale-auth-key" not in child_secrets
    # The parent's edge to its own default describes the parent's standalone
    # use and is untouched by what the child did.
    assert "tailscale-auth-key" in parent_secrets


def test_the_inheritance_edge_is_typed_as_such_and_is_the_only_one(
    inheriting_registry: Registry,
) -> None:
    edges = inheriting_registry.graph.edges_of("vm-template", "kid")
    inherited = [ref for ref in edges if ref.relationship is RefRelationship.INHERITS]
    assert [(ref.kind, ref.name) for ref in inherited] == [("vm-template", "base")]


def test_no_runtime_need_traversal_attributes_the_parents_default_to_the_child(
    inheriting_registry: Registry,
) -> None:
    """The three surfaces FR17 names, over the one registry.

    The secret union and the resolvability prediction both read a node's
    declared secret references rather than the graph, so what has to be
    checked of them is the reference set they are given; the closure is
    where the crossing used to happen.

    Both halves of the split are here, as EQUALITY rather than
    containment, which is what makes them one assertion: the parent's
    ``base-env-secret`` has to still reach the child (cutting the edge
    without the child publishing its merged declaration's needs would
    silently drop it), and it has to reach the child exactly once.
    """
    graph = inheriting_registry.graph

    # 1. The graph closure the secret projection walks.
    reachable = {name for kind, name in graph.runtime_reachable_from("vm-template", "kid") if kind == "secret"}
    assert reachable == {"kid-auth-key", "base-env-secret"}

    # 2. The secret union / eager-resolve collection built on it.
    assert {decl.name for decl in collect_secrets_for(inheriting_registry, ("vm-template", "kid"))} == {
        "kid-auth-key",
        "base-env-secret",
    }

    # 3. The prediction input, which is a node's own declared references.
    from agentworks.vms.nodes import vm_template_node
    from agentworks.vms.templates import resolve_template

    node = vm_template_node(resolve_template(inheriting_registry, "kid"))
    assert {ref.name for ref in node.config_secret_refs()} == {"kid-auth-key"}


def _disabling(*keys: tuple[str, str]) -> EnablementSource:
    return lambda _rows: dict.fromkeys(keys, DisabledMark(reason="enable its unit", source="test"))


def test_enablement_still_propagates_across_the_inheritance_edge() -> None:
    """FR17's policy call, asserted rather than left implicit: the recipe
    use-gate crosses the inheritance edge on purpose, because a parent
    template is source about to be compiled into the child's recipe, not a
    runtime need the child happens to have. Disabling the parent must
    therefore refuse the child at use.

    Built through ``finalize``'s own enablement-source seam rather than a
    fixture plugin, so what is under test is the gate and not a plugin's
    opt-in wiring.
    """
    registry = Registry.empty()
    origin = Origin.built_in(source="tests.inheritance")
    registry.add("vm-template", "base", VMTemplate(name="base"), origin)
    registry.add("vm-template", "kid", VMTemplate(name="kid", inherits=["base"]), origin)
    registry.finalize([_disabling(("vm-template", "base"))])

    # The child is enabled and names nothing disabled of its own; what
    # refuses it is the parent it inherits, reached across the edge.
    assert registry.graph.enablement_of("vm-template", "kid") is Enablement.enabled
    with pytest.raises(StateError):
        ensure_recipe_enabled(registry, "vm-template", "kid")


def test_the_gate_does_not_refuse_over_an_ancestor_leaf_the_child_overrode() -> None:
    """The other edge of the same policy, and the one a crosses-everything
    closure got wrong: the recipe is what the child NEEDS plus what it is
    MADE OF, not everything an ancestor happens to touch.

    ``kid`` renamed the auth key it inherits, so the parent's own key is in
    nothing the child runs. Refusing over it would be an operator blocked
    on a secret no part of the recipe reads, and it fails SAFE, so nothing
    but a test would ever surface it.
    """
    from agentworks.secrets.base import SecretDecl

    registry = Registry.empty()
    origin = Origin.built_in(source="tests.inheritance")
    registry.add("vm-template", "base", VMTemplate(name="base"), origin)
    registry.add("vm-template", "kid", VMTemplate(name="kid", inherits=["base"], tailscale_auth_key="kid-key"), origin)
    # Published rather than left to auto-declaration, because a row the
    # materialize pass synthesizes is not in the enablement map the fold
    # built and so cannot carry a mark.
    registry.add("secret", "tailscale-auth-key", SecretDecl(name="tailscale-auth-key", description=""), origin)
    registry.finalize([_disabling(("secret", "tailscale-auth-key"))])

    assert ("secret", "tailscale-auth-key") in registry.graph.runtime_reachable_from("vm-template", "base")
    ensure_recipe_enabled(registry, "vm-template", "kid")  # no raise
    # The parent, which does use it, is still refused: the gate narrowed
    # rather than stopped gating.
    with pytest.raises(StateError):
        ensure_recipe_enabled(registry, "vm-template", "base")
