"""``merge_layers`` order is a contract, and the thing it must agree with
is each kind's own resolver.

Callers read "the last layer that declared X" off this order to decide
which template an edge belongs to. If the order diverged from the merge
the resolver actually performs, an edge would be attributed to a layer
whose value the resolver overrode: a wrong answer that nothing else would
catch, since both orders produce a plausible-looking name.

So each kind is pinned by FOLDING the layers and getting the resolver's
own merged result, rather than by restating the expected order, which
would only pin this module against itself.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

import pytest

from agentworks.agents.template import AgentTemplate
from agentworks.agents.templates import effective_template as agent_effective
from agentworks.env.entry import EnvEntry
from agentworks.errors import InheritanceCycleError
from agentworks.resources.inheritance import declarers, merge_layers, resolution_layers
from agentworks.sessions.template import SessionTemplate
from agentworks.sessions.templates import effective_template as session_effective
from agentworks.vms.template import VMTemplate
from agentworks.vms.templates import effective_template as vm_effective
from agentworks.workspaces.template import WorkspaceTemplate
from agentworks.workspaces.templates import effective_template as workspace_effective


def _env(secret: str) -> dict[str, EnvEntry]:
    return {"K": EnvEntry({"secret": secret})}


def _diamond(build: Callable[..., Any]) -> dict[str, Any]:
    """``kid`` inherits ``left`` then ``right``, both inheriting ``root``.

    A diamond and an ordering case at once: every layer declares the same
    env key, so the folded result is decided entirely by the order, and
    ``right`` is the one a left-to-right merge lets win.
    """
    return {
        "root": build(name="root", env=_env("root-secret")),
        "left": build(name="left", inherits=["root"], env=_env("left-secret")),
        "right": build(name="right", inherits=["root"], env=_env("right-secret")),
        "kid": build(name="kid", inherits=["left", "right"]),
    }


@pytest.mark.parametrize(
    ("build", "resolve"),
    [
        (VMTemplate, vm_effective),
        (WorkspaceTemplate, workspace_effective),
        (AgentTemplate, agent_effective),
        (SessionTemplate, session_effective),
    ],
    ids=["vm-template", "workspace-template", "agent-template", "session-template"],
)
def test_folding_the_layers_reproduces_the_resolvers_own_merge(
    build: Callable[..., Any],
    resolve: Callable[..., Any],
) -> None:
    rows = _diamond(build)
    folded: dict[str, EnvEntry] = {}
    for layer in merge_layers(rows, "kid"):
        folded.update(layer.env or {})

    resolved = resolve(rows, "kid")
    # The session kind's effective value wraps its resolved template,
    # because it carries the harness pair beside it.
    effective = resolved.resolved if hasattr(resolved, "resolved") else resolved
    assert folded == effective.env


def test_the_declarer_of_a_contested_key_is_the_layer_whose_value_survived() -> None:
    rows = _diamond(VMTemplate)
    layers = merge_layers(rows, "kid")
    by_env = declarers(layers, "vm-template", lambda t: t.env)
    assert by_env["K"] == ("vm-template", "right")
    assert vm_effective(rows, "kid").env["K"].secret == "right-secret"


def test_a_cyclic_chain_stops_rather_than_raising() -> None:
    """The build walk may not raise; the registry's cycle pass is what
    reports the loop."""
    rows = {
        "a": VMTemplate(name="a", inherits=["b"]),
        "b": VMTemplate(name="b", inherits=["a"]),
    }
    assert [layer.name for layer in merge_layers(rows, "a")] == ["b", "a"]


def test_the_resolver_path_refuses_the_same_cycle() -> None:
    """The one difference between the two layer functions, and the reason
    both exist: a resolve has a caller that can be told."""
    rows = {
        "a": VMTemplate(name="a", inherits=["b"]),
        "b": VMTemplate(name="b", inherits=["a"]),
    }
    with pytest.raises(InheritanceCycleError):
        resolution_layers(rows, "a", "vm-template")


def test_an_unresolved_parent_contributes_nothing() -> None:
    rows = {"kid": VMTemplate(name="kid", inherits=["missing"])}
    assert [layer.name for layer in merge_layers(rows, "kid")] == ["kid"]


def test_a_layer_reached_twice_appears_once_and_before_everything_that_reaches_it() -> None:
    """The diamond, which is where a replay of the routes goes wrong twice
    over.

    ``root`` re-applied after ``left`` put a grandparent's value back on
    top of the parent that had overridden it, so ``kid`` resolved to
    ``root``'s ``cpus`` even though ``left`` declared its own. That is the
    same class of silent defect as a defaulted parent overwriting a
    declaring one, and it is inseparable from the size: replaying a route
    is what doubles the layer list per diamond.
    """
    rows = {
        "root": VMTemplate(name="root", cpus=2),
        "left": VMTemplate(name="left", inherits=["root"], cpus=16),
        "right": VMTemplate(name="right", inherits=["root"]),
        "kid": VMTemplate(name="kid", inherits=["left", "right"]),
    }

    assert [layer.name for layer in merge_layers(rows, "kid")] == ["root", "left", "right", "kid"]
    assert vm_effective(rows, "kid").cpus == 16


def test_a_diamond_ladder_produces_one_layer_per_row() -> None:
    """The measured symptom: 55 rows produced 1,048,573 layers, in the
    finalize pass that runs before the cycle detector meant to protect
    it."""
    levels = 18
    rows = {"root": VMTemplate(name="root")}
    for level in range(levels):
        parent = "root" if level == 0 else f"kid{level - 1}"
        rows[f"left{level}"] = VMTemplate(name=f"left{level}", inherits=[parent])
        rows[f"right{level}"] = VMTemplate(name=f"right{level}", inherits=[parent])
        rows[f"kid{level}"] = VMTemplate(name=f"kid{level}", inherits=[f"left{level}", f"right{level}"])

    assert len(merge_layers(rows, f"kid{levels - 1}")) == len(rows)


def test_a_chain_deeper_than_the_recursion_limit_resolves() -> None:
    """Chain depth is the operator's to choose, and the finalize pass may
    not raise, so ``RecursionError`` is not an available answer."""
    depth = sys.getrecursionlimit() * 2
    rows = {"t0": VMTemplate(name="t0", cpus=99)}
    for level in range(1, depth):
        rows[f"t{level}"] = VMTemplate(name=f"t{level}", inherits=[f"t{level - 1}"])

    assert len(merge_layers(rows, f"t{depth - 1}")) == depth
    assert vm_effective(rows, f"t{depth - 1}").cpus == 99
